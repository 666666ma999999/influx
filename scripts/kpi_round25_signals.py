#!/usr/bin/env python3
"""第25周: 残差リターン系四本バッチ（カタログ§7-N・T1〜T4）。

docs/stock-algo-kpi-catalog.md §7-N の事前登録（凍結）定義を厳密に実装する。in-sample
期間（2016-11〜2022-11。ただし36ヶ月履歴要件により実効シグナルは2019年後半以降）で
4試行を同時に実行し、探索的一次結論ルール（第18周基準）を適用する。holdout（2023年以降）は
使わない。

T1 resid_momentum_entry : TOPIX残差モメンタム（Σε[t-12..t-2] ÷ std）上位decile新規入り
T2 resid_strev_entry    : TOPIX残差 直近1ヶ月反転（−ε[t-1] ÷ std）上位decile新規入り
T3 raw_momentum_entry   : 素の累積リターン Σr[t-12..t-2]（σ標準化なし）上位decile新規入り
T4 raw_strev_entry      : 素の −r[t-1]（σ標準化なし）上位decile新規入り

実装の流儀は scripts/kpi_round24_signals.py（直近の同型実装）の run_trial 呼び出し契約を踏襲する。

Canonical Module 再利用（新規ロジックは月次パネル構築・月次OLS残差スコア・decile新規入り
クロス判定・月次pairedブロックブートストラップの束ね方のみ）:
- 月次リターンの原資料 AdjC / TOPIX Close: measure_base_rate.load_bars_day /
  load_topix_series（frozen モジュールを import 再利用）。
- カレンダー・営業日・月末営業日: measure_base_rate.load_calendar_days /
  all_business_days / month_ends_in_range。
- ユニバース所属・順リターン・重複除去・集計・§6判定・レポート・台帳:
  kpi_event_study の _universe_membership / compute_signal_returns / compute_stats /
  judge / bootstrap_ev_ci / write_report_md / append_trial（第24周と同一契約）。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変の最適化）。
- 探索的一次結論: kpi_event_batch_signals.classify_exploratory。
- paired ΔEV のブートストラップ両側p値相当・Holm CI水準: kpi_sue_exit_study._p_value_boundary /
  HOLM_SMALL_P_CI_LEVEL / HOLM_LARGE_P_CI_LEVEL（§7-J凍結手順の Canonical 実装を再利用）。

Usage:
    python3 scripts/kpi_round25_signals.py --trial all
    python3 scripts/kpi_round25_signals.py --trial t1
    python3 scripts/kpi_round25_signals.py --trial all --start 2019-09 --end 2020-06 --no-trials-append
"""
from __future__ import annotations

import argparse
import bisect
import math
import sys
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: ハーネス一式を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe を再利用)
import kpi_sue_exit_study  # noqa: E402  (Canonical Module: _p_value_boundary・Holm CI水準を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars・TOPIX読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-N・事後変更禁止） -----------------------------

# 月次リターンパネルの起点月（bars最古は2016-07。36ヶ月履歴要件によりシグナルは2019後半〜）
PANEL_START_MONTH = "2016-07"

REG_WINDOW = 36  # OLS窓（月 t-36〜t-1 の36観測・凍結）
MOM_LO_IDX = 24  # 窓内index: 月 t-12（=36-12）。モメンタム区間 t-12〜t-2 の下端
MOM_HI_IDX = 35  # 窓内index: 月 t-1（=36-1・排他上端。モメンタム区間は [24:34]）。t-1 は index 35
DECILE_FRAC = 0.10  # 上位decile境界（凍結）

TRIAL_DEFS = {
    "t1": {"kpi_name": "resid_momentum_entry", "kind": "resid_mom"},
    "t2": {"kpi_name": "resid_strev_entry", "kind": "resid_strev"},
    "t3": {"kpi_name": "raw_momentum_entry", "kind": "raw_mom"},
    "t4": {"kpi_name": "raw_strev_entry", "kind": "raw_strev"},
}
TRIAL_ORDER = ["t1", "t2", "t3", "t4"]

ROUND_TAG = "25_residual_returns_batch"
BATCH_TRIAL_NOTE = (
    "本ラウンドはT1〜T4の4試行同時登録であり累積試行数割引の対象（第25周時点で通算69〜72試行目）。"
    "この結果単独で運用変更しない。T3/T4は残差化の上乗せ価値を測る同型基準線（素の日本モメンタムは"
    "歴史的にnullが既知＝T3の期待はnull）。"
)
NEIGHBOR_NOTE = (
    "T4は素の strev_20d（日次20日リターン反転・lift1.83・EV-0.65%・pending）と発想が同族だが、"
    "月次decile新規入りイベント化で頻度・タイミングが別物。§2未検証v1『残差モメンタム』"
    "『残差短期リバーサル』（Blitz 2013 / Iwanaga 2024）を出所とする。"
)


