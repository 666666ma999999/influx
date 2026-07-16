#!/usr/bin/env python3
"""第37周: 新規軸二本バッチ — 空売りクラウディング撤退・NR7上放れ（カタログ§7-Z・T1〜T2）。

docs/stock-algo-kpi-catalog.md §7-Z の事前登録定義（凍結・Codex52反映GO）を実装する。
2試行とも in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で実行し、共通の
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round35_signals.py /
scripts/kpi_round29_signals.py の run_trial 標準フロー・prefilter_in_universe・複数trialドライバを
踏襲する。shortsaleデータの読み方は scripts/kpi_shortcover_signals.py（SHORTSALE_DIR/read_json_gz・
DiscDate順の状態更新・翌営業日シグナル確定）を、OHLCV品質ガードは §7-R T3（kpi_round29_signals）を
それぞれ踏襲する。

Canonical Module再利用（新規ロジックはT1/T2の生成関数のみ）:
- T1 `short_crowd_exit`: shortsale開示（DiscDate順バッチ）で、ポジション状態キー=(SSName,DICName,
  FundName)・Reporter=distinct SSName。銘柄XのアクティブReporter=ShrtPosToSO≥0.5%のアクティブ
  ポジションを1つ以上持つSSName。撤退イベント=DiscDate=Dのバッチ全レコード反映後に「バッチ前は
  アクティブだったSSNameのアクティブポジションが0になる」∩撤退前のアクティブReporter数≥2。
  シグナル日=DiscDateの翌営業日G（17時公表・shortcover_turnと同一Canonical準拠）。同日複数撤退は
  1シグナル。shortsale読込はkpi_shortcover_signals.SHORTSALE_DIR/jq_fetch.read_json_gzを再利用。
- T2 `nr7_breakout`: 営業日Eの日中レンジ比(AdjH(E)−AdjL(E))/AdjC(E-1)がE-6〜Eの7観測で最小
  （同値許容・7観測揃わなければ非該当・欠損の過去延長禁止・OHLCV品質ガード§7-R T3と同一）∩
  D=E直後1営業日限定でAdjC(D)>AdjH(E)∩Va(D)≥20日平均(D-20〜D-1の20有効観測)×1.5。
  bars読込はmeasure_base_rate.load_bars_dayを再利用。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変・第20周§7-I前例）。
- フォワードリターン・重複除去・集計・§6判定・レポート・台帳: kpi_event_study の
  compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trial を再利用。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。
- shortcover_turnとのJaccard診断: kpi_shortcover_signals.generate_shortcover_signals を再利用。

2試行とも defer_entry=True（§6手順6の第5周以降既定方針＝S高で買えない日は翌日繰り延べ）。

家族の割引解釈（§7-Z凍結・Codex52 M対応）: shortcover_family の近傍追加（判定軸=残高量→報告者
粒度の新軸だが適応的追加であることを明示）。同一in-sample期間で報告者粒度の変種を以後追加しない・
結果は前向き優先順位付け限定。近傍事実（shortcover_turn lift2.29・EV弱）で割引。T2は range_breakout
（60日レンジ幅・fail）とは軸が異なる（単日レンジの収縮順位）ことを宣言しfail家族の近傍事実で割引。

Usage:
    python3 scripts/kpi_round37_signals.py --trial all
    python3 scripts/kpi_round37_signals.py --trial t1
    python3 scripts/kpi_round37_signals.py --trial all --start 2017-01 --end 2017-12 --no-trials-append
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
import jq_fetch  # noqa: E402  (Canonical Module: now_jst / read_json_gz を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe/_earliest_bars_date を再利用)
import kpi_shortcover_signals  # noqa: E402  (Canonical Module: SHORTSALE_DIR / generate_shortcover_signals を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

VA_HISTORY_WINDOW = 20  # 「直近20回の有効Va観測値」（当日は判定後に追加＝D含まず。§7-R規約と同一）
WARMUP_BDAYS = 60  # deque(maxlen=20) と NR7 7日窓を確実に満たすための助走

# --- 事前登録パラメータ（カタログ§7-Z・事後変更禁止） -----------------------------

# T1: short_crowd_exit（空売り機関の撤退イベント）。
T1_KPI_NAME = "short_crowd_exit"
ACTIVE_THRESHOLD = 0.005  # ShrtPosToSO ≥ 0.5% = アクティブポジション（JPX報告義務境界）
T1_MIN_ACTIVE_REPORTERS = 2  # 撤退前のアクティブReporter数 ≥ 2

# T2: nr7_breakout（レンジ収縮7日最小→翌日上放れ）。
T2_KPI_NAME = "nr7_breakout"
T2_NR7_WINDOW = 7  # E-6〜Eの7観測
T2_VOL_MULTIPLIER = 1.5  # Va(D) ≥ 20日平均 × 1.5

ROUND_TAG = "37_new_axis_batch"

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T2の2試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)

SHORTCOVER_FAMILY_NOTE = (
    "short_crowd_exitはshortcover_family(判定軸=集計残高量)への報告者粒度(機関の数と個別撤退)の"
    "新軸追加。72試行で未使用の角度だが適応的追加であることを明示し、同一in-sample期間での報告者"
    "粒度の変種を以後追加しない。近傍事実(shortcover_turn: 残高減少転換 lift2.29・EV弱)で割引し、"
    "結果は統計的確認ではなく前向き候補の優先順位付けに限定する。"
)

NR7_NEIGHBOR_NOTE = (
    "nr7_breakoutは古典的NR7ブレイク。range_breakout(60日レンジ幅・fail)は期間レンジの『水準』、"
    "本試行は単日レンジの『収縮順位』で軸が異なることを宣言。fail家族(range_breakout)の近傍事実を"
    "割引の基準として持ち込む。"
)

DEFER_RATIONALE = (
    "§7-Z各試行はエントリー=T+1寄付。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
    "に従いdefer_entry=True。ショート撤退の買い戻し・NR7上放れいずれもT+1寄付がS高張り付きで買えない"
    "ことがあり得るため通常の繰延で扱う。"
)


# --- T1: short_crowd_exit シグナル生成（新規ロジック） -----------------------------


def _load_shortsale_full() -> pd.DataFrame:
    """shortsale の全レコードを T1 に必要なフィールド込みで読み込む（状態キー=(SSName,DICName,
    FundName)・PrevRptRatio整合診断のため）。canonical loader（kpi_shortcover_signals.
    load_all_shortsale_records）は5列に絞るため、同一 SHORTSALE_DIR / jq_fetch.read_json_gz を再利用
    しつつT1が必要とする列を保持する読込を行う（判定路の二重化ではなく列集合の拡張）。"""
    shortsale_dir = kpi_shortcover_signals.SHORTSALE_DIR
    if not shortsale_dir.exists() or not any(shortsale_dir.glob("*.json.gz")):
        raise SystemExit(
            f"FATAL: shortsaleキャッシュが見つかりません: {shortsale_dir}\n"
            f"先に `python3 scripts/jq_fetch.py --only shortsale` を実行してください。"
        )
    rows: list[dict] = []
    for path in sorted(shortsale_dir.glob("*.json.gz")):
        obj = jq_fetch.read_json_gz(path)
        rows.extend(obj["data"])
    if not rows:
        raise SystemExit(f"FATAL: shortsaleキャッシュが空です: {shortsale_dir}")
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["DiscDate", "CalcDate", "Code", "SSName", "ShrtPosToSO"])
    df["DiscDate"] = df["DiscDate"].str.replace("-", "", regex=False)
    df["CalcDate"] = df["CalcDate"].str.replace("-", "", regex=False)
    for col in ("DICName", "FundName"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("")
    if "PrevRptRatio" not in df.columns:
        df["PrevRptRatio"] = None
    return df[
        ["DiscDate", "CalcDate", "Code", "SSName", "DICName", "FundName", "ShrtPosToSO", "PrevRptRatio"]
    ]


def _active_reporters(state: dict[tuple, float]) -> set:
    """状態辞書 (SSName,DICName,FundName)->ratio から、アクティブポジション(≥0.5%)を1つ以上
    持つ distinct SSName の集合を返す。"""
    active: set = set()
    for (ssname, _dic, _fund), ratio in state.items():
        if ratio >= ACTIVE_THRESHOLD:
            active.add(ssname)
    return active


def generate_short_crowd_exit_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd] 内で空売り機関の撤退イベントシグナルを生成する（カタログ§7-Z T1）。

    銘柄ごとに全履歴（データ取得開始〜）を通しで状態管理し、出力のみ [start_bd, end_bd] に絞る
    （状態のウォームアップを正しく反映するため）。DiscDate順にバッチ処理し、バッチ全反映後に
    「バッチ前アクティブだったSSNameのアクティブポジションが0になった」∩「撤退前アクティブ
    Reporter数≥2」を撤退イベントとする。シグナル日=DiscDateの翌営業日G。同日複数撤退は1シグナル。
    """
    df = _load_shortsale_full()
    df.sort_values(["Code", "DiscDate", "CalcDate"], inplace=True)

    diag = {
        "total_records": int(len(df)),
        "disclosure_batches_processed": 0,     # (Code×DiscDate) バッチ数
        "withdrawal_events": 0,                # 撤退イベント成立数（G不能で捨てた分含む）
        "prevrpt_consistent_exit": 0,          # 消失レコード(ShrtPos<0.5%)でPrevRptRatio≥0.5%（整合）
        "prevrpt_inconsistent_exit": 0,        # 消失レコードだがPrevRptRatio<0.5%（整合しない）
        "prevrpt_missing_on_exit": 0,          # 消失レコードでPrevRptRatio欠損
        "skipped_disc_date_not_business_day": 0,
        "skipped_calendar_end": 0,
    }
    active_before_hist: dict[int, int] = defaultdict(int)   # 撤退前アクティブReporter数の分布
    withdrawn_hist: dict[int, int] = defaultdict(int)       # 1イベントで撤退したReporter数の分布

    rows: list[dict] = []
    for code, grp in df.groupby("Code", sort=False):
        state: dict[tuple, float] = {}
        for disc_date, batch in grp.groupby("DiscDate", sort=True):
            diag["disclosure_batches_processed"] += 1
            active_before = _active_reporters(state)
            for _, rec in batch.sort_values("CalcDate").iterrows():
                ssname = rec["SSName"]
                dic = rec["DICName"]
                fund = rec["FundName"]
                new_ratio = float(rec["ShrtPosToSO"])
                if new_ratio < ACTIVE_THRESHOLD:
                    prev = rec["PrevRptRatio"]
                    if prev is None or (isinstance(prev, float) and prev != prev):
                        diag["prevrpt_missing_on_exit"] += 1
                    elif float(prev) >= ACTIVE_THRESHOLD:
                        diag["prevrpt_consistent_exit"] += 1
                    else:
                        diag["prevrpt_inconsistent_exit"] += 1
                state[(ssname, dic, fund)] = new_ratio
            active_after = _active_reporters(state)
            withdrawn = active_before - active_after
            if withdrawn and len(active_before) >= T1_MIN_ACTIVE_REPORTERS:
                diag["withdrawal_events"] += 1
                active_before_hist[len(active_before)] += 1
                withdrawn_hist[len(withdrawn)] += 1
                if disc_date not in bday_index:
                    diag["skipped_disc_date_not_business_day"] += 1
                    continue
                idx = bday_index[disc_date]
                if idx + 1 >= len(all_bdays):
                    diag["skipped_calendar_end"] += 1
                    continue
                rows.append(
                    {
                        "signal_date": all_bdays[idx + 1],
                        "code": code,
                        "disc_date": disc_date,
                        "n_active_before": len(active_before),
                        "n_withdrawn": len(withdrawn),
                        "n_active_after": len(active_after),
                    }
                )

    signals_df = pd.DataFrame(rows)
    diag["signals_before_range_filter"] = int(len(signals_df))
    if not signals_df.empty:
        signals_df = signals_df[
            (signals_df["signal_date"] >= start_bd) & (signals_df["signal_date"] <= end_bd)
        ].reset_index(drop=True)
    diag["signals_in_range"] = int(len(signals_df))
    diag["active_before_distribution"] = dict(sorted(active_before_hist.items()))
    diag["withdrawn_distribution"] = dict(sorted(withdrawn_hist.items()))
    return signals_df, diag


