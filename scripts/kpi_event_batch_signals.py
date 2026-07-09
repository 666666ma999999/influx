#!/usr/bin/env python3
"""第18周: イベント系5本バッチ（カタログ§7-G・T1〜T5）。

docs/stock-algo-kpi-catalog.md §7-G の事前登録定義を実装する。5試行とも in-sample期間
（2016-11〜2022-11・従来と同一。holdout 2023年以降は使わない）で実行し、共通の探索的
一次結論ルール（EV(素・往復コスト込)のbootstrap CI下限>0 かつ point_lift>=1.5 → promising /
EV点推定<=0 または point_lift<1.0 → rejected / それ以外 → inconclusive）を全試行に適用する。
実装の流儀（run_event_study()を使わず低レベルCanonical関数を直接呼ぶことでEV CIをtrials.jsonlの
paramsに含める）は scripts/kpi_ul_fade_signals.py（第17周）を雛形にする。

Canonical Module再利用（新規ロジックはT2/T5の生成関数と_run_trialヘルパーのみ）:
- T1 `dividend_uprev`: kpi_uprev_signals.generate_uprev_signals() を doc_type=
  "DividendForecastRevision"・field="FDivFY" で呼び出す（第18周でdoc_type/reit_doc_type/field
  引数を追加した後方互換拡張・再実装なし）。
- T2 `sue_beat`: kpi_uprev_signals.build_fop_history(field=...)（同じく第18周でfield引数化）を
  "FOP2Q"/"FOP"の2回呼び出しで再利用。ループ構造自体はkpi_uprev_signals.generate_uprev_signals
  と同型だがフィールドペアリングがCurPerTypeで分岐するため新規実装（generate_sue_beat_signals）。
- T3 `high52_break_vol`: kpi_high52_signals.generate_high52_signals() を breakout_basis="close"・
  high_window=251 で呼び出す（第18周でbreakout_basis/high_window引数を追加した後方互換拡張）。
- T4 `range_squeeze_break`: kpi_range_breakout_signals.generate_range_breakout_signals() を
  breakout_mult=1.0 で呼び出す（既存CLI/関数のパラメータのみ・コード変更なし）。
- T5 `uprev_repeater`: kpi_uprev_signals.generate_uprev_signals()の全出力をそのまま履歴母集団に
  使い、常連判定（repeat_count>=2）のみ新規実装（generate_uprev_repeater_signals）。
- フォワードリターン・重複除去・ユニバース判定: kpi_event_study.compute_signal_returns
- 集計・§6正式判定・レポート・台帳記録: kpi_event_study.compute_stats/judge/write_report_md/
  append_trial/bootstrap_ev_ci

Usage:
    python3 scripts/kpi_event_batch_signals.py --trial all
    python3 scripts/kpi_event_batch_signals.py --trial t1
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci を再利用)
import kpi_high52_signals  # noqa: E402  (Canonical Module: generate_high52_signals(第18周拡張)を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・reaction_day・
# _parse_disc_time_minutes・load_fins_day を再利用)
import kpi_range_breakout_signals  # noqa: E402  (Canonical Module: generate_range_breakout_signals を再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: generate_uprev_signals(第18周拡張)・
# build_fop_history(第18周拡張)・_parse_numeric・_days_between・FINS_HISTORY_START_BD を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/regime読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-G・事後変更禁止） -----------------------------

T1_KPI_NAME = "dividend_uprev"
T1_THRESHOLD = 1e-9  # 「増額方向」のみを要求（実質>0判定・数値閾値は設けない）

T2_KPI_NAME = "sue_beat"
T2_THRESHOLD = 0.10  # beat_pct >= +10%
T2_LOOKBACK_DAYS = 365
T2_DOCTYPE_RE = re.compile(r"^(2Q|FY)FinancialStatements")

T3_KPI_NAME = "high52_break_vol"
T3_HIGH_WINDOW = 251
T3_VOL_MULTIPLIER = 2.0

T4_KPI_NAME = "range_squeeze_break"
T4_RANGE_WIDTH_MAX = 0.15
T4_BREAKOUT_MULT = 1.0  # 上乗せなし（既存range_breakoutの1.02から変更・カタログ§7-G T4参照）
T4_VOL_MULTIPLIER = 2.0

T5_KPI_NAME = "uprev_repeater"
T5_REPEAT_WINDOW_BDAYS = 504  # 過去2年
T5_MIN_REPEATS = 2
T5_BASE_THRESHOLD = 0.10  # uprev_fop10と完全同一閾値

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T5の5試行同時登録であり累積試行数割引の対象。"
    "個々の結果単独で運用変更しない。"
)


# --- T2: sue_beat シグナル生成（新規ロジック） -------------------------------------


def generate_sue_beat_signals(
    start_bd: str,
    end_bd: str,
    threshold: float = T2_THRESHOLD,
    lookback_days: int = T2_LOOKBACK_DAYS,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内の決算短信(2Q/FY限定)から実績営業利益が直前の会社予想を
    threshold以上上回った開示を抽出する（カタログ§7-G T2）。

    1Q/3Qを除外する理由: J-Quants finsスキーマにはFOP1Q/FOP3Qに相当するフィールドが
    存在せず、1Q/3Q時点の累計実績に対応する「直前の会社予想」を同一スコープで
    再構成できないため（2026-07-09実データ確認・事前登録時に確定）。

    予想値の履歴構築は kpi_uprev_signals.build_fop_history(field=...) を2Q用("FOP2Q")・
    FY用("FOP")で個別に呼び出して再利用する（新規実装なし）。

    `fiscal_year_key=True`固定でbuild_fop_historyを呼び出し、直前予想の探索を同一Code
    かつ同一対象会計年度(CurFYEn)の直近開示に限定する（Codexレビュー⑥指摘・2026-07-09修正。
    別年度の予想値を「直前予想」として誤比較する事例が生8688件中67件で確認されたため）。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    hist_start_bd = (
        kpi_uprev_signals.FINS_HISTORY_START_BD
        if kpi_uprev_signals.FINS_HISTORY_START_BD <= start_bd
        else start_bd
    )
    history_fop2q, hist_stats_2q = kpi_uprev_signals.build_fop_history(
        hist_start_bd, end_bd, all_bdays, field="FOP2Q", fiscal_year_key=True
    )
    history_fop, hist_stats_fy = kpi_uprev_signals.build_fop_history(
        hist_start_bd, end_bd, all_bdays, field="FOP", fiscal_year_key=True
    )

    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "history_scanned_days": hist_stats_2q["scanned_days"],
        "history_fop2q_numeric_records": hist_stats_2q["fop_numeric_records"],
        "history_fop_numeric_records": hist_stats_fy["fop_numeric_records"],
        "event_days_scanned": len(event_days),
        "total_disclosure_records": 0,
        "statement_2q_or_fy_events": 0,
        "actual_op_missing_or_nonnumeric": 0,
        "no_prior_forecast_found": 0,
        "current_cur_fy_en_missing": 0,
        "prior_fy_mismatch_excluded": 0,
        "prior_forecast_beyond_lookback": 0,
        "forecast_pair_established": 0,
        "deficit_forecast_excluded": 0,
        "signals_sue_beat": 0,
        "signals_2q": 0,
        "signals_fy": 0,
    }

    rows: list[dict] = []

    for d in event_days:
        records = kpi_pead_signals.load_fins_day(d)
        for rec in records:
            diag["total_disclosure_records"] += 1
            code = rec.get("Code")
            doc_type = rec.get("DocType", "") or ""
            if not code:
                continue
            m = T2_DOCTYPE_RE.match(doc_type)
            if not m:
                continue
            diag["statement_2q_or_fy_events"] += 1
            period_prefix = m.group(1)  # "2Q" or "FY"

            actual_val = kpi_uprev_signals._parse_numeric(rec.get("OP"))
            if actual_val is None:
                diag["actual_op_missing_or_nonnumeric"] += 1
                continue

            if period_prefix == "2Q":
                history = history_fop2q
                forecast_field_name = "FOP2Q"
            else:
                history = history_fop
                forecast_field_name = "FOP"

            disc_time_raw = rec.get("DiscTime")
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(disc_time_raw)
            cur_key = (d, disc_time_minutes if disc_time_minutes is not None else 0)

            prior_records = [t for t in history.get(code, []) if (t[0], t[1]) < cur_key]
            if not prior_records:
                diag["no_prior_forecast_found"] += 1
                continue
            cur_fy_en = rec.get("CurFYEn") or ""
            if not cur_fy_en:
                diag["current_cur_fy_en_missing"] += 1
                continue
            same_fy_prior = [t for t in prior_records if len(t) > 3 and t[3] == cur_fy_en]
            if not same_fy_prior:
                diag["prior_fy_mismatch_excluded"] += 1
                continue
            prior_records = same_fy_prior
            prev_date, prev_time, forecast_before = max(prior_records, key=lambda t: (t[0], t[1]))[:3]
            assert (prev_date, prev_time) < cur_key, (
                f"FATAL: look-ahead検出 code={code} event={cur_key} "
                f"prev=({prev_date},{prev_time})"
            )

            if kpi_uprev_signals._days_between(prev_date, d) > lookback_days:
                diag["prior_forecast_beyond_lookback"] += 1
                continue

            diag["forecast_pair_established"] += 1

            if forecast_before <= 0:
                diag["deficit_forecast_excluded"] += 1
                continue  # 赤字予想からの黒字化等・今回は母集団から除外（次周検討）

            beat_pct = actual_val / forecast_before - 1
            if beat_pct < threshold:
                continue

            g_date, rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
            if g_date is None:
                continue  # 検証期間の終端でカレンダー範囲外

            diag["signals_sue_beat"] += 1
            diag["signals_2q" if period_prefix == "2Q" else "signals_fy"] += 1
            rows.append(
                {
                    "signal_date": g_date,
                    "code": code,
                    "disclosed_date": d,
                    "disc_time": disc_time_raw,
                    "reaction_rule": rule,
                    "doc_type": doc_type,
                    "period_type": period_prefix,
                    "forecast_field": forecast_field_name,
                    "op_actual": actual_val,
                    "op_forecast_before": forecast_before,
                    "op_forecast_before_date": prev_date,
                    "beat_pct": beat_pct,
                }
            )

    return pd.DataFrame(rows), diag


# --- T5: uprev_repeater シグナル生成（新規ロジック） -------------------------------


def generate_uprev_repeater_signals(
    hist_start_bd: str,
    in_sample_start_bd: str,
    in_sample_end_bd: str,
    repeat_window_bdays: int = T5_REPEAT_WINDOW_BDAYS,
    min_repeats: int = T5_MIN_REPEATS,
    threshold: float = T5_BASE_THRESHOLD,
) -> tuple[pd.DataFrame, dict]:
    """uprev_fop10と完全同一閾値の上方修正イベント全履歴を kpi_uprev_signals.
    generate_uprev_signals() から取得し、各in-sampleイベントについて過去
    repeat_window_bdays営業日以内の同一code上方修正回数(repeat_count)がmin_repeats以上の
    ものを「常連の新規上方修正」シグナルとして抽出する（カタログ§7-G T5）。
    """
    base_df, base_diag = kpi_uprev_signals.generate_uprev_signals(
        hist_start_bd, in_sample_end_bd, threshold=threshold, fiscal_year_key=True
    )
    if base_df.empty:
        raise SystemExit("FATAL: T5の基礎となるuprevイベントが0件です（生成ロジックを確認してください）")

    base_df = base_df.sort_values(["code", "disclosed_date"]).reset_index(drop=True)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    diag = {
        "base_uprev_events_total": int(len(base_df)),
        "base_prior_fy_mismatch_excluded": base_diag.get("prior_fy_mismatch_excluded", 0),
        "in_sample_candidates": 0,
        "repeat_signals": 0,
    }
    rows: list[dict] = []

    for code, grp in base_df.groupby("code"):
        dates = grp["disclosed_date"].tolist()
        for i, d in enumerate(dates):
            if not (in_sample_start_bd <= d <= in_sample_end_bd):
                continue
            diag["in_sample_candidates"] += 1
            idx_d = bday_index[d]
            window_start_idx = max(0, idx_d - repeat_window_bdays)
            window_start_bd = all_bdays[window_start_idx]
            repeat_count = sum(1 for dd in dates[:i] if window_start_bd <= dd < d)
            if repeat_count >= min_repeats:
                diag["repeat_signals"] += 1
                row = grp.iloc[i].to_dict()
                row["repeat_count"] = repeat_count
                rows.append(row)

    return pd.DataFrame(rows), diag


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録 --------------------------------


def classify_exploratory(point_lift: Optional[float], ev_point: Optional[float], ev_ci_low: Optional[float]) -> str:
    """5試行共通の探索的一次結論ルール（カタログ§7-G事前凍結）。

    rejected: EV点推定<=0 または point_lift<1.0
    promising: 上記に該当せず、かつ CI下限>0 かつ point_lift>=1.5
    inconclusive: それ以外
    """
    if (ev_point is not None and ev_point <= 0) or (point_lift is not None and point_lift < 1.0):
        return "rejected"
    if (ev_ci_low is not None and ev_ci_low > 0) and (point_lift is not None and point_lift >= 1.5):
        return "promising"
    return "inconclusive"


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    defer_entry: bool,
    all_bdays: list[str],
    bday_index: dict[str, int],
    regime_by_day: dict[str, str],
    base_rate_by_month: dict[str, float],
    universes_by_month: dict[str, set],
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録までを行う
    （run_event_study()を使わない理由: EV CI(exploratory_conclusion判定用)をtrials.jsonlの
    paramsに含めるため、compute_stats後・append_trial前に追加計算する必要がある。
    実装の流儀は scripts/kpi_ul_fade_signals.py の run_t1 を踏襲する）。
    """
    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=defer_entry
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
    defer_stats = kpi_event_study._compute_defer_stats(in_universe_df) if defer_entry else None

    ev_ci = kpi_event_study.bootstrap_ev_ci(in_universe_df, ev_column="ret")
    conclusion = classify_exploratory(stats.get("point_lift"), ev_ci["point_ev"], ev_ci["ci_low"])

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
        "round": "18_event_batch",
        "multi_trial_discount_note": MULTI_TRIAL_NOTE,
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
        f.write("\n## 探索的一次結論（第18周・カタログ§7-G事前登録の共通ルール）\n")
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
        "verdict": verdict, "conclusion": conclusion, "returns_path": returns_path,
    }


