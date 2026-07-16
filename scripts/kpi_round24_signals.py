#!/usr/bin/env python3
"""第24周: ゴールデンクロス 25/75日 単独試行（カタログ§7-M・T1）。

docs/stock-algo-kpi-catalog.md §7-M の事前登録定義を実装する。in-sample期間
（2016-11〜2022-11・従来と同一。holdout 2023年以降は使わない）で1試行だけ実行し、
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round23_signals.py
（第23周・直近の同型実装）の run_trial 呼び出し契約と prefilter_in_universe をそのまま踏襲し、
SMA計算規約は scripts/kpi_ma200_reclaim_signals.py の SMA200（「D自身を含む直近N回の有効AdjC
観測値の平均」= deque(maxlen=N)）と完全に同一のものを踏襲する。

Canonical Module再利用（新規ロジックは golden_cross の生成関数のみ）:
- シグナル生成: bars の AdjC のみで完結（fins不使用）。SMA25/SMA75 を deque(maxlen=25/75) で
  逐次計算し、前日(D-1)と当日(D)のクロスを判定する。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe をそのまま import 再利用
  （第20周§7-I / 第23周T2で実証済みの統計結果不変な性能最適化）。
- フォワードリターン・重複除去・ユニバース判定: kpi_event_study.compute_signal_returns
  （defer_entry=True＝§6手順6の第5周以降既定方針・第23周T1/breakout系と整合）。
- 集計・§6正式判定・レポート・台帳記録: kpi_event_study.compute_stats/judge/write_report_md/
  append_trial/bootstrap_ev_ci。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。

Usage:
    python3 scripts/kpi_round24_signals.py
    python3 scripts/kpi_round24_signals.py --start 2017-01 --end 2017-12 --no-trials-append
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
import jq_fetch  # noqa: E402  (Canonical Module: now_jst / DATA_ROOT を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe を import 再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-M・事後変更禁止） -----------------------------

T1_KPI_NAME = "golden_cross_2575"
SMA_FAST_WINDOW = 25  # SMA25（D自身を含む直近25回の有効AdjC観測値。SMA200と同一規約）
SMA_SLOW_WINDOW = 75  # SMA75（同上・直近75回）
# SMA75 が満杯になるまで75回の有効観測が必要。前日SMAも要するため安全マージンを載せる
# （ma200_reclaim の WARMUP_BDAYS_MARGIN=330（260日必要）と同流儀で、75日必要に約45日上乗せ）。
WARMUP_BDAYS_MARGIN = 120

ROUND_TAG = "24_golden_cross_single"
SINGLE_TRIAL_NOTE = (
    "本ラウンドはT1単独試行だが累積試行数割引の対象（第24周時点で通算68試行目）。"
    "この結果単独で運用変更しない。"
)
# 近傍性注記（§7-M・テクニカル転換シグナル家族の割引解釈基準）
NEIGHBOR_NOTE = (
    "テクニカル転換シグナル家族の2試行目。近傍事実 ma200_reclaim（200日線奪回・lift0.95で"
    "fail済）を割引解釈の基準として持ち込む。線種・機構は異なる（クロスする2本の移動平均線 vs "
    "価格と1本の線）が、同家族のfail傾向を割引の起点とする。"
)


def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古営業日(YYYYMMDD)。

    kpi_ma200_reclaim_signals._earliest_bars_date と同じ役割（各kpiスクリプトが自己完結する
    既存慣習に合わせる）。
    """
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- T1: golden_cross_2575 シグナル生成（新規ロジック） ---------------------------


