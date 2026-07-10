#!/usr/bin/env python3
"""第19周: SUE×チャンピオン系複合検証（カタログ§7-H・H1/H2/H3）。

第18周T2 `sue_beat`（実績営業利益が直前の会社予想を+10%以上上回った開示・CurPerType∈{2Q,FY}・
fiscal_year_key=True・Codexレビュー⑥修正後確定値: n=818・lift=1.16・EV(なし)+1.46%
CI[+0.17%,+2.84%]・§6正式verdict=pending）の広いプラスのドリフトに、チャンピオン構成
`volshock_x_above200_quiet`（第13周確定）で確認済みの「文脈条件」を重ねて濃縮できるかを
検証する（カタログ§7-H事前登録）。

母集団は完全に再生成する（既存トライアルのCSVは再利用しない）。SUEイベント自体の生成ロジック
（`kpi_event_batch_signals.generate_sue_beat_signals`）とフォワードリターン計算
（`kpi_event_study.compute_signal_returns`）は第18周と完全に同一の呼び出しであり、
本トライアルの母集団=SUE素(n=818)は第18周T2の再現であることをFATALアサートで保証する。

Canonical Module再利用（新規ロジックは compute_dev200 と run_subset_trial のみ）:
- SUEシグナル生成: `kpi_event_batch_signals.generate_sue_beat_signals`（再実装なし）
- フォワードリターン・重複除去・ユニバース判定: `kpi_event_study.compute_signal_returns`
- above200(dev200)判定: 本ファイルの `compute_dev200`（新規・下記注記参照）
- ul_count_10bd判定: `kpi_volshock_v3_ul_earnings.compute_ul_count_10bd`（第16/17周・
  そのままimport再利用・閾値<=2も `UL_COUNT_A2_MAX` を再利用し新閾値を作らない）
- quiet_ratio判定: `kpi_volshock_v2_amplifiers.compute_quiet_ratio`（第13周・そのままimport再利用）
- 集計・§6正式判定・EVブートストラップCI・台帳記録: `kpi_event_study.compute_stats`/`judge`/
  `bootstrap_ev_ci`/`append_trial`
- 探索的一次結論（promising/rejected/inconclusive）: `kpi_event_batch_signals.classify_exploratory`
  （第18周と完全同一のルール・カタログ§7-H事前登録どおり再利用）

`compute_dev200` についての注記: `kpi_volshock_signals.py` のdev200は「毎日・全銘柄」を
逐次スキャンする際の副産物としてdeque(maxlen=200)で計算されている（バルク処理向けの実装）。
SUEイベントは日付がスパース（n=818件が6年に散在）なため、`compute_ul_count_10bd`/
`compute_quiet_ratio`（第16/17/13周で確立済みの「スパースイベント単位ルックアップ」パターン）
と同型で、イベントごとに過去へ遡って直近200回の有効AdjC観測値を集める方式で実装する。
定義（D自身を含む直近200回の有効AdjC観測値の平均に対する当日終値の乖離率）はdev200と完全に同一。

Usage:
    python3 scripts/kpi_sue_champion_signals.py --dry-run
    python3 scripts/kpi_sue_champion_signals.py
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical: generate_sue_beat_signals/classify_exploratory/T2_KPI_NAME等を再利用)
import kpi_event_study  # noqa: E402  (Canonical: compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/append_trial)
import kpi_pead_signals  # noqa: E402  (Canonical: IN_SAMPLE_START/END を再利用)
import kpi_volshock_v2_amplifiers as v2_amp  # noqa: E402  (Canonical: compute_quiet_ratio/STATS_TABLE_HEADER/_stats_row/PERIOD等を再利用)
import kpi_volshock_v3_ul_earnings as v3_ul  # noqa: E402  (Canonical: compute_ul_count_10bd/UL_COUNT_A2_MAX/UL_WINDOW_BDAYSを再利用)
import measure_base_rate  # noqa: E402  (Canonical: カレンダー・bars/regime読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")
OUTPUT_DIR = OUTPUT_ROOT / "sue_champion_composite"

SUE_BASE_EXPECTED_N = 818  # 第18周(Codexレビュー⑥修正後)確定値。再生成結果がズレたらFATAL停止

DEV200_WINDOW = 200  # kpi_volshock_signals.MA200_WINDOWと同一規約（D自身を含む直近200回の有効AdjC観測値）

H1_KPI_NAME = "sue_x_above200"
H2_KPI_NAME = "sue_x_above200_ul0"  # 名称は事前登録どおり。条件は ul_count_10bd<=2（==0ではない）
H3_KPI_NAME = "sue_x_quiet"
QUIET_RATIO_THRESHOLD = 1.2  # 第13周champion(quiet_bucket)と同一閾値

ROUND_TAG = "19_sue_champion_composite"
MULTI_TRIAL_NOTE = (
    "本ラウンドはH1〜H3の3試行同時登録であり累積試行数割引の対象。"
    "個々の結果単独で運用変更しない。"
)

# 比較用基準線（既存確定値の再掲のみ・本スクリプトでは再計算しない参考文字列）
SUE_BASELINE_TEXT = "SUE素(第18周T2確定値・Codex⑥修正後): n=818 lift=1.16 EV(なし)+1.46%[+0.17%,+2.84%] §6verdict=pending"
CHAMPION_BASELINE_TEXT = (
    "チャンピオン素(第13周確定値): volshock_x_above200_quiet n=73 lift=4.16 "
    "EV(E1シナリオ崩壊exit)+2.84% 月平均約1件"
)


# --- H1: dev200（above200判定・スパースイベント単位ルックアップ・新規実装） -----------


def compute_dev200(
    code: str, signal_bd: str, bday_index: dict[str, int], all_bdays: list[str]
) -> Optional[float]:
    """dev200 = (AdjC(D)-SMA200(D))/SMA200(D)。SMA200はD自身を含む直近200回の有効AdjC
    観測値平均（kpi_volshock_signals.pyのdev200と完全同一規約）。

    200日分の有効履歴が無い銘柄・時期（データ開始2016-07から約200営業日経過するまで、
    または売買停止等で有効AdjC観測値が200件に満たない場合）はNone（insufficient_dev200_history
    として呼び出し側で件数計上）。
    """
    idx = bday_index[signal_bd]
    adjc_d = measure_base_rate.load_bars_day(signal_bd).get(code, {}).get("AdjC")
    if adjc_d is None:
        return None
    valid: list[float] = []
    i = idx
    while i >= 0 and len(valid) < DEV200_WINDOW:
        d = all_bdays[i]
        rec = measure_base_rate.load_bars_day(d).get(code)
        if rec is not None and rec.get("AdjC") is not None:
            valid.append(rec["AdjC"])
        i -= 1
    if len(valid) < DEV200_WINDOW:
        return None
    sma200 = sum(valid) / len(valid)
    if sma200 == 0:
        return None
    return (adjc_d - sma200) / sma200


# --- 共通: サブセット試行の実行 + 探索的一次結論 + 台帳記録 --------------------------


def run_subset_trial(
    subset_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    base_rate_by_month: dict[str, float],
) -> dict:
    """既存in_universe行のサブセット（フォワードリターン再計算なし）に対し、
    集計〜§6正式判定〜EVブートストラップCI〜探索的一次結論〜台帳記録までを行う
    （kpi_volshock_v3_ul_earnings.pyのA1/A2/B1と同型のパターン）。
    """
    if subset_df.empty:
        raise SystemExit(f"FATAL: {kpi_name}のシグナルが0件です")

    stats = kpi_event_study.compute_stats(subset_df, base_rate_by_month, PERIOD)
    verdict, reasons = kpi_event_study.judge(stats)
    ev_ci = kpi_event_study.bootstrap_ev_ci(subset_df, ev_column="ret")
    conclusion = kpi_event_batch_signals.classify_exploratory(
        stats.get("point_lift"), ev_ci["point_ev"], ev_ci["ci_low"]
    )

    params = {
        **base_params,
        "ev_none_point": ev_ci["point_ev"],
        "ev_none_ci_low": ev_ci["ci_low"],
        "ev_none_ci_high": ev_ci["ci_high"],
        "ev_ci_n_boot_valid": ev_ci["n_boot_valid"],
        "exploratory_conclusion": conclusion,
        "pre_registered_success_rule": (
            "promising: EV(なし)CI下限>0 かつ point_lift>=1.5 / "
            "rejected: EV点推定<=0 または point_lift<1.0 / inconclusive: それ以外"
        ),
        "sue_baseline": SUE_BASELINE_TEXT,
        "champion_baseline": CHAMPION_BASELINE_TEXT,
        "round": ROUND_TAG,
        "multi_trial_discount_note": MULTI_TRIAL_NOTE,
    }
    record = {
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
        "entry_mode": "reused_from_sue_beat_regeneration",
        "regime_filter": None,
    }

    print(
        f"{kpi_name}: n={stats['n']} lift={stats.get('point_lift')} "
        f"ci95=[{stats.get('ci_low')},{stats.get('ci_high')}] "
        f"EV(なし)点推定={ev_ci['point_ev']} CI95=[{ev_ci['ci_low']},{ev_ci['ci_high']}] "
        f"verdict(§6)={verdict} 探索的一次結論={conclusion}"
    )

    return {
        "kpi_name": kpi_name, "subset_df": subset_df, "stats": stats, "ev_ci": ev_ci,
        "verdict": verdict, "reasons": reasons, "conclusion": conclusion, "record": record,
    }


# --- レポート出力 --------------------------------------------------------------------


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.2%}" if x is not None else "-"


def _fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "-"


def write_report(
    base_stats: dict,
    base_ev_ci: dict,
    cells: dict[str, dict],
    diag_counts: dict,
) -> None:
    h1, h2, h3 = cells["h1"], cells["h2"], cells["h3"]

    def cell_row(label: str, cell: dict) -> str:
        return v2_amp._stats_row(label, cell["stats"])

    lines = [
        "# 第19周: SUE×チャンピオン系複合検証 レポート（カタログ§7-H）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降は対象外）",
        f"母集団: `sue_beat`（第18周T2再生成・実測n={base_stats['n']}、期待値{SUE_BASE_EXPECTED_N}）",
        "",
        "> **多重比較の明記**: 本ラウンドはH1/H2/H3の3試行同時登録であり累積試行数割引の対象。"
        "この結果単独で運用変更しない。",
        "",
        "## 0. 基準線再掲",
        "",
        f"- {SUE_BASELINE_TEXT}",
        f"- {CHAMPION_BASELINE_TEXT}",
        f"- 本ラウンド内でのSUE素の再計算（内部整合性確認）: n={base_stats['n']} "
        f"lift={_fmt_ratio(base_stats.get('point_lift'))} "
        f"ci95=[{_fmt_ratio(base_stats.get('ci_low'))},{_fmt_ratio(base_stats.get('ci_high'))}] "
        f"EV(なし)点推定={_fmt_pct(base_ev_ci['point_ev'])} "
        f"CI95=[{_fmt_pct(base_ev_ci['ci_low'])},{_fmt_pct(base_ev_ci['ci_high'])}]",
        "",
        "## 1. 特徴量の付与状況（欠測・除外の内訳）",
        "",
        f"- dev200計算不能（insufficient_dev200_history）: {diag_counts['dev200_none']}"
        f"/{diag_counts['sue_base_n']}件",
        f"- ul_count_10bd計算不能（H1部分集合内）: {diag_counts['ul_none_in_h1']}"
        f"/{diag_counts['h1_n']}件",
        f"- quiet_ratio計算不能（insufficient_quiet_history）: {diag_counts['quiet_none']}"
        f"/{diag_counts['sue_base_n']}件",
        "",
        "## 2. サブセット間の重複関係（事前明記どおり）",
        "",
        f"- H1 `{H1_KPI_NAME}`（SUE ∩ dev200>0）: n={h1['stats']['n']}",
        f"- H2 `{H2_KPI_NAME}`（H1 ∩ ul_count_10bd<=2・H1の**部分集合**）: n={h2['stats']['n']}",
        f"- H3 `{H3_KPI_NAME}`（SUE ∩ quiet_ratio>=1.2・H1とは独立軸）: n={h3['stats']['n']}",
        f"- H2⊂H1であることの機械的確認: n(H2)<=n(H1) = {h2['stats']['n'] <= h1['stats']['n']}",
        "",
        "## 3. 結果一覧",
        "",
        v2_amp.STATS_TABLE_HEADER,
        cell_row("SUE素(baseline)", {"stats": base_stats}),
        cell_row(f"H1 {H1_KPI_NAME}", h1),
        cell_row(f"H2 {H2_KPI_NAME}", h2),
        cell_row(f"H3 {H3_KPI_NAME}", h3),
        "",
    ]
    for label, cell in (("H1", h1), ("H2", h2), ("H3", h3)):
        lines += [
            f"### {label} `{cell['kpi_name']}`",
            "",
            f"- §6正式verdict: {kpi_event_study.VERDICT_LABELS_JA.get(cell['verdict'], cell['verdict'])}",
            f"- §6判定理由: " + " / ".join(cell["reasons"]),
            f"- EV(なし)点推定={_fmt_pct(cell['ev_ci']['point_ev'])} "
            f"95%CI=[{_fmt_pct(cell['ev_ci']['ci_low'])}, {_fmt_pct(cell['ev_ci']['ci_high'])}] "
            f"（有効ブートストラップ数={cell['ev_ci']['n_boot_valid']}）",
            f"- **探索的一次結論: {cell['conclusion']}**"
            f"（promising/rejected/inconclusiveの3値・カタログ§7-H事前凍結ルール）",
            "",
        ]
    lines += [
        "## 4. 運用上の注意",
        "",
        f"- {MULTI_TRIAL_NOTE}",
        "- holdout(2023年以降)は開封していない。",
        "- H2⊂H1のネスト構造につき、H2の結果はH1の部分集合効果であり独立検証ではない。",
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- メイン処理 -----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="第19周: SUE×チャンピオン系複合検証（カタログ§7-H）")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="台帳追記(append_trial)とreport.md/signals_features.csv書き込みをスキップし、"
        "n/lift/EV一覧のみstdoutに出す",
    )
    args = parser.parse_args()

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = kpi_event_batch_signals._event_bd_bounds(PERIOD[0], PERIOD[1], all_bdays)

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    print("SUEシグナル再生成中（第18周T2と完全同一の呼び出し）...", file=sys.stderr)
    signals_df, gen_diag = kpi_event_batch_signals.generate_sue_beat_signals(start_bd, end_bd)
    print(
        f"SUEシグナル生成完了: {len(signals_df)}件 "
        f"(2Q/FY開示={gen_diag['statement_2q_or_fy_events']}, "
        f"年度不一致で除外={gen_diag['prior_fy_mismatch_excluded']}, "
        f"予想ペア成立={gen_diag['forecast_pair_established']}, "
        f"ゼロ/赤字予想除外={gen_diag['deficit_forecast_excluded']}, "
        f"シグナル成立={gen_diag['signals_sue_beat']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: SUEシグナルが0件です")

    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, _cs_diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=False
    )
    base_df = returns_df[returns_df["in_universe"]].reset_index(drop=True)
    if len(base_df) != SUE_BASE_EXPECTED_N:
        raise SystemExit(
            f"FATAL: SUE母集団のn={len(base_df)}が第18周T2の既知値{SUE_BASE_EXPECTED_N}と不一致。"
            f"生成ロジックが変わった可能性があり、基準線比較の前提が崩れるため実行を停止する。"
        )
    print(f"SUE母集団再現確認: n={len(base_df)} (期待値{SUE_BASE_EXPECTED_N}) OK", file=sys.stderr)

    print("dev200/ul_count_10bd/quiet_ratio 計算中...", file=sys.stderr)
    base_df["dev200"] = base_df.apply(
        lambda r: compute_dev200(r["code"], r["signal_date"], bday_index, all_bdays), axis=1
    )
    base_df["quiet_ratio"] = base_df.apply(
        lambda r: v2_amp.compute_quiet_ratio(r["code"], r["signal_date"], bday_index, all_bdays), axis=1
    )

    h1_df = base_df[base_df["dev200"] > 0].reset_index(drop=True)
    if h1_df.empty:
        raise SystemExit("FATAL: H1(above200)のシグナルが0件です")
    h1_df["ul_count_10bd"] = h1_df.apply(
        lambda r: v3_ul.compute_ul_count_10bd(r["code"], r["signal_date"], bday_index, all_bdays), axis=1
    )
    h2_df = h1_df[h1_df["ul_count_10bd"] <= v3_ul.UL_COUNT_A2_MAX].reset_index(drop=True)
    h3_df = base_df[base_df["quiet_ratio"] >= QUIET_RATIO_THRESHOLD].reset_index(drop=True)

    diag_counts = {
        "sue_base_n": len(base_df),
        "dev200_none": int(base_df["dev200"].isna().sum()),
        "quiet_none": int(base_df["quiet_ratio"].isna().sum()),
        "h1_n": len(h1_df),
        "ul_none_in_h1": int(h1_df["ul_count_10bd"].isna().sum()),
    }
    print(
        f"H1(above200): n={len(h1_df)} (dev200計算不能={diag_counts['dev200_none']}) / "
        f"H2(above200 かつ ul<=2): n={len(h2_df)} (ul計算不能={diag_counts['ul_none_in_h1']}) / "
        f"H3(quiet>=1.2): n={len(h3_df)} (quiet計算不能={diag_counts['quiet_none']})",
        file=sys.stderr,
    )

    base_stats = kpi_event_study.compute_stats(base_df, base_rate_by_month, PERIOD)
    base_ev_ci = kpi_event_study.bootstrap_ev_ci(base_df, ev_column="ret")
    print(
        f"SUE素 内部整合性確認: n={base_stats['n']} lift={base_stats.get('point_lift')} "
        f"EV(なし)点推定={base_ev_ci['point_ev']} CI95=[{base_ev_ci['ci_low']},{base_ev_ci['ci_high']}]",
        file=sys.stderr,
    )

    common_params = {
        "source_population": "sue_beat",
        "source_population_n": SUE_BASE_EXPECTED_N,
    }
    h1 = run_subset_trial(
        h1_df, H1_KPI_NAME,
        {**common_params, "filter": "dev200>0（above200・kpi_volshock系のdev200計算Canonicalを再利用）"},
        base_rate_by_month,
    )
    h2 = run_subset_trial(
        h2_df, H2_KPI_NAME,
        {
            **common_params,
            "filter": f"dev200>0 かつ ul_count_10bd<={v3_ul.UL_COUNT_A2_MAX}（第16/17周と同一閾値・変更なし）",
            "ul_window_bdays": v3_ul.UL_WINDOW_BDAYS,
            "nested_subset_of": H1_KPI_NAME,
        },
        base_rate_by_month,
    )
    h3 = run_subset_trial(
        h3_df, H3_KPI_NAME,
        {**common_params, "filter": f"quiet_ratio>={QUIET_RATIO_THRESHOLD}（第13周と同一閾値・above200非依存）"},
        base_rate_by_month,
    )
    cells = {"h1": h1, "h2": h2, "h3": h3}

    if args.dry_run:
        print("--dry-run: 台帳追記・report.md/signals_features.csv書き込みをスキップしました "
              "(trials.jsonl行数は変化しません)")
        return 0

    for cell in cells.values():
        kpi_event_study.append_trial(cell["record"], TRIALS_PATH)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base_df[["signal_date", "code", "ret", "month", "regime", "dev200", "quiet_ratio"]].to_csv(
        OUTPUT_DIR / "signals_features_sue_base.csv", index=False
    )
    h1_df[["signal_date", "code", "ret", "dev200", "ul_count_10bd"]].to_csv(
        OUTPUT_DIR / "signals_features_h1.csv", index=False
    )

    write_report(base_stats, base_ev_ci, cells, diag_counts)
    print(f"report: {OUTPUT_DIR / 'report.md'}")
    print(f"features: {OUTPUT_DIR / 'signals_features_sue_base.csv'}, {OUTPUT_DIR / 'signals_features_h1.csv'}")

    print("\n=== 第19周(§7-H)サマリー ===")
    for cell in cells.values():
        print(
            f"{cell['kpi_name']}: n={cell['stats']['n']} lift={cell['stats'].get('point_lift')} "
            f"verdict(§6)={cell['verdict']} 探索的一次結論={cell['conclusion']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
