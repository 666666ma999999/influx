#!/usr/bin/env python3
"""第16周: 白紙解剖発見①②（ストップ高履歴・決算接近）の事前登録検証（カタログ§7-E）。

チャンピオン構成 `volshock_x_above200_quiet`（第13周確定・n=73・in-sample
2016-11〜2022-11）の既存ポジションに2つの特徴を付与し、3本の事前登録フィルタを検証する。
エントリー再計算・フォワードリターン再計算は一切行わない（既存 returns.csv/exit_study の
再利用のみ）。

- A1: `ul_count_10bd`（シグナル日Dを含む直近10営業日のストップ高回数）==0
- A2: 同 <= 2（A1の弱フィルタ版・単調性チェック付き）
- B1: `earnings_remaining_bd`（fins開示履歴の間隔中央値による次回決算推定・PIT安全な
  proxy。J-QuantsのFly決算発表予定日エンドポイントは本プロジェクトで未取得のため、
  真の予定日ではなく過去開示間隔からの推定値である点に注意） <= 20営業日

3試行とも `kpi_event_study.judge()` による§6正式verdict（champion自体がn=73<MIN_N=100の
ため構造的にpending見込み）と、championベースラインとの比較による探索的一次結論
（additive/degrading/neutral・n小のため累積試行数割引の対象）の両方を記録する。

Canonical Module再利用（新規ロジックは compute_ul_count_10bd /
build_earnings_disclosure_history / estimate_earnings_remaining_bd /
earnings_proximity_label の4関数のみ）:
- シグナル母集団: `kpi_volshock_v2_amplifiers.load_positions` / `signals_features_*.csv`
- 決算開示の15:00反応日ルール: `kpi_pead_signals.reaction_day` / `_parse_disc_time_minutes`
- E1シナリオ崩壊exitの突合: `kpi_volshock_v2_followup.load_e1_lookup` / `attach_e1_ev`
- 集計・判定・台帳記録: `kpi_event_study.compute_stats` / `judge` / `append_trial`
- bars/カレンダー読込: `measure_base_rate`

Usage:
    python3 scripts/kpi_volshock_v3_ul_earnings.py --dry-run  # 台帳追記なし・stdout確認のみ
    python3 scripts/kpi_volshock_v3_ul_earnings.py            # 本実行
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_stats/judge/append_trial等を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: load_fins_day・reaction_day(15:00ルール)・IN_SAMPLE_ENDを再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: FINS_HISTORY_START_BDを再利用・二重定義しない)
import kpi_volshock_v2_amplifiers as v2_amp  # noqa: E402  (Canonical Module: load_positions/_stats_row/PERIOD等を再利用)
import kpi_volshock_v2_followup as v2_followup  # noqa: E402  (Canonical Module: load_e1_lookup/attach_e1_evを再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読込・summarizeを再利用)

PERIOD = v2_amp.PERIOD  # (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)

CHAMPION_KPI_NAME = "volshock_x_above200_quiet"  # daily_screen.py の CHAMPION_KPI_NAME と同一文字列
CHAMPION_EXPECTED_N = 73  # 既知の実測値。ズレたらFATALで停止
SOURCE_RETURNS = Path("output/kpi/volshock_x_above200/returns.csv")
SOURCE_FEATURES = Path("output/kpi/volshock_v2_amplifiers/signals_features_volshock_x_above200.csv")
EXIT_STUDY_POSITIONS = Path("output/kpi/exit_study/positions_volshock_x_above200.csv")

UL_WINDOW_BDAYS = 10  # D-9..D（Dを含む10営業日）
UL_COUNT_A2_MAX = 2  # A2: <3回 <=> <=2回

# 決算短信本体（四半期・本決算）のみ。EarnForecastRevision等の臨時開示は含めない
# （kpi_pead_signals.STATEMENT_DOCTYPE_REと同じ母集団だが、こちらは開示種別の先頭一致で
# より厳格に絞る。実データで四半期/本決算のDocTypeが必ず "1Q/2Q/3Q/FY" + "FinancialStatements_"
# で始まることを確認済み）。
EARNINGS_DOC_TYPE_RE = re.compile(r"^(1Q|2Q|3Q|FY)FinancialStatements_")
EARNINGS_PROXIMITY_MAX_BD = 20  # カタログ§2-E「決算発表接近プレミアム」の既存定義をそのまま流用
MIN_DISCLOSURES_FOR_ESTIMATE = 2  # 過去開示<2件は推定不能（interval中央値が計算できないため）
FINS_HIST_START_BD = kpi_uprev_signals.FINS_HISTORY_START_BD  # "20160706"（Canonical再利用）

# championベースライン実測値（第13周フォローアップレポート確定値）。以後変更しない。
CHAMPION_BASELINE_LIFT = 4.16
CHAMPION_BASELINE_EV_E1 = 0.0284
EV_E1_TOLERANCE = 0.01  # additive判定でのEV(E1)許容劣化幅
ADDITIVE_MIN_N = 30
DEGRADING_MAX_N_EXCLUSIVE = 20  # n < 20 は解釈不能なほど小さいとみなしdegrading側に倒す

OUTPUT_DIR = Path("output/kpi/volshock_v3_ul_earnings")
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
BASE_RATE_DIR = v2_amp.BASE_RATE_DIR
UNIVERSE_WINDOW = v2_amp.UNIVERSE_WINDOW
ROUND_TAG = "16_ul_earnings_orthogonal"


# --- ①ul_count_10bd計算 -----------------------------------------------------------


def compute_ul_count_10bd(
    code: str, signal_bd: str, bday_index: dict[str, int], all_bdays: list[str]
) -> Optional[int]:
    """シグナル日Dを含む直近UL_WINDOW_BDAYS営業日[D-9,D]のストップ高(UL=='1')回数を数える。

    ウォームアップ不足（データ開始日付近）は None（フィルタ判定から自然除外）。
    """
    idx = bday_index[signal_bd]
    if idx - (UL_WINDOW_BDAYS - 1) < 0:
        return None
    window_days = all_bdays[idx - (UL_WINDOW_BDAYS - 1) : idx + 1]
    count = 0
    for d in window_days:
        rec = measure_base_rate.load_bars_day(d).get(code)
        if rec is not None and rec.get("UL") == "1":
            count += 1
    return count


# --- ②決算開示PIT履歴構築・②'次回決算推定 -----------------------------------------


def build_earnings_disclosure_history(
    hist_start_bd: str, hist_end_bd: str, all_bdays: list[str], bday_index: dict[str, int]
) -> tuple[dict[str, list[str]], dict]:
    """[hist_start_bd, hist_end_bd]の全fins開示から決算短信(EARNINGS_DOC_TYPE_RE)のみを
    抽出し、Code別の「有効日」履歴を構築する。

    有効日は `kpi_pead_signals.reaction_day`（15:00反応日ルール・Canonical再利用）で
    決める: DiscTime>=15:00または欠損の開示は「有効日=翌営業日」として扱う（同日引け後
    開示のPIT違反防止）。同一営業日に複数レコード（連結/非連結等）が来ても同じ有効日に
    集約されるため、setで重複排除して1回の開示イベントとして扱う。

    hist_end_bdは呼び出し側でIN_SAMPLE_END月末営業日に打ち切ること（holdout期間の
    finsデータを間接的に学習に使わないため）。
    """
    history: dict[str, set] = defaultdict(set)
    diag = {
        "scanned_days": 0,
        "total_records": 0,
        "earnings_doctype_records": 0,
        "codes_with_history": 0,
        "effective_date_out_of_range": 0,
    }
    scan_days = [d for d in all_bdays if hist_start_bd <= d <= hist_end_bd]
    diag["scanned_days"] = len(scan_days)
    for d in scan_days:
        records = kpi_pead_signals.load_fins_day(d)
        for rec in records:
            diag["total_records"] += 1
            code = rec.get("Code")
            if not code:
                continue
            doc_type = rec.get("DocType", "") or ""
            if not EARNINGS_DOC_TYPE_RE.match(doc_type):
                continue
            diag["earnings_doctype_records"] += 1
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
            eff_date, _rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
            if eff_date is None:
                diag["effective_date_out_of_range"] += 1
                continue
            history[code].add(eff_date)
    diag["codes_with_history"] = len(history)
    return {code: sorted(dates) for code, dates in history.items()}, diag


def estimate_earnings_remaining_bd(
    code: str, signal_bd: str, bday_index: dict[str, int], history: dict[str, list[str]]
) -> tuple[Optional[float], str]:
    """過去開示間隔の中央値から次回決算までの残り営業日数を推定する（真の予定日は使わない）。

    signal_bd以前（有効日<=signal_bd）の開示のみを使うためPIT安全。過去開示<2件は
    中央値が定義できず (None, "insufficient_history") を返す。負値（推定日を既に超過）も
    そのまま返す（切り捨てない。超過している状態自体がD時点で計算可能な情報のため）。
    """
    past_dates = [d for d in history.get(code, []) if d <= signal_bd]
    if len(past_dates) < MIN_DISCLOSURES_FOR_ESTIMATE:
        return None, "insufficient_history"
    idxs = sorted(bday_index[d] for d in past_dates)
    intervals = [b - a for a, b in zip(idxs, idxs[1:])]
    median_interval = statistics.median(intervals)
    elapsed = bday_index[signal_bd] - idxs[-1]
    remaining = median_interval - elapsed
    return remaining, "ok"


def earnings_proximity_label(remaining: Optional[float], reason: str) -> str:
    """near(<=20bd)/far/unknown(推定不能)の3値ラベルを返す（Falseに混ぜない・件数を別報告）。"""
    if reason == "insufficient_history":
        return "unknown"
    if remaining is not None and remaining <= EARNINGS_PROXIMITY_MAX_BD:
        return "near"
    return "far"


# --- 探索的一次結論（additive/degrading/neutral・§6完全合格とは別軸） -----------------


def classify_vs_baseline(
    n: int,
    point_lift: Optional[float],
    ev_e1: Optional[float],
    baseline_lift: float = CHAMPION_BASELINE_LIFT,
    baseline_ev_e1: float = CHAMPION_BASELINE_EV_E1,
) -> str:
    """比較基準線（既定=championのlift=4.16・EV(E1)=+2.84%）との比較で探索的一次結論を出す。

    第17周（T2・カタログ§7-F）で「championではなく母集団自身の実測値」を基準線に差し替えて
    再利用できるよう `baseline_lift`/`baseline_ev_e1` を引数化した（既定値はchampionの値の
    ままのため、本ファイル内の既存呼び出し=第16周A1/A2/B1の判定結果は一切変わらない）。

    degrading判定を先に評価する（additiveのAND条件のいずれかが崩れれば自動的にdegrading側の
    OR条件も満たすため、両者が同時にTrueになることはない。point_lift/ev_e1がNoneの場合は
    「additive条件を満たせない」という意味でdegrading側に倒す）。
    """
    degrading = (
        point_lift is None
        or point_lift < baseline_lift
        or ev_e1 is None
        or ev_e1 < baseline_ev_e1 - EV_E1_TOLERANCE
        or n < DEGRADING_MAX_N_EXCLUSIVE
    )
    if degrading:
        return "degrading"
    additive = (
        point_lift >= baseline_lift
        and ev_e1 >= baseline_ev_e1 - EV_E1_TOLERANCE
        and n >= ADDITIVE_MIN_N
    )
    return "additive" if additive else "neutral"


CONCLUSION_LABELS_JA = {
    "additive": "additive（頑健性強化・支持）",
    "degrading": "degrading（棄却）",
    "neutral": "neutral（判定保留）",
}


# --- メイン処理 -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="第16周: ul_count_10bd × earnings_remaining_bd 事前登録検証")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="台帳追記(append_trial)とreport.md/signals_features_champion.csv書き込みをスキップし、"
        "n/lift/EV一覧のみstdoutに出す",
    )
    args = parser.parse_args()

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    returns_df = v2_amp.load_positions(SOURCE_RETURNS)

    features_df = pd.read_csv(
        SOURCE_FEATURES,
        usecols=["signal_date", "code", "quiet_bucket"],
        dtype={"signal_date": str, "code": str},
    )
    merged_df = returns_df.merge(
        features_df, on=["signal_date", "code"], how="left", validate="one_to_one"
    )
    required_cols = {"month", "regime", "ret"}
    missing_cols = required_cols - set(merged_df.columns)
    if missing_cols:
        raise SystemExit(f"FATAL: マージ後に必須列が欠落しています: {missing_cols}")

    champion_df = merged_df[merged_df["quiet_bucket"] == "quiet"].reset_index(drop=True)
    if len(champion_df) != CHAMPION_EXPECTED_N:
        raise SystemExit(
            f"FATAL: champion母集団のn={len(champion_df)}が既知値{CHAMPION_EXPECTED_N}と不一致。"
            f"データが再生成された可能性。ベースライン比較の前提が崩れるため実行を停止する"
        )
    print(f"champion({CHAMPION_KPI_NAME})再現確認: n={len(champion_df)} (期待値{CHAMPION_EXPECTED_N}) OK", file=sys.stderr)

    print("ul_count_10bd計算中...", file=sys.stderr)
    champion_df["ul_count_10bd"] = champion_df.apply(
        lambda r: compute_ul_count_10bd(r["code"], r["signal_date"], bday_index, all_bdays), axis=1
    )

    hist_end_bd = measure_base_rate.month_ends_in_range(
        calendar_days, kpi_pead_signals.IN_SAMPLE_END, kpi_pead_signals.IN_SAMPLE_END
    )[0]
    print("決算開示PIT履歴構築中...", file=sys.stderr)
    earnings_history, earn_diag = build_earnings_disclosure_history(
        FINS_HIST_START_BD, hist_end_bd, all_bdays, bday_index
    )
    print(
        f"決算開示履歴構築完了: 銘柄数={earn_diag['codes_with_history']} "
        f"(走査日数={earn_diag['scanned_days']}, 総レコード数={earn_diag['total_records']}, "
        f"決算短信レコード数={earn_diag['earnings_doctype_records']}, "
        f"有効日算出不能={earn_diag['effective_date_out_of_range']})",
        file=sys.stderr,
    )

    remaining_reason = champion_df.apply(
        lambda r: estimate_earnings_remaining_bd(r["code"], r["signal_date"], bday_index, earnings_history),
        axis=1,
    )
    champion_df["earnings_remaining_bd"] = remaining_reason.apply(lambda t: t[0])
    champion_df["earnings_diag_reason"] = remaining_reason.apply(lambda t: t[1])
    champion_df["earnings_proximity"] = champion_df.apply(
        lambda r: earnings_proximity_label(r["earnings_remaining_bd"], r["earnings_diag_reason"]), axis=1
    )

    e1_lookup = v2_followup.load_e1_lookup(EXIT_STUDY_POSITIONS)
    champion_df["ret_costadj_E1"] = champion_df.apply(
        lambda r: e1_lookup.get((r["signal_date"], r["code"])), axis=1
    )

    subset_a1 = champion_df[champion_df["ul_count_10bd"] == 0].reset_index(drop=True)
    subset_a2 = champion_df[champion_df["ul_count_10bd"] <= UL_COUNT_A2_MAX].reset_index(drop=True)
    subset_b1 = champion_df[champion_df["earnings_proximity"] == "near"].reset_index(drop=True)
    subset_b1_complement = champion_df[champion_df["earnings_proximity"] == "far"].reset_index(drop=True)
    n_unknown_earnings = int((champion_df["earnings_proximity"] == "unknown").sum())

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    def build_cell(label: str, df: pd.DataFrame, conclusion: Optional[str] = None) -> dict:
        stats = kpi_event_study.compute_stats(df, base_rate_by_month, PERIOD)
        ev_e1, e1_missing = v2_followup.attach_e1_ev(df, e1_lookup)
        p_mfe20_reference_only = measure_base_rate.summarize(df).get("p_mfe20")
        cell = {
            "label": label, "df": df, "stats": stats, "ev_e1": ev_e1, "e1_missing": e1_missing,
            "p_mfe20_reference_only": p_mfe20_reference_only,
        }
        if conclusion is None:
            conclusion = classify_vs_baseline(stats["n"], stats.get("point_lift"), ev_e1)
        cell["conclusion"] = conclusion
        return cell

    cells = {
        "baseline": build_cell("champion(baseline)", champion_df, conclusion="n/a(基準線自体)"),
        "a1": build_cell("A1(ul_count_10bd==0)", subset_a1),
        "a2": build_cell("A2(ul_count_10bd<=2)", subset_a2),
        "b1": build_cell("B1(earnings_proximity==near)", subset_b1),
        "b1_complement": build_cell("B1対照(earnings_proximity==far)", subset_b1_complement, conclusion="n/a(対照参考行)"),
    }

    monotonicity_ok = (
        cells["a2"]["stats"]["n"] >= cells["a1"]["stats"]["n"]
        and (
            cells["a2"]["stats"].get("point_lift") is None
            or cells["a1"]["stats"].get("point_lift") is None
            or cells["a2"]["stats"]["point_lift"] <= cells["a1"]["stats"]["point_lift"]
        )
    )
    # 単調性NGのadditiveは機械可読ラベル自体を降格する（Codexレビュー②反映）。
    # 下流の台帳集計が params.exploratory_conclusion 単体を読んでも
    # monotonicity_vs_a1_ok の見落としで「支持」と誤読しないようにするため。
    if cells["a2"]["conclusion"] == "additive" and not monotonicity_ok:
        cells["a2"]["conclusion"] = "additive_fragile"

    trial_defs = [
        (
            "volshock_x_above200_quiet_ul0",
            "a1",
            {"filter": "ul_count_10bd==0", "ul_window_bdays": UL_WINDOW_BDAYS},
        ),
        (
            "volshock_x_above200_quiet_ul_lt3",
            "a2",
            {
                "filter": f"ul_count_10bd<={UL_COUNT_A2_MAX}",
                "ul_window_bdays": UL_WINDOW_BDAYS,
                "monotonicity_vs_a1_ok": monotonicity_ok,
                "monotonicity_check": "n(A2)>=n(A1) AND point_lift(A2)<=point_lift(A1)（弱フィルタほど濃縮が薄まる期待）",
            },
        ),
        (
            "volshock_x_above200_quiet_earnprox20",
            "b1",
            {
                "filter": f"earnings_remaining_bd<={EARNINGS_PROXIMITY_MAX_BD}bd(推定値・真の予定日ではない)",
                "insufficient_history_count": n_unknown_earnings,
                "insufficient_history_rate": n_unknown_earnings / max(1, len(champion_df)),
            },
        ),
    ]

    trial_verdicts: dict[str, tuple[str, list[str]]] = {}
    trial_records: list[dict] = []
    for kpi_name, cell_key, extra_params in trial_defs:
        cell = cells[cell_key]
        stats = cell["stats"]
        verdict, reasons = kpi_event_study.judge(stats)
        trial_verdicts[kpi_name] = (verdict, reasons)
        params = {
            "source_population": CHAMPION_KPI_NAME,
            "source_returns_csv": str(SOURCE_RETURNS),
            "champion_baseline_lift": CHAMPION_BASELINE_LIFT,
            "champion_baseline_ev_e1": CHAMPION_BASELINE_EV_E1,
            "ev_e1_scenario_break": cell["ev_e1"],
            "ev_e1_missing_positions": cell["e1_missing"],
            "exploratory_conclusion": cell["conclusion"],
            "pre_registered_success_rule": (
                f"additive: point_lift>={CHAMPION_BASELINE_LIFT} AND "
                f"EV(E1)>={CHAMPION_BASELINE_EV_E1 - EV_E1_TOLERANCE:.4f} AND n>={ADDITIVE_MIN_N} / "
                f"degrading: point_lift<{CHAMPION_BASELINE_LIFT} OR "
                f"EV(E1)<{CHAMPION_BASELINE_EV_E1 - EV_E1_TOLERANCE:.4f} OR n<{DEGRADING_MAX_N_EXCLUSIVE} / "
                "neutral: 上記どちらにも当てはまらない場合"
            ),
            "round": ROUND_TAG,
            "multi_trial_discount_note": (
                "本ラウンドは3試行同時登録であり累積試行数割引の対象。この結果単独で運用変更しない。"
            ),
            **extra_params,
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
            "entry_mode": "reused_from_existing_returns_csv",
            "regime_filter": None,
        }
        trial_records.append(record)
        print(
            f"{'[DRY-RUN] ' if args.dry_run else ''}{kpi_name}: n={stats['n']} "
            f"lift={_fmt_ratio(stats.get('point_lift'))} "
            f"ci95=[{_fmt_ratio(stats.get('ci_low'))},{_fmt_ratio(stats.get('ci_high'))}] "
            f"EV(E1)={_fmt_pct(cell['ev_e1'])} verdict(§6)={verdict} 探索的一次結論={cell['conclusion']}"
        )

    if args.dry_run:
        print(f"--dry-run: 台帳追記・report.md/signals_features_champion.csv書き込みをスキップしました "
              f"(trials.jsonl行数は変化しません)")
        return 0

    for record in trial_records:
        kpi_event_study.append_trial(record, TRIALS_PATH)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    champion_df[[
        "signal_date", "code", "quiet_bucket", "ul_count_10bd",
        "earnings_remaining_bd", "earnings_diag_reason", "earnings_proximity",
        "ret", "ret_costadj_E1",
    ]].to_csv(OUTPUT_DIR / "signals_features_champion.csv", index=False)

    write_report(cells, monotonicity_ok, n_unknown_earnings, earn_diag, trial_verdicts)
    print(f"report: {OUTPUT_DIR / 'report.md'}")
    print(f"features: {OUTPUT_DIR / 'signals_features_champion.csv'}")
    return 0


# --- レポート出力 ------------------------------------------------------------------


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.2%}" if x is not None else "-"


def _fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "-"


def _cell_row(cell: dict) -> str:
    return v2_amp._stats_row(cell["label"], cell["stats"])


def write_report(
    cells: dict[str, dict],
    monotonicity_ok: bool,
    n_unknown_earnings: int,
    earn_diag: dict,
    trial_verdicts: dict[str, tuple[str, list[str]]],
) -> None:
    baseline = cells["baseline"]
    a1, a2, b1, b1c = cells["a1"], cells["a2"], cells["b1"], cells["b1_complement"]

    lines = [
        "# 第16周: 白紙解剖発見①②の事前登録検証 レポート（カタログ§7-E）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降は対象外）",
        f"母集団: チャンピオン構成 `{CHAMPION_KPI_NAME}`（第13周確定）",
        "",
        "> **多重比較の明記**: 本ラウンドは3試行（A1/A2/B1）同時登録であり累積試行数割引の対象。"
        "この結果単独で運用変更しない。",
        "",
        "## 1. 概要・ベースライン再掲",
        "",
        f"champion n={CHAMPION_EXPECTED_N}再現: OK（実測n={baseline['stats']['n']}）",
        f"比較基準線（第13周フォローアップ確定値）: lift={CHAMPION_BASELINE_LIFT} "
        f"EV(E1シナリオ崩壊exit)={CHAMPION_BASELINE_EV_E1:.2%} n={CHAMPION_EXPECTED_N}",
        "",
        v2_amp.STATS_TABLE_HEADER,
        _cell_row(baseline),
        "",
        f"EV(E1) baseline = {_fmt_pct(baseline['ev_e1'])}（E1突合不能={baseline['e1_missing']}件）",
        "",
        "## 2. A1: champion × ul_count_10bd==0（直近10営業日ストップ高回数=0）",
        "",
        v2_amp.STATS_TABLE_HEADER,
        _cell_row(baseline),
        _cell_row(a1),
        "",
        f"EV(E1) = {_fmt_pct(a1['ev_e1'])}（E1突合不能={a1['e1_missing']}件）",
        f"参考: reach基準P(MFE+20%) baseline={_fmt_pct(baseline['p_mfe20_reference_only'])} "
        f"/ A1={_fmt_pct(a1['p_mfe20_reference_only'])}"
        "（judge/探索的一次結論の計算には一切使用しない参考値）",
        "",
        f"§6正式verdict: **{kpi_event_study.VERDICT_LABELS_JA.get(trial_verdicts['volshock_x_above200_quiet_ul0'][0], trial_verdicts['volshock_x_above200_quiet_ul0'][0])}**"
        "（champion自体がn=73<MIN_N=100のため構造的にpending見込み。§6完全合格の判定軸とは別）",
        f"**探索的一次結論（n小・§6正式合格ではない・累積試行数割引の対象）: "
        f"{CONCLUSION_LABELS_JA.get(a1['conclusion'], a1['conclusion'])}**",
        "",
    ]
    lines += [f"- {r}" for r in trial_verdicts["volshock_x_above200_quiet_ul0"][1]]

    lines += [
        "",
        "## 3. A2: champion × ul_count_10bd<=2（弱フィルタ・A1の頑健性確認）",
        "",
        v2_amp.STATS_TABLE_HEADER,
        _cell_row(baseline),
        _cell_row(a1),
        _cell_row(a2),
        "",
        f"EV(E1) = {_fmt_pct(a2['ev_e1'])}（E1突合不能={a2['e1_missing']}件）",
        f"参考: reach基準P(MFE+20%) A2={_fmt_pct(a2['p_mfe20_reference_only'])}（参考値・判定には不使用）",
        "",
        f"§6正式verdict: **{kpi_event_study.VERDICT_LABELS_JA.get(trial_verdicts['volshock_x_above200_quiet_ul_lt3'][0], trial_verdicts['volshock_x_above200_quiet_ul_lt3'][0])}**",
        f"**探索的一次結論: {CONCLUSION_LABELS_JA.get(a2['conclusion'], a2['conclusion'])}**",
        "",
        f"単調性チェック（事前登録項目・n(A2)>=n(A1) かつ point_lift(A2)<=point_lift(A1)が期待方向）: "
        f"**{'OK（期待通り）' if monotonicity_ok else 'NG（逆転・偶然の産物の可能性。追加のグリッドサーチ・再解釈は行わない）'}**"
        f"（n: A1={a1['stats']['n']} / A2={a2['stats']['n']}、"
        f"point_lift: A1={_fmt_ratio(a1['stats'].get('point_lift'))} / A2={_fmt_ratio(a2['stats'].get('point_lift'))}）",
        "",
    ]
    lines += [f"- {r}" for r in trial_verdicts["volshock_x_above200_quiet_ul_lt3"][1]]

    unknown_rate = n_unknown_earnings / max(1, baseline["stats"]["n"])
    lines += [
        "",
        "## 4. B1: champion × earnings_proximity==near（次回決算まで推定20営業日以内）",
        "",
        "**限定事項**: J-Quantsの「決算発表予定日」エンドポイントは本プロジェクトで一切"
        "fetch/cacheされていない（`jq_fetch.py`は実開示のみ取得）。`earnings_remaining_bd`は"
        "**真の予定日ではなく過去開示間隔の中央値から推定した値**である。",
        "",
        f"推定不能（過去開示<{MIN_DISCLOSURES_FOR_ESTIMATE}件・`unknown`扱いで母集団から除外）: "
        f"{n_unknown_earnings}件 / {baseline['stats']['n']}件（{unknown_rate:.1%}）",
        "",
        v2_amp.STATS_TABLE_HEADER,
        _cell_row(baseline),
        _cell_row(b1),
        _cell_row(b1c),
        "",
        f"EV(E1) B1(near) = {_fmt_pct(b1['ev_e1'])}（E1突合不能={b1['e1_missing']}件） / "
        f"対照(far) = {_fmt_pct(b1c['ev_e1'])}（E1突合不能={b1c['e1_missing']}件）",
        f"参考: reach基準P(MFE+20%) B1(near)={_fmt_pct(b1['p_mfe20_reference_only'])}（参考値・判定には不使用）",
        "",
        f"§6正式verdict: **{kpi_event_study.VERDICT_LABELS_JA.get(trial_verdicts['volshock_x_above200_quiet_earnprox20'][0], trial_verdicts['volshock_x_above200_quiet_earnprox20'][0])}**",
        f"**探索的一次結論: {CONCLUSION_LABELS_JA.get(b1['conclusion'], b1['conclusion'])}**",
        "",
    ]
    lines += [f"- {r}" for r in trial_verdicts["volshock_x_above200_quiet_earnprox20"][1]]

    lines += [
        "",
        "## 5. 月次シグナル頻度への影響（見積り vs 実測）",
        "",
        "| 区分 | 実行前の参考レンジ見積り | 実測n | 月平均(検証期間全体) |",
        "|---|---|---|---|",
        f"| A1(ul_count==0) | 25〜55程度 | {a1['stats']['n']} | {a1['stats']['avg_monthly_n']:.2f} |",
        f"| A2(ul_count<=2) | 45〜65程度 | {a2['stats']['n']} | {a2['stats']['avg_monthly_n']:.2f} |",
        f"| B1(earnings_proximity==near) | 15〜30程度 | {b1['stats']['n']} | {b1['stats']['avg_monthly_n']:.2f} |",
        "",
        "全サブセットはchampion(n=73)からの絞り込みのみのため n<=73 が数学的に保証される"
        "（AND条件追加は単調減少にしかならない）。",
        "",
        "## 6. reach/close非対称の注記",
        "",
        "`kpi_event_study.compute_stats()`はret列（20bd終値基準）のみで判定しており、"
        "`measure_base_rate.summarize()`が計算する`p_mfe20`（高値タッチ=reach基準）は"
        "judge()の入力（statsディクショナリ）には一切混入していない"
        "（本レポートの「参考: reach基準」行として別出力するのみ）。",
        f"baseline P(+20%終値)={_fmt_pct(baseline['stats'].get('p20'))} vs "
        f"P(MFE+20%)={_fmt_pct(baseline['p_mfe20_reference_only'])}"
        "（reach基準は常にclose基準以上になる。乖離が大きい場合は「高値では届くが引けでは届かない」"
        "傾向を示すが、本ラウンドの合否判定には使用しない）。",
        "",
        "## 7. 実装ノート（Canonical再利用の明記）",
        "- エントリー価格・保有期間・ユニバース判定・フォワードリターン(ret/mfe/mae/ret_stop8等)は"
        "一切再計算していない（`output/kpi/volshock_x_above200/returns.csv`の既存列をそのまま使用）",
        "- `ul_count_10bd` = シグナル日Dを含む直近10営業日`[D-9,D]`の`bars[code][\"UL\"]=='1'`の日数"
        "（`measure_base_rate.load_bars_day`・lru_cache済み関数を再利用）",
        "- `earnings_remaining_bd` = 過去の決算短信(1Q/2Q/3Q/FY FinancialStatements)開示の"
        "有効日間隔の中央値 - シグナル日までの経過営業日。有効日は`kpi_pead_signals.reaction_day`"
        "（15:00反応日ルール）で決定し、同日引け後開示は翌営業日扱いとしてPIT違反を防止した",
        f"- 決算開示履歴の走査範囲は{FINS_HIST_START_BD}〜{measure_base_rate.month_ends_in_range(measure_base_rate.load_calendar_days(), kpi_pead_signals.IN_SAMPLE_END, kpi_pead_signals.IN_SAMPLE_END)[0]}"
        "（IN_SAMPLE_END月末営業日で打ち切り。holdout期間のfinsデータを間接的に学習に使わない）",
        f"- 決算開示履歴構築診断: 走査日数={earn_diag['scanned_days']} 総レコード数={earn_diag['total_records']} "
        f"決算短信レコード数={earn_diag['earnings_doctype_records']} 有効日算出不能={earn_diag['effective_date_out_of_range']}",
        "- E1シナリオ崩壊exitのEVは既存`output/kpi/exit_study/positions_volshock_x_above200.csv`の"
        "`ret_costadj_E1`列を`kpi_volshock_v2_followup.load_e1_lookup`/`attach_e1_ev`で突合したのみ"
        "（再計算なし）",
        "- 集計（n/lift/CI/EV）は`kpi_event_study.compute_stats`、§6正式判定は`kpi_event_study.judge`"
        "をそのまま再利用（再実装なし）",
        "- 閾値（UL_COUNT_A2_MAX=2, EARNINGS_PROXIMITY_MAX_BD=20）は実行結果を見てからの調整を"
        "一切行っていない（事前登録どおり1回だけ記録）",
        "",
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
