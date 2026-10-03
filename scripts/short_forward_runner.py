#!/usr/bin/env python3
"""short_forward_v1 前向き記録ランナー（空売り版・記録だけの試行・実際のお金は入れない）。

仕様正本: tasks/short_forward_sellreg_preregister.md。凍結定数・損益式・日の関門・判定の正本は
`short_forward_v1_spec.py`（本ファイルはオーケストレーションのみ＝式を持たない）。
手本: tob_forward_runner.py（状態機械）／pair_forward_scan.py（lock＋再読込＋append＋fsync・
code_tree_sha・FROZEN_START ガード）。

流れ（毎回 FROZEN_START〜最新データ日の未処理分を全部走査する冪等処理・処理済み＝`day_done` 事象あり）:
  1. 合図日 D ごとに `day` 事象（上位500社内の発火数・銘柄・名簿月・bars取得時刻・code sha・初検出時刻）
     → 発火 → 最後に `day_done`。発火0件の日も残す。10件以上は `skipped_day_count`。
     D-1・D の bars が欠け／不完全（銘柄数<通常の90%）なら day を書かず再試行（不完全は `incomplete`）。
     名簿が作れない月は `universe_unavailable`（同じく再試行）。
  2. 1〜9件の日は発火ごとに `signal`（入口 D+2・出口 D+22 は取引所カレンダーで数える）。
     初検出（その日を確定した瞬間の時刻）が入口日の寄り以降なら `late_detected`（主集計外）。
     保有中の再発火は overlap_open=true。
  3. 入口日の完全な bars で `filled`（AdjO・取得時刻・尺度の基準日を保存）／`unfilled`（行なし・寄り値なし）。
     参照する bars が欠け・不完全、または TOPIX が欠ける間は `pending_data`（段階ごとに1回だけ記録して待つ）。
  4. 入口〜出口の bars が全部完全になったら `matured`（保存した入口価格と、入口の尺度へ揃えた出口価格）／
     出口日に引け値が無ければ `censored`（最後の価格とその価格日を保存・主集計外）。
  5. 判定時点に達したら、その日までに成熟した日コホートだけで一度だけ `judgment` を書いて凍結する。

台帳: data/paper_trades/short_forward_ledger.jsonl（追記のみ・lock＋fsync）。本線の ledger.jsonl・
paper_watchlist・scoreboard・daily_reco には一切書かない。試行一覧（trials.jsonl）にも足さない。

実行:
    python3 scripts/short_forward_runner.py
    python3 scripts/short_forward_runner.py --ledger <scratch> --since 20260901 --allow-pre-start   # smoke 専用
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import jq_fetch  # noqa: E402  DATA_ROOT / split_scale / LOG_PATH / now_jst
import kpi_round23_signals  # noqa: E402  generate_sell_reg_trigger_signals（既存・再利用）
import measure_base_rate  # noqa: E402  カレンダー（営業日の正本）・load_bars_day
import short_forward_v1_spec as S  # noqa: E402  凍結仕様

LEDGER_DEFAULT = (ROOT / "data/paper_trades/short_forward_ledger.jsonl").resolve()
CODE_TREE_FILES = [
    "scripts/short_forward_runner.py", "scripts/short_forward_v1_spec.py",
    "scripts/kpi_round23_signals.py", "scripts/measure_base_rate.py", "scripts/jq_fetch.py",
    "tasks/short_forward_sellreg_preregister.md",
]
TAG = "[short-forward]"

# universe_fn(D) -> (codes or None, t_date or None, reason or None)
UniverseFn = Callable[[str], tuple[Optional[set], Optional[str], Optional[str]]]


def code_tree_sha() -> str:
    h = hashlib.sha256()
    for rel in CODE_TREE_FILES:
        p = ROOT / rel
        h.update(rel.encode())
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def check_pre_start_guard(ledger_path: Path, allow_pre_start: bool) -> None:
    if allow_pre_start and Path(ledger_path).resolve() == LEDGER_DEFAULT:
        raise SystemExit("FATAL: --allow-pre-start は本番台帳に対して使用禁止（smoke専用）")


def assert_not_pre_start(day: str, frozen_start: str, allow_pre_start: bool) -> None:
    """FROZEN_START より前の合図日は記録しない（書き込み直前の最後の砦）。"""
    if day < frozen_start and not allow_pre_start:
        raise SystemExit(f"FATAL: FROZEN_START({frozen_start}) より前の合図日 {day} は記録できません")


def load_ledger_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            raise SystemExit(f"FATAL: 台帳の壊れたJSONL行 {path}:{i}（手動確認が必要・自動修復しない）")
    return out


@dataclass
class Env:
    """データの所在（本番は build_env・テストは一時ディレクトリで差し替え）。"""
    bdays: list[str]                       # 取引所カレンダーの営業日（bars の有無で作らない）
    universe_fn: UniverseFn
    topix_o: dict[str, float]
    topix_c: dict[str, float]
    bars_fetch_ts: dict[str, str] = field(default_factory=dict)
    month_ends: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.bidx = {d: i for i, d in enumerate(self.bdays)}

    @staticmethod
    def bars_path(day: str) -> Path:
        return jq_fetch.DATA_ROOT / "bars" / f"{day}.json.gz"

    def has_bars(self, day: str) -> bool:
        return self.bars_path(day).exists()

    def last_bar(self) -> str:
        names = [p.name[:8] for p in (jq_fetch.DATA_ROOT / "bars").glob("*.json.gz")]
        return max(names) if names else ""

    def offset(self, day: str, k: int) -> Optional[str]:
        i = self.bidx.get(day)
        if i is None or i + k >= len(self.bdays):
            return None
        return self.bdays[i + k]


def load_topix() -> tuple[dict[str, float], dict[str, float]]:
    path = jq_fetch.DATA_ROOT / "topix.json.gz"
    if not path.exists():
        return {}, {}
    o = json.load(gzip.open(path))
    rows = o if isinstance(o, list) else (o.get("data") or o.get("topix") or [])
    to, tc = {}, {}
    for r in rows:
        d = str(r.get("Date", "")).replace("-", "")
        if r.get("O") is not None:
            to[d] = float(r["O"])
        if r.get("C") is not None:
            tc[d] = float(r["C"])
    return to, tc


def load_bars_fetch_ts() -> dict[str, str]:
    """{bars の日付: 最後に保存した時刻(ISO)}（fetch_log.jsonl の kind=bars・status=saved）。"""
    out: dict[str, str] = {}
    path = jq_fetch.LOG_PATH
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "bars" and r.get("status") == "saved" and r.get("date") and r.get("ts"):
                out[r["date"]] = r["ts"]
    return out


def build_env() -> Env:
    import daily_screen  # 本番と同じ名簿（UniverseCache）を再利用・重い import なので本番経路だけで読む
    cal = measure_base_rate.load_calendar_days()
    bdays = measure_base_rate.all_business_days(cal)
    bidx = {d: i for i, d in enumerate(bdays)}
    uc = daily_screen.UniverseCache(cal, bidx, bdays)
    assert daily_screen.UNIVERSE_WINDOW == S.UNIVERSE_WINDOW and daily_screen.UNIVERSE_TOP_N == S.UNIVERSE_TOP_N

    def universe_fn(day: str):
        month = f"{day[:4]}-{day[4:6]}"
        codes = uc.universe_for_month(month)
        t_date = uc._t_date_for_month(month)
        return codes, t_date, (None if codes is not None else uc.failed_months().get(month, "unknown"))

    to, tc = load_topix()
    mends = measure_base_rate.month_ends_in_range(cal, S.FROZEN_START[:4] + "-" + S.FROZEN_START[4:6], "2099-12")
    return Env(bdays=bdays, universe_fn=universe_fn, topix_o=to, topix_c=tc,
               bars_fetch_ts=load_bars_fetch_ts(), month_ends=mends)


# --- 本体 -------------------------------------------------------------------------------
#
# Codex 実装レビュー（2026-10-03・6件）反映:
#  #1 判定は判定時点までに成熟した日コホートだけで一度だけ計算し `judgment` 事象で凍結（以後再計算しない）
#  #2 入口価格は filled に保存した値を使い、出口価格は入口の尺度（scale_base_date）へ split_scale で揃える。
#     分割の走査は入口日〜最新 bars まで（paper_eval と同じく後日の取り直しも吸収）
#  #3 初検出時刻は日ごとに合図を確定した瞬間の壁時計で取る
#  #4 参照する bars ファイル（D-1・D・入口日・入口〜出口・分割走査の範囲）は全部完全性検査に掛け、
#     欠け・不完全なら pending_data（再試行）。終端状態（unfilled/censored/0件の日）は完全なファイルでだけ確定。
#     入口日の完全なファイルに銘柄の行が無い＝unfilled（reason=no_bar・本当に寄らなかったと同じ扱い）
#  #5 TOPIX の入口寄り・出口引け（censored は最後の価格日の引け）が両方そろうまで pending_data
#  #6 日ごとに day＋signal を書いた後に `day_done` を書いて fsync。処理済み＝day_done あり。
#     途中で落ちた日は再処理し、事象キー（event, D, code, stage）で重複を弾いて欠けた行だけ補う
# Codex 再レビュー（同日・5件）反映:
#  再#1 matured/censored に recorded_at（記録した壁時計）。判定は出口日<=判定日 かつ 記録が判定日 23:59:59 JST まで
#  再#2 分割走査の範囲（入口〜最新）で営業日の bars が欠けたら飛ばさず待つ（範囲は取引所の営業日だけ）
#  再#3 行が無い／寄り値・引け値が無い（unfilled・censored）は、その日の bars が翌営業日以降に取得されて
#       いる時だけ確定（fetch_log の時刻が不明・早すぎるなら pending_data）
#  再#4 day 行だけ残って落ちた日は、保存済みの codes と detected_at で復元（合図も時刻も再計算しない）
#  再#5 判定は最新 bars が判定日より後（判定日のデータが入りきった後）でだけ書く


def _key(r: dict) -> tuple[str, str]:
    return (r["code"], r["D"])


def event_key(r: dict) -> tuple:
    stage = r.get("stage")
    if r.get("event") == "incomplete":
        stage = r.get("bars_n_codes")
    elif r.get("event") == "pending_data" and r.get("reason") == "absence_unconfirmed":
        stage = (stage, r.get("first_absent_fetched_at"))   # 取得時刻ごとに1行（初回観測の更新を残せる）
    return (r.get("event"), r.get("D"), r.get("code"), stage)


def _recent_counts(env: Env, day: str) -> list[int]:
    i = env.bidx[day]
    out: list[int] = []
    j = i - 1
    while j >= 0 and len(out) < S.INCOMPLETE_LOOKBACK and i - j <= S.INCOMPLETE_LOOKBACK * 3:
        d = env.bdays[j]
        if env.has_bars(d):
            out.append(len(measure_base_rate.load_bars_day(d)))
        j -= 1
    return out


class _Run:
    """1回の走査の状態（台帳の既存事象・追記・完全性検査のメモ）。"""

    def __init__(self, env: Env, ledger_path: Path, records: list[dict], sha: str,
                 clock: Callable[[], str], until: str, frozen_start: str, allow_pre_start: bool):
        self.env, self.ledger_path, self.sha, self.clock, self.until = env, ledger_path, sha, clock, until
        self.frozen_start, self.allow_pre_start = frozen_start, allow_pre_start
        self.keys = {event_key(r) for r in records}
        self.new_rows: list[dict] = []
        self._fs: dict[str, str] = {}
        self.sigs = {_key(r): dict(r) for r in records if r.get("event") == "signal"}
        self.done_entry = {_key(r): r for r in records if r.get("event") in ("filled", "unfilled")}
        self.done_exit = {_key(r): r for r in records if r.get("event") in ("matured", "censored")}
        self.day_rows = {r["D"]: r for r in records if r.get("event") == "day"}
        # 欠けの初回観測（段階ごとに、記録済みの最も早い取得時刻）＝3回目 (b) の比較の基準
        self.absent_first: dict = {}
        for r in records:
            if r.get("event") == "pending_data" and r.get("first_absent_fetched_at"):
                k = (_key(r), r.get("stage"))
                ts = r["first_absent_fetched_at"]
                if k not in self.absent_first or S._parse_ts(ts) < S._parse_ts(self.absent_first[k]):
                    self.absent_first[k] = ts
        for k, ev in self.done_entry.items():
            if ev["event"] == "unfilled" and k in self.sigs:
                self.sigs[k]["_unfilled"] = True

    def append(self, rows: list[dict]) -> list[dict]:
        """事象キーが既にある行は弾き、残りを追記して fsync（#6）。"""
        out = []
        for r in rows:
            k = event_key(r)
            if k in self.keys:
                continue
            self.keys.add(k)
            out.append(r)
        if out:
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                for r in out:
                    f.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")},
                                       ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.new_rows.extend(out)
        return out

    def file_state(self, day: str) -> str:
        """'missing' / 'incomplete' / 'ok'（#4）。incomplete は `incomplete` 事象を1回だけ残す。"""
        if day in self._fs:
            return self._fs[day]
        if not self.env.has_bars(day):
            st = "missing"
        else:
            n = len(measure_base_rate.load_bars_day(day))
            st = "incomplete" if S.is_incomplete(n, _recent_counts(self.env, day)) else "ok"
            if st == "incomplete":
                self.append([{"event": "incomplete", "D": day, "bars_n_codes": n,
                              "detected_at": self.clock(), "code_tree_sha": self.sha,
                              "spec_version": S.SPEC_VERSION}])
        self._fs[day] = st
        return st

    def first_bad(self, days: list[str]) -> Optional[tuple[str, str]]:
        for d in days:
            st = self.file_state(d)
            if st != "ok":
                return d, st
        return None

    # --- 合図日 D の確定 ---
    def detect_day(self, day: str) -> bool:
        """D を確定して day＋signal＋day_done を書く。戻り値=確定したか（False なら再試行待ち）。"""
        env = self.env
        if env.bidx.get(day, 0) == 0:
            return False
        if day in self.day_rows:                   # 再レビュー #4: 落ちた日は保存済みの day 行から復元
            return self._finish_day(self.day_rows[day])
        prev = env.offset(day, -1)
        if self.first_bad([prev, day]):
            return False
        n_codes = len(measure_base_rate.load_bars_day(day))
        codes_u, t_date, reason = env.universe_fn(day)
        if codes_u is None:
            self.append([{"event": "universe_unavailable", "D": day, "universe_t_date": t_date,
                          "reason": reason, "detected_at": self.clock(), "code_tree_sha": self.sha,
                          "spec_version": S.SPEC_VERSION}])
            return False
        df, _diag = kpi_round23_signals.generate_sell_reg_trigger_signals(day, day, env.bdays, env.bidx)
        raw = [] if df is None or df.empty else [str(c) for c in df["code"]]
        top = sorted({c for c in raw if c in codes_u})
        detected_at = self.clock()                 # #3: この日の合図が決まった瞬間の壁時計
        gate = S.day_gate(len(top))
        return self._finish_day({
            "event": "day", "D": day, "n_raw": len(raw), "n_top500": len(top), "gate": gate,
            "codes": top, "universe_month": day[:6], "universe_t_date": t_date,
            "bars_fetched_at": env.bars_fetch_ts.get(day), "bars_n_codes": n_codes,
            "code_tree_sha": self.sha, "detected_at": detected_at, "spec_version": S.SPEC_VERSION,
        })

    def _finish_day(self, day_row: dict) -> bool:
        """day 行から skipped/signal/late 行と day_done を書く（新規確定と、書き込み途中で落ちた日の
        復元の共通経路）。復元では保存済み day 行の銘柄一覧（codes）と初検出時刻（detected_at）だけを使い、
        合図も時刻も再計算しない（Codex 再レビュー #4）。"""
        env = self.env
        day, gate, top, detected_at = day_row["D"], day_row["gate"], day_row["codes"], day_row["detected_at"]
        rows: list[dict] = [day_row]
        if gate == "skipped_day_count":
            rows.append({"event": "skipped_day_count", "D": day, "n_top500": len(top),
                         "detected_at": detected_at, "code_tree_sha": self.sha})
        elif gate == "recorded":
            for code in top:
                if (code, day) in self.sigs:       # #6: 落ちる前に書けていた発火はそのまま使う
                    continue
                entry = env.offset(day, S.ENTRY_OFFSET_BD)
                exit_ = env.offset(day, S.EXIT_OFFSET_BD)
                overlap = any(s["code"] == code and s["D"] < day
                              and (s.get("exit_day") is None or s["exit_day"] >= day)
                              and not s.get("_unfilled") for s in self.sigs.values())
                late = bool(entry) and S.is_late(detected_at, entry)
                sig = {"event": "signal", "code": code, "D": day, "entry_day": entry, "exit_day": exit_,
                       "overlap_open": overlap, "late_detected": late, "detected_at": detected_at,
                       "code_tree_sha": self.sha, "spec_version": S.SPEC_VERSION}
                rows.append(sig)
                self.sigs[(code, day)] = sig
                if late:
                    rows.append({"event": "late_detected", "code": code, "D": day, "entry_day": entry,
                                 "detected_at": detected_at, "code_tree_sha": self.sha})
        for r in rows:
            assert_not_pre_start(r["D"], self.frozen_start, self.allow_pre_start)
        self.append(rows)
        self.append([{"event": "day_done", "D": day, "done_at": self.clock(), "code_tree_sha": self.sha}])
        return True

    # --- 1発火の状態を進める ---
    def _pend(self, key, stage: str, **info) -> None:
        self.append([{"event": "pending_data", "stage": stage, "code": key[0], "D": key[1],
                      "processed_at": self.clock(), "code_tree_sha": self.sha, **info}])

    def _splits(self, code: str, i0: int, i_last: int) -> Optional[list[tuple[str, float]]]:
        """入口日〜最新 bars の分割（#2）。範囲内の営業日に欠け・不完全なファイルがあれば None（待つ）。

        走査するのは取引所カレンダーの営業日だけ（非営業日はそもそも範囲に入らない）。営業日の
        ファイル欠けを飛ばすと分割日を見落とすため待つ（Codex 再レビュー #2）。
        """
        ev = []
        for j in range(i0, i_last + 1):
            d = self.env.bdays[j]
            if self.file_state(d) != "ok":
                return None
            bar = measure_base_rate.load_bars_day(d).get(code)
            fac = bar.get("AdjFactor") if bar else None
            if fac not in (None, 1, 1.0):
                ev.append((d, float(fac)))
        return ev

    def _absence_confirmed(self, key, stage: str, day: str) -> Optional[dict]:
        """銘柄の行が day の bars に無いことを、1ファイルだけで確定しない（Codex 3回目 (b)）。

        初めて欠けを見た時は `pending_data`（reason=absence_unconfirmed・first_absent_fetched_at=その時の
        取得時刻）を残して待つ。別の回の走査で、同じ日付のファイルがそれより**厳密に後に取り直され**
        （fetch_log の時刻が新しい）、なお行が無い時だけ確定する。取得時刻が不明な間は確定しない。
        戻り値=確定時の来歴 dict（確定しないなら None）。
        """
        ts = self.env.bars_fetch_ts.get(day)
        first = self.absent_first.get((key, stage))
        if first is None or (ts and S._parse_ts(ts) < S._parse_ts(first)):
            row = {"event": "pending_data", "stage": stage, "code": key[0], "D": key[1],
                   "reason": "absence_unconfirmed", "missing_bars": day,
                   "first_absent_fetched_at": ts, "processed_at": self.clock(), "code_tree_sha": self.sha}
            self.append([row])
            if ts:
                self.absent_first[(key, stage)] = ts
            return None
        if not ts or S._parse_ts(ts) <= S._parse_ts(first):
            return None
        return {"first_absent_fetched_at": first, "confirm_fetched_at": ts}

    def _price_null_confirmed(self, key, stage: str, day: str) -> Optional[dict]:
        """行はあるが価格（入口の AdjO／出口の AdjC）が欠けている時、確定してよいか（Codex 4回目）。

        day の bars の取得時刻（JST）が day の翌営業日以降なら確定＝来歴 {"null_confirm_fetched_at": ts}。
        取得時刻が不明・それより早い（例: 入口日当日 11:30 の途中取得）なら `pending_data`
        （reason=price_null_unconfirmed・stage ごとに重複排除）を残して None＝後の回で価格が入れば
        filled／matured に進める。
        """
        ts = self.env.bars_fetch_ts.get(day)
        nbd = self.env.offset(day, 1)
        if ts and nbd and S._parse_ts(ts).astimezone(S.JST).strftime("%Y%m%d") >= nbd:
            return {"null_confirm_fetched_at": ts}
        self._pend(key, stage, reason="price_null_unconfirmed", null_bars=day, bars_fetched_at=ts)
        return None

    def advance(self, sig: dict) -> None:
        env = self.env
        code, day = sig["code"], sig["D"]
        key = (code, day)
        entry = sig.get("entry_day") or env.offset(day, S.ENTRY_OFFSET_BD)
        exit_ = sig.get("exit_day") or env.offset(day, S.EXIT_OFFSET_BD)
        base = {"code": code, "D": day, "code_tree_sha": self.sha}

        if key not in self.done_entry:
            if not entry or entry > self.until:
                return
            bad = self.first_bad([entry])
            if bad:
                self._pend(key, "entry", missing_bars=bad[0], reason=bad[1])
                return
            bar = measure_base_rate.load_bars_day(entry).get(code)
            adjo = bar.get("AdjO") if bar else None
            if adjo is None or adjo <= 0:
                # 寄り値が無い（no_open・行はある）→ 入口日ファイルの取得が翌営業日以降の時だけ確定（4回目）。
                # 行が無い（no_bar）は、同じ日付のファイルが後で取り直されてもなお無い時だけ確定（3回目 (b)）
                if bar is None:
                    prov = self._absence_confirmed(key, "entry_absent", entry)
                else:
                    prov = self._price_null_confirmed(key, "entry_price_null", entry)
                if prov is None:
                    return
                ev = {"event": "unfilled", "entry_day": entry,
                      "reason": "no_bar" if bar is None else "no_open", "processed_at": self.clock(),
                      "recorded_at": self.clock(), **prov, **base}
                sig["_unfilled"] = True
            else:
                got_ts = env.bars_fetch_ts.get(entry)
                got = got_ts[:10].replace("-", "") if got_ts else None
                scale_base = entry
                if got:   # 取得日より前の分割は足に反映済み（split_scale の規約）＝基準日=max(入口日, 取得日の前日)
                    prev_cal = (S._date(got) - dt.timedelta(days=1)).strftime("%Y%m%d")
                    scale_base = max(entry, prev_cal)
                ev = {"event": "filled", "entry_day": entry, "exit_day": exit_, "entry_px": float(adjo),
                      "entry_bars_fetched_at": got_ts, "scale_base_date": scale_base,
                      "topix_open_entry": env.topix_o.get(entry), "processed_at": self.clock(), **base}
            self.append([ev])
            self.done_entry[key] = ev
        fill = self.done_entry[key]
        if fill["event"] != "filled" or key in self.done_exit:
            return
        if not exit_ or exit_ > self.until:
            return                                         # exit_pending（出口日のデータ待ち）
        i0, i1 = env.bidx[entry], env.bidx[exit_]
        bad = self.first_bad(env.bdays[i0:i1 + 1])
        if bad:
            self._pend(key, "exit", missing_bars=bad[0], reason=bad[1])
            return
        i_last = max(i for i, d in enumerate(env.bdays) if d <= self.until)
        splits = self._splits(code, i0, i_last)
        if splits is None:
            self._pend(key, "exit_splits", reason="missing_or_incomplete_in_split_range")
            return
        entry_px = float(fill["entry_px"])                 # #2: 保存した入口価格（読み直さない）
        sbase = fill.get("scale_base_date") or entry

        def scaled(px: float, d: str) -> float:
            return px * jq_fetch.split_scale(d, sbase, splits) if splits else px

        xbar = measure_base_rate.load_bars_day(exit_).get(code)
        adjc = xbar.get("AdjC") if xbar else None
        t0 = env.topix_o.get(entry)
        if adjc:
            t1 = env.topix_c.get(exit_)
            if not t0 or not t1:                           # #5
                self._pend(key, "exit_topix", missing_topix=entry if not t0 else exit_)
                return
            exit_px = scaled(float(adjc), exit_)
            cd = S.calendar_days(entry, exit_)
            rs = S.ret_short(entry_px, exit_px, cd)
            ev = {"event": "matured", "entry_day": entry, "exit_day": exit_, "entry_px": entry_px,
                  "scale_base_date": sbase, "exit_px_adj": exit_px, "exit_px_raw": adjc, "splits": splits,
                  "caldays": cd, "ret_short": rs, "ret_pair": S.ret_pair(rs, t0, t1),
                  "topix_open_entry": t0, "topix_close_exit": t1, "processed_at": self.clock(),
                  "recorded_at": self.clock(), **base}
        else:
            # 出口日に行が無い → 取り直しでなお無い時だけ censored（3回目 (b)）。
            # 行はあるが引け値が無い → 出口日ファイルの取得が翌営業日以降の時だけ censored（4回目）
            if xbar is None:
                prov = self._absence_confirmed(key, "exit_absent", exit_)
            else:
                prov = self._price_null_confirmed(key, "exit_price_null", exit_)
            if prov is None:
                return
            last_px = last_date = None
            for j in range(i1 - 1, i0 - 1, -1):
                b = measure_base_rate.load_bars_day(env.bdays[j]).get(code)
                if b and b.get("AdjC"):
                    last_date = env.bdays[j]
                    last_px = scaled(float(b["AdjC"]), last_date)
                    break
            rs = rp = cd = None
            if last_px:
                t1 = env.topix_c.get(last_date)
                if not t0 or not t1:                       # #5
                    self._pend(key, "exit_topix", missing_topix=entry if not t0 else last_date)
                    return
                cd = S.calendar_days(entry, last_date)
                rs = S.ret_short(entry_px, last_px, cd)
                rp = S.ret_pair(rs, t0, t1)
            ev = {"event": "censored", "entry_day": entry, "exit_day": exit_, "entry_px": entry_px,
                  "scale_base_date": sbase, "last_px_adj": last_px, "last_px_date": last_date,
                  "splits": splits, "caldays": cd, "ret_short_at_last": rs, "ret_pair_at_last": rp,
                  "processed_at": self.clock(), "recorded_at": self.clock(), **prov, **base}
        self.append([ev])
        self.done_exit[key] = ev