def generate_golden_cross_signals(
    start_bd: str,
    end_bd: str,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd](YYYYMMDD営業日)内の全銘柄・全営業日からゴールデンクロス
    （SMA25/75）シグナルを生成する（カタログ§7-M T1）。

    定義（凍結）: 前日 SMA25(D-1) < SMA75(D-1)、当日 SMA25(D) > SMA75(D)。
    SMA は AdjC の単純移動平均（D自身を含む直近N回の有効観測値・SMA200と同一規約=deque(maxlen=N)）。
    D-1/D いずれかで SMA25/75 が計算不能（75有効観測未満等）なら非シグナル。
    シグナル確定=T終値・エントリー=T+1寄付。

    1営業日1パスの逐次スキャンで実装する（各銘柄のAdjC履歴をdequeで保持し、SMA25/75を
    毎日読み直さない）。SMA更新は当日判定より先（D自身を含む規約）、前日SMAの退避は判定より後。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, sma25_d, sma75_d,
        prev_sma25, prev_sma75, adjc。diag はフィルタ段階別の件数内訳。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    idx_earliest_bars = bday_index[_earliest_bars_date()]
    warmup_idx = max(idx_earliest_bars, idx_start - WARMUP_BDAYS_MARGIN)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    fast_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=SMA_FAST_WINDOW))
    slow_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=SMA_SLOW_WINDOW))
    prev_sma_fast: dict[str, float] = {}
    prev_sma_slow: dict[str, float] = {}

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "insufficient_history": 0,  # D または D-1 の SMA25/75 が計算不能
        "both_ready": 0,  # D・D-1 とも SMA25/75 計算可能
        "prev_bearish": 0,  # D-1 で SMA25 < SMA75（デッドクロス側）
        "signals_golden_cross": 0,
    }
    rows: list[dict] = []

    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            adjc_d = rec.get("AdjC")

            # SMA(D) は「D自身を含む」規約のため判定より先に更新する（ma200_reclaim の dev200 と同順序）
            fh = fast_hist[code]
            sh = slow_hist[code]
            if adjc_d is not None:
                fh.append(adjc_d)
                sh.append(adjc_d)
            sma_fast_d = (sum(fh) / len(fh)) if len(fh) == SMA_FAST_WINDOW else None
            sma_slow_d = (sum(sh) / len(sh)) if len(sh) == SMA_SLOW_WINDOW else None

            if in_event_window:
                diag["code_day_observations"] += 1
                p_fast = prev_sma_fast.get(code)
                p_slow = prev_sma_slow.get(code)
                history_ready = (
                    sma_fast_d is not None and sma_slow_d is not None
                    and p_fast is not None and p_slow is not None
                )
                if history_ready:
                    diag["both_ready"] += 1
                    prev_bearish = p_fast < p_slow
                    if prev_bearish:
                        diag["prev_bearish"] += 1
                    curr_bullish = sma_fast_d > sma_slow_d
                    if prev_bearish and curr_bullish:
                        diag["signals_golden_cross"] += 1
                        rows.append(
                            {
                                "signal_date": d,
                                "code": code,
                                "sma25_d": sma_fast_d,
                                "sma75_d": sma_slow_d,
                                "prev_sma25": p_fast,
                                "prev_sma75": p_slow,
                                "adjc": adjc_d,
                            }
                        )
                else:
                    diag["insufficient_history"] += 1

            # 前日SMAの退避は当日判定より後（翌日以降の D-1 としてのみ使用・look-aheadでない）
            if sma_fast_d is not None:
                prev_sma_fast[code] = sma_fast_d
            if sma_slow_d is not None:
                prev_sma_slow[code] = sma_slow_d

    return pd.DataFrame(rows), diag


# --- ハーネス実行 + 探索的一次結論 + 台帳記録 -------------------------------------