# --- 月次リターンパネル構築（新規ロジック） -------------------------------------


def _month_end_bdays(start_month: str, end_month: str) -> list[str]:
    """[start_month, end_month]（YYYY-MM）の各暦月最終営業日(YYYYMMDD)を昇順で返す。"""
    calendar_days = measure_base_rate.load_calendar_days()
    return measure_base_rate.month_ends_in_range(calendar_days, start_month, end_month)


def build_monthly_return_panel(
    month_end_bdays: list[str],
) -> tuple[list[str], list[str], np.ndarray, list[str], np.ndarray]:
    """月末AdjCパネルから月次単純リターン行列を構築する（§7-N凍結: r=AdjC_t/AdjC_{t-1}-1）。

    月は month_end_bdays と1対1（暦月連続）。行 i の月次リターンは
    AdjC(month_end[i]) / AdjC(month_end[i-1]) - 1（行0は前月なしでNaN）。TOPIXも同一規約。

    Returns:
        (months, month_end_bdays, ret_matrix, codes, topix_ret)。
        months: 'YYYYMM' 昇順。ret_matrix: shape (M, Ncodes)・NaNは欠損月。
        codes: 列順の銘柄コード。topix_ret: shape (M,)・行0とTOPIX欠損はNaN。
    """
    months = [d[:6] for d in month_end_bdays]

    # 月末AdjCを {code: adjc} の Series として月ごとに集める
    adjc_series: list[pd.Series] = []
    for d in month_end_bdays:
        bars = measure_base_rate.load_bars_day(d)
        adjc_series.append(
            pd.Series({code: rec.get("AdjC") for code, rec in bars.items()}, dtype="float64")
        )
    adjc_df = pd.DataFrame(adjc_series).sort_index(axis=1)  # 行=月位置・列=code
    ret_df = adjc_df / adjc_df.shift(1) - 1.0  # 行 i = 月 i の単純リターン（行0はNaN）
    codes = list(ret_df.columns)
    ret_matrix = ret_df.to_numpy(dtype="float64")

    topix_close = measure_base_rate.load_topix_series()
    topix_vals = np.array([topix_close.get(d, np.nan) for d in month_end_bdays], dtype="float64")
    topix_ret = np.full(len(months), np.nan, dtype="float64")
    topix_ret[1:] = topix_vals[1:] / topix_vals[:-1] - 1.0

    return months, month_end_bdays, ret_matrix, codes, topix_ret


# --- 月次OLS残差スコア + 上位decile（新規ロジック・凍結数式） -------------------


def _latest_universe_month(month_str: str, universe_months_sorted: list[str]) -> Optional[str]:
    """month_str（YYYYMM）より厳密に前の最新ユニバース確定月を返す（前月末確定TOP500）。

    kpi_event_study._universe_membership と同一の『直近の確定済み月末』選択則（look-ahead回避）。
    """
    i = bisect.bisect_left(universe_months_sorted, month_str)
    return universe_months_sorted[i - 1] if i > 0 else None


