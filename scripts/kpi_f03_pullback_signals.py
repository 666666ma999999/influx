#!/usr/bin/env python3
"""第41周: ツイート由来ファクトリー第2バッチ batch_v2t — F03押し目買いシグナル生成
（カタログ§7-AF・凍結グリッド `config/screening_grid_v2t.json`）。

凍結グリッドの `cell_common_definitions`/`cells[].trigger_full_logic` を一言一句実装する
シグナル生成器。4セル（v2t-F03-01〜04・(k,N)∈{5,10}×{10,20}の閾値変種）を1回のスキャンで
まとめて生成する。エピソード/クールダウンの状態機械は `scripts/kpi_round29_signals.py`
`generate_ll_release_rebound_signals` / `scripts/kpi_round32_signals.py`
`generate_updown_vol_ratio_signals` と同型（逐次スキャン・per-code dict状態・
「先行エピソード優先」「クールダウン中でもエピソード消費」パターンの再利用）。

フォワードリターン・重複除去（ポジション占有ベース）・ユニバース所属判定は
`kpi_event_study.compute_signal_returns` をそのまま再利用する（本ファイルでの再実装なし）。
エントリーは§6手順6の繰延（最大3営業日・不能なら除外）を適用するため defer_entry=True で呼ぶ
（grid `protocol.entry_exit` の明示規定）。

Usage:
    docker compose run --rm xstock python scripts/kpi_f03_pullback_signals.py --freq-dry-run
    docker compose run --rm xstock python scripts/kpi_f03_pullback_signals.py
    docker compose run --rm xstock python scripts/kpi_f03_pullback_signals.py \
        --start-date 2016-11-01 --end-date 2017-01-31 --output-dir /tmp/smoke  # スモーク用
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical: compute_signal_returns/load_universe_by_month を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical: _earliest_bars_date を再利用)
import measure_base_rate  # noqa: E402  (Canonical: カレンダー・bars読込・regime系列を再利用)

GRID_PATH = Path("config/screening_grid_v2t.json")
# 凍結時点のsha256（§7-AF・カタログ本文の値と完全一致）。以後グリッドが変更されたら即FATAL停止する。
EXPECTED_GRID_SHA256 = "e9a06feee5e19e09b190dd8fd89909dbdd5a88cb6d85128ecfd0deb965293f33"

BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
DEFAULT_OUTPUT_DIR = Path("output/kpi_screening/batch_v2t")

DISCOVERY_START, DISCOVERY_END = "2016-11-01", "2019-12-31"
CONFIRM_START, CONFIRM_END = "2020-01-01", "2022-11-30"
FULL_START, FULL_END = DISCOVERY_START, CONFIRM_END  # 1回の連続スキャンで両期間分を生成する

MA25_WINDOW = 25  # uptrend_base(D)の判定に使うMA_25
HIGH_WINDOW_BDAYS = 60  # 60日高値更新日判定の遡及窓
HIGH_MIN_VALID = 40  # 窓実長がこれ未満なら判定不能=非更新
COOLDOWN_BDAYS = 20  # クールダウン=cell_id×code内独立20営業日（grid protocol.pseudo_replication_control）
WARMUP_BDAYS = 90  # MA25(25)・60日高値窓(60)を確実に満たすための助走


# --- グリッド読込・整合性検証 -------------------------------------------------------


def sha256_full(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_grid_frozen(grid_path: Path = GRID_PATH) -> dict:
    """凍結グリッドJSONのsha256を照合する（全モード共通・頻度dry-run含む）。不一致は即FATAL。"""
    if not grid_path.exists():
        raise SystemExit(f"FATAL: グリッドJSONが見つかりません: {grid_path}")
    actual = sha256_full(grid_path)
    if actual != EXPECTED_GRID_SHA256:
        raise SystemExit(
            f"FATAL: グリッドJSONのsha256が凍結値と不一致: actual={actual} expected={EXPECTED_GRID_SHA256}"
            "（凍結後のグリッド変更は禁止・変更する場合は本バッチ放棄→新規事前登録）"
        )
    return json.loads(grid_path.read_text(encoding="utf-8"))


_CELL_N_RE = re.compile(r"recent_high\(D,\s*(\d+)\)")
_CELL_K_RE = re.compile(r"first_touch\(D,\s*(\d+)\)")


def load_cells_from_grid(grid: dict) -> list[dict]:
    """grid["cells"]のtrigger_full_logic文字列からk/Nを機械的に抽出する
    （ハードコードによる凍結値との乖離事故を避けるため、正本=JSONから毎回導出する）。"""
    cells = []
    for c in grid["cells"]:
        n_match = _CELL_N_RE.search(c["trigger_full_logic"])
        k_match = _CELL_K_RE.search(c["trigger_full_logic"])
        if not n_match or not k_match:
            raise SystemExit(f"FATAL: セル{c['cell_id']}のtrigger_full_logicからk/Nを抽出できません: {c['trigger_full_logic']}")
        cells.append({"cell_id": c["cell_id"], "name": c["name"], "k": int(k_match.group(1)), "N": int(n_match.group(1))})
    if len(cells) != 4:
        raise SystemExit(f"FATAL: グリッドのセル数が4ではありません: {len(cells)}件（凍結仕様=F03の4セルのみ）")
    return cells


def _bd_bounds_from_dates(start_date: str, end_date: str, all_bdays: list[str]) -> tuple[str, str]:
    start_bound = start_date.replace("-", "")
    end_bound = end_date.replace("-", "")
    start_bd = next((d for d in all_bdays if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit(f"FATAL: 指定期間[{start_date},{end_date}]に営業日が見つかりません")
    return start_bd, end_bd


# --- シグナル生成（逐次スキャン・per-code状態機械） ---------------------------------


def generate_f03_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
    cells: list[dict],
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """[start_bd, end_bd]内から4セル分のF03押し目シグナルを1回のスキャンでまとめて生成する。

    cell_common_definitions（MA_k・high_update_day・uptrend_base・recent_high・first_touch・
    episode_rule）とcells[].trigger_full_logicの完全実装。エピソード消費はcode×k単位で共有
    （first_touch(D,k)自体がNに依存しないため、同一kを共有するセル間で消費タイミングは
    数学的に一致する。recent_high(D,N)はcell単位で別途ANDする独立ゲート）。

    Returns:
        (rows_by_cell, diag_by_cell)。rows_by_cellは各セルの採用イベント[{signal_date,code},...]。
        diag_by_cellは pseudo_replication_control が要求する診断必須項目一式。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    warmup_idx = max(earliest_idx, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    ks = sorted({c["k"] for c in cells})
    cells_by_k: dict[int, list[dict]] = defaultdict(list)
    for c in cells:
        cells_by_k[c["k"]].append(c)

    # --- per-code状態 ---
    adjc_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=MA25_WINDOW))
    last_idx_seen: dict[str, int] = {}
    adjh_window: dict[str, deque] = defaultdict(deque)  # deque[(idx, AdjH)] 、[i-60,i-1]にトリム
    u_star_idx: dict[str, int] = {}  # code -> 直近『60日高値更新日』のidx（U*<D保証）
    episode_touched_u_star: dict[tuple[str, int], int] = {}  # (code,k) -> 消費済みu_star idx
    prev_state: dict[str, dict] = {}  # code -> {"idx": i, "ma": {k: val}, "adjl": val}（D-1参照・staleness検査つき）
    cooldown_until_idx: dict[tuple[str, str], int] = {}  # (cell_id, code) -> このidxまで抑止

    diag_by_cell: dict[str, dict] = {
        c["cell_id"]: {
            "cell_id": c["cell_id"], "k": c["k"], "N": c["N"],
            "raw_signal_count": 0, "event_count": 0, "duplicate_discarded": 0,
            "missing_undetermined_count": 0, "cooldown_blocked_first_touch_count": 0,
            "_codes": set(), "_month_counts": Counter(),
        }
        for c in cells
    }
    rows_by_cell: dict[str, list[dict]] = {c["cell_id"]: [] for c in cells}

    for d in scan_days:
        i = bday_index[d]
        in_window = start_bd <= d <= end_bd
        month = d[:6]
        bars_d = measure_base_rate.load_bars_day(d)

        for code, rec in bars_d.items():
            adjc = rec.get("AdjC")
            adjh = rec.get("AdjH")
            adjl = rec.get("AdjL")
            valid_c = adjc is not None and adjc > 0
            adjh_valid = adjh is not None and adjh > 0
            adjl_valid = adjl is not None and adjl > 0

            # (1) high_update_day(D)判定: [i-60, i-1]の既存窓（今日を含まない）で評価してから今日を追加する。
            win = adjh_window[code]
            while win and win[0][0] < i - HIGH_WINDOW_BDAYS:
                win.popleft()
            if len(win) >= HIGH_MIN_VALID and adjh_valid:
                window_max = max(v for _, v in win)
                is_update_day = adjh > window_max  # 同値は非更新
            else:
                is_update_day = False

            # (2) MA_k(D)算出用のAdjC履歴更新: ギャップ(欠測日を挟む)/非正なら履歴を全消去する
            #     （「ちょうどk観測」規約=deque内が常に連続営業日であることを保証するための不変条件）。
            last_seen = last_idx_seen.get(code)
            contiguous = last_seen is not None and i == last_seen + 1
            if not contiguous or not valid_c:
                adjc_hist[code].clear()
            if valid_c:
                adjc_hist[code].append(adjc)
            last_idx_seen[code] = i

            hist = adjc_hist[code]
            ma25 = (sum(list(hist)[-MA25_WINDOW:]) / MA25_WINDOW) if len(hist) >= MA25_WINDOW else None
            ma_by_k: dict[int, Optional[float]] = {
                k: (sum(list(hist)[-k:]) / k) if len(hist) >= k else None for k in ks
            }
            uptrend = (adjc > ma25) if ma25 is not None else None  # None=判定不能（MA25欠損）

            # (3) 前日値の取得（idxスタンプでstaleness検査。code不在日を挟んだ場合はNone扱い）
            ps = prev_state.get(code)
            if ps is not None and ps["idx"] == i - 1:
                pm, pa = ps["ma"], ps["adjl"]
            else:
                pm, pa = {}, None

            first_touch_by_k: dict[int, Optional[bool]] = {}
            for k in ks:
                mak_d, mak_prev = ma_by_k[k], pm.get(k)
                if mak_d is None or mak_prev is None or not adjl_valid or pa is None:
                    first_touch_by_k[k] = None  # 判定不能
                else:
                    first_touch_by_k[k] = (adjl <= 1.01 * mak_d) and (pa > 1.01 * mak_prev)

            # (4) エピソード消費（code×k単位・first_touch(D,k)成立時点でN/クールダウンに関わらず消費）
            u_star = u_star_idx.get(code)
            is_first_of_episode_by_k: dict[int, bool] = {}
            for k in ks:
                first_of_episode = False
                if first_touch_by_k[k] and u_star is not None:
                    key = (code, k)
                    if episode_touched_u_star.get(key) != u_star:
                        episode_touched_u_star[key] = u_star
                        first_of_episode = True
                is_first_of_episode_by_k[k] = first_of_episode

            # (5) セル別判定（in_window日のみ診断/出力を計上。状態更新自体は助走日も継続する）
            if in_window:
                for k in ks:
                    for c in cells_by_k[k]:
                        cid, N = c["cell_id"], c["N"]
                        d_entry = diag_by_cell[cid]
                        if uptrend is None or first_touch_by_k[k] is None:
                            d_entry["missing_undetermined_count"] += 1
                            continue
                        if not uptrend:
                            continue
                        if not first_touch_by_k[k]:
                            continue
                        if u_star is None or (i - u_star) > N:
                            continue  # recent_high(D,N)不成立（このセルのNには届かない）
                        d_entry["raw_signal_count"] += 1
                        if not is_first_of_episode_by_k[k]:
                            d_entry["duplicate_discarded"] += 1
                            continue
                        cd_key = (cid, code)
                        cd_until = cooldown_until_idx.get(cd_key)
                        if cd_until is not None and i <= cd_until:
                            d_entry["cooldown_blocked_first_touch_count"] += 1
                            continue
                        cooldown_until_idx[cd_key] = i + COOLDOWN_BDAYS
                        d_entry["event_count"] += 1
                        d_entry["_codes"].add(code)
                        d_entry["_month_counts"][month] += 1
                        rows_by_cell[cid].append({"signal_date": d, "code": code})

            # (6) U*更新はここで（今日の判定に使ったu_starはあくまで昨日以前の値=U<D保証）
            if is_update_day:
                u_star_idx[code] = i
            if adjh_valid:
                win.append((i, adjh))

            # (7) 次営業日のD-1参照用に今日の値を保存
            prev_state[code] = {
                "idx": i,
                "ma": {k: v for k, v in ma_by_k.items() if v is not None},
                "adjl": adjl if adjl_valid else None,
            }

    return rows_by_cell, diag_by_cell