def scan(env: Env, ledger_path: Path, now=None, since: Optional[str] = None,
         until: Optional[str] = None, frozen_start: str = S.FROZEN_START,
         allow_pre_start: bool = False, clock: Optional[Callable[[], str]] = None,
         today: Optional[str] = None) -> dict:
    """冪等な追いつき走査。now= 固定時刻の文字列（テスト用）／clock= 時刻関数（既定=壁時計）。"""
    check_pre_start_guard(ledger_path, allow_pre_start)
    if clock is None:
        clock = (lambda: now) if isinstance(now, str) else (lambda: jq_fetch.now_jst().isoformat())
    last_bar = env.last_bar()
    until = min(until or last_bar, last_bar) if last_bar else ""
    since = since or frozen_start
    if not allow_pre_start:
        since = max(since, frozen_start)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(".lock")
    with open(lock_path, "w") as lock_f:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{TAG} 別プロセスが実行中のためスキップ（ロック {lock_path}）")
            return {"locked": True, "new_rows": [], "records": []}
        records = load_ledger_lines(ledger_path)
        run = _Run(env, ledger_path, records, code_tree_sha(), clock, until, frozen_start, allow_pre_start)
        processed = {r["D"] for r in records if r.get("event") == "day_done"}   # #6
        if until and since <= until:
            for day in env.bdays:
                if since <= day <= until and day not in processed:
                    run.detect_day(day)
        for key in sorted(run.sigs, key=lambda k: (k[1], k[0])):
            run.advance(run.sigs[key])
        # #1: 判定時点に達したら一度だけ凍結して書く（既に judgment があれば再計算しない）
        all_rows = records + run.new_rows
        if not any(r.get("event") == "judgment" for r in all_rows):
            t = today or clock()[:10].replace("-", "")
            sm = summarize(all_rows)
            due = S.judgment_due(t, sm["cohorts"], env.month_ends)   # 3回目 (c): 判定と同じ利用可能規則で60日を数える
            # 再レビュー #5: 判定日のデータが入りきってから（最新 bars が判定日より後）だけ書く
            if due and last_bar > due:
                res = S.judge_as_of(due, sm["cohorts"])
                run.append([{"event": "judgment", "due": due, "result": res, "judged_at": clock(),
                             "code_tree_sha": run.sha, "spec_version": S.SPEC_VERSION}])
    return {"locked": False, "new_rows": run.new_rows, "records": records + run.new_rows,
            "since": since, "until": until, "last_bar": last_bar}


