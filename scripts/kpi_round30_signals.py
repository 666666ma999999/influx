#!/usr/bin/env python3
"""第30周: 新規発明イベント三本バッチ（カタログ§7-S・T1〜T3）。

docs/stock-algo-kpi-catalog.md §7-S の事前登録定義（凍結・Codexレビュー㉞㉟GO）を実装する。
3試行とも in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で実行し、共通の
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round29_signals.py の
run_trial 標準フロー・prefilter_in_universe・複数trialドライバを踏襲する。

Canonical Module再利用（新規ロジックはT1/T2/T3の生成関数のみ）:
- T1 `three_up_ignition`: bars四本値のみ。D-2〜Dの3営業日連続で(陽線∩AdjH切り上げ∩AdjC切り上げ)、
  3日合計Va≥20日平均(D-3以前20観測)×3×1.5、初回性（終点D-5〜D-1で完全T1条件が未成立・
  履歴D-7まで）。銘柄ごとに直近数日の記録と20回Va履歴を保持する逐次スキャン。
- T2 `rs_line_high`: RS=AdjC/TOPIX（measure_base_rate.load_topix_series 再利用）。過去252営業日窓に
  paired有効252観測（AdjCとTOPIXがともに有限・正）が揃う銘柄で、RS(D)>過去252 paired RS最大・
  AdjH(D)≤過去252有効AdjH最大・Va≥20日平均×1.5。単調最大デックで窓最大をO(1)保持。
- T3 `engulf_reversal_day`: bars四本値のみ。AdjO<前日AdjC×0.99 ∩ AdjC>前日AdjC ∩ AdjC>AdjO ∩
  レンジ位置(AdjC-AdjL)/(AdjH-AdjL)≥0.7 ∩ Va≥20日平均×1.5 ∩ OHLCV品質ガード（第29周T3と同一）。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変・第20周§7-I前例）。
- フォワードリターン・重複除去・集計・§6判定・レポート・台帳: kpi_event_study の
  compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trial を再利用。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。

3試行とも defer_entry=True（§6手順6の第5周以降既定方針＝S高で買えない日は翌日繰り延べ）。

Usage:
    python3 scripts/kpi_round30_signals.py --trial all
    python3 scripts/kpi_round30_signals.py --trial t1
    python3 scripts/kpi_round30_signals.py --trial all --start 2017-01 --end 2017-12 --no-trials-append
"""
from __future__ import annotations

import argparse
import sys
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe/_earliest_bars_date を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/topix読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

VA_HISTORY_WINDOW = 20  # 「直近20回の有効Va観測値」（当日は判定後に追加＝D含まず）
WARMUP_BDAYS = 60  # T1/T3の短期履歴（deque・3日構造）を満たす助走

# --- 事前登録パラメータ（カタログ§7-S・事後変更禁止） -----------------------------

T1_KPI_NAME = "three_up_ignition"
T1_CONSEC_DAYS = 3  # D-2〜Dの3営業日連続
T1_VOL_MULTIPLIER = 1.5  # 3日合計Va ≥ 20日平均 × 3 × 1.5
T1_FIRSTNESS_LOOKBACK = 5  # 終点D-5〜D-1で完全T1条件が未成立

T2_KPI_NAME = "rs_line_high"
T2_RS_WINDOW_BDAYS = 252  # 過去252営業日窓（D自身含まず）
T2_VOL_MULTIPLIER = 1.5  # Va ≥ 20日平均 × 1.5

T3_KPI_NAME = "engulf_reversal_day"
T3_GAP_DOWN_RATIO = 0.99  # AdjO < 前日AdjC × 0.99（安く寄る）
T3_RANGE_LOC_MIN = 0.7  # (AdjC-AdjL)/(AdjH-AdjL) ≥ 0.7
T3_VOL_MULTIPLIER = 1.5  # Va ≥ 20日平均 × 1.5

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T3の3試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)
ROUND_TAG = "30_new_event_batch"

DEFER_RATIONALE = (
    "§7-S各試行はエントリー=T+1寄付。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
    "に従いdefer_entry=True。点火・相対力・切り返しいずれもS高張り付きが起きうるため通常の繰延で扱う。"
)


def _is_pos(v) -> bool:
    """有限・正の数値か（None/NaN/非数/非正を弾く）。"""
    return isinstance(v, (int, float)) and v == v and v > 0


# --- T1: three_up_ignition シグナル生成（新規ロジック） ---------------------------


