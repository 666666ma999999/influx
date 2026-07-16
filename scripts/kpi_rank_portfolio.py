#!/usr/bin/env python3
"""§7-AD 型転換v1: 事前固定8因子の等ウェイト合成ランキング・ポートフォリオ バックテスト。

docs/stock-algo-kpi-catalog.md の §7-AD（凍結）を機械適用する。単発イベントの+20%二値検証
（kpi_event_study ハーネス）とは評価軸が異なる「弱シグナル合成→横断ランキング→上位分散保有」型
のため、run_event_study は使わずここに月次リバランス・バックテストを実装する。ただし
**因子の as-of 読み・ユニバース構築・バーデータ読み込みは既存 Canonical を再利用**する
（再実装禁止・§7-AD凍結）。

因子（凍結・8本）:
  F1 volume_shock = Va(D)/mean(直近20回の有効Va・D自身を含まない)  … kpi_volshock_signals の倍率定義と同一数式
  F2 dev200       = C(D)/SMA200(D)-1                              … kpi_sue_champion_signals.compute_dev200 を import
  F3 quiet_ratio  = mean(Va[D-10..D-1])/mean(Va[D-30..D-11])       … kpi_volshock_v2_amplifiers.compute_quiet_ratio を import
  F4 sue_cont     = (実績OP-直前会社予想OP)/|直前予想OP|            … kpi_event_batch_signals.generate_sue_beat_signals を閾値開放で連続値化
  F5 sales_cont   = Sales/直前FSales-1                             … kpi_round33_signals.generate_sales_beat_signals を閾値開放で連続値化
  F6 guidance_cont= NxFSales/当期Sales-1                           … kpi_round35_signals.generate_guidance_fy_strong_signals を閾値開放で連続値化
  F7 opmargin_yoy = 営業利益率のYoY改善幅(pt)                       … kpi_round26_signals.generate_margin_expand_signals を閾値開放で連続値化
  F8 strev        = -(C(D)/C(D_prev)-1)  (D_prev=前回リバランス日)  … 第25周§7-N raw_strev と同型・同一数式

「閾値開放」の意味（Codex64「閾値なし連続版」の実装）: F4/F5 は threshold=-inf を渡し、
F6/F7 は凍結閾値定数(T1_MIN_GROWTH / OPM_DELTA_MIN_PT)を実行時に -inf へ差し替えて呼び出す。
これによりシグナル閾値のみを外し、各参照モジュールの as-of 機構・構造フィルタ・会計年度キー・
遡り上限・訂正再発火ロジック等は凍結定義のまま流用する（＝再実装ゼロ）。各開示イベントの
連続値カラム(beat_pct / growth_pct / opm_delta_pt)を disclosed_date でインデックスし、
各リバランス日Dで「disclosed_date <= D の直近開示値」を as-of 読みする。

標準化・売買時点・コスト・成功基準は全て §7-AD 凍結値の機械適用（実装裁量なし）。

Usage:
    python3 scripts/kpi_rank_portfolio.py                     # 本実行(2016-11〜2026-06・台帳3行append)
    python3 scripts/kpi_rank_portfolio.py --start 2016-11 --end 2017-06 --no-trials-append   # smoke
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402
import measure_base_rate as mbr  # noqa: E402
import kpi_event_study as kes  # noqa: E402  (append_trial / DEFAULT_TRIALS_PATH を再利用)
import kpi_sue_champion_signals as kscs  # noqa: E402  (F2: compute_dev200)
import kpi_volshock_v2_amplifiers as kva  # noqa: E402  (F3: compute_quiet_ratio)
import kpi_event_batch_signals as keb  # noqa: E402  (F4: generate_sue_beat_signals)
import kpi_round33_signals as kr33  # noqa: E402  (F5: generate_sales_beat_signals)
import kpi_round35_signals as kr35  # noqa: E402  (F6: generate_guidance_fy_strong_signals)
import kpi_round26_signals as kr26  # noqa: E402  (F7: generate_margin_expand_signals)
import kpi_uprev_signals as kus  # noqa: E402  (FINS_HISTORY_START_BD)

# --- §7-AD 凍結パラメータ（以後変更禁止・事後調整禁止） -----------------------------
UNIVERSE_WINDOW = 21          # TOP500 の売買代金トレーリング窓（既存 base_rate universes_w21 と同一）
UNIVERSE_TOP_N = 500          # TOP500 ユニバース
VOL_HISTORY_WINDOW = 20       # F1: 直近何回の有効Va平均で割るか（kpi_volshock_signals.VOL_HISTORY_WINDOW と同一）
PORT_TOP_N = 50               # 上位50銘柄・等ウェイト
Z_CLIP = 3.0                  # z を [-3, +3] にクリップ
MIN_VALID_FACTORS = 6         # 有効因子数 6/8 未満の銘柄は当月除外
N_FACTORS = 8                 # 常に8で除す
ONE_WAY_COST = 0.0015         # 片道0.15%（判定用）
ONE_WAY_COST_SENS = 0.0030    # 片道0.30%（判定不使用・感度併記）
NW_LAG = 3                    # Newey-West lag=3 固定
PERIOD_START = "2016-11"
PERIOD_END = "2026-06"

FACTOR_NAMES = [
    "F1_volshock", "F2_dev200", "F3_quiet", "F4_sue",
    "F5_sales", "F6_guidance", "F7_opmargin", "F8_strev",
]


# --- 価格アクセス（AdjO/AdjC・欠損フォールバック） ---------------------------------


def _price_on(day: str, code: str, prefer: str) -> Optional[float]:
    """day の code の価格を返す。prefer('open'/'close') を優先し、無ければもう一方で代替。"""
    rec = mbr.load_bars_day(day).get(code)
    if rec is None:
        return None
    if prefer == "open":
        return rec.get("AdjO") or rec.get("AdjC")
    return rec.get("AdjC") or rec.get("AdjO")


def _adjc(day: str, code: str) -> Optional[float]:
    rec = mbr.load_bars_day(day).get(code)
    return rec.get("AdjC") if rec else None


# --- F1/F8（同一数式で実装・バー系） ----------------------------------------------


def compute_vol_shock(code: str, d: str, bday_index: dict[str, int], all_bdays: list[str]) -> Optional[float]:
    """F1 = Va(D)/mean(直近20回の有効Va観測値・D自身を含まない)。

    kpi_volshock_signals の出来高倍率定義と同一数式（va_hist は当日判定より後に更新される＝
    判定時点の hist は D より厳密に前の直近20回の有効Va。ここでも D を含めず D-1 から遡って
    有効Vaを20件集める）。20件に満たない銘柄・時期は None（欠損）。
    """
    idx = bday_index[d]
    va_d = mbr.load_bars_day(d).get(code, {}).get("Va")
    if not va_d:
        return None
    hist: list[float] = []
    i = idx - 1
    while i >= 0 and len(hist) < VOL_HISTORY_WINDOW:
        v = mbr.load_bars_day(all_bdays[i]).get(code, {}).get("Va")
        if v:
            hist.append(v)
        i -= 1
    if len(hist) < VOL_HISTORY_WINDOW:
        return None
    va_avg = sum(hist) / len(hist)
    if va_avg <= 0:
        return None
    return va_d / va_avg


def compute_strev(code: str, d: str, d_prev: str) -> Optional[float]:
    """F8 = -(C(D)/C(D_prev)-1)（月次リバーサル・負け組買いの正方向）。C=AdjC。"""
    c_d = _adjc(d, code)
    c_prev = _adjc(d_prev, code)
    if c_d is None or c_prev is None or c_prev <= 0:
        return None
    return -(c_d / c_prev - 1.0)


# --- F4〜F7 as-of シリーズ構築（既存ジェネレータを閾値開放して連続値化・再実装なし） -----


def _build_asof_series(df: pd.DataFrame, value_col: str) -> dict[str, tuple[list[int], list[float]]]:
    """イベントDataFrame(列: code, disclosed_date, value_col) を
    code -> (昇順の disclosed_date[int] リスト, 対応 value リスト) に変換する（as-of 二分探索用）。
    同一 disclosed_date が複数ある場合は後勝ち（最新値で上書き）。
    """
    series: dict[str, dict[int, float]] = {}
    if df is None or df.empty:
        return {}
    for code, dd, val in zip(df["code"].astype(str), df["disclosed_date"].astype(str), df[value_col]):
        if val is None or (isinstance(val, float) and np.isnan(val)):
            continue
        series.setdefault(code, {})[int(dd)] = float(val)
    out: dict[str, tuple[list[int], list[float]]] = {}
    for code, dmap in series.items():
        dates = sorted(dmap)
        out[code] = (dates, [dmap[dt] for dt in dates])
    return out


def _asof_value(series: dict, code: str, d: str) -> Optional[float]:
    """disclosed_date <= d の直近開示値を返す（as-of 読み）。無ければ None。"""
    entry = series.get(code)
    if entry is None:
        return None
    dates, vals = entry
    di = int(d)
    pos = bisect.bisect_right(dates, di)  # dates[pos-1] <= di < dates[pos]
    if pos == 0:
        return None
    return vals[pos - 1]


def build_fins_asof_series(fe_start: str, fe_end: str, all_bdays: list[str], bday_index: dict[str, int]) -> dict:
    """F4〜F7 の as-of シリーズを一括構築する（凍結ジェネレータを閾値開放で連続値化）。"""
    series: dict[str, dict] = {}

    # F4 sue_cont: threshold=-inf で全 forecast_pair_established イベントを beat_pct 付きで取得。
    #   参照モジュールは forecast_before<=0 を deficit として除外するため、残る全イベントは
    #   forecast_before>0 ＝ beat_pct == (実績-予想)/|予想|（§7-AD F4 の絶対値分母と一致）。
    df4, _ = keb.generate_sue_beat_signals(fe_start, fe_end, threshold=float("-inf"))
    series["F4_sue"] = _build_asof_series(df4, "beat_pct")

    # F5 sales_cont: 同型（threshold=-inf）。返り値は3-tuple。
    df5, _, _ = kr33.generate_sales_beat_signals(fe_start, fe_end, threshold=float("-inf"))
    series["F5_sales"] = _build_asof_series(df5, "beat_pct")

    # F6 guidance_cont: 凍結閾値 T1_MIN_GROWTH を実行時に -inf へ差し替え（＝閾値なし連続版）。
    #   構造フィルタ(両者>0・変則決算ガード)は凍結のまま。value=growth_pct。
    saved_growth = kr35.T1_MIN_GROWTH
    try:
        kr35.T1_MIN_GROWTH = float("-inf")
        df6, _ = kr35.generate_guidance_fy_strong_signals(fe_start, fe_end, all_bdays, bday_index)
    finally:
        kr35.T1_MIN_GROWTH = saved_growth
    series["F6_guidance"] = _build_asof_series(df6, "growth_pct")

    # F7 opmargin_yoy: 凍結閾値 OPM_DELTA_MIN_PT を -inf へ差し替え（passing = s_q>s_qyoy のみ・
    #   増収は凍結定義の構造条件として保持）。value=opm_delta_pt（pt単位・zスコアのスケール不変）。
    saved_opm = kr26.OPM_DELTA_MIN_PT
    try:
        kr26.OPM_DELTA_MIN_PT = float("-inf")
        df7, _, _, _ = kr26.generate_margin_expand_signals(
            fe_start, fe_end, kus.FINS_HISTORY_START_BD
        )
    finally:
        kr26.OPM_DELTA_MIN_PT = saved_opm
    series["F7_opmargin"] = _build_asof_series(df7, "opm_delta_pt")

    return series


# --- 因子行列・スコア構築（§7-AD 標準化 凍結） -------------------------------------


def compute_factor_matrix(
    codes: list[str],
    d: str,
    d_prev: Optional[str],
    bday_index: dict[str, int],
    all_bdays: list[str],
    fins_series: dict,
) -> pd.DataFrame:
    """リバランス日Dのユニバース codes について 8 因子の生値行列を返す（欠損は NaN）。"""
    rows = []
    for code in codes:
        f1 = compute_vol_shock(code, d, bday_index, all_bdays)
        f2 = kscs.compute_dev200(code, d, bday_index, all_bdays)
        f3 = kva.compute_quiet_ratio(code, d, bday_index, all_bdays)
        f8 = compute_strev(code, d, d_prev) if d_prev is not None else None
        f4 = _asof_value(fins_series["F4_sue"], code, d)
        f5 = _asof_value(fins_series["F5_sales"], code, d)
        f6 = _asof_value(fins_series["F6_guidance"], code, d)
        f7 = _asof_value(fins_series["F7_opmargin"], code, d)
        rows.append({
            "code": code,
            "F1_volshock": f1, "F2_dev200": f2, "F3_quiet": f3, "F4_sue": f4,
            "F5_sales": f5, "F6_guidance": f6, "F7_opmargin": f7, "F8_strev": f8,
        })
    return pd.DataFrame(rows).set_index("code")


def standardize_and_score(raw: pd.DataFrame) -> pd.DataFrame:
    """§7-AD 凍結の標準化。各因子を横断zスコア(ddof=0)→[-3,3]クリップ、σ=0の因子は当月無効(z=0)、
    欠損はz=0で補完し常に8で除す。有効因子数<6の銘柄は除外用フラグを立てる。

    Returns:
        index=code、列 z_F* / valid_count / composite / z_F2only / z_F8only / eligible(bool)。
    """
    out = pd.DataFrame(index=raw.index)
    z_cols = []
    valid_mask = pd.DataFrame(index=raw.index)
    for f in FACTOR_NAMES:
        col = raw[f].astype(float)
        present = col.notna()
        active = False
        if present.sum() > 0:
            mu = col[present].mean()
            sd = col[present].std(ddof=0)
            if sd is not None and sd > 0:
                active = True
                z = (col - mu) / sd
                z = z.clip(lower=-Z_CLIP, upper=Z_CLIP)
                z = z.where(present, 0.0)  # 欠損は z=0（中立）
            else:
                z = pd.Series(0.0, index=raw.index)  # σ=0: 当月この因子は無効
        else:
            z = pd.Series(0.0, index=raw.index)
        out[f"z_{f}"] = z
        z_cols.append(f"z_{f}")
        # 有効 = その銘柄で raw 非欠損 かつ 因子が当月アクティブ(σ>0)
        valid_mask[f] = present & active
    out["valid_count"] = valid_mask.sum(axis=1).astype(int)
    out["composite"] = out[z_cols].sum(axis=1) / N_FACTORS  # 常に8で除す
    out["z_F2only"] = out["z_F2_dev200"]
    out["z_F8only"] = out["z_F8_strev"]
    out["eligible"] = out["valid_count"] >= MIN_VALID_FACTORS
    # 単因子構成の適格性: その因子が当月アクティブかつ非欠損
    out["eligible_f2"] = valid_mask["F2_dev200"]
    out["eligible_f8"] = valid_mask["F8_strev"]
    return out


# --- ポートフォリオ・リターン計算 -------------------------------------------------


def _holding_returns(codes: list[str], buy_day: str, sell_day: str, hold_days: list[str]) -> dict[str, float]:
    """buy_day 寄付→sell_day 寄付の各銘柄リターン。売却時に価格が無ければ保有窓内最終AdjCで代替。"""
    rets = {}
    for code in codes:
        bp = _price_on(buy_day, code, "open")
        if bp is None or bp <= 0:
            continue  # 買えない銘柄は保有対象外（等ウェイトは約定できた銘柄で再正規化）
        sp = _price_on(sell_day, code, "open")
        if sp is None:
            # 上場廃止等: 保有窓内で最後に観測された AdjC で決済
            last = None
            for dd in hold_days:
                c = _adjc(dd, code)
                if c is not None:
                    last = c
            sp = last
        if sp is None or sp <= 0:
            continue
        rets[code] = sp / bp - 1.0
    return rets


def _turnover(w_pretrade: dict[str, float], w_new: dict[str, float]) -> float:
    """回転率 = 0.5 * Σ|w_new - w_pretrade|（両集合の和で評価）。"""
    codes = set(w_pretrade) | set(w_new)
    return 0.5 * sum(abs(w_new.get(c, 0.0) - w_pretrade.get(c, 0.0)) for c in codes)


def _drift_weights(w: dict[str, float], rets: dict[str, float]) -> dict[str, float]:
    """保有ウェイトを各銘柄リターンでドリフトさせ再正規化した pre-trade ウェイトを返す。"""
    if not w:
        return {}
    grown = {c: w[c] * (1.0 + rets.get(c, 0.0)) for c in w}
    tot = sum(grown.values())
    if tot <= 0:
        return {}
    return {c: v / tot for c, v in grown.items()}


# --- 統計 -----------------------------------------------------------------------


def newey_west_tstat(x: np.ndarray, lag: int) -> Optional[float]:
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return None
    mu = x.mean()
    e = x - mu
    gamma0 = float(e @ e) / n
    s = gamma0
    for l in range(1, min(lag, n - 1) + 1):
        w = 1.0 - l / (lag + 1)
        cov = float(e[l:] @ e[:-l]) / n
        s += 2.0 * w * cov
    if s <= 0:
        return None
    se = np.sqrt(s / n)
    if se == 0:
        return None
    return float(mu / se)


def max_drawdown(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return 0.0
    eq = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    return float(dd.min())


def summarize_series(excess: np.ndarray, port_net: np.ndarray, bench_net: np.ndarray) -> dict:
    excess = np.asarray(excess, dtype=float)
    n = len(excess)
    if n == 0:
        return {"n": 0}
    mean_ex = float(excess.mean())
    sd_ex = float(excess.std(ddof=1)) if n > 1 else 0.0
    nw_t = newey_west_tstat(excess, NW_LAG)
    ir = (mean_ex / sd_ex * np.sqrt(12)) if sd_ex > 0 else None
    return {
        "n": n,
        "mean_excess": mean_ex,
        "nw_t": nw_t,
        "ir": ir,
        "win_rate": float((excess > 0).mean()),
        "maxdd_port": max_drawdown(port_net),
        "maxdd_bench": max_drawdown(bench_net),
        "maxdd_diff": max_drawdown(port_net) - max_drawdown(bench_net),
    }


# --- バックテスト本体 -----------------------------------------------------------


def run_backtest(start_month: str, end_month: str) -> dict:
    calendar_days = mbr.load_calendar_days()
    all_bdays = mbr.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    topix = mbr.load_topix_series()
    regime_by_day = mbr.build_regime_series(topix)

    rebal_dates = mbr.month_ends_in_range(calendar_days, start_month, end_month)
    if len(rebal_dates) < 2:
        raise SystemExit(f"FATAL: リバランス月末が不足しています（{len(rebal_dates)}件）")

    # F4〜F7 as-of シリーズを一括構築（イベント範囲 = fins先頭 〜 最終リバランス日）。
    fe_start = kus.FINS_HISTORY_START_BD
    fe_end = rebal_dates[-1]
    print(f"[fins] as-of シリーズ構築中 {fe_start}..{fe_end} ...", flush=True)
    fins_series = build_fins_asof_series(fe_start, fe_end, all_bdays, bday_index)
    for f in ("F4_sue", "F5_sales", "F6_guidance", "F7_opmargin"):
        print(f"[fins] {f}: {len(fins_series[f])} codes", flush=True)

    configs = ["composite", "f2only", "f8only"]
    # 各構成の前月ウェイト状態
    prev_w = {c: {} for c in configs}
    records = {c: [] for c in configs}  # 月次レコード
    universe_sizes = []
    bench_prev: dict[str, float] = {}   # ベンチマーク(TOP500等ウェイト)の前月ウェイト
    bench_turnovers: list[float] = []

    # 形成は rebal_dates[0..-2]、売却は次リバランス日+1寄付（最終 rebal は退出のみ）。
    for i in range(len(rebal_dates) - 1):
        d = rebal_dates[i]
        d_next = rebal_dates[i + 1]
        d_prev = rebal_dates[i - 1] if i > 0 else None
        buy_day = all_bdays[bday_index[d] + 1]
        sell_day = all_bdays[bday_index[d_next] + 1]
        hold_days = all_bdays[bday_index[buy_day]: bday_index[sell_day] + 1]
        regime = regime_by_day.get(d, "unknown")
        year = d[:4]

        selected, _ustats = mbr.build_universe(d, bday_index, all_bdays, UNIVERSE_WINDOW, UNIVERSE_TOP_N)
        uni_codes = [c for c, _tv in selected]
        universe_sizes.append(len(uni_codes))

        raw = compute_factor_matrix(uni_codes, d, d_prev, bday_index, all_bdays, fins_series)
        scored = standardize_and_score(raw)

        # ユニバース等ウェイト・ベンチマークの各銘柄リターン（同一時点・同一コスト規約）
        uni_rets = _holding_returns(uni_codes, buy_day, sell_day, hold_days)
        priceable = [c for c in uni_codes if c in uni_rets]

        # ベンチマーク（TOP500等ウェイト）回転率: pre-trade=前月ユニバースのドリフト後（config非依存）。
        bench_new = {c: 1.0 / len(priceable) for c in priceable} if priceable else {}
        bench_gross = sum(bench_new[c] * uni_rets[c] for c in priceable) if priceable else 0.0
        bench_prev_rets = _holding_returns(list(bench_prev.keys()), buy_day, sell_day, hold_days) if bench_prev else {}
        bench_pretrade = _drift_weights(bench_prev, bench_prev_rets)
        bench_turnovers.append(_turnover(bench_pretrade, bench_new))
        bench_prev = bench_new

        for cfg in configs:
            if cfg == "composite":
                elig = scored[scored["eligible"]]
                score_col = "composite"
            elif cfg == "f2only":
                elig = scored[scored["eligible_f2"]]
                score_col = "z_F2only"
            else:
                elig = scored[scored["eligible_f8"]]
                score_col = "z_F8only"
            # 約定可能な銘柄のみ選択対象（買えない銘柄は保有できない）
            elig = elig[elig.index.isin(priceable)]
            ranked = elig.sort_values([score_col], ascending=False)
            # tie は銘柄コード昇順で決定的に切る（sort は安定・index昇順を副キーに）
            ranked = ranked.reset_index().sort_values(
                [score_col, "code"], ascending=[False, True]
            ).set_index("code")
            top = ranked.head(PORT_TOP_N)
            bottom = ranked.tail(PORT_TOP_N)
            top_codes = list(top.index)

            if not top_codes:
                # 適格銘柄ゼロ（データウォームアップ初期等）→ 当月はポジション無し（系列から除外）
                records[cfg].append({
                    "rebal_date": d, "buy_day": buy_day, "sell_day": sell_day,
                    "regime": regime, "year": year, "n_holdings": 0,
                    "port_gross": None, "bench_gross": None, "turnover": 0.0,
                    "turnover_bench": 0.0, "excess_net": None, "excess_net_sens": None,
                    "spread": None,
                })
                prev_w[cfg] = {}
                continue

            w_new = {c: 1.0 / len(top_codes) for c in top_codes}
            port_gross = sum(w_new[c] * uni_rets[c] for c in top_codes)

            # 回転率: 前月ウェイトを当月保有リターンでドリフト → pre-trade。初回は空(全額片道)。
            prev = prev_w[cfg]
            # 前月保有の当月リターン（ドリフト用）。前月銘柄が当月ユニバース外でもリターンは
            # 実際の価格変化で評価する（保有していた実体があるため）。
            prev_rets = _holding_returns(list(prev.keys()), buy_day, sell_day, hold_days) if prev else {}
            w_pretrade = _drift_weights(prev, prev_rets)
            turnover = _turnover(w_pretrade, w_new)
            prev_w[cfg] = w_new

            spread = None
            if len(top) > 0 and len(bottom) > 0:
                top_r = np.mean([uni_rets[c] for c in top.index if c in uni_rets])
                bot_r = np.mean([uni_rets[c] for c in bottom.index if c in uni_rets])
                spread = float(top_r - bot_r)

            records[cfg].append({
                "rebal_date": d, "buy_day": buy_day, "sell_day": sell_day,
                "regime": regime, "year": year, "n_holdings": len(top_codes),
                "port_gross": port_gross, "bench_gross": bench_gross,
                "turnover": turnover, "excess_net": None, "excess_net_sens": None,
                "spread": spread,
            })

    # コスト計上して純超過リターン系列を確定（最終月に退出コスト加算）。
    results = {}
    for cfg in configs:
        recs = records[cfg]
        n = len(recs)
        for j, r in enumerate(recs):
            if r["port_gross"] is None:
                continue
            is_last = j == n - 1
            for cost_key, oneway in (("excess_net", ONE_WAY_COST), ("excess_net_sens", ONE_WAY_COST_SENS)):
                port_cost = 2.0 * oneway * r["turnover"]
                bench_cost = 2.0 * oneway * bench_turnovers[j]
                if is_last:
                    port_cost += oneway  # 退出コスト（全額片道売却）
                    bench_cost += oneway
                port_net = r["port_gross"] - port_cost
                bench_net = r["bench_gross"] - bench_cost
                r[cost_key] = port_net - bench_net
                if cost_key == "excess_net":
                    r["port_net"] = port_net
                    r["bench_net"] = bench_net
        results[cfg] = recs

    return {
        "configs": configs,
        "results": results,
        "rebal_dates": rebal_dates,
        "universe_sizes": universe_sizes,
        "bench_turnovers": bench_turnovers,
        "period": (start_month, end_month),
    }


# --- 判定・レポート -------------------------------------------------------------


def compute_config_stats(recs: list[dict]) -> dict:
    valid = [r for r in recs if r.get("excess_net") is not None]
    excess = np.array([r["excess_net"] for r in valid])
    excess_sens = np.array([r["excess_net_sens"] for r in valid])
    port_net = np.array([r["port_net"] for r in valid])
    bench_net = np.array([r["bench_net"] for r in valid])
    turnovers = np.array([r["turnover"] for r in valid])
    spreads = np.array([r["spread"] for r in valid if r["spread"] is not None])

    s = summarize_series(excess, port_net, bench_net)
    s["mean_excess_sens"] = float(excess_sens.mean()) if len(excess_sens) else None
    s["nw_t_sens"] = newey_west_tstat(excess_sens, NW_LAG) if len(excess_sens) else None
    s["avg_turnover"] = float(turnovers.mean()) if len(turnovers) else None
    s["avg_spread"] = float(spreads.mean()) if len(spreads) else None
    s["skipped_months"] = len(recs) - len(valid)

    # レジーム別・年別
    def _sub(pred):
        sub = [r for r in valid if pred(r)]
        if not sub:
            return None
        ex = np.array([r["excess_net"] for r in sub])
        return {"n": len(sub), "mean_excess": float(ex.mean()),
                "nw_t": newey_west_tstat(ex, NW_LAG), "win_rate": float((ex > 0).mean())}
    s["regime_bull"] = _sub(lambda r: r["regime"] == "bull")
    s["regime_bear"] = _sub(lambda r: r["regime"] == "bear")
    years = sorted({r["year"] for r in valid})
    s["by_year"] = {y: _sub(lambda r, yy=y: r["year"] == yy) for y in years}
    return s


def judge_composite(stats: dict) -> tuple[bool, list[str]]:
    """§7-AD 成功基準（判定対象=composite のみ・凍結値の機械適用）:
    コスト後月次超過リターン>0 ∩ NW t値≥2 ∩ IR≥0.5。"""
    mean_ex = stats.get("mean_excess")
    nw_t = stats.get("nw_t")
    ir = stats.get("ir")
    checks = {
        f"コスト後月次超過リターン={mean_ex:.4%} > 0" if mean_ex is not None else "月次超過リターン=None > 0":
            (mean_ex is not None and mean_ex > 0),
        f"NW t値(lag=3)={nw_t:.3f} ≥ 2" if nw_t is not None else "NW t値=None ≥ 2":
            (nw_t is not None and nw_t >= 2.0),
        f"IR(年率)={ir:.3f} ≥ 0.5" if ir is not None else "IR=None ≥ 0.5":
            (ir is not None and ir >= 0.5),
    }
    reasons = [("OK " if v else "NG ") + k for k, v in checks.items()]
    return all(checks.values()), reasons


def _fmt(x, pct=False, nd=4):
    if x is None:
        return "-"
    if pct:
        return f"{x:.{nd}%}"
    return f"{x:.{nd}f}"


def write_report(path: Path, bt: dict, all_stats: dict, verdict: bool, reasons: list[str]) -> None:
    period = bt["period"]
    lines = [
        "# §7-AD 型転換v1 — 8因子等ウェイト合成ランキング・ポートフォリオ",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"評価期間: {period[0]} 〜 {period[1]}（月次リバランス・{len(bt['rebal_dates'])}リバランス月末）",
        f"ユニバース: TOP{UNIVERSE_TOP_N}（window={UNIVERSE_WINDOW}）・保有上位{PORT_TOP_N}・等ウェイト",
        f"コスト: 判定=片道{ONE_WAY_COST:.2%} / 感度併記=片道{ONE_WAY_COST_SENS:.2%}（往復=片道×2×回転）",
        "",
        "> 汚染開示（§7-AD凍結）: 因子選定は第1〜39周・§7-AC結果を知った上で行っており中立でない。",
        "> **全統計量は記述的指標であり確証的p値ではない**（Codex64指示）。確定は前向きのみ。",
        "> F4〜F7 は各参照ジェネレータを閾値開放（threshold=-inf / 凍結閾値定数の実行時-inf差し替え）で",
        "> 連続値化して as-of 読み（再実装なし）。F7 は凍結定義の増収条件(s_q>s_qyoy)を保持した閾値なし版。",
        "",
        "## 3構成 主要指標比較",
        "| 構成 | 月数 | 月次超過(コスト後) | NW t(lag3) | IR(年率) | 月次勝率 | maxDD差 | 平均回転 | 上位-下位spread | 感度0.30%月次超過 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cfg in bt["configs"]:
        s = all_stats[cfg]
        lines.append(
            f"| {cfg} | {s['n']} | {_fmt(s.get('mean_excess'), pct=True)} | "
            f"{_fmt(s.get('nw_t'), nd=3)} | {_fmt(s.get('ir'), nd=3)} | "
            f"{_fmt(s.get('win_rate'), pct=True, nd=1)} | {_fmt(s.get('maxdd_diff'), pct=True, nd=2)} | "
            f"{_fmt(s.get('avg_turnover'), nd=3)} | {_fmt(s.get('avg_spread'), pct=True, nd=2)} | "
            f"{_fmt(s.get('mean_excess_sens'), pct=True)} |"
        )

    lines += ["", "## 成功基準判定（判定対象=composite のみ・§7-AD凍結値の機械適用）", ""]
    lines.append(f"**{'合格（次段階§7-AEへ）' if verdict else '不合格（v1棄却記録）'}**")
    lines += [""] + [f"- {r}" for r in reasons]

    lines += ["", "## 増分比較（対照・合否判定不使用）",
              "composite が単因子(F2/F8)を上回るか＝合成の付加価値を記録する。"]
    comp = all_stats["composite"]
    for cfg in ("f2only", "f8only"):
        s = all_stats[cfg]
        d_ex = (comp.get("mean_excess") or 0) - (s.get("mean_excess") or 0)
        lines.append(
            f"- vs {cfg}: composite月次超過 {_fmt(comp.get('mean_excess'), pct=True)} − "
            f"{cfg} {_fmt(s.get('mean_excess'), pct=True)} = 差 {_fmt(d_ex, pct=True)}"
        )

    for cfg in bt["configs"]:
        s = all_stats[cfg]
        lines += ["", f"## 診断: {cfg}"]
        lines.append(f"- 有効月数 {s['n']} / スキップ月(適格銘柄0) {s.get('skipped_months', 0)}")
        rb, rr = s.get("regime_bull"), s.get("regime_bear")
        if rb:
            lines.append(f"- bull: n={rb['n']} 月次超過={_fmt(rb['mean_excess'], pct=True)} "
                         f"NW t={_fmt(rb['nw_t'], nd=3)} 勝率={_fmt(rb['win_rate'], pct=True, nd=1)}")
        if rr:
            lines.append(f"- bear: n={rr['n']} 月次超過={_fmt(rr['mean_excess'], pct=True)} "
                         f"NW t={_fmt(rr['nw_t'], nd=3)} 勝率={_fmt(rr['win_rate'], pct=True, nd=1)}")
        lines.append("- 年別:")
        for y, ys in s.get("by_year", {}).items():
            if ys:
                lines.append(f"  - {y}: n={ys['n']} 月次超過={_fmt(ys['mean_excess'], pct=True)} "
                             f"NW t={_fmt(ys['nw_t'], nd=3)} 勝率={_fmt(ys['win_rate'], pct=True, nd=1)}")

    lines += ["", "## 実装ノート",
              "- ユニバース/バー/財務as-of は既存Canonical(measure_base_rate.build_universe / "
              "compute_dev200 / compute_quiet_ratio / 各ジェネレータ)を再利用（再実装なし）",
              "- 標準化: 横断z(ddof=0)→[-3,3]クリップ・σ=0因子は当月無効・欠損z=0・常に8で除す・"
              "有効因子<6/8の銘柄は当月除外・tie=銘柄コード昇順",
              "- 売買: 月末D終値でスコア確定→D+1寄付約定→次リバランスD+1寄付まで保有（ベンチも同一時点）",
              "- 回転率=0.5Σ|w_new-w_pretrade|(pre-trade=前月保有のドリフト後)・初回=全額片道・最終月=退出コスト",
              "- NW t値 lag=3固定・IR=mean/sd×√12・maxDDは純リターン累積の最大drawdown"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_monthly_csv(path: Path, bt: dict) -> None:
    rows = []
    for cfg in bt["configs"]:
        for r in bt["results"][cfg]:
            rows.append({"config": cfg, **r})
    pd.DataFrame(rows).to_csv(path, index=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="§7-AD 型転換v1 ランキングポートフォリオ・バックテスト")
    ap.add_argument("--start", default=PERIOD_START, help="評価開始 YYYY-MM")
    ap.add_argument("--end", default=PERIOD_END, help="評価終了 YYYY-MM")
    ap.add_argument("--output-dir", default="output/kpi_rank_port")
    ap.add_argument("--no-trials-append", action="store_true", help="台帳追記をスキップ（smoke用）")
    ap.add_argument("--trials-path", default=str(kes.DEFAULT_TRIALS_PATH))
    args = ap.parse_args()

    bt = run_backtest(args.start, args.end)
    all_stats = {cfg: compute_config_stats(bt["results"][cfg]) for cfg in bt["configs"]}
    verdict, reasons = judge_composite(all_stats["composite"])

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_report(out_dir / "report.md", bt, all_stats, verdict, reasons)
    write_monthly_csv(out_dir / "monthly_returns.csv", bt)

    print(f"[done] composite: n={all_stats['composite']['n']} "
          f"mean_excess={_fmt(all_stats['composite'].get('mean_excess'), pct=True)} "
          f"NW_t={_fmt(all_stats['composite'].get('nw_t'), nd=3)} "
          f"IR={_fmt(all_stats['composite'].get('ir'), nd=3)} verdict={'PASS' if verdict else 'FAIL'}", flush=True)
    for cfg in ("f2only", "f8only"):
        s = all_stats[cfg]
        print(f"[done] {cfg}: n={s['n']} mean_excess={_fmt(s.get('mean_excess'), pct=True)} "
              f"NW_t={_fmt(s.get('nw_t'), nd=3)} IR={_fmt(s.get('ir'), nd=3)}", flush=True)

    if not args.no_trials_append:
        trials_path = Path(args.trials_path)
        for cfg in bt["configs"]:
            s = all_stats[cfg]
            rec = {
                "run_id": uuid.uuid4().hex,
                "ts": jq_fetch.now_jst().isoformat(),
                "kpi_name": f"rank_port_v1_{cfg}",
                "trial_type": "rank_portfolio_v1_7AD",
                "params": {
                    "universe": f"TOP{UNIVERSE_TOP_N}", "window": UNIVERSE_WINDOW,
                    "top_n": PORT_TOP_N, "factors": N_FACTORS, "min_valid_factors": MIN_VALID_FACTORS,
                    "z_clip": Z_CLIP, "one_way_cost": ONE_WAY_COST, "nw_lag": NW_LAG,
                    "judged": cfg == "composite",
                },
                "period": {"start": args.start, "end": args.end},
                "n": s["n"],
                "mean_excess_monthly": s.get("mean_excess"),
                "nw_t_lag3": s.get("nw_t"),
                "ir_annual": s.get("ir"),
                "win_rate": s.get("win_rate"),
                "maxdd_diff": s.get("maxdd_diff"),
                "avg_turnover": s.get("avg_turnover"),
                "mean_excess_sens_030": s.get("mean_excess_sens"),
                "verdict": ("pass_candidate" if verdict else "rejected") if cfg == "composite" else "reference_only",
            }
            kes.append_trial(rec, trials_path)
        print(f"[trials] appended 3 rows → {trials_path}", flush=True)

    print(f"[report] {out_dir / 'report.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
