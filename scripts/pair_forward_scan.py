#!/usr/bin/env python3
"""pair_forward_v1 — KPI×KPIペアの前向き観察スキャナ（tasks/pair_forward_preregister.md の実装）。

仕様正本: tasks/pair_forward_preregister.md（2026-07-29 凍結・Codex R2条件付きGO）。
実装レビュー: Codex R3 NO-GO（BLOCKER3/MAJOR4）→ 本版で全対応 → R4 で再判定。

設計（R3反映後）:
- 8ペア固定・窓[-3,0]営業日・FROZEN_START=20260730・専用台帳のみ・イベント行追記（上書き禁止）。
- 主KPIの「取引可能イベント」判定は Canonical の保有期間dedup を厳守:
  * P1〜P6主（paper watchlist 5KPI）= paper ledger 行を (kpi,code) ごとに日付順で走査し、
    直前の in-universe 採用行の exit（closed なら実 exit_date・未クローズなら entry+20bd の予定満期）
    までの後続シグナルを rejected_duplicate として除外（BLOCKER-2）。ペア間（P2/P3等）で採否を共有。
    開始日以前の建玉も占有状態に含める（prereg「Canonical不変」）。
  * P7/P8主（high52_breakout）= 検出時は raw+universe フラグの candidate。成熟時に
    FROZEN_START〜成熟末日の全raw を kpi_event_study.compute_signal_returns へ**一括投入**して
    Canonical dedup/defer/リターンを適用し（BLOCKER-1）、採用行のみ matured、
    不採用は rejected_duplicate / rejected_out_of_universe / rejected_entry_missing で終端（MAJOR-2）。
- 成熟（P1〜P6）は paper 行が closed かつ ret_nostop / ret_e1 / ret_net が数値で揃うまで待機（MAJOR-1）。
  paper 行が entry_missing なら rejected_entry_missing で終端（R4 MAJOR-1・永久pending禁止）。
  占有終端は stop8 の早期 exit を使わず nostop 満期基準（R4 BLOCKER-1・canonical_block_until）。
  outcome に primary_ret=ret_nostop（primary exit=nostop・prereg準拠）を明示。
- 台帳 append は lockファイルで read→判定→append を排他し、lock内で台帳を再読込・fsync（MAJOR-4）。
- --allow-pre-start の本番台帳ガードは resolve() 比較（BLOCKER-3）。
- code_tree_sha = スキャナ+5生成器+kpi_event_study+measure_base_rate+prereg の連結sha（MINOR-1）。
- universe 判定は kpi_event_study._universe_membership を再利用（MINOR-2）。

実行（毎営業日・daily_screen 末尾から独立サブプロセスで）:
    python3 scripts/pair_forward_scan.py
    python3 scripts/pair_forward_scan.py --ledger <scratch> --since 20260713 --until 20260728 --allow-pre-start
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_base_rate  # noqa: E402
import kpi_event_study  # noqa: E402
import kpi_round23_signals  # noqa: E402
import kpi_round29_signals  # noqa: E402
import kpi_round30_signals  # noqa: E402
import kpi_volshock_signals  # noqa: E402
import kpi_high52_signals  # noqa: E402
import jq_fetch  # noqa: E402

FROZEN_START = "20260730"
WINDOW_BD = (-3, 0)
HORIZON_BD = 20
MATURITY_BUFFER_BD = 5
LEDGER_DEFAULT = (ROOT / "data/paper_trades/pair_forward_ledger.jsonl").resolve()
PAPER_LEDGER = ROOT / "data/paper_trades/ledger.jsonl"
BASE_RATE_DIR = ROOT / "output/base_rate"
UNIVERSE_WINDOW = 21
TAINTED = "peek_20260729"

PAIRS = [
    ("P1", "three_up_ignition", "turnover_rank_surge"),
    ("P2", "turnover_rank_surge", "gap_hold_close_strong"),
    ("P3", "turnover_rank_surge", "volshock_5x"),
    ("P4", "sell_reg_trigger_rebound", "engulf_reversal_day"),
    ("P5", "engulf_reversal_day", "sell_reg_trigger_rebound"),
    ("P6", "volshock_5x", "turnover_rank_surge"),
    ("P7", "high52_breakout", "gap_hold_close_strong"),
    ("P8", "high52_breakout", "volshock_5x"),
]
LEDGER_BACKED_MAINS = {"three_up_ignition", "turnover_rank_surge", "sell_reg_trigger_rebound",
                       "engulf_reversal_day", "volshock_5x"}
CODE_TREE_FILES = [
    "scripts/pair_forward_scan.py", "scripts/kpi_round23_signals.py", "scripts/kpi_round29_signals.py",
    "scripts/kpi_round30_signals.py", "scripts/kpi_volshock_signals.py", "scripts/kpi_high52_signals.py",
    "scripts/kpi_event_study.py", "scripts/measure_base_rate.py", "tasks/pair_forward_preregister.md",
]


def code_tree_sha() -> str:
    """判定を左右するコード＋仕様の連結sha（MINOR-1）。"""
    h = hashlib.sha256()
    for rel in CODE_TREE_FILES:
        h.update(rel.encode())
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()[:16]


def check_pre_start_guard(ledger_path: Path, allow_pre_start: bool) -> None:
    """--allow-pre-start は本番台帳に対し使用禁止（BLOCKER-3: resolve比較で相対パス回避を塞ぐ）。"""
    if allow_pre_start and ledger_path.resolve() == LEDGER_DEFAULT:
        raise SystemExit("FATAL: --allow-pre-start は本番台帳に対して使用禁止（smoke専用）")


def ledger_outcome_ready(row: dict) -> bool:
    """P1〜P6の成熟条件（MAJOR-1）: closed かつ nostop/e1 並走成績が数値で確定していること。"""
    if row.get("status") != "closed":
        return False
    for k in ("ret_nostop", "ret_e1", "ret_net"):
        v = row.get(k)
        if not isinstance(v, (int, float)):
            return False
    return True


def accepted_ledger_mains(paper_rows: list[dict], kpi: str,
                          bday_index: dict, all_bdays: list[str]) -> dict[tuple, dict]:
    """paper ledger 行に Canonical 保有期間dedup を適用した「採用主イベント」集合（BLOCKER-2）。

    規則（kpi_event_study.compute_signal_returns と同一の思想）:
    (kpi, code) ごとに signal_date 昇順で走査し、直前の**建玉が成立した**採用行（entry_dateあり。
    ledger行は全てin-universe）の exit まで後続シグナルを rejected_duplicate として除外する。
    exit = closed なら exit_date、未クローズ（pending/open）なら entry（未充足なら planned_entry）
    + HORIZON_BD の予定満期。entry_missing 行はブロックしない。開始日以前の行も占有状態に含める。
    戻り値 key=(code, signal_date) → 元行。ペア間（同一主KPI）で共有する。
    """
    accepted: dict[tuple, dict] = {}
    rows = [r for r in paper_rows if r.get("kpi_name") == kpi]
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    for code, lst in by_code.items():
        lst.sort(key=lambda x: x.get("signal_date", ""))
        block_until: str | None = None
        for r in lst:
            sd = r.get("signal_date", "")
            if block_until is not None and sd <= block_until:
                continue  # rejected_duplicate（Canonical: 保有期間中の再発火）
            if r.get("status") == "entry_missing":
                continue  # 建たなかった行は採用もブロックもしない
            accepted[(code, sd)] = r
            block_until = canonical_block_until(r, bday_index, all_bdays)
    return accepted


def canonical_block_until(r: dict, bday_index: dict, all_bdays: list[str]) -> str | None:
    """採用行の Canonical 占有終端（R4 BLOCKER-1）。

    Canonical（compute_signal_returns）は nostop の20営業日満期まで後続をブロックするため、
    stop8 の早期 exit_date を終端に使ってはならない:
    - closed かつ exit_reason==stop_loss → exit_date_nostop 確定済みならそれ・未確定なら entry+20bd
    - closed かつそれ以外（time_exit/delisted）→ 実 exit_date
    - 未クローズ → entry（未充足なら planned_entry）+20bd の予定満期
    """
    def horizon_end(anchor: str | None) -> str | None:
        if anchor and anchor in bday_index:
            return all_bdays[min(bday_index[anchor] + HORIZON_BD, len(all_bdays) - 1)]
        return None

    anchor = r.get("entry_date") or r.get("planned_entry_date")
    if r.get("status") == "closed" and r.get("exit_date"):
        if r.get("exit_reason") == "stop_loss":
            return r.get("exit_date_nostop") or horizon_end(anchor)
        return r["exit_date"]
    return horizon_end(anchor)


def classify_h52_candidate(code: str, signal_date: str,
                           h52_accepted: dict[tuple, dict]) -> tuple[str, dict | None]:
    """P7/P8候補の終端分類（R4 MAJOR-2: main()から抽出し単体テスト可能に）。

    h52_accepted = compute_signal_returns の全量一括結果 {(code, signal_date): row}。
    P7/P8は同一の h52_accepted を共有するため、同じ主キーは必ず同じ終端になる。
    - 採用かつ in_universe → ("matured", outcome)  primary_ret=ret（nostop相当・20bd終値）
    - 採用かつユニバース外 → ("rejected_out_of_universe", None)
    - 不採用で、先行する in-universe 採用行の exit が signal_date 以降 → ("rejected_duplicate", None)
    - それ以外の不採用 → ("rejected_entry_missing", None)
    """
    row = h52_accepted.get((code, signal_date))
    if row is not None:
        if row.get("in_universe"):
            outcome = {k: row.get(k) for k in
                       ("entry_date", "exit_date", "ret", "ret_stop8", "defer_bdays")}
            outcome["primary_ret"] = row.get("ret")
            return "matured", outcome
        return "rejected_out_of_universe", None
    dup = any(c == code and a.get("in_universe")
              and str(a.get("exit_date") or "") >= signal_date
              and str(sd2) < signal_date
              for (c, sd2), a in h52_accepted.items())
    return ("rejected_duplicate" if dup else "rejected_entry_missing"), None


def _gen(kpi: str, start_bd: str, end_bd: str, all_bdays: list[str], bidx: dict) -> pd.DataFrame:
    """prereg §2 対応表の generator 直接出力（raw・パラメータ=§7凍結値）。"""
    if kpi == "three_up_ignition":
        df, _ = kpi_round30_signals.generate_three_up_ignition_signals(start_bd, end_bd, all_bdays, bidx)
    elif kpi == "engulf_reversal_day":
        df, _ = kpi_round30_signals.generate_engulf_reversal_signals(start_bd, end_bd, all_bdays, bidx)
    elif kpi == "gap_hold_close_strong":
        df, _ = kpi_round29_signals.generate_gap_hold_close_strong_signals(start_bd, end_bd, all_bdays, bidx)
    elif kpi == "sell_reg_trigger_rebound":
        df, _ = kpi_round23_signals.generate_sell_reg_trigger_signals(start_bd, end_bd, all_bdays, bidx)
    elif kpi == "turnover_rank_surge":
        df, _ = kpi_round23_signals.generate_turnover_rank_surge_signals(start_bd, end_bd, all_bdays, bidx)
    elif kpi == "volshock_5x":
        df, _ = kpi_volshock_signals.generate_volshock_signals(
            start_bd, end_bd, vol_multiplier=5.0, day_ret_min=0.02, day_ret_max=0.08)
    elif kpi == "high52_breakout":
        df, _ = kpi_high52_signals.generate_high52_signals(start_bd, end_bd)
    else:
        raise SystemExit(f"FATAL: 未知のKPI {kpi}")
    if df is None or df.empty or "signal_date" not in df.columns:
        return pd.DataFrame(columns=["signal_date", "code"])
    return df[["signal_date", "code"]].astype(str)


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=None,
                    help="走査開始（既定=FROZEN_START。毎回全期間を再走査し冪等去重で吸収する）")
    ap.add_argument("--until", default=None, help="走査終了（既定=bars実在の最終営業日）")
    ap.add_argument("--ledger", default=str(LEDGER_DEFAULT))
    ap.add_argument("--allow-pre-start", action="store_true",
                    help="smoke専用: FROZEN_STARTより前の窓を許可（本番台帳へはresolve比較でFATAL）")
    args = ap.parse_args()
    ledger_path = Path(args.ledger)
    check_pre_start_guard(ledger_path, args.allow_pre_start)

    cal = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(cal)
    have = {p.name[:8] for p in (ROOT / "data/jquants/bars").glob("*.json.gz")}
    all_bdays = [d for d in all_bdays if d <= max(have)]
    bidx = {d: i for i, d in enumerate(all_bdays)}
    last_bar = all_bdays[-1]

    until = args.until or last_bar
    since = args.since or FROZEN_START
    if not args.allow_pre_start:
        since = max(since, FROZEN_START)
    if since > until:
        print(f"[pair-forward] 走査対象なし（since={since} > until={until}）")
        return 0

    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    now = jq_fetch.now_jst().isoformat()
    sha = code_tree_sha()

    # --- 排他区間: lock取得→台帳再読込→判定→append→fsync（MAJOR-4） ---
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(".lock")
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        records = load_ledger_lines(ledger_path)
        seen = {(r.get("pair_id"), r.get("code"), r.get("signal_date"))
                for r in records if r.get("event") in ("signal", "signal_candidate")}
        finalized = {(r.get("pair_id"), r.get("code"), r.get("signal_date"))
                     for r in records if r.get("event") in
                     ("matured", "rejected_duplicate", "rejected_out_of_universe", "rejected_entry_missing")}

        paper = load_ledger_lines(PAPER_LEDGER)
        accepted_cache: dict[str, dict] = {}

        gstart = all_bdays[max(0, bidx.get(since, 0) + WINDOW_BD[0])] if since in bidx else since
        partner_cache: dict[str, pd.DataFrame] = {}
        raw_main_cache: dict[str, pd.DataFrame] = {}
        new_rows: list[dict] = []

        # --- 検出 ---
        for pair_id, main_kpi, partner_kpi in PAIRS:
            if partner_kpi not in partner_cache:
                partner_cache[partner_kpi] = _gen(partner_kpi, gstart, until, all_bdays, bidx)
            partner_idx: dict[str, list[int]] = {}
            for _, r in partner_cache[partner_kpi].iterrows():
                i = bidx.get(r["signal_date"])
                if i is not None:
                    partner_idx.setdefault(r["code"], []).append(i)

            if main_kpi in LEDGER_BACKED_MAINS:
                if main_kpi not in accepted_cache:
                    accepted_cache[main_kpi] = accepted_ledger_mains(paper, main_kpi, bidx, all_bdays)
                mains = [{"code": c, "signal_date": sd, "source": "paper_ledger_canonical_dedup"}
                         for (c, sd) in accepted_cache[main_kpi]
                         if since <= sd <= until]
                event_name = "signal"
            else:
                if main_kpi not in raw_main_cache:
                    raw_main_cache[main_kpi] = _gen(main_kpi, since, until, all_bdays, bidx)
                mains = []
                for _, r in raw_main_cache[main_kpi].iterrows():
                    in_u, um = kpi_event_study._universe_membership(
                        r["code"], r["signal_date"][:6], universes_by_month)
                    mains.append({"code": r["code"], "signal_date": r["signal_date"],
                                  "source": "generator_raw", "in_universe": in_u,
                                  "universe_month_used": um})
                event_name = "signal_candidate"

            hit = 0
            for m in mains:
                i = bidx.get(m["signal_date"])
                if i is None:
                    continue
                match = [all_bdays[j] for j in partner_idx.get(m["code"], [])
                         if WINDOW_BD[0] <= j - i <= WINDOW_BD[1]]
                if not match:
                    continue
                key = (pair_id, m["code"], m["signal_date"])
                if key in seen:
                    continue
                seen.add(key)
                hit += 1
                new_rows.append({
                    "event": event_name, "pair_id": pair_id, "code": m["code"],
                    "signal_date": m["signal_date"], "main_kpi": main_kpi,
                    "partner_kpi": partner_kpi, "partner_signal_dates": sorted(match),
                    "main_source": m["source"], "in_universe": m.get("in_universe"),
                    "entry_mode": "fixed_t1" if main_kpi == "sell_reg_trigger_rebound" else "defer_max3bd",
                    "detected_at": now, "code_tree_sha": sha, "tainted_origin": TAINTED,
                })
            print(f"[pair-forward] {pair_id} {main_kpi}×{partner_kpi}: 新規{hit}件")

        # --- 成熟（P1〜P6: paper成績転記 / P7・P8: 一括Canonical確定） ---
        pending = [r for r in records + new_rows
                   if r.get("event") in ("signal", "signal_candidate")
                   and (r["pair_id"], r["code"], r["signal_date"]) not in finalized]

        def mature_enough(sd: str) -> bool:
            i = bidx.get(sd)
            return i is not None and i + 1 + HORIZON_BD + MATURITY_BUFFER_BD < len(all_bdays)

        # P7/P8: 成熟対象があれば high52 raw 全量を一括Canonical処理（BLOCKER-1）
        h52_pending = [r for r in pending if r["main_kpi"] == "high52_breakout"
                       and mature_enough(r["signal_date"])]
        h52_accepted: dict[tuple, dict] = {}
        if h52_pending:
            end_h = max(r["signal_date"] for r in h52_pending)
            raw = _gen("high52_breakout", since, end_h, all_bdays, bidx)
            res, _diag = kpi_event_study.compute_signal_returns(
                raw, bidx, all_bdays, {}, universes_by_month, defer_entry=True)
            if res is not None and not res.empty:
                for _, row in res.iterrows():
                    h52_accepted[(str(row["code"]), str(row["signal_date"]))] = row.to_dict()

        for r in pending:
            key = (r["pair_id"], r["code"], r["signal_date"])
            if not mature_enough(r["signal_date"]):
                continue
            if r["main_kpi"] in LEDGER_BACKED_MAINS:
                src = next((p for p in paper
                            if p.get("kpi_name") == r["main_kpi"] and p.get("code") == r["code"]
                            and p.get("signal_date") == r["signal_date"]), None)
                if src is None:
                    print(f"WARN: paper行が見つからない（監査要）: {key}", file=sys.stderr)
                    continue
                if src.get("status") == "entry_missing":
                    # R4 MAJOR-1: 建たなかった主イベントは終端させる（永久pending禁止）
                    new_rows.append({"event": "rejected_entry_missing", "pair_id": r["pair_id"],
                                     "code": r["code"], "signal_date": r["signal_date"],
                                     "outcome_matured_at": now, "code_tree_sha": sha,
                                     "tainted_origin": TAINTED})
                    finalized.add(key)
                    continue
                if not ledger_outcome_ready(src):
                    continue  # nostop/e1/net が数値で揃うまで待機（MAJOR-1）
                outcome = {k: src.get(k) for k in
                           ("entry_date", "entry_price", "exit_date", "exit_price", "exit_reason",
                            "ret_gross", "ret_net", "ret_nostop", "ret_e1")}
                outcome["primary_ret"] = src.get("ret_nostop")
                new_rows.append({"event": "matured", "pair_id": r["pair_id"], "code": r["code"],
                                 "signal_date": r["signal_date"], "outcome": outcome,
                                 "outcome_matured_at": now, "code_tree_sha": sha,
                                 "tainted_origin": TAINTED})
                finalized.add(key)
            else:
                if not h52_pending:
                    continue
                event, outcome = classify_h52_candidate(r["code"], r["signal_date"], h52_accepted)
                rec = {"event": event, "pair_id": r["pair_id"], "code": r["code"],
                       "signal_date": r["signal_date"], "outcome_matured_at": now,
                       "code_tree_sha": sha, "tainted_origin": TAINTED}
                if outcome is not None:
                    rec["outcome"] = outcome
                new_rows.append(rec)
                finalized.add(key)

        if new_rows:
            with open(ledger_path, "a", encoding="utf-8") as f:
                for r in new_rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        n_sig = sum(1 for r in new_rows if r["event"] in ("signal", "signal_candidate"))
        n_mat = sum(1 for r in new_rows if r["event"] == "matured")
        n_rej = sum(1 for r in new_rows if r["event"].startswith("rejected"))
        print(f"[pair-forward] append: 検出{n_sig}・成熟{n_mat}・却下{n_rej} -> {ledger_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