def _full_t1_at(i: int, hist: dict, va_list: deque) -> Optional[dict]:
    """終点=業務日index i の「完全T1条件（3連陽線∩高値/終値切り上げ∩出来高）」を評価する。

    hist は当該銘柄の {業務日index: record}。i-3〜i の4営業日が連続して存在する必要がある
    （高値/終値の切り上げは D-2>D-3 から始まるため i-3 が必要）。出来高基準の20日平均は
    D-3(=i-3)以前の有効Va20観測（va_list から idx<=i-3 を抽出した末尾20件）。

    Returns:
        条件成立時は診断値dict、非成立は None。
    """
    r0, r1, r2, r3 = hist.get(i), hist.get(i - 1), hist.get(i - 2), hist.get(i - 3)
    if r0 is None or r1 is None or r2 is None or r3 is None:
        return None
    # 3営業日(D-2=i-2, D-1=i-1, D=i)が連続陽線 AdjC>AdjO。
    for r in (r2, r1, r0):
        if not (r["AdjC"] > r["AdjO"]):
            return None
    # 高値切り上げ AdjH(d)>AdjH(d-1)（d=i-2,i-1,i）。
    if not (r2["AdjH"] > r3["AdjH"] and r1["AdjH"] > r2["AdjH"] and r0["AdjH"] > r1["AdjH"]):
        return None
    # 終値切り上げ AdjC(d)>AdjC(d-1)（d=i-2,i-1,i）。
    if not (r2["AdjC"] > r3["AdjC"] and r1["AdjC"] > r2["AdjC"] and r0["AdjC"] > r1["AdjC"]):
        return None
    # 3日合計Va ≥ 20日平均(D-3以前20観測) × 3 × 1.5。
    prior = [va for (idx, va) in va_list if idx <= i - 3]
    if len(prior) < VA_HISTORY_WINDOW:
        return None
    avg20 = sum(prior[-VA_HISTORY_WINDOW:]) / VA_HISTORY_WINDOW
    va_sum3 = r2["Va"] + r1["Va"] + r0["Va"]
    if not (avg20 > 0 and va_sum3 >= avg20 * T1_CONSEC_DAYS * T1_VOL_MULTIPLIER):
        return None
    return {"avg20_va": avg20, "va_sum3": va_sum3, "adjc": r0["AdjC"], "adjo": r0["AdjO"]}


def generate_three_up_ignition_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内から三連陽線・高値切り上げ点火シグナルを生成する（カタログ§7-S T1）。

    完全T1条件(3連陽線∩高値/終値切り上げ∩3日合計出来高≥20日平均×3×1.5)を満たし、かつ初回性
    （終点D-5〜D-1のいずれでも完全T1条件が未成立）の日DでシグナルをDに確定する。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    warmup_idx = max(earliest_idx, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    hist: dict[str, dict] = defaultdict(dict)  # code -> {業務日index: record}
    va_list: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))  # code -> [(idx, Va)]（id<=i-3抽出用）
    fire_idxs: dict[str, deque] = defaultdict(lambda: deque(maxlen=T1_FIRSTNESS_LOOKBACK + 2))  # 完全T1成立の終点idx

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "ohlcv_quality_fail": 0,
        "full_t1_endpoints": 0,  # 完全T1条件が成立した終点(延べ)
        "firstness_excluded": 0,  # 完全T1は成立したが初回性で除外
        "signals_three_up_ignition": 0,
    }
    rows: list[dict] = []

    for d in scan_days:
        in_window = start_bd <= d <= end_bd
        i = bday_index[d]
        bars_d = measure_base_rate.load_bars_day(d)
        if in_window:
            diag["business_days_scanned"] += 1
        for code, rec in bars_d.items():
            if in_window:
                diag["code_day_observations"] += 1
            adjo, adjh, adjl, adjc, va = (
                rec.get("AdjO"), rec.get("AdjH"), rec.get("AdjL"), rec.get("AdjC"), rec.get("Va"),
            )
            if not (_is_pos(adjo) and _is_pos(adjh) and _is_pos(adjl) and _is_pos(adjc) and _is_pos(va)):
                if in_window:
                    diag["ohlcv_quality_fail"] += 1
                continue  # 欠測/不正はhist未追加＝連続性が切れる（切り上げ判定不能）
            hist[code][i] = {"AdjO": adjo, "AdjH": adjh, "AdjL": adjl, "AdjC": adjc, "Va": va}
            va_list[code].append((i, va))
            # 完全T1条件（初回性を含まない）を終点iで評価。
            detail = _full_t1_at(i, hist[code], va_list[code])
            if detail is not None:
                if in_window:
                    diag["full_t1_endpoints"] += 1
                # 初回性: 終点i-5〜i-1で完全T1が一度も成立していない。
                first = not any((i - T1_FIRSTNESS_LOOKBACK) <= j <= (i - 1) for j in fire_idxs[code])
                if in_window:
                    if first:
                        diag["signals_three_up_ignition"] += 1
                        rows.append(
                            {
                                "signal_date": d,
                                "code": code,
                                "adjc": detail["adjc"],
                                "adjo": detail["adjo"],
                                "va_sum3": detail["va_sum3"],
                                "avg20_va": detail["avg20_va"],
                            }
                        )
                    else:
                        diag["firstness_excluded"] += 1
                fire_idxs[code].append(i)  # 発火(初回性可否に関わらず完全T1成立を記録＝将来の初回性判定に使用)
            # 古いhistの掃除（メモリ抑制・直近7営業日分あれば十分）。
            hmap = hist[code]
            if len(hmap) > 12:
                for old in [k for k in hmap if k < i - 10]:
                    del hmap[old]

    return pd.DataFrame(rows), diag