def run_trial(
    signals_df: pd.DataFrame,
    base_params: dict,
    harness_ctx: dict,
    report_extra_lines: list[str],
    append_to_ledger: bool,
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録までを行う
    （実装の流儀は kpi_round23_signals.run_trial を踏襲。台帳appendは append_to_ledger で制御し、
    smokeテスト時は False にして台帳を汚さない）。defer_entry は §7-M 既定の True 固定。"""
    kpi_name = T1_KPI_NAME
    defer_entry = True
    all_bdays = harness_ctx["all_bdays"]
    bday_index = harness_ctx["bday_index"]
    regime_by_day = harness_ctx["regime_by_day"]
    base_rate_by_month = harness_ctx["base_rate_by_month"]
    universes_by_month = harness_ctx["universes_by_month"]

    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month,
        defer_entry=defer_entry,
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
        "defer_entry": defer_entry,
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
        "single_trial_discount_note": SINGLE_TRIAL_NOTE,
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
        f.write("\n## 探索的一次結論（第24周・カタログ§7-M事前登録ルール）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            if ev_ci["point_ev"] is not None
            else "- EV(なし)点推定 = 計算不能\n"
        )
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {SINGLE_TRIAL_NOTE}\n")
        f.write("\n## 近傍事実・件数注記（§7-M）\n")
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
            "entry_mode": "defer_max3bd" if defer_entry else "fixed_t1",
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
        description="第24周: ゴールデンクロス25/75日 単独試行（カタログ§7-M・T1）"
    )
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
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} はholdout期間(2023年以降)に抵触します。in-sample評価は"
            f"{kpi_pead_signals.IN_SAMPLE_END}までに限定してください。",
            file=sys.stderr,
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _event_bd_bounds(args.start, args.end, all_bdays)

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    harness_ctx = {
        "all_bdays": all_bdays,
        "bday_index": bday_index,
        "regime_by_day": regime_by_day,
        "base_rate_by_month": base_rate_by_month,
        "universes_by_month": universes_by_month,
    }

    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = generate_golden_cross_signals(start_bd, end_bd)
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 "
        f"(営業日走査={gen_diag['business_days_scanned']}, "
        f"銘柄日観測={gen_diag['code_day_observations']}, "
        f"履歴不足(SMA25/75未充足)={gen_diag['insufficient_history']}, "
        f"D/D-1とも計算可={gen_diag['both_ready']}, "
        f"D-1デッドクロス側={gen_diag['prev_bearish']}, "
        f"シグナル成立={gen_diag['signals_golden_cross']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")

    kpi_dir = OUTPUT_ROOT / T1_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)  # 生シグナル全件（事前フィルタ前）

    # ユニバース事前フィルタ（第20周§7-I / 第23周T2で実証済みの統計結果不変な性能最適化）。
    # 判定に絶対使われないユニバース外シグナルを順リターン計算前に除去する。
    filtered_df, n_raw, n_filtered = kpi_round23_signals.prefilter_in_universe(
        signals_df, universes_by_month
    )
    print(
        f"T1 ユニバース事前フィルタ: 生{n_raw}件 → ハーネス投入{n_filtered}件"
        f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変)",
        file=sys.stderr,
    )
    if filtered_df.empty:
        raise SystemExit("FATAL: T1の事前フィルタ後シグナルが0件です")

    base_params = {
        "signal_definition": (
            f"前日 SMA{SMA_FAST_WINDOW}(D-1) < SMA{SMA_SLOW_WINDOW}(D-1) かつ "
            f"当日 SMA{SMA_FAST_WINDOW}(D) > SMA{SMA_SLOW_WINDOW}(D)。"
            f"SMA=AdjCの単純移動平均（D自身を含む直近N回の有効観測値・SMA200と同一規約）"
        ),
        "sma_fast_window": SMA_FAST_WINDOW,
        "sma_slow_window": SMA_SLOW_WINDOW,
        "warmup_bdays_margin": WARMUP_BDAYS_MARGIN,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "defer_rationale": (
            "§7-M T1は繰り延べを明示せず。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
            "に従いdefer_entry=True。第23周T1/breakout系(high52/volshock)と整合"
        ),
        "neighbor_note": NEIGHBOR_NOTE,
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "prefilter_note": (
            "ユニバース事前フィルタ適用(第20周§7-I / 第23周T2前例・統計結果不変)。"
            "kpi_event_study._universe_membershipと同一判定でユニバース外を順リターン計算前に除去。"
            "in_universe_dfは不変のためn/lift/EVは無フィルタ時と一致する"
        ),
        "sma_regime_diag": {
            "business_days_scanned": gen_diag["business_days_scanned"],
            "both_ready": gen_diag["both_ready"],
            "prev_bearish": gen_diag["prev_bearish"],
            "signals_golden_cross": gen_diag["signals_golden_cross"],
        },
    }
    report_extra = [
        NEIGHBOR_NOTE,
        (
            f"生シグナル数={n_raw}件 / ユニバース事前フィルタ後のハーネス投入数={n_filtered}件"
            f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変の性能最適化・"
            f"第20周§7-I / 第23周T2前例)。"
        ),
        (
            f"シグナル生成診断: 営業日走査={gen_diag['business_days_scanned']} / "
            f"D・D-1ともSMA計算可={gen_diag['both_ready']} / D-1デッドクロス側={gen_diag['prev_bearish']} / "
            f"ゴールデンクロス成立={gen_diag['signals_golden_cross']}。"
        ),
    ]

    result = run_trial(
        filtered_df, base_params, harness_ctx, report_extra,
        append_to_ledger=not args.no_trials_append,
    )

    print("\n=== 第24周 T1 golden_cross_2575 完了サマリー ===")
    print(
        f"{result['kpi_name']}: n={result['n']} 月平均n={result.get('avg_monthly_n')} "
        f"lift={result['lift']}[{result['ci_low']},{result['ci_high']}] "
        f"EV(なし)={result['ev_point']}[{result['ev_ci_low']},{result['ev_ci_high']}] "
        f"EV(stop8)={result['ev_stop8']} "
        f"verdict(§6)={result['verdict']} 一次結論={result['conclusion']} "
        f"entry_missing={result['entry_missing']}/raw={result['raw_signal_count']} "
        f"dup除去={result['duplicate_discarded']} ユニバース外={result['out_of_universe']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
