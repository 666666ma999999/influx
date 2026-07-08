#!/usr/bin/env python3
"""第17周: ULフェード大サンプル検証（カタログ§7-F・T1/T2）。

第16周（`scripts/kpi_volshock_v3_ul_earnings.py`）はチャンピオン構成
`volshock_x_above200_quiet`（n=73）に `ul_count_10bd` を重ねたが、母集団が小さくA2は
additive_fragile（除外3件依存）で決着した。本ラウンドは in-sample データ全体
（2016-11〜2022-11・従来と同一。holdout 2023年以降は使わない）を使い、2つの事前登録
試行を実施する:

- T1 `ul_fade_standalone`: UL連続発生後のフェード仮説そのものを、champion母集団とは独立に
  全営業日×前月末確定ユニバースから大サンプルで生成し検証する。
- T2 `volshock_x_above200_ul_lt3`: 第16周A2（champion限定n=73）ではなく、
  above200のみのフィルタ前広母集団（n=180）で同一閾値(`ul_count_10bd<=2`)を再確認する。

Canonical Module再利用（新規ロジックはgenerate_ul_fade_signals/_fallback_universe_month/
run_t1/run_t2のみ）:
- ul_count_10bd計算: `kpi_volshock_v3_ul_earnings.compute_ul_count_10bd`（そのままimport・再実装なし）
- フォワードリターン・重複除去・ユニバース判定: `kpi_event_study.compute_signal_returns`
  （`measure_base_rate.compute_forward_return_for_code`/`compute_forward_return_deferred`経由）
- 集計・§6正式判定・台帳記録: `kpi_event_study.compute_stats`/`judge`/`append_trial`/`write_report_md`
- EVの月次ブロック・ブートストラップCI: `kpi_event_study.bootstrap_ev_ci`
  （第17周で新規追加した拡張点。`bootstrap_lift_ci`と同一の月ブロック抽出方式をEVに適用）
- T2のchampion基準線比較ロジック: `kpi_volshock_v3_ul_earnings.classify_vs_baseline`
  （baseline_lift/baseline_ev_e1をoptional引数化した後方互換リファクタで、本試行自身の
  母集団実測値を基準線として再利用する）
- E1シナリオ崩壊exitの突合: `kpi_volshock_v2_followup.load_e1_lookup`/`attach_e1_ev`
- 既存returns.csvの読み込み: `kpi_volshock_v2_amplifiers.load_positions`

T1はカタログ§7-F事前登録どおり「前月末確定ユニバース(TOP500・w21・厳格前月ルール)」を
シグナル生成の候補コード集合として使う。これは `kpi_event_study._universe_membership`
（シグナル月より厳密に前で直近確定していたユニバース月を使う規則）と同一のルールを
月単位で事前構築したものであり、下流の`compute_signal_returns`が独立に行う
in_universe判定と一致するはず（不一致があれば`diag["out_of_universe"]`>0として検知される）。

Usage:
    python3 scripts/kpi_ul_fade_signals.py --part both
    python3 scripts/kpi_ul_fade_signals.py --part t1
    python3 scripts/kpi_ul_fade_signals.py --part t2
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
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/append_trial/write_report_md/bootstrap_ev_ci を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END を再利用)
import kpi_volshock_v2_amplifiers as v2_amp  # noqa: E402  (Canonical Module: load_positions/STATS_TABLE_HEADER/_stats_row を再利用)
import kpi_volshock_v2_followup as v2_followup  # noqa: E402  (Canonical Module: load_e1_lookup/attach_e1_ev を再利用)
import kpi_volshock_v3_ul_earnings as v3_ul  # noqa: E402  (Canonical Module: compute_ul_count_10bd/classify_vs_baseline/UL_WINDOW_BDAYS/EV_E1_TOLERANCE等を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/regime読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

UL_COUNT_ENTER_THRESHOLD = 3  # T1: ul_count_10bd(D)>=3 かつ (D-1)<3 で「新規到達」
UL_COUNT_T2_MAX = 2  # T2: 第16周A2と完全同一の閾値（変更禁止）

T1_KPI_NAME = "ul_fade_standalone"
T2_KPI_NAME = "volshock_x_above200_ul_lt3"
T2_OUTPUT_DIR = OUTPUT_ROOT / T2_KPI_NAME
T2_SOURCE_RETURNS = Path("output/kpi/volshock_x_above200/returns.csv")
T2_EXIT_STUDY_POSITIONS = Path("output/kpi/exit_study/positions_volshock_x_above200.csv")
T2_EXPECTED_N = 180  # 既知の実測値（第13〜16周で確認済み）。ズレたらFATALで停止

PROGRESS_EVERY_N_DAYS = 60  # 進捗表示間隔（営業日）


# --- 共通: ユニバース月選択（kpi_event_study._universe_membershipと同一規則） ------------


def _fallback_universe_month(signal_month: str, universes_by_month: dict[str, set]) -> Optional[str]:
    """`kpi_event_study._universe_membership`と同一の月選択規則（シグナル月より厳密に前で
    直近確定していたユニバース月を選ぶ）。ここではT1のシグナル生成候補コード集合を月単位で
    事前構築するためだけに使うヘルパーで、実際のin_universe判定自体は下流の
    `compute_signal_returns`側で改めて独立に行われる（本関数の結果と食い違えば
    diag["out_of_universe"]>0として検知できる設計）。
    """
    earlier_months = [m for m in universes_by_month if m < signal_month]
    if not earlier_months:
        return None
    return max(earlier_months)


# --- T1: ul_fade_standalone -------------------------------------------------------


def generate_ul_fade_signals(
    start_bd: str,
    end_bd: str,
    bday_index: dict[str, int],
    all_bdays: list[str],
    universes_by_month: dict[str, set],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]の全営業日×前月末確定ユニバースの銘柄からUL新規到達シグナルを生成する。

    ul_count_10bdの計算は`kpi_volshock_v3_ul_earnings.compute_ul_count_10bd`を直接呼び出す
    （再実装なし）。同一(code, day)への重複呼び出しを避けるため本関数内だけで有効な
    ローカルメモ化キャッシュを使う（同じ日の値が翌営業日の判定でD-1として再利用されるため、
    実質1回の計算で済む。compute_ul_count_10bd自体はcodeと日付とカレンダーのみに依存する
    純粋関数のため、月をまたいだキャッシュ再利用も安全）。
    """
    scan_days = [d for d in all_bdays if start_bd <= d <= end_bd]
    ul_cache: dict[tuple[str, str], Optional[int]] = {}

    def ul_count(code: str, day: str) -> Optional[int]:
        key = (code, day)
        if key not in ul_cache:
            ul_cache[key] = v3_ul.compute_ul_count_10bd(code, day, bday_index, all_bdays)
        return ul_cache[key]

    month_candidates: dict[str, set] = {}  # 月ごとの候補コード集合（同月内の全営業日で使い回す）

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "insufficient_ul_history_current": 0,
        "insufficient_ul_history_prev": 0,
        "signals_ul_fade_standalone": 0,
        "months_with_no_fallback_universe": 0,
    }
    rows: list[dict] = []

    for d in scan_days:
        diag["business_days_scanned"] += 1
        signal_month = d[:6]
        if signal_month not in month_candidates:
            fallback_month = _fallback_universe_month(signal_month, universes_by_month)
            month_candidates[signal_month] = (
                set() if fallback_month is None else universes_by_month[fallback_month]
            )
            if fallback_month is None:
                diag["months_with_no_fallback_universe"] += 1
        candidates = month_candidates[signal_month]
        if not candidates:
            continue

        idx = bday_index[d]
        d_prev = all_bdays[idx - 1] if idx > 0 else None

        for code in candidates:
            diag["code_day_observations"] += 1
            count_d = ul_count(code, d)
            if count_d is None:
                diag["insufficient_ul_history_current"] += 1
                continue
            if d_prev is None:
                diag["insufficient_ul_history_prev"] += 1
                continue
            count_prev = ul_count(code, d_prev)
            if count_prev is None:
                diag["insufficient_ul_history_prev"] += 1
                continue
            if count_d >= UL_COUNT_ENTER_THRESHOLD and count_prev < UL_COUNT_ENTER_THRESHOLD:
                diag["signals_ul_fade_standalone"] += 1
                rows.append(
                    {
                        "signal_date": d,
                        "code": code,
                        "ul_count_10bd": count_d,
                        "ul_count_10bd_prev": count_prev,
                    }
                )

        if diag["business_days_scanned"] % PROGRESS_EVERY_N_DAYS == 0:
            print(
                f"  進捗: {diag['business_days_scanned']}/{len(scan_days)}営業日走査済み "
                f"(直近={d}・シグナル成立={diag['signals_ul_fade_standalone']}件・"
                f"ULキャッシュサイズ={len(ul_cache)})",
                file=sys.stderr,
            )

    return pd.DataFrame(rows), diag