# --- T2: rs_line_high シグナル生成（新規ロジック） --------------------------------


class _MonoMax:
    """(idx, value) の前方スライド窓に対する最大値を単調デックで O(1) 償却保持する。"""

    __slots__ = ("dq",)

    def __init__(self) -> None:
        self.dq: deque = deque()  # value降順、要素 (idx, value)

    def push(self, idx: int, value: float) -> None:
        while self.dq and self.dq[-1][1] <= value:
            self.dq.pop()
        self.dq.append((idx, value))

    def prune(self, low_idx: int) -> None:
        while self.dq and self.dq[0][0] < low_idx:
            self.dq.popleft()

    def max(self) -> Optional[float]:
        return self.dq[0][1] if self.dq else None

    def clear(self) -> None:
        self.dq.clear()


def generate_rs_line_high_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
    topix_map: dict[str, float],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内から相対力線52週新高値（価格未新高値）シグナルを生成する（カタログ§7-S T2）。

    RS(d)=AdjC(d)/TOPIX(d)。過去252営業日窓（D自身含まず）に paired有効252観測（AdjCとTOPIXが
    ともに有限・正）が揃う銘柄で、RS(D)>過去252 paired RS最大（厳密不等号）∩ AdjH(D)≤過去252有効
    AdjH最大（＝価格未新高値）∩ Va≥20日平均(D含まず)×1.5。paired有効の連続性が途切れた銘柄は
    ストリークを切り、252観測が再度揃うまで非シグナル（欠損日の過去延長はしない・暦固定窓）。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    warmup_idx = earliest_idx  # T2は252営業日窓のため取得可能な最古から助走する
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VA_HISTORY_WINDOW))
    rs_max: dict[str, _MonoMax] = defaultdict(_MonoMax)  # code -> paired RS の窓最大
    adjh_max: dict[str, _MonoMax] = defaultdict(_MonoMax)  # code -> 有効AdjH の窓最大
    last_valid_idx: dict[str, int] = {}  # code -> 最後にpaired有効だった業務日index
    streak_start: dict[str, int] = {}  # code -> 現在の連続paired有効ストリーク開始index

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "paired_valid_obs": 0,  # paired有効(AdjC∩TOPIX有限正)の延べ数
        "topix_missing_days": 0,  # TOPIXが欠測/不正だった業務日数
        "qualified_windows": 0,  # 過去252 paired有効が揃った銘柄日(延べ・シグナル候補母数)
        "excluded_rs_not_high": 0,  # 窓成立だがRS新高値でない
        "excluded_price_also_high": 0,  # RS新高値だが価格(AdjH)も新高値だったため除外
        "excluded_va": 0,  # RS新高値∩価格未新高値だが出来高不足
        "signals_rs_line_high": 0,
    }
    rows: list[dict] = []

    for d in scan_days:
        in_window = start_bd <= d <= end_bd
        i = bday_index[d]
        bars_d = measure_base_rate.load_bars_day(d)
        topix_d = topix_map.get(d)
        topix_ok = _is_pos(topix_d)
        if in_window:
            diag["business_days_scanned"] += 1
            if not topix_ok:
                diag["topix_missing_days"] += 1

        for code, rec in bars_d.items():
            if in_window:
                diag["code_day_observations"] += 1
            adjc = rec.get("AdjC")
            adjh = rec.get("AdjH")
            va = rec.get("Va")
            paired = topix_ok and _is_pos(adjc)
            if not paired:
                continue  # paired有効でない＝ストリークに寄与しない（次の有効日でギャップ検出→リセット）

            if in_window:
                diag["paired_valid_obs"] += 1
            rs_d = adjc / topix_d

            # 連続性: 直前業務日にpaired有効でなければストリークをリセット。
            if last_valid_idx.get(code) != i - 1:
                rs_max[code].clear()
                adjh_max[code].clear()
                streak_start[code] = i

            # 過去252窓 [i-252, i-1] へ剪定（D自身含まず）。
            low = i - T2_RS_WINDOW_BDAYS
            rs_max[code].prune(low)
            adjh_max[code].prune(low)

            # 窓成立判定: ストリーク開始が i-252 以前＝[i-252, i-1] の252営業日が全てpaired有効。
            qualified = streak_start[code] <= low
            if qualified and in_window:
                diag["qualified_windows"] += 1
                max_rs_prev = rs_max[code].max()
                max_adjh_prev = adjh_max[code].max()
                hist = va_hist.get(code)
                if max_rs_prev is None:
                    pass  # 論理上到達しない（ストリーク中は必ずpush済）
                elif not (rs_d > max_rs_prev):
                    diag["excluded_rs_not_high"] += 1
                elif not (_is_pos(adjh) and max_adjh_prev is not None and adjh <= max_adjh_prev):
                    diag["excluded_price_also_high"] += 1
                elif hist is None or len(hist) < VA_HISTORY_WINDOW:
                    diag["excluded_va"] += 1
                else:
                    avg20 = sum(hist) / VA_HISTORY_WINDOW
                    if not (_is_pos(va) and avg20 > 0 and va >= avg20 * T2_VOL_MULTIPLIER):
                        diag["excluded_va"] += 1
                    else:
                        diag["signals_rs_line_high"] += 1
                        rows.append(
                            {
                                "signal_date": d,
                                "code": code,
                                "rs": rs_d,
                                "rs_max_prev252": max_rs_prev,
                                "adjh": adjh,
                                "adjh_max_prev252": max_adjh_prev,
                                "adjc": adjc,
                                "topix": topix_d,
                                "va": va,
                                "avg20_va": avg20,
                            }
                        )

            # 当日を窓へ push（次営業日以降の「過去」の一員として。D含まず規約と整合）。
            rs_max[code].push(i, rs_d)
            if _is_pos(adjh):
                adjh_max[code].push(i, adjh)
            last_valid_idx[code] = i

            # Va履歴更新（D含まず: 判定後に当日Vaを追加）。
            if _is_pos(va):
                va_hist[code].append(va)

    return pd.DataFrame(rows), diag