def compute_position_deciles(
    p: int,
    months: list[str],
    ret_matrix: np.ndarray,
    topix_ret: np.ndarray,
    codes: list[str],
    universes_by_month: dict[str, set],
    universe_months_sorted: list[str],
) -> Optional[dict]:
    """月位置 p のOLS残差スコア・素スコアを計算し、4試行それぞれの資格集合と上位decileを返す。

    OLS窓 = 行 p-REG_WINDOW 〜 p-1（36観測・月 t-36〜t-1）。銘柄別に r_i=α+β·r_TOPIX+ε。
    36観測すべて有効な銘柄のみスコア計算可能（欠損月あれば不能）。順位母集団 = 前月末確定
    TOP500ユニバース ∩ スコア計算可能。上位decile = 順位 ≤ ceil(N×0.1)（スコア降順・同値Code昇順）。

    Returns:
        None（p<REG_WINDOW+1 で履歴不足）または dict:
        {'n_universe': int, 'beta': {code:β}, 'alpha': {code:α},
         'trials': {trial: {'eligible': set, 'decile': set, 'score': {code:s}, 'rank': {code:r},
                            'n_eligible': int, 'cutoff': int}}}
    """
    if p < REG_WINDOW + 1:
        return None  # 行0がNaNのため36有効観測には p>=REG_WINDOW+1 が必要

    w0, w1 = p - REG_WINDOW, p  # 行 p-36 .. p-1（36行）
    x = topix_ret[w0:w1]
    month_str = months[p]
    univ_month = _latest_universe_month(month_str, universe_months_sorted)
    univ_set = universes_by_month.get(univ_month, set()) if univ_month is not None else set()

    empty_trials = {
        t: {"eligible": set(), "decile": set(), "score": {}, "rank": {}, "n_eligible": 0, "cutoff": 0}
        for t in TRIAL_ORDER
    }
    if np.isnan(x).any() or not univ_set:
        return {"n_universe": len(univ_set), "beta": {}, "alpha": {}, "trials": empty_trials}

    Ywin = ret_matrix[w0:w1, :]  # (36, Ncodes)
    complete_mask = ~np.isnan(Ywin).any(axis=0)
    idxs = [i for i, c in enumerate(codes) if complete_mask[i] and c in univ_set]
    if not idxs:
        return {"n_universe": len(univ_set), "beta": {}, "alpha": {}, "trials": empty_trials}

    sel_codes = [codes[i] for i in idxs]
    Yc = Ywin[:, idxs]  # (36, k)・全列36有効
    X = np.column_stack([np.ones(REG_WINDOW), x])  # (36, 2)
    coef, *_ = np.linalg.lstsq(X, Yc, rcond=None)  # (2, k) = [α; β]
    resid = Yc - X @ coef  # (36, k)
    alpha_arr, beta_arr = coef[0], coef[1]

    with np.errstate(divide="ignore", invalid="ignore"):
        sum_resid_mom = resid[MOM_LO_IDX:MOM_HI_IDX].sum(axis=0)  # Σε[t-12..t-2]（11行）
        std_resid_mom = np.std(resid[MOM_LO_IDX:MOM_HI_IDX], axis=0, ddof=1)  # 同11残差の標本SD
        std_resid_all = np.std(resid, axis=0, ddof=1)  # 全36残差の標本SD
        t1_score = np.where(std_resid_mom > 0, sum_resid_mom / std_resid_mom, np.nan)
        t2_score = np.where(std_resid_all > 0, -resid[MOM_HI_IDX] / std_resid_all, np.nan)
    t3_score = Yc[MOM_LO_IDX:MOM_HI_IDX].sum(axis=0)  # Σr[t-12..t-2]（素・σ標準化なし）
    t4_score = -Yc[MOM_HI_IDX]  # −r[t-1]（素・σ標準化なし）

    score_by_trial = {"t1": t1_score, "t2": t2_score, "t3": t3_score, "t4": t4_score}
    beta_map = {c: float(beta_arr[j]) for j, c in enumerate(sel_codes)}
    alpha_map = {c: float(alpha_arr[j]) for j, c in enumerate(sel_codes)}

    trials_out = {}
    for t in TRIAL_ORDER:
        s = score_by_trial[t]
        pairs = [(sel_codes[j], float(s[j])) for j in range(len(sel_codes)) if not np.isnan(s[j])]
        # スコア降順・同値Code昇順の一意順位
        pairs.sort(key=lambda cs: (-cs[1], cs[0]))
        n_elig = len(pairs)
        cutoff = math.ceil(n_elig * DECILE_FRAC)
        score_map = {c: v for c, v in pairs}
        rank_map = {c: r + 1 for r, (c, _) in enumerate(pairs)}
        eligible = set(score_map.keys())
        decile = {c for c, _ in pairs[:cutoff]}
        trials_out[t] = {
            "eligible": eligible, "decile": decile, "score": score_map, "rank": rank_map,
            "n_eligible": n_elig, "cutoff": cutoff,
        }

    return {"n_universe": len(univ_set), "beta": beta_map, "alpha": alpha_map, "trials": trials_out}