# --- 要約（途中は記述だけ・合否は判定時点まで出さない） ----------------------------------------


def summarize(records: list[dict]) -> dict:
    sigs = {_key(r): r for r in records if r.get("event") == "signal"}
    entry = {_key(r): r for r in records if r.get("event") in ("filled", "unfilled")}
    exit_ = {_key(r): r for r in records if r.get("event") in ("matured", "censored")}
    days = [r for r in records if r.get("event") == "day"]
    states: dict[str, int] = {}
    for k in sigs:
        if k in exit_:
            st = exit_[k]["event"]
        elif k in entry:
            st = "unfilled" if entry[k]["event"] == "unfilled" else "exit_pending"
        else:
            st = "pending_data"
        states[st] = states.get(st, 0) + 1
    n_late = sum(1 for s in sigs.values() if s.get("late_detected"))
    # 日コホート（主集計）: 記録日・late 除外・filled が全部成熟・censored を含む日は別集計
    by_day: dict[str, list] = {}
    for k, s in sigs.items():
        if not s.get("late_detected"):
            by_day.setdefault(s["D"], []).append(k)
    day_values: dict[str, float] = {}
    mature_dates: list[str] = []
    cohorts: dict[str, tuple[str, list[float]]] = {}
    event_values: list[float] = []
    censored_days = 0
    unfilled_in_main = 0
    for d, keys in by_day.items():
        filled = [k for k in keys if k in entry and entry[k]["event"] == "filled"]
        unfilled_in_main += sum(1 for k in keys if k in entry and entry[k]["event"] == "unfilled")
        if any(k not in entry for k in keys) or not filled:
            continue
        if any(k not in exit_ for k in filled):
            continue
        if any(exit_[k]["event"] == "censored" for k in filled):
            censored_days += 1
            continue
        vals = [exit_[k]["ret_pair"] for k in filled if exit_[k].get("ret_pair") is not None]
        if len(vals) != len(filled):
            continue
        day_values[d] = sum(vals) / len(vals)
        mature_dates.append(max(exit_[k]["exit_day"] for k in filled))
        # 3回目 (a): その D の全終端事象（matured と unfilled）の記録時刻。1つでも無ければ判定に使わない
        recs = [exit_[k].get("recorded_at") for k in filled]
        recs += [entry[k].get("recorded_at") for k in keys if k in entry and entry[k]["event"] == "unfilled"]
        rec_max = None if None in recs else max(recs, key=S._parse_ts)
        cohorts[d] = (mature_dates[-1], rec_max, vals)
        event_values.extend(vals)
    gates: dict[str, int] = {}
    for r in days:
        gates[r["gate"]] = gates.get(r["gate"], 0) + 1
    return {"n_days": len(days), "gates": gates, "n_signals": len(sigs), "states": states,
            "n_late": n_late, "day_values": day_values, "mature_dates": mature_dates,
            "cohorts": cohorts,
            "judgment": next((r for r in records if r.get("event") == "judgment"), None),
            "event_values": event_values, "censored_days": censored_days,
            "unfilled_in_main": unfilled_in_main}