# --- 各トライアルのドライバ ---------------------------------------------------------


def _event_bd_bounds(start_month: str, end_month: str, all_bdays: list[str]) -> tuple[str, str]:
    start_bound = start_month.replace("-", "") + "01"
    end_bound = end_month.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")
    return start_bd, end_bd


def run_t1(shared: dict) -> dict:
    start_bd, end_bd = shared["start_bd"], shared["end_bd"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = kpi_uprev_signals.generate_uprev_signals(
        start_bd, end_bd,
        threshold=T1_THRESHOLD,
        doc_type="DividendForecastRevision",
        reit_doc_type="REITDividendForecastRevision",
        field="FDivFY",
        fiscal_year_key=True,
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 "
        f"(開示レコード総数={gen_diag['total_disclosure_records']}, "
        f"REIT除外={gen_diag['reit_earn_forecast_revision_excluded']}, "
        f"revision件数={gen_diag['revision_events']}, "
        f"年度不一致で除外={gen_diag['prior_fy_mismatch_excluded']}, "
        f"予想ペア成立={gen_diag['fop_pair_established']}, "
        f"ゼロ/赤字ベース除外={gen_diag['deficit_to_profit_excluded']}, "
        f"シグナル成立={gen_diag['signals_uprev10']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    kpi_dir = OUTPUT_ROOT / T1_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

    base_params = {
        "signal_definition": "DividendForecastRevision(FDivFY新/FDivFY前-1>0・増額方向のみ・同一CurFYEnのみ比較)",
        "field": "FDivFY",
        "doc_type": "DividendForecastRevision",
        "lookback_days": kpi_uprev_signals.LOOKBACK_DAYS_DEFAULT,
        "reaction_cutoff": "15:00",
        "fiscal_year_key": True,
        "codex6_fix_note": (
            "Codexレビュー⑥指摘(2026-07-09)により、直前予想の探索を同一CurFYEn(対象会計年度)"
            "限定に修正して再実行(初回実行値は§7-Gに併記)"
        ),
    }
    return run_trial(signals_df, T1_KPI_NAME, base_params, defer_entry=False, **shared["harness_ctx"])


def run_t2(shared: dict) -> dict:
    start_bd, end_bd = shared["start_bd"], shared["end_bd"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = generate_sue_beat_signals(start_bd, end_bd)
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 "
        f"(2Q/FY開示={gen_diag['statement_2q_or_fy_events']}, "
        f"年度不一致で除外={gen_diag['prior_fy_mismatch_excluded']}, "
        f"予想ペア成立={gen_diag['forecast_pair_established']}, "
        f"ゼロ/赤字予想除外={gen_diag['deficit_forecast_excluded']}, "
        f"シグナル成立={gen_diag['signals_sue_beat']}(2Q={gen_diag['signals_2q']}/FY={gen_diag['signals_fy']}))",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    kpi_dir = OUTPUT_ROOT / T2_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

    base_params = {
        "signal_definition": "OP実績/直前OP予想-1>=+10%（CurPerType in {2Q,FY}限定・同一CurFYEnのみ比較）",
        "excluded_periods": "1Q/3Q（J-QuantsにFOP1Q/FOP3Qフィールドが存在しないため）",
        "beat_threshold": T2_THRESHOLD,
        "lookback_days": T2_LOOKBACK_DAYS,
        "reaction_cutoff": "15:00",
        "fiscal_year_key": True,
        "codex6_fix_note": (
            "Codexレビュー⑥指摘(2026-07-09)により、直前予想の探索を同一CurFYEn(対象会計年度)"
            "限定に修正して再実行(初回実行値は§7-Gに併記)"
        ),
    }
    return run_trial(signals_df, T2_KPI_NAME, base_params, defer_entry=False, **shared["harness_ctx"])


def run_t3(shared: dict) -> dict:
    start_bd, end_bd = shared["start_bd"], shared["end_bd"]
    print(f"T3 {T3_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = kpi_high52_signals.generate_high52_signals(
        start_bd, end_bd, vol_multiplier=T3_VOL_MULTIPLIER,
        breakout_basis="close", high_window=T3_HIGH_WINDOW,
    )
    print(
        f"T3 シグナル生成完了: {len(signals_df)}件 "
        f"(営業日走査={gen_diag['business_days_scanned']}, 履歴不足={gen_diag['insufficient_history']}, "
        f"新高値ブレイク={gen_diag['new_high_252']}, 出来高未達={gen_diag['volume_below_threshold']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T3シグナルが0件です")
    kpi_dir = OUTPUT_ROOT / T3_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

    base_params = {
        "signal_definition": f"AdjC(D)>過去{T3_HIGH_WINDOW}日AdjH最大値 かつ Va(D)>=20日平均Va×{T3_VOL_MULTIPLIER}",
        "high_window": T3_HIGH_WINDOW,
        "breakout_basis": "close",
        "vol_multiplier": T3_VOL_MULTIPLIER,
        "related_existing_kpi": "high52_breakout(AdjH基準・252日窓・第9周)",
    }
    return run_trial(signals_df, T3_KPI_NAME, base_params, defer_entry=True, **shared["harness_ctx"])


def run_t4(shared: dict) -> dict:
    raise SystemExit(
        "T4(range_squeeze_break)はteam-lead裁定(2026-07-09)により取り下げ・不実施。"
        "既存range_breakout(第15周・verdict=fail・lift=0.12)と同一パラメータ帯のため"
        "累積試行数割引の予算を無駄遣いすると判断。カタログ§7-G T4項参照。"
        "実行するにはこのSystemExit行を削除する必要があるが、team-lead裁定を上書きしない限り行わないこと。"
    )
    start_bd, end_bd = shared["start_bd"], shared["end_bd"]  # noqa: F841 — unreachable, withdrawn trial
    print(f"T4 {T4_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = kpi_range_breakout_signals.generate_range_breakout_signals(
        start_bd, end_bd,
        range_width_max=T4_RANGE_WIDTH_MAX, breakout_mult=T4_BREAKOUT_MULT, vol_multiplier=T4_VOL_MULTIPLIER,
    )
    print(
        f"T4 シグナル生成完了: {len(signals_df)}件 "
        f"(レンジ収縮成立={gen_diag['range_narrow']}, 上放れ成立={gen_diag['breakout_over_range']}, "
        f"出来高未達={gen_diag['volume_below_threshold']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T4シグナルが0件です")
    kpi_dir = OUTPUT_ROOT / T4_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

    base_params = {
        "signal_definition": (
            f"60日レンジ幅(高値-安値)/安値<{T4_RANGE_WIDTH_MAX:.0%} かつ AdjC(D)>レンジ上限×"
            f"{T4_BREAKOUT_MULT} かつ Va(D)>=20日平均Va×{T4_VOL_MULTIPLIER}"
        ),
        "range_width_max": T4_RANGE_WIDTH_MAX,
        "breakout_mult": T4_BREAKOUT_MULT,
        "vol_multiplier": T4_VOL_MULTIPLIER,
        "related_existing_kpi": "range_breakout(breakout_mult=1.02・第15周・verdict=fail・lift=0.12)",
    }
    return run_trial(signals_df, T4_KPI_NAME, base_params, defer_entry=True, **shared["harness_ctx"])


def run_t5(shared: dict) -> dict:
    start_bd, end_bd = shared["start_bd"], shared["end_bd"]
    hist_start_bd = (
        kpi_uprev_signals.FINS_HISTORY_START_BD
        if kpi_uprev_signals.FINS_HISTORY_START_BD <= start_bd
        else start_bd
    )
    print(f"T5 {T5_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = generate_uprev_repeater_signals(hist_start_bd, start_bd, end_bd)
    print(
        f"T5 シグナル生成完了: {len(signals_df)}件 "
        f"(基礎uprevイベント総数={gen_diag['base_uprev_events_total']}, "
        f"基礎側年度不一致で除外={gen_diag['base_prior_fy_mismatch_excluded']}, "
        f"in-sample候補={gen_diag['in_sample_candidates']}, "
        f"repeat_count>={T5_MIN_REPEATS}成立={gen_diag['repeat_signals']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T5シグナルが0件です")
    kpi_dir = OUTPUT_ROOT / T5_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

    base_params = {
        "signal_definition": (
            f"uprev_fop10(閾値{T5_BASE_THRESHOLD:.0%})のイベントのうち、過去{T5_REPEAT_WINDOW_BDAYS}"
            f"営業日以内の同一code上方修正回数(repeat_count)が{T5_MIN_REPEATS}回以上のもの"
        ),
        "repeat_window_bdays": T5_REPEAT_WINDOW_BDAYS,
        "min_repeats": T5_MIN_REPEATS,
        "base_uprev_threshold": T5_BASE_THRESHOLD,
        "baseline_comparison": "uprev_fop10(n=450,lift=1.72[0.98,2.89],EV(stop8)+0.88%,pending)",
        "note_history_warmup": (
            "finsデータ先頭2016-07-06のため、in-sample開始直後(2016-11〜)は「過去2年」の履歴が"
            "最大約4ヶ月分しかなく、repeat_count>=2の実質的な有効開始はおよそ2018-07以降と推定"
        ),
        "fiscal_year_key": True,
        "codex6_fix_note": (
            "基礎uprevイベント生成(kpi_uprev_signals.generate_uprev_signals)にCodexレビュー⑥指摘の"
            "同一CurFYEn限定修正を適用して再実行(初回実行値は§7-Gに併記)"
        ),
    }
    return run_trial(signals_df, T5_KPI_NAME, base_params, defer_entry=False, **shared["harness_ctx"])


TRIAL_RUNNERS = {"t1": run_t1, "t2": run_t2, "t3": run_t3, "t4": run_t4, "t5": run_t5}


# --- メイン処理 ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="第18周: イベント系5本バッチ（カタログ§7-G・T1〜T5）")
    parser.add_argument("--trial", choices=["t1", "t2", "t3", "t4", "t5", "all"], default="all")
    args = parser.parse_args()

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _event_bd_bounds(PERIOD[0], PERIOD[1], all_bdays)

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    shared = {
        "start_bd": start_bd,
        "end_bd": end_bd,
        "harness_ctx": {
            "all_bdays": all_bdays,
            "bday_index": bday_index,
            "regime_by_day": regime_by_day,
            "base_rate_by_month": base_rate_by_month,
            "universes_by_month": universes_by_month,
        },
    }

    # T4はteam-lead裁定(2026-07-09)により取り下げ・不実施。--trial all はt1/t2/t3/t5の4試行のみ実行する。
    trials_to_run = ["t1", "t2", "t3", "t5"] if args.trial == "all" else [args.trial]
    results = []
    for t in trials_to_run:
        results.append(TRIAL_RUNNERS[t](shared))

    print("\n=== 第18周バッチ完了サマリー ===")
    for r in results:
        print(
            f"{r['kpi_name']}: n={r['n']} lift={r['lift']} verdict(§6)={r['verdict']} "
            f"探索的一次結論={r['conclusion']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