def generate_cross_signals(
    trial: str,
    p_start: int,
    p_end: int,
    months: list[str],
    month_end_bdays: list[str],
    decile_cache: dict[int, Optional[dict]],
) -> tuple[pd.DataFrame, dict]:
    """指定試行の『上位decile新規入り』シグナルを月位置 [p_start, p_end] で生成する（§7-N凍結）。

    発火条件（凍結）: 当月・前月の両方でスコア計算可能（=eligible）∧ 前月decile非該当 ∧ 当月decile該当。
    前月状態は前月自身のOLS（decile_cache[p-1]）から算出（当月モデルの遡及使用禁止）。
    シグナル確定=月末営業日T終値・エントリーはハーネス側でT+1寄付。

    Returns:
        (signals_df, diag)。signals_df 列: signal_date, code, month, score, rank, n_eligible,
        decile_cutoff, beta, alpha。diag は月別のユニバース/資格件数・除外率内訳。
    """
    rows: list[dict] = []
    monthly = []  # 月別診断（除外率レポート用）
    for p in range(p_start, p_end + 1):
        cur = decile_cache.get(p)
        prev = decile_cache.get(p - 1)
        if cur is None:
            monthly.append({"month": months[p], "n_universe": None, "n_eligible": 0, "signals": 0})
            continue
        ct = cur["trials"][trial]
        n_universe = cur["n_universe"]
        n_eligible = ct["n_eligible"]
        n_sig = 0
        if prev is not None:
            pt = prev["trials"][trial]
            prev_eligible = pt["eligible"]
            prev_decile = pt["decile"]
            for code in ct["decile"]:
                if code in prev_eligible and code not in prev_decile:
                    rows.append({
                        "signal_date": month_end_bdays[p],
                        "code": code,
                        "month": months[p],
                        "score": ct["score"][code],
                        "rank": ct["rank"][code],
                        "n_eligible": n_eligible,
                        "decile_cutoff": ct["cutoff"],
                        "beta": cur["beta"].get(code),
                        "alpha": cur["alpha"].get(code),
                    })
                    n_sig += 1
        monthly.append({
            "month": months[p], "n_universe": n_universe,
            "n_eligible": n_eligible, "signals": n_sig,
        })

    diag = {"monthly": monthly}
    return pd.DataFrame(rows), diag


# --- new/existing（ユニバース入替バイアス分解・§6手順5） ------------------------


def classify_membership(
    code: str, signal_month: str, universes_by_month: dict[str, set], universe_months_sorted: list[str]
) -> str:
    """シグナル月の前月・前々月ユニバースに連続在籍していれば existing、そうでなければ new。

    measure_base_rate._membership（3ヶ月連続在籍判定）を、確定済み直近月末の選択則に合わせて適用。
    """
    m1_month = _latest_universe_month(signal_month, universe_months_sorted)
    m1 = universes_by_month.get(m1_month) if m1_month is not None else None
    m2_month = _latest_universe_month(m1_month, universe_months_sorted) if m1_month is not None else None
    m2 = universes_by_month.get(m2_month) if m2_month is not None else None
    return measure_base_rate._membership(code, m1, m2)


# --- ハーネス実行 + 探索的一次結論 + 台帳記録（第24周 run_trial 契約を踏襲） -----