def format_summary(sm: dict) -> str:
    lines = [f"{TAG} 記録日 {sm['n_days']}（関門: {sm['gates'] or '-'}）・発火 {sm['n_signals']}"
             f"・状態 {sm['states'] or '-'}・遅れて検出 {sm['n_late']}"]
    dv = sm["day_values"]
    if dv:
        vals = list(dv.values())
        ev = sm["event_values"]
        lines.append(f"{TAG} 参考（判定前・合否なし）: 成熟日コホート {len(vals)}日 平均 TOPIX組 "
                     f"{100*sum(vals)/len(vals):+.2f}% 勝ち日率 {100*sum(v > 0 for v in vals)/len(vals):.1f}%"
                     f"／1発火1票 {len(ev)}件 平均 {100*sum(ev)/len(ev):+.2f}%"
                     f"／打ち切り含む日 {sm['censored_days']}・寄らず {sm['unfilled_in_main']}件")
    else:
        lines.append(f"{TAG} 参考（判定前・合否なし）: 成熟した日コホートはまだありません")
    jd = sm.get("judgment")
    if jd:   # #1: 凍結済みの判定をそのまま表示（再計算しない）
        lines.append(f"{TAG} 判定（{jd['due']} で凍結・{jd.get('judged_at')} 記録）: "
                     f"{json.dumps(jd['result'], ensure_ascii=False, default=str)}")
    else:
        lines.append(f"{TAG} 判定時点は未到達（成熟日コホート≥{S.JUDGE_MIN_MATURED_DAYS}かつ開始から"
                     f"{S.JUDGE_MIN_MONTHS}か月以上の最初の月末・遅くとも {S.JUDGE_HARD_DEADLINE}）")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="short_forward_v1 前向き記録ランナー")
    ap.add_argument("--since", default=None, help="走査開始（既定=FROZEN_START）")
    ap.add_argument("--until", default=None, help="走査終了（既定=bars実在の最終日）")
    ap.add_argument("--ledger", default=str(LEDGER_DEFAULT))
    ap.add_argument("--allow-pre-start", action="store_true",
                    help="smoke専用: FROZEN_STARTより前を許可（本番台帳へは FATAL）")
    args = ap.parse_args(argv)
    ledger_path = Path(args.ledger)
    check_pre_start_guard(ledger_path, args.allow_pre_start)
    env = build_env()
    res = scan(env, ledger_path, since=args.since, until=args.until,
               allow_pre_start=args.allow_pre_start)   # 時刻は日ごとに壁時計で取る（#3）
    if res["locked"]:
        return 0
    counts: dict[str, int] = {}
    for r in res["new_rows"]:
        counts[r["event"]] = counts.get(r["event"], 0) + 1
    print(f"{TAG} 走査 {res['since']}〜{res['until'] or '-'}（FROZEN_START={S.FROZEN_START}・"
          f"最新bars={res['last_bar'] or '-'}）追記 {len(res['new_rows'])}行 {counts or ''} -> {ledger_path}")
    print(format_summary(summarize(res["records"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