def run_t1(bday_index: dict[str, int], all_bdays: list[str], universes_by_month: dict[str, set]) -> None:
    start_bound = PERIOD[0].replace("-", "") + "01"
    end_bound = PERIOD[1].replace("-", "") + "31"
    start_bd = next(d for d in all_bdays if d >= start_bound)
    end_bd = next(d for d in reversed(all_bdays) if d <= end_bound)

    print(f"T1 {T1_KPI_NAME}: シグナル生成中 ({start_bd}〜{end_bd})...", file=sys.stderr)
    signals_df, gen_diag = generate_ul_fade_signals(start_bd, end_bd, bday_index, all_bdays, universes_by_month)
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 "
        f"(走査営業日={gen_diag['business_days_scanned']}, 銘柄日観測={gen_diag['code_day_observations']}, "
        f"UL履歴不足(当日)={gen_diag['insufficient_ul_history_current']}, "
        f"UL履歴不足(前日)={gen_diag['insufficient_ul_history_prev']}, "
        f"フォールバックユニバースなし月={gen_diag['months_with_no_fallback_universe']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です（生成ロジックを確認してください）")

    kpi_dir = OUTPUT_ROOT / T1_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

    # --- 以下、kpi_event_study.run_event_study()と同一のCanonical呼び出し列を直接実行する。
    # run_event_study()をそのまま使わない理由: EVブートストラップ(fade確認の一次結論)は
    # 台帳(trials.jsonl)のparamsに含めたいが、その値はcompute_stats後・append_trial前でしか
    # 計算できない。第16周(kpi_volshock_v3_ul_earnings.py)・第13周(v2_amplifiers.py)も同じ
    # 理由でrun_event_studyを使わずCanonical関数を直接呼ぶ設計になっており、本スクリプトも
    # その前例に倣う（再実装ではなく同一関数の直接呼び出し）。
    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=True
    )
    diag["regime_filter"] = None
    diag["pre_regime_filter_count"] = len(harness_signals_df)
    if diag.get("out_of_universe", 0) > 0:
        print(
            f"WARN: T1事前フィルタとハーネスのuniverse判定に不一致の可能性"
            f"(out_of_universe={diag['out_of_universe']}件・想定=0件)。原因を要確認。",
            file=sys.stderr,
        )

    in_universe_df = (
        returns_df[returns_df["in_universe"]].reset_index(drop=True) if len(returns_df) else returns_df
    )
    if in_universe_df.empty:
        raise SystemExit("FATAL: T1のin_universeシグナルが0件です")

    stats = kpi_event_study.compute_stats(in_universe_df, base_rate_by_month, PERIOD)
    verdict, reasons = kpi_event_study.judge(stats)
    # defer_entry=True時の内部呼び出しをrun_event_studyと同様に踏襲（再実装せずそのまま呼ぶ）
    defer_stats = kpi_event_study._compute_defer_stats(in_universe_df)

    ev_ci = kpi_event_study.bootstrap_ev_ci(in_universe_df, ev_column="ret")
    mae20_rate = float((in_universe_df["mae"] <= -0.20).mean())
    fade_confirmed = bool(
        ev_ci["point_ev"] is not None
        and ev_ci["ci_high"] is not None
        and ev_ci["point_ev"] < 0
        and ev_ci["ci_high"] < 0
    )

    params = {
        "signal_definition": "ul_count_10bd(D)>=3 かつ ul_count_10bd(D-1)<3（新規到達日のみ・連続発火防止）",
        "ul_window_bdays": v3_ul.UL_WINDOW_BDAYS,
        "ul_count_enter_threshold": UL_COUNT_ENTER_THRESHOLD,
        "universe_rule": "前月末確定ユニバース(TOP500・w21・厳格前月ルール=kpi_event_study._universe_membershipと同一)",
        "defer_entry": True,
        "ev_none_point": ev_ci["point_ev"],
        "ev_none_ci_low": ev_ci["ci_low"],
        "ev_none_ci_high": ev_ci["ci_high"],
        "ev_ci_n_boot_valid": ev_ci["n_boot_valid"],
        "mae_leq_neg20_rate": mae20_rate,
        "fade_confirmed": fade_confirmed,
        "exploratory_conclusion": "fade_confirmed" if fade_confirmed else "fade_not_confirmed",
        "pre_registered_success_rule": (
            "fade確認: EV(なし・往復コスト0.3%控除込)点推定<0 かつ "
            "月次ブロックブートストラップ95%CI上限(97.5%tile)<0"
        ),
        "round": "17_ul_fade_standalone",
        "multi_trial_discount_note": (
            "本ラウンドはT2(volshock_x_above200_ul_lt3)との2試行同時登録であり"
            "累積試行数割引の対象。この結果単独で運用変更しない。"
        ),
    }

    kpi_dir_returns = kpi_dir / "returns.csv"
    report_path = kpi_dir / "report.md"
    returns_df.to_csv(kpi_dir_returns, index=False)
    kpi_event_study.write_report_md(
        report_path, T1_KPI_NAME, params, PERIOD, diag, stats, verdict, reasons, in_universe_df,
        defer_stats=defer_stats,
    )
    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n## fade確認（第17周・カタログ§7-F事前登録の一次結論）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
        )
        f.write(
            f"- **fade確認（事前凍結基準: EV点推定<0 かつ CI上限<0）: "
            f"{'成立' if fade_confirmed else '不成立'}**\n"
        )
        f.write(
            f"- 参考（判定には未使用）: P(+20%終値)={stats.get('p20')} "
            f"リフト点推定={stats.get('point_lift')} リフト95%CI=[{stats.get('ci_low')},{stats.get('ci_high')}] "
            f"mae<=-20%タッチ率={mae20_rate:.2%}\n"
        )
        f.write(
            "- 注記: リフトが高くても（+20%到達確率自体は高くても）fade判定(EV基準)とは矛盾しない"
            "（『当たれば大きいが期待値はマイナス』は両立しうる。事前登録どおり判定には使わない）\n"
        )

    trial_record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": T1_KPI_NAME,
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

    print(
        f"T1 完了: n={stats['n']} lift={stats.get('point_lift')} verdict(§6)={verdict} "
        f"EV点推定={ev_ci['point_ev']} CI95=[{ev_ci['ci_low']},{ev_ci['ci_high']}] "
        f"fade確認={'成立' if fade_confirmed else '不成立'}"
    )
    print(f"T1 report: {report_path}")
    print(f"T1 returns: {kpi_dir_returns}")