def run_trial(
    trial: str,
    signals_df: pd.DataFrame,
    base_params: dict,
    harness_ctx: dict,
    report_extra_lines: list[str],
    append_to_ledger: bool,
) -> dict:
    """低レベルCanonical関数を直接呼んでハーネス実行〜探索的結論〜台帳記録まで行う
    （第24周 kpi_round24_signals.run_trial と同一契約。defer_entry は§6手順6既定の True 固定）。"""
    kpi_name = TRIAL_DEFS[trial]["kpi_name"]
    defer_entry = True
    all_bdays = harness_ctx["all_bdays"]
    bday_index = harness_ctx["bday_index"]
    regime_by_day = harness_ctx["regime_by_day"]
    base_rate_by_month = harness_ctx["base_rate_by_month"]
    universes_by_month = harness_ctx["universes_by_month"]
    universe_months_sorted = harness_ctx["universe_months_sorted"]

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

    # new/existing（ユニバース入替バイアス分解・§6手順5）
    in_universe_df = in_universe_df.copy()
    in_universe_df["membership"] = [
        classify_membership(str(c), str(m), universes_by_month, universe_months_sorted)
        for c, m in zip(in_universe_df["code"], in_universe_df["month"])
    ]

    stats = kpi_event_study.compute_stats(in_universe_df, base_rate_by_month, PERIOD)
    verdict, reasons = kpi_event_study.judge(stats)
    defer_stats = kpi_event_study._compute_defer_stats(in_universe_df)
    ev_ci = kpi_event_study.bootstrap_ev_ci(in_universe_df, ev_column="ret")
    conclusion = kpi_event_batch_signals.classify_exploratory(
        stats.get("point_lift"), ev_ci["point_ev"], ev_ci["ci_low"]
    )

    # new/existing別の集計（同一の Canonical compute_stats/bootstrap_ev_ci を部分集合へ適用）
    membership_breakdown = {}
    for mkey in ("new", "existing"):
        sub = in_universe_df[in_universe_df["membership"] == mkey].reset_index(drop=True)
        if sub.empty:
            membership_breakdown[mkey] = {"n": 0, "ev_point": None, "point_lift": None}
            continue
        sub_stats = kpi_event_study.compute_stats(sub, base_rate_by_month, PERIOD)
        sub_ev = kpi_event_study.bootstrap_ev_ci(sub, ev_column="ret")
        membership_breakdown[mkey] = {
            "n": sub_stats["n"], "ev_point": sub_ev["point_ev"],
            "ev_ci_low": sub_ev["ci_low"], "ev_ci_high": sub_ev["ci_high"],
            "point_lift": sub_stats.get("point_lift"),
            "lift_ci_low": sub_stats.get("ci_low"), "lift_ci_high": sub_stats.get("ci_high"),
        }

    # 実効評価期間（分散月数・bull/bear分布）
    regime_counts = in_universe_df["regime"].value_counts().to_dict()
    effective_period = {
        "months_spanned": stats["months_spanned"],
        "first_month": str(in_universe_df["month"].min()),
        "last_month": str(in_universe_df["month"].max()),
        "regime_counts": {k: int(v) for k, v in regime_counts.items()},
    }

    params = {
        **base_params,
        "trial": trial,
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
        "batch_trial_discount_note": BATCH_TRIAL_NOTE,
        "membership_breakdown": membership_breakdown,
        "effective_period": effective_period,
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
        f.write("\n## 探索的一次結論（第25周・カタログ§7-N事前登録ルール）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            if ev_ci["point_ev"] is not None
            else "- EV(なし)点推定 = 計算不能\n"
        )
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {BATCH_TRIAL_NOTE}\n")
        f.write("\n## new/existing別（ユニバース入替バイアス分解・§6手順5）\n")
        for mkey in ("new", "existing"):
            b = membership_breakdown[mkey]
            f.write(
                f"- {mkey}: n={b['n']} / EV(なし)点推定={_fmt_pct(b['ev_point'])} / "
                f"lift点推定={b['point_lift']}\n"
            )
        f.write("\n## 実効評価期間\n")
        f.write(
            f"- 分散月数={effective_period['months_spanned']} / "
            f"範囲={effective_period['first_month']}〜{effective_period['last_month']} / "
            f"レジーム分布={effective_period['regime_counts']}\n"
        )
        f.write("\n## 近傍事実・件数注記（§7-N）\n")
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
        f"一次結論={conclusion}"
    )

    return {
        "trial": trial, "kpi_name": kpi_name, "n": stats["n"], "lift": stats.get("point_lift"),
        "ci_low": stats.get("ci_low"), "ci_high": stats.get("ci_high"),
        "ev_point": ev_ci["point_ev"], "ev_ci_low": ev_ci["ci_low"], "ev_ci_high": ev_ci["ci_high"],
        "ev_stop8": stats.get("ev_stop8"),
        "verdict": verdict, "conclusion": conclusion,
        "avg_monthly_n": stats.get("avg_monthly_n"),
        "membership_breakdown": membership_breakdown,
        "effective_period": effective_period,
        "in_universe_df": in_universe_df,
    }


# --- 月次pairedブロックブートストラップ（§7-N凍結・T1vsT3 / T2vsT4） --------------


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.4%}" if x is not None else "-"


def _fmt_ci(lo: Optional[float], hi: Optional[float]) -> str:
    if lo is None or hi is None:
        return "[-, -]"
    return f"[{lo:.4%}, {hi:.4%}]"


def build_paired_month_df(resid_df: pd.DataFrame, raw_df: pd.DataFrame) -> pd.DataFrame:
    """残差版・素版のin_universe_dfから月次群平均paired（両群1件以上の月のみ）を作る（§7-N凍結）。

    各月の両版イベント群平均20bdリターン（cost控除は差分で相殺するため生retで算出）から
    ΔEV_month = mean(resid.ret) − mean(raw.ret) を作る。片方0件の月は除外（0扱いにしない）。

    Returns:
        列 month, diff の DataFrame（bootstrap_ev_ci が month をブロック単位に使う）。
    """
    resid_monthly = resid_df.groupby("month")["ret"].mean()
    raw_monthly = raw_df.groupby("month")["ret"].mean()
    common = sorted(set(resid_monthly.index) & set(raw_monthly.index))
    rows = [{"month": m, "diff": float(resid_monthly[m] - raw_monthly[m])} for m in common]
    return pd.DataFrame(rows)