# --- T2: nr7_breakout シグナル生成（新規ロジック） --------------------------------


def _ohlcv_ok(rec: Optional[dict]) -> bool:
    """OHLCV品質ガード（§7-R T3と同一）: AdjO/AdjH/AdjL/AdjC/Va が全て有限・正・AdjH>AdjL。"""
    if rec is None:
        return False
    adjo, adjh, adjl, adjc, va = (
        rec.get("AdjO"), rec.get("AdjH"), rec.get("AdjL"), rec.get("AdjC"), rec.get("Va")
    )
    vals = [adjo, adjh, adjl, adjc, va]
    if any(v is None for v in vals):
        return False
    if any((not isinstance(v, (int, float)) or v != v or v <= 0) for v in vals):
        return False
    return adjh > adjl


def generate_nr7_breakout_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd] 内から NR7上放れシグナルを生成する（カタログ§7-Z T2）。

    営業日Eの日中レンジ比 (AdjH(E)−AdjL(E))/AdjC(E-1) が E-6〜Eの7観測で最小（同値許容・7観測が
    OHLCV品質ガード通過で連続して揃わなければ非該当）のとき、D=E直後1営業日限定で
    AdjC(D)>AdjH(E) ∩ Va(D)≥20日平均(D-20〜D-1)×1.5 ならDにシグナル確定。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    warmup_idx = max(earliest_idx, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VA_HISTORY_WINDOW))
    # 各銘柄の直近レンジ比（(bday_index, range_ratio) を最大7件保持・OHLCV品質通過分のみ append）。
    rr_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=T2_NR7_WINDOW))
    # E直後1営業日=Dで消費する保留NR7: code -> (E_idx, AdjH(E))。
    pending_nr7: dict[str, tuple[int, float]] = {}

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "ohlcv_quality_fail": 0,
        "prev_close_missing": 0,
        "nr7_days": 0,                       # NR7成立（レンジ比7日最小）日×銘柄
        "nr7_breakout_check": 0,             # E直後Dで上放れ判定に到達した回数
        "breakout_fail_price": 0,            # AdjC(D)>AdjH(E) 不成立
        "breakout_fail_volume": 0,           # Va(D)≥20日平均×1.5 不成立
        "breakout_fail_ohlcv_or_vahist": 0,  # D側のOHLCV品質不良/Va履歴不足
        "signals_nr7_breakout": 0,
    }
    rows: list[dict] = []

    prev_bars: Optional[dict] = None
    for d in scan_days:
        i = bday_index[d]
        in_window = start_bd <= d <= end_bd
        bars_d = measure_base_rate.load_bars_day(d)
        if in_window:
            diag["business_days_scanned"] += 1

        # (1) E直後1営業日=D での上放れ判定（E_idx == i-1 の保留のみ・不成立時の後日振替なし）。
        for code in list(pending_nr7.keys()):
            e_idx, adjh_e = pending_nr7[code]
            if e_idx != i - 1:
                del pending_nr7[code]  # 直後1営業日を過ぎた保留は破棄（後日振替なし）
                continue
            del pending_nr7[code]
            if not in_window:
                continue
            diag["nr7_breakout_check"] += 1
            rec = bars_d.get(code)
            hist = va_hist.get(code)
            # Codexレビュー53修正: 窓の厳格化 — 20件が厳密に D-20〜D-1（インデックス連続）で
            # 揃っている場合のみ有効。欠損/Va≤0の日が窓内にあると連続性が壊れて非シグナル
            # （従来は「直近20件の正のVa」で過去延長していた=§7-Z凍結からの逸脱）。
            if (
                not _ohlcv_ok(rec)
                or hist is None
                or len(hist) < VA_HISTORY_WINDOW
                or hist[0][0] != i - VA_HISTORY_WINDOW
                or hist[-1][0] != i - 1
            ):
                diag["breakout_fail_ohlcv_or_vahist"] += 1
                continue
            adjc_d = rec.get("AdjC")
            va_d = rec.get("Va")
            if not (adjc_d > adjh_e):
                diag["breakout_fail_price"] += 1
                continue
            avg20 = sum(v for (_k, v) in hist) / len(hist)
            if not (avg20 > 0 and va_d >= avg20 * T2_VOL_MULTIPLIER):
                diag["breakout_fail_volume"] += 1
                continue
            diag["signals_nr7_breakout"] += 1
            rows.append(
                {
                    "signal_date": d,
                    "code": code,
                    "nr7_day": all_bdays[e_idx],
                    "adjh_e": adjh_e,
                    "adjc_d": adjc_d,
                    "va_d": va_d,
                    "avg20_va": avg20,
                }
            )

        # (2) 当日をEとみなす: レンジ比を計算・履歴更新し、7日最小ならDに向けて保留する。
        for code, rec in bars_d.items():
            if in_window:
                diag["code_day_observations"] += 1
            if not _ohlcv_ok(rec):
                if in_window:
                    diag["ohlcv_quality_fail"] += 1
                continue
            prev_rec = (prev_bars or {}).get(code)
            adjc_prev = prev_rec.get("AdjC") if prev_rec else None
            if adjc_prev is None or adjc_prev <= 0:
                if in_window:
                    diag["prev_close_missing"] += 1
                continue
            range_ratio = (rec.get("AdjH") - rec.get("AdjL")) / adjc_prev
            rr_hist[code].append((i, range_ratio))
            # NR7判定: 直近7件が連続営業日(i-6..i)で揃い、当日Eが最小（同値許容）。
            hist = rr_hist[code]
            if len(hist) == T2_NR7_WINDOW and hist[0][0] == i - (T2_NR7_WINDOW - 1) and hist[-1][0] == i:
                if all(range_ratio <= rr for (_k, rr) in hist):
                    if in_window:
                        diag["nr7_days"] += 1
                    pending_nr7[code] = (i, rec.get("AdjH"))

        # (3) 履歴更新（D含まず: 判定より後に当日Vaを追加）。(bday_index, Va) を保持し、
        # 判定側でインデックス連続性を検査する（Codexレビュー53修正）。
        for code, rec in bars_d.items():
            va = rec.get("Va")
            if va and va > 0:
                va_hist[code].append((i, va))
        prev_bars = bars_d

    signals_df = pd.DataFrame(rows)
    if not signals_df.empty:
        cluster = signals_df.groupby("signal_date").size()
        diag["same_day_fire_max"] = int(cluster.max())
        diag["breakout_success_rate"] = (
            diag["signals_nr7_breakout"] / diag["nr7_breakout_check"]
            if diag["nr7_breakout_check"] else None
        )
    else:
        diag["same_day_fire_max"] = 0
        diag["breakout_success_rate"] = (
            0.0 if diag["nr7_breakout_check"] else None
        )
    return signals_df, diag


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
        f.write("\n## 探索的一次結論（第37周・カタログ§7-Z事前登録の共通ルール）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            if ev_ci["point_ev"] is not None
            else "- EV(なし)点推定 = 計算不能\n"
        )
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(f"- §6/§7-C verdict = {verdict}\n")
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {MULTI_TRIAL_NOTE}\n")
        if report_extra_lines:
            f.write("\n## 近傍事実・必須診断・注記（§7-Z）\n")
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