# --- T3: engulf_reversal_day シグナル生成（新規ロジック） -------------------------


def generate_engulf_reversal_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内から寄安引高の切り返し日シグナルを生成する（カタログ§7-S T3）。

    (AdjO<前日AdjC×0.99) ∩ (AdjC>前日AdjC) ∩ (AdjC>AdjO) ∩ ((AdjC-AdjL)/(AdjH-AdjL)≥0.7・AdjH>AdjL)
    ∩ (Va≥20日平均(D含まず)×1.5)。OHLCV全て有限・正・AdjH>AdjL・Va>0（第29周T3と同一の品質ガード）。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    warmup_idx = max(earliest_idx, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VA_HISTORY_WINDOW))

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "ohlcv_quality_fail": 0,
        "prev_close_missing": 0,
        "insufficient_va_history": 0,
        "pass_gap_down": 0,
        "pass_close_gt_prev": 0,
        "pass_bullish": 0,
        "pass_range_location": 0,
        "signals_engulf_reversal_day": 0,
    }
    rows: list[dict] = []

    prev_bars: Optional[dict] = None
    for d in scan_days:
        in_window = start_bd <= d <= end_bd
        bars_d = measure_base_rate.load_bars_day(d)
        if in_window:
            diag["business_days_scanned"] += 1
            for code, rec in bars_d.items():
                diag["code_day_observations"] += 1
                adjo, adjh, adjl, adjc, va = (
                    rec.get("AdjO"), rec.get("AdjH"), rec.get("AdjL"), rec.get("AdjC"), rec.get("Va"),
                )
                # OHLCV品質ガード（全て有限・正・AdjH>AdjL・Va>0）。
                if not (_is_pos(adjo) and _is_pos(adjh) and _is_pos(adjl) and _is_pos(adjc) and _is_pos(va)):
                    diag["ohlcv_quality_fail"] += 1
                    continue
                if not (adjh > adjl):
                    diag["ohlcv_quality_fail"] += 1
                    continue
                prev_rec = (prev_bars or {}).get(code)
                adjc_prev = prev_rec.get("AdjC") if prev_rec else None
                if not _is_pos(adjc_prev):
                    diag["prev_close_missing"] += 1
                    continue
                if not (adjo < adjc_prev * T3_GAP_DOWN_RATIO):
                    continue  # 安く寄る
                diag["pass_gap_down"] += 1
                if not (adjc > adjc_prev):
                    continue  # 前日終値超えで引ける
                diag["pass_close_gt_prev"] += 1
                if not (adjc > adjo):
                    continue  # 陽線
                diag["pass_bullish"] += 1
                range_loc = (adjc - adjl) / (adjh - adjl)
                if not (range_loc >= T3_RANGE_LOC_MIN):
                    continue
                diag["pass_range_location"] += 1
                hist = va_hist.get(code)
                if hist is None or len(hist) < VA_HISTORY_WINDOW:
                    diag["insufficient_va_history"] += 1
                    continue
                avg20 = sum(hist) / VA_HISTORY_WINDOW
                if not (avg20 > 0 and va >= avg20 * T3_VOL_MULTIPLIER):
                    continue
                diag["signals_engulf_reversal_day"] += 1
                rows.append(
                    {
                        "signal_date": d,
                        "code": code,
                        "adjo": adjo,
                        "adjc_prev": adjc_prev,
                        "adjc": adjc,
                        "adjl": adjl,
                        "adjh": adjh,
                        "range_location": range_loc,
                        "va": va,
                        "avg20_va": avg20,
                    }
                )

        # 履歴更新（D含まず）。
        for code, rec in bars_d.items():
            va = rec.get("Va")
            if _is_pos(va):
                va_hist[code].append(va)
        prev_bars = bars_d

    return pd.DataFrame(rows), diag


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録（round29 run_trial 標準フローを踏襲） ---


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    harness_ctx: dict,
    report_extra_lines: Optional[list[str]] = None,
    append_to_ledger: bool = True,
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録までを行う
    （kpi_round29_signals.run_trial と同一の標準フロー。全試行 defer_entry=True）。"""
    all_bdays = harness_ctx["all_bdays"]
    bday_index = harness_ctx["bday_index"]
    regime_by_day = harness_ctx["regime_by_day"]
    base_rate_by_month = harness_ctx["base_rate_by_month"]
    universes_by_month = harness_ctx["universes_by_month"]

    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=True
    )
    diag["regime_filter"] = None
    diag["pre_regime_filter_count"] = len(harness_signals_df)

    in_universe_df = (
        returns_df[returns_df["in_universe"]].reset_index(drop=True) if len(returns_df) else returns_df
    )
    if in_universe_df.empty:
        raise SystemExit(f"FATAL: {kpi_name}のin_universeシグナルが0件です")

    stats = kpi_event_study.compute_stats(in_universe_df, base_rate_by_month, PERIOD)
    verdict, reasons = kpi_event_study.judge(stats)
    defer_stats = kpi_event_study._compute_defer_stats(in_universe_df)

    ev_ci = kpi_event_study.bootstrap_ev_ci(in_universe_df, ev_column="ret")
    conclusion = kpi_event_batch_signals.classify_exploratory(
        stats.get("point_lift"), ev_ci["point_ev"], ev_ci["ci_low"]
    )

    params = {
        **base_params,
        "defer_entry": True,
        "defer_rationale": DEFER_RATIONALE,
        "ev_none_point": ev_ci["point_ev"],
        "ev_none_ci_low": ev_ci["ci_low"],
        "ev_none_ci_high": ev_ci["ci_high"],
        "ev_ci_n_boot_valid": ev_ci["n_boot_valid"],
        "exploratory_conclusion": conclusion,
        "pre_registered_success_rule": (
            "promising: EV(なし)CI下限>0 かつ point_lift>=1.5 / "
            "rejected: EV点推定<=0 または point_lift<1.0 / inconclusive: それ以外"
        ),
        "round": ROUND_TAG,
        "multi_trial_discount_note": MULTI_TRIAL_NOTE,
        "entry_missing": diag["entry_missing"],
        "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"],
        "out_of_universe": diag["out_of_universe"],
    }

    kpi_dir = OUTPUT_ROOT / kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    returns_path = kpi_dir / "returns.csv"
    report_path = kpi_dir / "report.md"
    returns_df.to_csv(returns_path, index=False)
    kpi_event_study.write_report_md(
        report_path, kpi_name, params, PERIOD, diag, stats, verdict, reasons, in_universe_df,
        defer_stats=defer_stats,
    )
    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n## 探索的一次結論（第30周・カタログ§7-S事前登録の共通ルール）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            if ev_ci["point_ev"] is not None
            else "- EV(なし)点推定 = 計算不能\n"
        )
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {MULTI_TRIAL_NOTE}\n")
        if report_extra_lines:
            f.write("\n## 近傍事実・必須診断・注記（§7-S）\n")
            for line in report_extra_lines:
                f.write(f"- {line}\n")

    if append_to_ledger:
        trial_record = {
            "run_id": uuid.uuid4().hex,
            "ts": jq_fetch.now_jst().isoformat(),
            "kpi_name": kpi_name,
            "params": params,
            "period": {"start": PERIOD[0], "end": PERIOD[1]},
            "n": stats["n"],
            "lift": stats.get("point_lift"),
            "ci_low": stats.get("ci_low"),
            "ci_high": stats.get("ci_high"),
            "ev": stats.get("ev_stop8"),
            "verdict": verdict,
            "entry_mode": "defer_max3bd",
            "regime_filter": None,
        }
        kpi_event_study.append_trial(trial_record, TRIALS_PATH)
        print(f"{kpi_name}: 台帳へ append 済み ({TRIALS_PATH})")
    else:
        print(f"{kpi_name}: --no-trials-append 指定のため台帳へは記録しませんでした")

    print(
        f"{kpi_name} 完了: n={stats['n']} lift={stats.get('point_lift')} verdict(§6)={verdict} "
        f"EV点推定={ev_ci['point_ev']} CI95=[{ev_ci['ci_low']},{ev_ci['ci_high']}] "
        f"探索的一次結論={conclusion}"
    )
    print(f"{kpi_name} report: {report_path}")
    print(f"{kpi_name} returns: {returns_path}")

    return {
        "kpi_name": kpi_name, "n": stats["n"], "lift": stats.get("point_lift"),
        "ci_low": stats.get("ci_low"), "ci_high": stats.get("ci_high"),
        "ev_point": ev_ci["point_ev"], "ev_ci_low": ev_ci["ci_low"], "ev_ci_high": ev_ci["ci_high"],
        "ev_stop8": stats.get("ev_stop8"),
        "verdict": verdict, "conclusion": conclusion, "returns_path": returns_path,
        "avg_monthly_n": stats.get("avg_monthly_n"),
        "entry_missing": diag["entry_missing"], "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"], "out_of_universe": diag["out_of_universe"],
        "in_universe_df": in_universe_df,
    }


# --- 各トライアルのドライバ ---------------------------------------------------------


def _prefilter_and_save(signals_df: pd.DataFrame, kpi_name: str, hc: dict) -> tuple[pd.DataFrame, int, int]:
    """生シグナルを保存し、ユニバース事前フィルタ（統計結果不変）を適用する。"""
    kpi_dir = OUTPUT_ROOT / kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)
    filtered_df, n_raw, n_filtered = kpi_round23_signals.prefilter_in_universe(
        signals_df, hc["universes_by_month"]
    )
    print(
        f"{kpi_name} ユニバース事前フィルタ: 生{n_raw}件 → ハーネス投入{n_filtered}件"
        f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変・第20周§7-I前例)",
        file=sys.stderr,
    )
    if filtered_df.empty:
        raise SystemExit(f"FATAL: {kpi_name}の事前フィルタ後シグナルが0件です")
    return filtered_df, n_raw, n_filtered


def run_t1(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_three_up_ignition_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"銘柄日観測={gd['code_day_observations']}, OHLCV品質除外={gd['ohlcv_quality_fail']}, "
        f"完全T1終点(延べ)={gd['full_t1_endpoints']}, 初回性除外={gd['firstness_excluded']}, "
        f"シグナル成立={gd['signals_three_up_ignition']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T1_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            "D-2〜Dの3営業日連続で(AdjC>AdjO=陽線 ∩ AdjH(d)>AdjH(d-1)=高値切り上げ ∩ "
            "AdjC(d)>AdjC(d-1)=終値切り上げ)、かつ3日合計Va≥20日平均(D-3以前20観測)×3×1.5、"
            "かつ初回性(終点D-5〜D-1で完全T1条件が未成立)"
        ),
        "consec_days": T1_CONSEC_DAYS,
        "vol_multiplier": T1_VOL_MULTIPLIER,
        "firstness_lookback_bdays": T1_FIRSTNESS_LOOKBACK,
        "va_history_window": VA_HISTORY_WINDOW,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_full_t1_endpoints": gd["full_t1_endpoints"],
        "diag_firstness_excluded": gd["firstness_excluded"],
        "diag_ohlcv_quality_fail": gd["ohlcv_quality_fail"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "neighbor_note": (
            "volshock(単日出来高倍率)・gap_hold(単日値動きの質)とは判定軸が異なる(本試行は複数日の"
            "連続構造=持続性の証跡)。単日ショックの再検証ではない"
        ),
    }
    report_extra = [
        f"必須診断: 完全T1終点(延べ)={gd['full_t1_endpoints']} / 初回性除外={gd['firstness_excluded']} "
        f"/ OHLCV品質除外={gd['ohlcv_quality_fail']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    res = run_trial(
        filtered_df, T1_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["signals_raw_df"] = signals_df
    return res


def run_t2(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_rs_line_high_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"], shared["topix_map"]
    )
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"paired有効(延べ)={gd['paired_valid_obs']}, TOPIX欠測日={gd['topix_missing_days']}, "
        f"窓成立(延べ)={gd['qualified_windows']}, RS未新高値除外={gd['excluded_rs_not_high']}, "
        f"価格も新高値除外={gd['excluded_price_also_high']}, 出来高不足除外={gd['excluded_va']}, "
        f"シグナル成立={gd['signals_rs_line_high']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T2_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            "RS(d)=AdjC(d)÷TOPIX(d)。過去252営業日窓(D自身含まず)にpaired有効(AdjC∩TOPIX有限正)252観測"
            f"が揃う銘柄で、RS(D)>過去252 paired RS最大 ∩ AdjH(D)≤過去252有効AdjH最大(価格未新高値) ∩ "
            f"Va≥20日平均(D含まず)×{T2_VOL_MULTIPLIER}"
        ),
        "rs_window_bdays": T2_RS_WINDOW_BDAYS,
        "vol_multiplier": T2_VOL_MULTIPLIER,
        "va_history_window": VA_HISTORY_WINDOW,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_paired_valid_obs": gd["paired_valid_obs"],
        "diag_qualified_windows": gd["qualified_windows"],
        "diag_excluded_rs_not_high": gd["excluded_rs_not_high"],
        "diag_excluded_price_also_high": gd["excluded_price_also_high"],
        "diag_excluded_va": gd["excluded_va"],
        "diag_topix_missing_days": gd["topix_missing_days"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "data_note": (
            "bars/TOPIXの実在最古は2016-07。252営業日窓のため有効シグナルは概ね2017年央以降"
            "(それ以前は窓不成立で構造的に非シグナル・欠損の過去延長はしない)"
        ),
        "neighbor_note": (
            "high52_breakout(価格の絶対水準ブレイク・pending/EV負)とは軸が異なる(相対線vs絶対価格・"
            "かつAdjH(D)≤過去252有効AdjH最大で価格未新高値を条件にし排他的)"
        ),
    }
    report_extra = [
        f"必須診断: paired有効(延べ)={gd['paired_valid_obs']} / 窓成立(延べ=候補母数)={gd['qualified_windows']} "
        f"/ RS未新高値除外={gd['excluded_rs_not_high']} / RS新高値かつ価格も新高値の除外={gd['excluded_price_also_high']} "
        f"/ 出来高不足除外={gd['excluded_va']} / TOPIX欠測日={gd['topix_missing_days']}。",
        base_params["data_note"],
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    res = run_trial(
        filtered_df, T2_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["signals_raw_df"] = signals_df
    return res


def run_t3(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T3 {T3_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_engulf_reversal_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T3 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"銘柄日観測={gd['code_day_observations']}, OHLCV品質除外={gd['ohlcv_quality_fail']}, "
        f"前日終値なし={gd['prev_close_missing']}, Va履歴不足={gd['insufficient_va_history']}, "
        f"安寄通過={gd['pass_gap_down']}, 前日終値超通過={gd['pass_close_gt_prev']}, "
        f"陽線通過={gd['pass_bullish']}, レンジ位置通過={gd['pass_range_location']}, "
        f"シグナル成立={gd['signals_engulf_reversal_day']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T3シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T3_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            f"(AdjO<前日AdjC×{T3_GAP_DOWN_RATIO}=安寄) ∩ (AdjC>前日AdjC) ∩ (AdjC>AdjO=陽線) ∩ "
            f"((AdjC-AdjL)/(AdjH-AdjL)≥{T3_RANGE_LOC_MIN}・AdjH>AdjL) ∩ "
            f"(Va≥20日平均(D含まず)×{T3_VOL_MULTIPLIER})。OHLCV全て有限・正・AdjH>AdjL・Va>0"
        ),
        "gap_down_ratio": T3_GAP_DOWN_RATIO,
        "range_location_min": T3_RANGE_LOC_MIN,
        "vol_multiplier": T3_VOL_MULTIPLIER,
        "va_history_window": VA_HISTORY_WINDOW,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_ohlcv_quality_fail": gd["ohlcv_quality_fail"],
        "diag_pass_gap_down": gd["pass_gap_down"],
        "diag_pass_close_gt_prev": gd["pass_close_gt_prev"],
        "diag_pass_bullish": gd["pass_bullish"],
        "diag_pass_range_location": gd["pass_range_location"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "neighbor_note": (
            "gap_hold_close_strong(買いで始まり買いで終わる・rejected・lift2.57/EV-0.32%)と対になる"
            "『攻守転換』(売りで始まり買いで制す)。リバーサル家族のEV負傾向を割引基準として持ち込む"
        ),
    }
    report_extra = [
        f"必須診断(段階別通過): 安寄={gd['pass_gap_down']} / 前日終値超={gd['pass_close_gt_prev']} "
        f"/ 陽線={gd['pass_bullish']} / レンジ位置≥{T3_RANGE_LOC_MIN}={gd['pass_range_location']} "
        f"/ OHLCV品質除外={gd['ohlcv_quality_fail']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    res = run_trial(
        filtered_df, T3_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["signals_raw_df"] = signals_df
    return res


TRIAL_RUNNERS = {"t1": run_t1, "t2": run_t2, "t3": run_t3}


# --- Jaccard 診断（診断限定・(code, signal_date) イベントキー） ---------------------


def _event_keys(df: pd.DataFrame) -> set:
    """DataFrame から (code, signal_date) の集合を作る（型を str に正規化して突合）。"""
    if df is None or len(df) == 0 or "code" not in df.columns or "signal_date" not in df.columns:
        return set()
    return set(zip(df["code"].astype(str), df["signal_date"].astype(str)))


def _jaccard(a: set, b: set) -> tuple[float, int]:
    """Jaccard係数と積集合サイズを返す（両空なら 0.0）。"""
    if not a and not b:
        return 0.0, 0
    inter = len(a & b)
    union = len(a | b)
    return (inter / union if union else 0.0), inter


def _load_existing_raw(kpi_name: str) -> set:
    """output/kpi/<kpi_name>/signals_raw.csv から (code, signal_date) 集合を読む（診断限定）。"""
    p = OUTPUT_ROOT / kpi_name / "signals_raw.csv"
    if not p.exists():
        print(f"[Jaccard診断] 既存 {kpi_name}/signals_raw.csv が無いためスキップ", file=sys.stderr)
        return set()
    df = pd.read_csv(p, usecols=["signal_date", "code"], dtype=str)
    return _event_keys(df)


def print_jaccard_diagnostics(new_keys: dict[str, set]) -> list[str]:
    """T1/T2/T3相互 + 各×volshock_5x/high52_breakout/gap_hold_close_strong のJaccardを算出・表示する。

    Codex㉞: T1/T2は出来高を伴う上昇継続系で独立とみなさないための解釈補助（診断限定）。
    Returns: レポート/報告用の1行文字列リスト。
    """
    lines: list[str] = []
    existing = {
        name: _load_existing_raw(name)
        for name in ("volshock_5x", "high52_breakout", "gap_hold_close_strong")
    }
    labels = {"t1": T1_KPI_NAME, "t2": T2_KPI_NAME, "t3": T3_KPI_NAME}

    # 相互（生成された試行のみ）。
    present = [t for t in ("t1", "t2", "t3") if t in new_keys]
    for a, b in [("t1", "t2"), ("t1", "t3"), ("t2", "t3")]:
        if a in new_keys and b in new_keys:
            j, inter = _jaccard(new_keys[a], new_keys[b])
            line = (
                f"Jaccard {labels[a]}×{labels[b]} = {j:.4f} "
                f"(積集合={inter} / |{labels[a]}|={len(new_keys[a])} / |{labels[b]}|={len(new_keys[b])})"
            )
            lines.append(line)

    # 各試行 × 既存3集合。
    for t in present:
        for name, ekeys in existing.items():
            j, inter = _jaccard(new_keys[t], ekeys)
            line = (
                f"Jaccard {labels[t]}×{name}(既存) = {j:.4f} "
                f"(積集合={inter} / |{labels[t]}|={len(new_keys[t])} / |{name}|={len(ekeys)})"
            )
            lines.append(line)

    print("\n=== Jaccard診断（診断限定・(code,signal_date)イベントキー・§7-S） ===")
    for ln in lines:
        print(ln)
    return lines


# --- メイン処理 ---------------------------------------------------------------------


def _event_bd_bounds(start_month: str, end_month: str, all_bdays: list[str]) -> tuple[str, str]:
    start_bound = start_month.replace("-", "") + "01"
    end_bound = end_month.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")
    return start_bd, end_bd


def main() -> int:
    parser = argparse.ArgumentParser(description="第30周: 新規発明イベント三本バッチ（カタログ§7-S・T1〜T3）")
    parser.add_argument("--trial", choices=["t1", "t2", "t3", "all"], default="all")
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})。smokeテスト用",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})。smokeテスト用",
    )
    parser.add_argument(
        "--no-trials-append", action="store_true",
        help="台帳(trials.jsonl)へ記録しない(smokeテスト用・成果物のみ生成)",
    )
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    # holdout(2023年以降)抵触は FATAL（in-sampleのみで評価する事前登録の凍結境界を機械で強制）。
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        raise SystemExit(
            f"FATAL: --end={args.end} はholdout期間(2023年以降)に抵触します。"
            f"§7-Sはin-sample({kpi_pead_signals.IN_SAMPLE_END}まで)で評価します。holdoutは使用しません。"
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _event_bd_bounds(args.start, args.end, all_bdays)

    topix_close = measure_base_rate.load_topix_series()
    topix_map = {str(k): float(v) for k, v in topix_close.to_dict().items()}
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    shared = {
        "start_bd": start_bd,
        "end_bd": end_bd,
        "append_to_ledger": not args.no_trials_append,
        "topix_map": topix_map,
        "harness_ctx": {
            "all_bdays": all_bdays,
            "bday_index": bday_index,
            "regime_by_day": regime_by_day,
            "base_rate_by_month": base_rate_by_month,
            "universes_by_month": universes_by_month,
        },
    }

    trials_to_run = ["t1", "t2", "t3"] if args.trial == "all" else [args.trial]
    results = []
    new_keys: dict[str, set] = {}
    for t in trials_to_run:
        r = TRIAL_RUNNERS[t](shared)
        results.append(r)
        new_keys[t] = _event_keys(r.get("signals_raw_df"))

    jaccard_lines = print_jaccard_diagnostics(new_keys)

    print("\n=== 第30周バッチ完了サマリー ===")
    for r in results:
        print(
            f"{r['kpi_name']}: n={r['n']} 月平均n={r.get('avg_monthly_n')} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={r['ev_point']}[{r['ev_ci_low']},{r['ev_ci_high']}] EV(stop8)={r['ev_stop8']} "
            f"verdict(§6)={r['verdict']} 一次結論={r['conclusion']} "
            f"entry_missing={r['entry_missing']}/raw={r['raw_signal_count']}"
        )
    for ln in jaccard_lines:
        print(f"  [Jaccard] {ln}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