def compute_paired_report(results: dict) -> dict:
    """T1vsT3（momentum）・T2vsT4（strev）の月次paired ΔEVを算出しHolm補正（§7-J方式）を併記する。"""
    comparisons = {
        "momentum_t1_vs_t3": ("t1", "t3"),
        "strev_t2_vs_t4": ("t2", "t4"),
    }
    out = {}
    for name, (resid_t, raw_t) in comparisons.items():
        paired_df = build_paired_month_df(
            results[resid_t]["in_universe_df"], results[raw_t]["in_universe_df"]
        )
        if paired_df.empty:
            out[name] = {"n_months": 0, "point": None, "ci_low": None, "ci_high": None,
                         "p_boundary": 1.0}
            continue
        ev = kpi_event_study.bootstrap_ev_ci(paired_df, ev_column="diff", cost=0.0)
        p_boundary = kpi_sue_exit_study._p_value_boundary(paired_df, "diff")
        out[name] = {
            "n_months": len(paired_df), "point": ev["point_ev"],
            "ci_low": ev["ci_low"], "ci_high": ev["ci_high"],
            "p_boundary": p_boundary, "paired_df": paired_df,
        }

    # Holm step-down（2比較）: 小さいp値に97.5%CI・他方に95%CI（§7-J凍結手順と同一）
    valid = {k: v for k, v in out.items() if v.get("paired_df") is not None}
    order = sorted(valid.items(), key=lambda kv: kv[1]["p_boundary"])
    level_by_name = {}
    if len(order) >= 1:
        level_by_name[order[0][0]] = kpi_sue_exit_study.HOLM_SMALL_P_CI_LEVEL
    if len(order) >= 2:
        level_by_name[order[1][0]] = kpi_sue_exit_study.HOLM_LARGE_P_CI_LEVEL
    for name, v in valid.items():
        level = level_by_name.get(name, kpi_sue_exit_study.HOLM_LARGE_P_CI_LEVEL)
        r = kpi_event_study.bootstrap_ev_ci(
            v["paired_df"], ev_column="diff", cost=0.0, ci_level=level
        )
        v["holm_ci_level"] = level
        v["holm_ci_low"] = r["ci_low"]
        v["holm_ci_high"] = r["ci_high"]
        v["holm_ci_low_gt_zero"] = bool(r["ci_low"] is not None and r["ci_low"] > 0)
    return out