def _monthly_counts(signals_df: pd.DataFrame) -> dict:
    """signal_date の月別件数（YYYYMM -> 件数・昇順）。"""
    if signals_df.empty:
        return {}
    return (
        signals_df.assign(month=signals_df["signal_date"].str[:6])["month"]
        .value_counts().sort_index().to_dict()
    )


def _event_key_set(signals_df: pd.DataFrame) -> set:
    """(code, signal_date) のイベントキー集合。"""
    if signals_df.empty:
        return set()
    return set(zip(signals_df["code"], signals_df["signal_date"]))


def _jaccard(a: set, b: set) -> Optional[float]:
    u = a | b
    return (len(a & b) / len(u)) if u else None


def run_t1(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_short_crowd_exit_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 (総レコード={gd['total_records']}, "
        f"開示バッチ={gd['disclosure_batches_processed']}, 撤退イベント={gd['withdrawal_events']}, "
        f"アクティブReporter数分布={gd['active_before_distribution']}, "
        f"撤退Reporter数分布={gd['withdrawn_distribution']}, "
        f"PrevRpt整合/不整合/欠損={gd['prevrpt_consistent_exit']}/{gd['prevrpt_inconsistent_exit']}/"
        f"{gd['prevrpt_missing_on_exit']}, 範囲内シグナル={gd['signals_in_range']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T1_KPI_NAME, hc)
    monthly_counts = _monthly_counts(signals_df)

    # shortcover_turn とのJaccard診断（診断限定・合否判定には使わない）。
    print("T1 shortcover_turnシグナル(Jaccard診断用)を生成中...", file=sys.stderr)
    sc_df, _sc_diag = kpi_shortcover_signals.generate_shortcover_signals(
        shared["start_bd"], shared["end_bd"]
    )
    my_keys = _event_key_set(signals_df)
    sc_keys = _event_key_set(sc_df)
    jac = {
        "my_keys": len(my_keys),
        "shortcover_keys": len(sc_keys),
        "intersection": len(my_keys & sc_keys),
        "union": len(my_keys | sc_keys),
        "jaccard": _jaccard(my_keys, sc_keys),
    }

    prevrpt_total = (
        gd["prevrpt_consistent_exit"] + gd["prevrpt_inconsistent_exit"] + gd["prevrpt_missing_on_exit"]
    )
    prevrpt_consistency_rate = (
        gd["prevrpt_consistent_exit"] / prevrpt_total if prevrpt_total else None
    )
    base_params = {
        "signal_definition": (
            "shortsale開示(DiscDate順バッチ)で状態キー=(SSName,DICName,FundName)・Reporter=distinct "
            "SSName。銘柄XのアクティブReporter=ShrtPosToSO≥0.5%のポジションを1つ以上持つSSName。"
            "撤退イベント=バッチ全反映後にバッチ前アクティブSSNameのアクティブポジションが0∩撤退前"
            f"アクティブReporter数≥{T1_MIN_ACTIVE_REPORTERS}。シグナル日=DiscDate翌営業日G。同日複数撤退は1シグナル"
        ),
        "active_threshold": ACTIVE_THRESHOLD,
        "min_active_reporters": T1_MIN_ACTIVE_REPORTERS,
        "reaction": "シグナル確定=DiscDate翌営業日G終値・エントリー=G+1寄付（17時公表・shortcover_turn同一Canonical）",
        "diag_total_records": gd["total_records"],
        "diag_disclosure_batches": gd["disclosure_batches_processed"],
        "diag_withdrawal_events": gd["withdrawal_events"],
        "diag_active_before_distribution": gd["active_before_distribution"],
        "diag_withdrawn_distribution": gd["withdrawn_distribution"],
        "diag_prevrpt_consistent_exit": gd["prevrpt_consistent_exit"],
        "diag_prevrpt_inconsistent_exit": gd["prevrpt_inconsistent_exit"],
        "diag_prevrpt_missing_on_exit": gd["prevrpt_missing_on_exit"],
        "diag_prevrpt_consistency_rate": prevrpt_consistency_rate,
        "diag_signals_before_range_filter": gd["signals_before_range_filter"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "monthly_cluster": monthly_counts,
        "shortcover_turn_jaccard": jac,
        "neighbor_note": SHORTCOVER_FAMILY_NOTE,
    }
    report_extra = [
        f"必須診断(T1): 開示バッチ={gd['disclosure_batches_processed']} / 撤退イベント={gd['withdrawal_events']} "
        f"/ アクティブReporter数分布={gd['active_before_distribution']} / 撤退Reporter数分布={gd['withdrawn_distribution']} "
        f"/ PrevRpt整合率={prevrpt_consistency_rate}(整合={gd['prevrpt_consistent_exit']}/不整合={gd['prevrpt_inconsistent_exit']}"
        f"/欠損={gd['prevrpt_missing_on_exit']}) / 範囲外含む撤退={gd['signals_before_range_filter']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        f"月別クラスタ分布: {monthly_counts}。",
        f"shortcover_turnとのJaccard={jac['jaccard']}(積集合={jac['intersection']}/和集合={jac['union']}/"
        f"自試行キー={jac['my_keys']}/shortcoverキー={jac['shortcover_keys']})。",
        SHORTCOVER_FAMILY_NOTE,
    ]
    res = run_trial(
        filtered_df, T1_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["jaccard"] = jac
    res["monthly_counts"] = monthly_counts
    return res


def run_t2(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_nr7_breakout_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"銘柄日観測={gd['code_day_observations']}, OHLCV品質除外={gd['ohlcv_quality_fail']}, "
        f"NR7該当日数={gd['nr7_days']}, 上放れ判定到達={gd['nr7_breakout_check']}, "
        f"価格不成立={gd['breakout_fail_price']}, 出来高不成立={gd['breakout_fail_volume']}, "
        f"D側品質/Va不足={gd['breakout_fail_ohlcv_or_vahist']}, ブレイク成立率={gd['breakout_success_rate']}, "
        f"同日発火最大={gd['same_day_fire_max']}, シグナル成立={gd['signals_nr7_breakout']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T2_KPI_NAME, hc)
    monthly_counts = _monthly_counts(signals_df)

    base_params = {
        "signal_definition": (
            f"営業日Eの日中レンジ比(AdjH(E)−AdjL(E))/AdjC(E-1)がE-6〜Eの{T2_NR7_WINDOW}観測で最小"
            "(同値許容・7観測揃わなければ非該当・欠損の過去延長禁止・OHLCV品質ガード§7-R T3と同一)∩"
            f"D=E直後1営業日限定でAdjC(D)>AdjH(E)∩Va(D)≥20日平均(D-20〜D-1)×{T2_VOL_MULTIPLIER}"
        ),
        "nr7_window": T2_NR7_WINDOW,
        "vol_multiplier": T2_VOL_MULTIPLIER,
        "va_history_window": VA_HISTORY_WINDOW,
        "reaction": "シグナル確定=D終値・エントリー=D+1寄付(PIT: E判定=E終値・D判定=D終値で確定済み)",
        "diag_ohlcv_quality_fail": gd["ohlcv_quality_fail"],
        "diag_prev_close_missing": gd["prev_close_missing"],
        "diag_nr7_days": gd["nr7_days"],
        "diag_nr7_breakout_check": gd["nr7_breakout_check"],
        "diag_breakout_fail_price": gd["breakout_fail_price"],
        "diag_breakout_fail_volume": gd["breakout_fail_volume"],
        "diag_breakout_fail_ohlcv_or_vahist": gd["breakout_fail_ohlcv_or_vahist"],
        "diag_breakout_success_rate": gd["breakout_success_rate"],
        "diag_same_day_fire_max": gd["same_day_fire_max"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "monthly_cluster": monthly_counts,
        "neighbor_note": NR7_NEIGHBOR_NOTE,
    }
    report_extra = [
        f"必須診断(T2): NR7該当日数={gd['nr7_days']} / 上放れ判定到達={gd['nr7_breakout_check']} "
        f"/ ブレイク成立率={gd['breakout_success_rate']} / 価格不成立={gd['breakout_fail_price']} "
        f"/ 出来高不成立={gd['breakout_fail_volume']} / D側品質・Va不足={gd['breakout_fail_ohlcv_or_vahist']} "
        f"/ OHLCV品質除外={gd['ohlcv_quality_fail']} / 同日発火最大={gd['same_day_fire_max']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        f"月別クラスタ分布: {monthly_counts}。",
        NR7_NEIGHBOR_NOTE,
    ]
    res = run_trial(
        filtered_df, T2_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["monthly_counts"] = monthly_counts
    return res


TRIAL_RUNNERS = {"t1": run_t1, "t2": run_t2}


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
    parser = argparse.ArgumentParser(
        description="第37周: 新規軸二本バッチ（カタログ§7-Z・T1〜T2）"
    )
    parser.add_argument("--trial", choices=["t1", "t2", "all"], default="all")
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
            f"§7-Zはin-sample({kpi_pead_signals.IN_SAMPLE_END}まで)で評価します。holdoutは使用しません。"
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _event_bd_bounds(args.start, args.end, all_bdays)

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    shared = {
        "start_bd": start_bd,
        "end_bd": end_bd,
        "append_to_ledger": not args.no_trials_append,
        "harness_ctx": {
            "all_bdays": all_bdays,
            "bday_index": bday_index,
            "regime_by_day": regime_by_day,
            "base_rate_by_month": base_rate_by_month,
            "universes_by_month": universes_by_month,
        },
    }

    trials_to_run = ["t1", "t2"] if args.trial == "all" else [args.trial]
    results = []
    for t in trials_to_run:
        results.append(TRIAL_RUNNERS[t](shared))

    print("\n=== 第37周バッチ完了サマリー ===")
    for r in results:
        jac = r.get("jaccard")
        jac_str = f" shortcover_Jaccard={jac['jaccard']}" if jac else ""
        print(
            f"{r['kpi_name']}: n={r['n']} 月平均n={r.get('avg_monthly_n')} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={r['ev_point']}[{r['ev_ci_low']},{r['ev_ci_high']}] EV(stop8)={r['ev_stop8']} "
            f"verdict(§6)={r['verdict']} 一次結論={r['conclusion']} "
            f"entry_missing={r['entry_missing']}/raw={r['raw_signal_count']}{jac_str}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