# --- 出力 --------------------------------------------------------------------------


def write_freq_dry_run(path: Path, grid_sha: str, period: tuple[str, str], diag_by_cell: dict[str, dict]) -> None:
    out = {
        "grid_sha256": grid_sha,
        "period": {"start": period[0], "end": period[1]},
        "generated_ts": jq_fetch.now_jst().isoformat(),
        "note": "凍結後・リターン結合前の頻度監査のみ（リターン列は一切生成していない）。件数を見た後の定義変更は禁止。",
        "cells": {},
    }
    for cid, d in diag_by_cell.items():
        out["cells"][cid] = {
            "cell_id": d["cell_id"], "k": d["k"], "N": d["N"],
            "raw_signal_count": d["raw_signal_count"],
            "event_count": d["event_count"],
            "duplicate_discarded": d["duplicate_discarded"],
            "missing_undetermined_count": d["missing_undetermined_count"],
            "cooldown_blocked_first_touch_count": d["cooldown_blocked_first_touch_count"],
            "unique_codes": len(d["_codes"]),
            "month_counts": dict(sorted(d["_month_counts"].items())),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def _print_diag_summary(diag_by_cell: dict[str, dict]) -> None:
    for cid, d in sorted(diag_by_cell.items()):
        print(
            f"{cid} (k={d['k']}, N={d['N']}): raw={d['raw_signal_count']} event={d['event_count']} "
            f"dup={d['duplicate_discarded']} missing={d['missing_undetermined_count']} "
            f"cooldown_blocked={d['cooldown_blocked_first_touch_count']} "
            f"unique_codes={len(d['_codes'])} months={len(d['_month_counts'])}",
            file=sys.stderr,
        )


# --- メイン処理 -----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="第41周: F03押し目買いシグナル生成（カタログ§7-AF batch_v2t）")
    parser.add_argument("--freq-dry-run", action="store_true", help="リターン列を生成せず頻度診断のみ出力する（凍結仕様で1回だけ許可）")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default=FULL_START, help="スモーク用: 既定は発見期間開始2016-11-01")
    parser.add_argument("--end-date", default=FULL_END, help="スモーク用: 既定は確認期間終了2022-11-30")
    args = parser.parse_args()

    grid = verify_grid_frozen()
    cells = load_cells_from_grid(grid)
    print(f"grid_sha256={EXPECTED_GRID_SHA256} 照合OK。セル: {[c['cell_id'] for c in cells]}", file=sys.stderr)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _bd_bounds_from_dates(args.start_date, args.end_date, all_bdays)
    print(f"対象期間: {args.start_date}〜{args.end_date} (bd: {start_bd}〜{end_bd})", file=sys.stderr)

    print("シグナル生成中...", file=sys.stderr)
    rows_by_cell, diag_by_cell = generate_f03_signals(start_bd, end_bd, all_bdays, bday_index, cells)
    _print_diag_summary(diag_by_cell)

    output_dir = Path(args.output_dir)
    if args.freq_dry_run:
        out_path = output_dir / "freq_dry_run.json"
        write_freq_dry_run(out_path, EXPECTED_GRID_SHA256, (args.start_date, args.end_date), diag_by_cell)
        print(f"freq_dry_run: {out_path}")
        return 0

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    cells_dir = output_dir / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)
    for c in cells:
        cid = c["cell_id"]
        rows = rows_by_cell[cid]
        if not rows:
            print(f"WARN: {cid} はイベント0件。空のreturns.csvを出力する", file=sys.stderr)
            returns_df = pd.DataFrame(columns=["signal_date", "code", "month", "regime", "in_universe", "ret", "ret_stop8"])
        else:
            signals_df = pd.DataFrame(rows)
            returns_df, rs_diag = kpi_event_study.compute_signal_returns(
                signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=True
            )
            print(
                f"{cid}: compute_signal_returns raw={rs_diag['raw_signal_count']} "
                f"重複除去(占有ベース)={rs_diag['duplicate_discarded']} entry_missing={rs_diag['entry_missing']} "
                f"out_of_universe={rs_diag['out_of_universe']} "
                f"defer(+1/+2/+3)={rs_diag.get('defer_1bd')}/{rs_diag.get('defer_2bd')}/{rs_diag.get('defer_3bd')}",
                file=sys.stderr,
            )
            returns_df = returns_df[["signal_date", "code", "month", "regime", "in_universe", "ret", "ret_stop8"]]
        out_csv = cells_dir / f"{cid}.csv"
        returns_df.to_csv(out_csv, index=False)
        n_in_universe = int(returns_df["in_universe"].sum()) if len(returns_df) else 0
        print(f"{cid}: returns.csv書き出し完了 n_rows={len(returns_df)} n_in_universe={n_in_universe} -> {out_csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