def write_paired_report(path: Path, paired: dict, results: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 第25周 paired差分レポート（§7-N凍結・月次群平均paired）",
        "",
        "残差版と素版は採用銘柄集合が異なるためポジション単位のpairはせず、各月の両版イベント群平均",
        "20bdリターンの差 ΔEV_month = EV_resid_month − EV_raw_month を月次pairedブロック・ブートストラップ",
        "（両群1件以上の月のみ・片方0件の月は除外）で点推定+95%CIを算出する。コストは差分で相殺。",
        "『残差化の上乗せ』の正式主張にはHolm補正（§7-J方式=小さいp値に97.5%CI・他方95%CI）を要する。",
        "",
        "| 比較 | 対象月数 | ΔEV点推定 | 95%CI | p値境界(両側相当) | Holm CI水準 | Holm補正後CI | 下限>0 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    label = {"momentum_t1_vs_t3": "momentum: T1残差 − T3素", "strev_t2_vs_t4": "strev: T2残差 − T4素"}
    for name in ("momentum_t1_vs_t3", "strev_t2_vs_t4"):
        v = paired[name]
        if v.get("point") is None:
            lines.append(f"| {label[name]} | {v['n_months']} | - | [-, -] | - | - | [-, -] | - |")
            continue
        lines.append(
            f"| {label[name]} | {v['n_months']} | {_fmt_pct(v['point'])} | "
            f"{_fmt_ci(v['ci_low'], v['ci_high'])} | {v['p_boundary']:.4f} | "
            f"{v.get('holm_ci_level', '-')} | {_fmt_ci(v.get('holm_ci_low'), v.get('holm_ci_high'))} | "
            f"{v.get('holm_ci_low_gt_zero')} |"
        )
    lines += [
        "",
        "## 各試行サマリー（参考）",
        "| 試行 | KPI | n | 月平均n | lift[CI] | EV(なし)[CI] | EV(stop8) | verdict | 一次結論 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in TRIAL_ORDER:
        r = results[t]
        lines.append(
            f"| {t.upper()} | {r['kpi_name']} | {r['n']} | "
            f"{r['avg_monthly_n']:.2f} | {r['lift']}[{r['ci_low']},{r['ci_high']}] | "
            f"{_fmt_pct(r['ev_point'])}{_fmt_ci(r['ev_ci_low'], r['ev_ci_high'])} | "
            f"{_fmt_pct(r['ev_stop8'])} | {r['verdict']} | {r['conclusion']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- メイン処理 ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="第25周: 残差リターン系四本バッチ（カタログ§7-N・T1〜T4）"
    )
    parser.add_argument("--trial", default="all", choices=["t1", "t2", "t3", "t4", "all"],
                        help="実行する試行（既定 all）")
    parser.add_argument("--start", default=kpi_pead_signals.IN_SAMPLE_START,
                        help=f"シグナル検索開始月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_START}）")
    parser.add_argument("--end", default=kpi_pead_signals.IN_SAMPLE_END,
                        help=f"シグナル検索終了月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_END}）")
    parser.add_argument("--no-trials-append", action="store_true",
                        help="台帳(trials.jsonl)へ記録しない（smokeテスト用）")
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        raise SystemExit(
            f"FATAL: --end={args.end} はholdout期間(2023年以降)に抵触します。in-sample評価は"
            f"{kpi_pead_signals.IN_SAMPLE_END}まで（Codexレビュー⑳MINOR対応・holdout保護をWARN→FATAL化）"
        )

    trials = TRIAL_ORDER if args.trial == "all" else [args.trial]

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universe_months_sorted = sorted(universes_by_month.keys())

    # 月次リターンパネル（パネル起点は履歴確保のため常に PANEL_START_MONTH）
    print(f"月次リターンパネル構築中（{PANEL_START_MONTH}〜{args.end}）...", file=sys.stderr)
    month_end_bdays = _month_end_bdays(PANEL_START_MONTH, args.end)
    months, month_end_bdays, ret_matrix, codes, topix_ret = build_monthly_return_panel(month_end_bdays)
    print(f"パネル: {len(months)}ヶ月 × {len(codes)}銘柄（{months[0]}〜{months[-1]}）", file=sys.stderr)

    # シグナル検索対象の月位置範囲
    start_key = args.start.replace("-", "")
    end_key = args.end.replace("-", "")
    sig_positions = [p for p, m in enumerate(months) if start_key <= m <= end_key]
    if not sig_positions:
        raise SystemExit("FATAL: 指定期間にシグナル対象月がありません")
    p_start, p_end = sig_positions[0], sig_positions[-1]

    # 各月位置の decile を一度だけ計算（4試行で共有・前月参照のため p_start-1 から）
    print(f"月次OLS残差スコア・decile計算中（位置{p_start - 1}〜{p_end}）...", file=sys.stderr)
    decile_cache: dict[int, Optional[dict]] = {}
    for p in range(p_start - 1, p_end + 1):
        if p < 0:
            continue
        decile_cache[p] = compute_position_deciles(
            p, months, ret_matrix, topix_ret, codes, universes_by_month, universe_months_sorted
        )

    harness_ctx = {
        "all_bdays": all_bdays, "bday_index": bday_index, "regime_by_day": regime_by_day,
        "base_rate_by_month": base_rate_by_month, "universes_by_month": universes_by_month,
        "universe_months_sorted": universe_months_sorted,
    }

    results: dict = {}
    for trial in trials:
        kpi_name = TRIAL_DEFS[trial]["kpi_name"]
        print(f"\n=== {trial.upper()} {kpi_name}: シグナル生成 ===", file=sys.stderr)
        signals_df, gen_diag = generate_cross_signals(
            trial, p_start, p_end, months, month_end_bdays, decile_cache
        )
        monthly = gen_diag["monthly"]
        eligible_months = [m for m in monthly if m["n_universe"]]
        excl_rates = [
            1 - m["n_eligible"] / m["n_universe"] for m in eligible_months if m["n_universe"]
        ]
        total_sig = int(signals_df.shape[0])
        print(
            f"{trial.upper()} 生成: {total_sig}件 / 対象月={len(monthly)} / "
            f"履歴不足除外率(平均)={np.mean(excl_rates):.1%} "
            f"[{np.min(excl_rates):.1%}, {np.max(excl_rates):.1%}]"
            if excl_rates else f"{trial.upper()} 生成: {total_sig}件",
            file=sys.stderr,
        )
        if signals_df.empty:
            raise SystemExit(f"FATAL: {trial} のシグナルが0件です")

        kpi_dir = OUTPUT_ROOT / kpi_name
        kpi_dir.mkdir(parents=True, exist_ok=True)
        signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

        filtered_df, n_raw, n_filtered = kpi_round23_signals.prefilter_in_universe(
            signals_df, universes_by_month
        )
        if filtered_df.empty:
            raise SystemExit(f"FATAL: {trial} の事前フィルタ後シグナルが0件です")

        base_params = {
            "signal_definition": (
                f"TOPIX 1ファクター(CAPM)残差 r_i=α+β·r_TOPIX+ε（月t-36〜t-1の36観測OLS）に基づく"
                f"{TRIAL_DEFS[trial]['kind']}スコアの上位decile(≤ceil(N×0.1))新規入り。"
                f"前月非該当∧当月該当∧両月計算可能で発火。シグナル確定=月末T終値・エントリー=T+1寄付"
            ),
            "score_formula": {
                "t1": "Σε[t-12..t-2] ÷ std(同11残差,ddof=1)",
                "t2": "−ε[t-1] ÷ std(全36残差,ddof=1)",
                "t3": "Σr[t-12..t-2]（素・σ標準化なし）",
                "t4": "−r[t-1]（素・σ標準化なし）",
            }[trial],
            "reg_window": REG_WINDOW,
            "decile_frac": DECILE_FRAC,
            "monthly_return_rule": "r=AdjC(月末)/AdjC(前月末)-1（単純・無リスク金利0近似）",
            "prev_state_rule": "前月状態は前月自身のOLS（窓t-37〜t-2）から別途算出（遡及使用禁止）",
            "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
            "defer_rationale": "§6手順6『S高で買えない日は翌日繰り延べ(第5周以降既定)』に従いdefer_entry=True",
            "neighbor_note": NEIGHBOR_NOTE,
            "monthly_exclusion_diag": {
                "n_months": len(eligible_months),
                "excl_rate_mean": float(np.mean(excl_rates)) if excl_rates else None,
                "excl_rate_min": float(np.min(excl_rates)) if excl_rates else None,
                "excl_rate_max": float(np.max(excl_rates)) if excl_rates else None,
            },
            "raw_signal_count_full": n_raw,
            "harness_input_count": n_filtered,
            "prefilter_note": (
                "ユニバース事前フィルタ適用(第20周§7-I / 第23周T2前例・統計結果不変)。"
                "シグナルは構成上すべて前月末確定ユニバース内のため除去は基本0件"
            ),
        }
        report_extra = [
            NEIGHBOR_NOTE,
            (
                f"生シグナル数={n_raw}件 / 事前フィルタ後={n_filtered}件"
                f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変)。"
            ),
            (
                f"履歴不足の月別除外率: 平均={np.mean(excl_rates):.1%} "
                f"[{np.min(excl_rates):.1%}, {np.max(excl_rates):.1%}]（対象月={len(eligible_months)}）。"
                if excl_rates else "履歴不足除外率: 計算対象月なし"
            ),
        ]

        results[trial] = run_trial(
            trial, filtered_df, base_params, harness_ctx, report_extra,
            append_to_ledger=not args.no_trials_append,
        )

    # paired差分（全4試行を実行したときのみ）
    if set(trials) == set(TRIAL_ORDER):
        print("\n=== paired差分（T1vsT3・T2vsT4）計算中 ===", file=sys.stderr)
        paired = compute_paired_report(results)
        paired_path = OUTPUT_ROOT / "round25_paired_report.md"
        write_paired_report(paired_path, paired, results)
        print(f"paired差分レポート: {paired_path}")
        for name in ("momentum_t1_vs_t3", "strev_t2_vs_t4"):
            v = paired[name]
            print(
                f"  {name}: 対象月={v['n_months']} ΔEV点推定={_fmt_pct(v.get('point'))} "
                f"95%CI={_fmt_ci(v.get('ci_low'), v.get('ci_high'))} "
                f"p境界={v.get('p_boundary')} Holm補正後CI下限>0={v.get('holm_ci_low_gt_zero')}"
            )

    print("\n=== 第25周 完了サマリー ===")
    for trial in trials:
        r = results[trial]
        b_new = r["membership_breakdown"]["new"]
        b_exi = r["membership_breakdown"]["existing"]
        print(
            f"{r['kpi_name']}: n={r['n']} 月平均n={r['avg_monthly_n']:.2f} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={_fmt_pct(r['ev_point'])}{_fmt_ci(r['ev_ci_low'], r['ev_ci_high'])} "
            f"EV(stop8)={_fmt_pct(r['ev_stop8'])} verdict(§6)={r['verdict']} 一次結論={r['conclusion']} "
            f"| new n={b_new['n']}(EV={_fmt_pct(b_new['ev_point'])}) "
            f"existing n={b_exi['n']}(EV={_fmt_pct(b_exi['ev_point'])})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