# --- T2: volshock_x_above200_ul_lt3（広母集団再確認） ------------------------------


def write_t2_report(
    baseline_stats: dict,
    subset_stats: dict,
    baseline_ev_e1: Optional[float],
    baseline_e1_missing: int,
    subset_ev_e1: Optional[float],
    subset_e1_missing: int,
    n_unknown: int,
    excluded_n: int,
    excluded_mean_ret: Optional[float],
    retained_mean_ret: Optional[float],
    conclusion: str,
    verdict: str,
    reasons: list[str],
) -> None:
    def fmt_pct(x: Optional[float]) -> str:
        return f"{x:.2%}" if x is not None else "-"

    def fmt_ratio(x: Optional[float]) -> str:
        return f"{x:.2f}" if x is not None else "-"

    excluded_worse = (
        excluded_mean_ret is not None and retained_mean_ret is not None and excluded_mean_ret < retained_mean_ret
    )
    lines = [
        "# 第17周 T2: volshock_x_above200_ul_lt3（広母集団再確認・カタログ§7-F）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降は未使用）",
        "母集団: `volshock_x_above200`（above200のみ・quiet絞りなし。第16周championとは別母集団）",
        "",
        "> **多重比較の明記**: 本ラウンドはT1(`ul_fade_standalone`)との2試行同時登録であり"
        "累積試行数割引の対象。この結果単独で運用変更しない。",
        "",
        f"## 1. 基準線（母集団n={baseline_stats['n']}・素の実測）",
        "",
        v2_amp.STATS_TABLE_HEADER,
        v2_amp._stats_row("母集団(baseline)", baseline_stats),
        "",
        f"EV(E1シナリオ崩壊exit) baseline = {fmt_pct(baseline_ev_e1)}（E1突合不能={baseline_e1_missing}件）",
        f"ul_count_10bd計算不能(insufficient history) = {n_unknown}件/{baseline_stats['n']}件",
        "",
        "## 2. フィルタ適用後: ul_count_10bd<=2（第16周A2と完全同一閾値）",
        "",
        v2_amp.STATS_TABLE_HEADER,
        v2_amp._stats_row("母集団(baseline)", baseline_stats),
        v2_amp._stats_row(f"ul_count_10bd<={UL_COUNT_T2_MAX}", subset_stats),
        "",
        f"EV(E1シナリオ崩壊exit) = {fmt_pct(subset_ev_e1)}（E1突合不能={subset_e1_missing}件）",
        "",
        "## 3. 除外群の診断（単調性チェックの代替・事前登録項目）",
        "",
        f"- 除外件数（ul_count_10bd>={UL_COUNT_T2_MAX + 1}）: {excluded_n}件",
        f"- 除外群の平均リターン（生値・ret列） = {fmt_pct(excluded_mean_ret)}",
        f"- 残す群(ul_count_10bd<={UL_COUNT_T2_MAX})の平均リターン = {fmt_pct(retained_mean_ret)}",
        "",
        f"判定: 除外群の平均リターンは残す群より"
        f"**{'悪い（事前予想と整合）' if excluded_worse else '悪くない（事前予想と不整合・少数除外依存の疑いを要注記）'}**。",
        "",
        "## 4. 探索的一次結論（§6完全合格とは別軸・第16周classify_vs_baselineと同一ルール構造）",
        "",
        f"基準線（本試行自身の母集団実測・championの4.16/2.84%とは別物）: "
        f"lift={fmt_ratio(baseline_stats.get('point_lift'))} EV(E1)={fmt_pct(baseline_ev_e1)}",
        f"**探索的一次結論: {v3_ul.CONCLUSION_LABELS_JA.get(conclusion, conclusion)}**",
        "",
        f"§6正式verdict: **{kpi_event_study.VERDICT_LABELS_JA.get(verdict, verdict)}**",
        "",
    ]
    lines += [f"- {r}" for r in reasons]
    lines += [
        "",
        "## 5. 実装ノート",
        "- エントリー価格・保有期間・ユニバース判定・フォワードリターン(ret/mfe/mae/ret_stop8等)は"
        "一切再計算していない（`output/kpi/volshock_x_above200/returns.csv`の既存列をそのまま使用）",
        "- `ul_count_10bd`は第16周`kpi_volshock_v3_ul_earnings.compute_ul_count_10bd`をそのまま再利用",
        "- `classify_vs_baseline`は`baseline_lift`/`baseline_ev_e1`を本試行自身の母集団実測値に"
        "差し替えて呼び出した（第16周スクリプトの既存呼び出し=champion基準は変更なし・後方互換）",
        "- EV(E1)は既存`output/kpi/exit_study/positions_volshock_x_above200.csv`（180件全件を"
        "カバー済み）を`kpi_volshock_v2_followup.load_e1_lookup`/`attach_e1_ev`で突合したのみ"
        "（再計算なし）",
        f"- 閾値(ul_count_10bd<={UL_COUNT_T2_MAX})は第16周A2と完全同一・事後変更なし",
        "",
    ]
    T2_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (T2_OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def run_t2(bday_index: dict[str, int], all_bdays: list[str]) -> None:
    print(f"T2 {T2_KPI_NAME}: 母集団読み込み中...", file=sys.stderr)
    baseline_df = v2_amp.load_positions(T2_SOURCE_RETURNS)
    if len(baseline_df) != T2_EXPECTED_N:
        raise SystemExit(
            f"FATAL: T2母集団のn={len(baseline_df)}が既知値{T2_EXPECTED_N}と不一致。"
            f"データが再生成された可能性。ベースライン比較の前提が崩れるため実行を停止する"
        )
    print(f"T2 母集団再現確認: n={len(baseline_df)} (期待値{T2_EXPECTED_N}) OK", file=sys.stderr)

    print("T2 ul_count_10bd計算中...", file=sys.stderr)
    baseline_df["ul_count_10bd"] = baseline_df.apply(
        lambda r: v3_ul.compute_ul_count_10bd(r["code"], r["signal_date"], bday_index, all_bdays), axis=1
    )
    baseline_df["ul_count_10bd"] = pd.to_numeric(baseline_df["ul_count_10bd"], errors="coerce")
    n_unknown = int(baseline_df["ul_count_10bd"].isna().sum())
    if n_unknown:
        print(
            f"WARN: T2 ul_count_10bd計算不能(insufficient history)={n_unknown}件（母集団から自然除外）",
            file=sys.stderr,
        )

    subset_df = baseline_df[baseline_df["ul_count_10bd"] <= UL_COUNT_T2_MAX].reset_index(drop=True)
    excluded_df = baseline_df[baseline_df["ul_count_10bd"] > UL_COUNT_T2_MAX].reset_index(drop=True)

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    baseline_stats = kpi_event_study.compute_stats(baseline_df, base_rate_by_month, PERIOD)
    subset_stats = kpi_event_study.compute_stats(subset_df, base_rate_by_month, PERIOD)

    e1_lookup = v2_followup.load_e1_lookup(T2_EXIT_STUDY_POSITIONS)
    baseline_ev_e1, baseline_e1_missing = v2_followup.attach_e1_ev(baseline_df, e1_lookup)
    subset_ev_e1, subset_e1_missing = v2_followup.attach_e1_ev(subset_df, e1_lookup)
    if baseline_e1_missing == len(baseline_df):
        print("WARN: T2 E1突合が母集団全件で不能（E1系列なし）", file=sys.stderr)

    if baseline_stats.get("point_lift") is None or baseline_ev_e1 is None:
        raise SystemExit(
            "FATAL: T2基準線(母集団)のlift/EV(E1)が計算不能です。データ整合性を確認してください。"
        )

    excluded_n = len(excluded_df)
    excluded_mean_ret = float(excluded_df["ret"].mean()) if excluded_n else None
    retained_mean_ret = float(subset_df["ret"].mean()) if len(subset_df) else None

    conclusion = v3_ul.classify_vs_baseline(
        subset_stats["n"],
        subset_stats.get("point_lift"),
        subset_ev_e1,
        baseline_lift=baseline_stats["point_lift"],
        baseline_ev_e1=baseline_ev_e1,
    )
    verdict, reasons = kpi_event_study.judge(subset_stats)

    features_path = T2_OUTPUT_DIR / "signals_features.csv"
    T2_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline_df["ret_costadj_E1"] = baseline_df.apply(
        lambda r: e1_lookup.get((r["signal_date"], r["code"])), axis=1
    )
    baseline_df[["signal_date", "code", "ul_count_10bd", "ret", "ret_costadj_E1"]].to_csv(
        features_path, index=False
    )

    params = {
        "source_population": "volshock_x_above200",
        "source_returns_csv": str(T2_SOURCE_RETURNS),
        "filter": f"ul_count_10bd<={UL_COUNT_T2_MAX}",
        "ul_window_bdays": v3_ul.UL_WINDOW_BDAYS,
        "baseline_population_n": baseline_stats["n"],
        "baseline_lift": baseline_stats.get("point_lift"),
        "baseline_ev_e1": baseline_ev_e1,
        "baseline_ev_e1_missing": baseline_e1_missing,
        "ev_e1_scenario_break": subset_ev_e1,
        "ev_e1_missing_positions": subset_e1_missing,
        "excluded_count": excluded_n,
        "excluded_mean_ret": excluded_mean_ret,
        "retained_mean_ret": retained_mean_ret,
        "insufficient_ul_history_count": n_unknown,
        "exploratory_conclusion": conclusion,
        "pre_registered_success_rule": (
            f"additive: point_lift>=baseline({baseline_stats['point_lift']:.4f}) AND "
            f"EV(E1)>=baseline-{v3_ul.EV_E1_TOLERANCE:.4f}({baseline_ev_e1 - v3_ul.EV_E1_TOLERANCE:.4f}) AND "
            f"n>={v3_ul.ADDITIVE_MIN_N} / degrading: 上記いずれか未達 または n<{v3_ul.DEGRADING_MAX_N_EXCLUSIVE} / "
            "neutral: 上記どちらにも当てはまらない場合"
        ),
        "round": "17_ul_fade_t2_broad_reconfirm",
        "multi_trial_discount_note": (
            "本ラウンドはT1(ul_fade_standalone)との2試行同時登録であり"
            "累積試行数割引の対象。この結果単独で運用変更しない。"
        ),
    }
    trial_record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": T2_KPI_NAME,
        "params": params,
        "period": {"start": PERIOD[0], "end": PERIOD[1]},
        "n": subset_stats["n"],
        "lift": subset_stats.get("point_lift"),
        "ci_low": subset_stats.get("ci_low"),
        "ci_high": subset_stats.get("ci_high"),
        "ev": subset_stats.get("ev_stop8"),
        "verdict": verdict,
        "entry_mode": "reused_from_existing_returns_csv",
        "regime_filter": None,
    }
    kpi_event_study.append_trial(trial_record, TRIALS_PATH)

    write_t2_report(
        baseline_stats, subset_stats, baseline_ev_e1, baseline_e1_missing,
        subset_ev_e1, subset_e1_missing, n_unknown, excluded_n, excluded_mean_ret,
        retained_mean_ret, conclusion, verdict, reasons,
    )
    print(
        f"T2 完了: n={subset_stats['n']}(母集団{baseline_stats['n']}) "
        f"lift={subset_stats.get('point_lift')} EV(E1)={subset_ev_e1} "
        f"除外={excluded_n}件(平均{excluded_mean_ret}) verdict(§6)={verdict} 探索的一次結論={conclusion}"
    )
    print(f"T2 report: {T2_OUTPUT_DIR / 'report.md'}")
    print(f"T2 features: {features_path}")


# --- メイン処理 ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="第17周: ULフェード大サンプル検証（カタログ§7-F・T1/T2）")
    parser.add_argument("--part", choices=["t1", "t2", "both"], default="both")
    args = parser.parse_args()

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    if args.part in ("t1", "both"):
        universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
        run_t1(bday_index, all_bdays, universes_by_month)
    if args.part in ("t2", "both"):
        run_t2(bday_index, all_bdays)
    return 0


if __name__ == "__main__":
    sys.exit(main())
