#!/usr/bin/env python3
"""KPI Exit設計ラウンド（第12周・カタログ§7-A「負け側の本丸」）。

固定%損切りが出来高ショック系（深押し→復活型）と構造的に不適合という26試行の確定知見への
直接処方。volshock_x_above200（主・n=180）と volshock_5x（対照母集団・n=292）の既存ポジション
に対し、**エントリー日・エントリー価格は再計算せず** `output/kpi/<kpi>/returns.csv` の
entry_date/entry_price/in_universe をそのまま再利用し、exitルールだけを7通り（対照C1-C3・
新案E1-E4）に差し替えて横並び比較する（同一サンプルでの複数exit比較＝順位付け目的）。

フォワードリターン計算の基盤（bars読み込み・カレンダー・往復コスト定数・リフト/CI再利用）は
scripts/measure_base_rate.py / scripts/kpi_event_study.py の関数・定数をそのまま再利用する
（Canonical Module原則。measure_base_rate.py は§6準拠のため変更禁止）。

Exitルール定義（docs/stock-algo-kpi-catalog.md §7-A）:
    C1: 固定-8%損切り（ギャップダウンは寄付約定・ザラ場タッチは水準約定）
    C2: 固定-10%損切り（同上）
    C3: 損切りなし（20営業日目終値で決済）
    E1: シナリオ崩壊 — 保有中に(前日終値>=SMA200(前日))かつ(当日終値<SMA200(当日))で翌営業日寄付手仕舞い
    E2: ATR適応ストップ — 損切り水準=entry-2*ATR20（ATR20はエントリー前20営業日のTrue Range平均）
    E3: β調整ストップ — 累積超過リターンΣ(r_i-β*r_mkt)が-8%以下になった最初の日の終値で手仕舞い
    E4: 3段階トレーリング — +1ATRで建値ストップ有効化→+2ATRで「最高終値-2ATR」のトレールに切替

Usage:
    docker compose run --rm xstock python scripts/kpi_exit_study.py
"""
from __future__ import annotations

import sys
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT・now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: base_rate/universe読込・bootstrap・judge・append_trialを再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込み・ROUND_TRIP_COSTを再利用)

# --- 本ラウンド固有パラメータ（§7-A事前登録仕様） -------------------------------
FORWARD_WINDOW_BD = measure_base_rate.FORWARD_WINDOW_BD  # 20
ROUND_TRIP_COST = measure_base_rate.ROUND_TRIP_COST  # 0.003
MA200_WINDOW = 200  # E1用SMA200のウィンドウ（D自身を含む直近200回の有効AdjC観測値。kpi_volshock_signals.pyと同一規約）
ATR_WINDOW = 20  # E2/E4用ATR20
ATR_STOP_MULT = 2.0  # E2: entry - ATR_STOP_MULT*ATR20
STAGE1_ATR_MULT = 1.0  # E4: +1ATRで建値ストップ有効化
STAGE2_ATR_MULT = 2.0  # E4: +2ATRでトレール開始・トレール幅も2ATR
BETA_WINDOW = 250  # E3用βの回帰ウィンドウ（エントリー前250営業日）
BETA_STOP_THRESHOLD = -0.08  # E3: 累積超過リターンがこの水準以下で手仕舞い
MIN_BETA_OBS = 60  # 回帰に使う有効ペア日数がこれ未満ならデータ不足とみなしβ=1.0にフォールバック
                    # （新規上場等で250日分の対TOPIX共変動が確保できないケースを想定。約3ヶ月分）
SCAN_BUFFER_BDAYS = 270  # 価格履歴プリロードのウォームアップ（ATR21日・β251日・SMA200warmupの全てを
                          # 単一バッファでカバー。kpi_volshock_signals.pyのWARMUP_BDAYS=260に準拠+若干の余裕）

RULES = ["C1", "C2", "C3", "E1", "E2", "E3", "E4"]

POPULATIONS = {
    "volshock_x_above200": Path("output/kpi/volshock_x_above200/returns.csv"),
    "volshock_5x": Path("output/kpi/volshock_5x/returns.csv"),
}
PERIOD = ("2016-11", "2022-11")  # 既存2母集団と同一のin-sample期間（§6凍結）
LOG_TO_TRIALS_POPULATION = "volshock_x_above200"  # 台帳追記は主母集団のみ（team-lead指示）

OUTPUT_DIR = Path("output/kpi/exit_study")
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21


def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古の営業日(YYYYMMDD)を返す(ハードコードしない)。

    kpi_volshock_signals.py / kpi_high52_signals.py の同名関数と同じ実装を意図的に複製している
    (measure_base_rate.py は§6準拠のため変更禁止・追加関数を足すのは避ける方針に合わせる)。
    """
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- ポジション読み込み（既存エントリー計算の再利用・再計算しない） -----------------


def load_positions(returns_csv_path: Path) -> pd.DataFrame:
    """既存ハーネスが出力済みの returns.csv から in_universe ポジションのみを読み込む。

    entry_date/entry_price はここでは一切再計算しない(既存のdefer-entry込みエントリー確定結果を
    そのまま再利用する。team-lead指示「再計算を最小化せよ」)。
    """
    if not returns_csv_path.exists():
        raise SystemExit(f"FATAL: returns.csv が見つかりません: {returns_csv_path}")
    df = pd.read_csv(
        returns_csv_path,
        dtype={
            "signal_date": str, "code": str, "month": str, "regime": str,
            "entry_date": str, "exit_date": str, "universe_month_used": str,
        },
    )
    df = df[df["in_universe"] == True].reset_index(drop=True)  # noqa: E712 (CSV読込のbool比較)
    if df.empty:
        raise SystemExit(f"FATAL: {returns_csv_path} に in_universe=True の行がありません")
    return df[["signal_date", "code", "month", "regime", "entry_date", "entry_price"]].copy()


# --- 価格履歴の一括プリロード（1営業日1パスの逐次スキャン・全ポジション共通） -------


def build_price_history(
    codes: set[str], scan_days: list[str]
) -> tuple[dict[str, dict[str, dict]], dict[str, dict[str, Optional[float]]]]:
    """codesの各銘柄について、scan_days全体のAdjO/AdjH/AdjL/AdjC辞書と、SMA200系列
    (D自身を含む直近200回の有効AdjC観測値の平均。kpi_volshock_signals.pyのdev200と同一規約)を返す。

    1営業日1パスの逐次スキャン(deque)で構築し、日次barsファイルの重複読み込み・銘柄ごとの
    ウィンドウ再走査を避ける(全ポジション共通で1回だけ実行する)。
    """
    prices: dict[str, dict[str, dict]] = {c: {} for c in codes}
    sma200: dict[str, dict[str, Optional[float]]] = {c: {} for c in codes}
    hist200: dict[str, deque] = {c: deque(maxlen=MA200_WINDOW) for c in codes}
    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        for c in codes:
            rec = bars_d.get(c)
            if rec is not None:
                prices[c][d] = rec
                adjc = rec.get("AdjC")
                if adjc is not None:
                    hist200[c].append(adjc)
            sma200[c][d] = (sum(hist200[c]) / MA200_WINDOW) if len(hist200[c]) == MA200_WINDOW else None
    return prices, sma200


# --- ATR20・β の計算（エントリー前の固定ウィンドウ・1ポジション1回） -----------------


def compute_atr20(code: str, entry_idx: int, all_bdays: list[str], prices: dict) -> Optional[float]:
    """エントリー前20営業日のTrue Range平均。TR = max(H-L, |H-前日C|, |L-前日C|)（Adj系列）。

    データ不足(欠損日はスキップ)で1件もTRが計算できない場合はNone（呼び出し側でE2/E4が
    時間切れexitにフォールバックする）。
    """
    days = all_bdays[max(0, entry_idx - ATR_WINDOW - 1) : entry_idx]
    series = prices.get(code, {})
    trs = []
    for i in range(1, len(days)):
        d, d_prev = days[i], days[i - 1]
        rec, rec_prev = series.get(d), series.get(d_prev)
        if rec is None or rec_prev is None:
            continue
        h, l, c_prev = rec.get("AdjH"), rec.get("AdjL"), rec_prev.get("AdjC")
        if h is None or l is None or c_prev is None:
            continue
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return (sum(trs) / len(trs)) if trs else None


def compute_beta(
    code: str, entry_idx: int, all_bdays: list[str], prices: dict, topix_close: dict[str, float]
) -> tuple[float, bool, int]:
    """エントリー前250営業日の対TOPIX回帰係数β。有効ペア観測数がMIN_BETA_OBS未満ならβ=1.0
    にフォールバックする(§7-A E3「データ不足時は1.0にフォールバック」)。

    Returns:
        (beta, is_fallback, n_obs)
    """
    days = all_bdays[max(0, entry_idx - BETA_WINDOW - 1) : entry_idx]
    series = prices.get(code, {})
    r_stock, r_mkt = [], []
    for i in range(1, len(days)):
        d, d_prev = days[i], days[i - 1]
        rec, rec_prev = series.get(d), series.get(d_prev)
        c = rec.get("AdjC") if rec else None
        c_prev = rec_prev.get("AdjC") if rec_prev else None
        if c is None or c_prev is None:
            continue
        t, t_prev = topix_close.get(d), topix_close.get(d_prev)
        if t is None or t_prev is None:
            continue
        r_stock.append(c / c_prev - 1)
        r_mkt.append(t / t_prev - 1)
    n_obs = len(r_stock)
    if n_obs < MIN_BETA_OBS:
        return 1.0, True, n_obs
    r_stock_arr = np.array(r_stock)
    r_mkt_arr = np.array(r_mkt)
    var_mkt = r_mkt_arr.var(ddof=1)
    if var_mkt == 0:
        return 1.0, True, n_obs
    cov = np.cov(r_stock_arr, r_mkt_arr, ddof=1)[0, 1]
    return float(cov / var_mkt), False, n_obs


# --- ポジション1件の7ルール横並びウォーク（1回の日次パスで全ルールを判定） -------------


def _time_based_exit(fwd_days: list[str], price_series: dict) -> tuple[str, float, str]:
    """20営業日目終値の時間切れexit（上場廃止等で終値が無ければ窓内最後のAdjCで決済）。

    全ルール共通のフォールバック（§7-A「全ルール共通: 20営業日目終値の時間切れexitで必ずクローズ」）。
    measure_base_rate.compute_forward_return_for_code のexit決定ロジックと同一の規約。
    """
    last_seen_adjc = None
    last_seen_day = fwd_days[0]
    entry_rec = price_series.get(fwd_days[0])
    if entry_rec is not None and entry_rec.get("AdjC") is not None:
        last_seen_adjc = entry_rec["AdjC"]
    for d in fwd_days[1:]:
        rec = price_series.get(d)
        if rec is not None and rec.get("AdjC") is not None:
            last_seen_adjc = rec["AdjC"]
            last_seen_day = d
    terminal_day = fwd_days[-1]
    terminal_rec = price_series.get(terminal_day)
    if terminal_rec is not None and terminal_rec.get("AdjC"):
        return terminal_day, terminal_rec["AdjC"], "time"
    return last_seen_day, last_seen_adjc, "time_delisted"


def _walk_static_stop(
    fwd_days: list[str], price_series: dict, threshold: float, last_seen_day: str,
    time_exit: tuple[str, float, str],
) -> tuple[str, float, str]:
    """固定水準の損切りストップ（C1/C2/E2共通）。ギャップダウンは寄付約定・ザラ場タッチは
    水準約定（§7-A共通規約）。発動なしなら時間切れexitにフォールバックする。
    """
    for d in fwd_days:
        if d > last_seen_day:
            break  # 上場廃止後は約定しえない
        rec = price_series.get(d)
        if rec is None:
            continue
        ao, al = rec.get("AdjO"), rec.get("AdjL")
        if ao is not None and ao <= threshold:
            return d, ao, "stop_gap"
        if al is not None and al <= threshold:
            return d, threshold, "stop_touch"
    return time_exit


def _walk_e1_scenario_break(
    fwd_days: list[str], price_series: dict, sma_series: dict, prev_day: str,
    time_exit: tuple[str, float, str],
) -> tuple[str, float, str]:
    """E1: 保有中に(前日終値>=SMA200(前日))かつ(当日終値<SMA200(当日))で翌営業日寄付手仕舞い。

    決済不能日(翌営業日のAdjOが無い)は残り窓内で最初に約定可能な日まで繰り延べる。
    窓の最終日で崩壊を検知した場合は「翌営業日」が窓外になるため時間切れexitを採用する
    (§7-A共通規約「20営業日目終値の時間切れexitで必ずクローズ」)。
    """
    prev_close = price_series.get(prev_day, {}).get("AdjC")
    prev_sma = sma_series.get(prev_day)
    for i, d in enumerate(fwd_days):
        cur_rec = price_series.get(d)
        cur_close = cur_rec.get("AdjC") if cur_rec else None
        cur_sma = sma_series.get(d)
        crossed = (
            prev_close is not None and prev_sma is not None
            and cur_close is not None and cur_sma is not None
            and prev_close >= prev_sma and cur_close < cur_sma
        )
        if crossed:
            if d == fwd_days[-1]:
                return time_exit[0], time_exit[1], "time_cross_on_last_day"
            for d2 in fwd_days[i + 1 :]:
                rec2 = price_series.get(d2)
                if rec2 is not None and rec2.get("AdjO") is not None:
                    return d2, rec2["AdjO"], "cross_exit"
            return time_exit[0], time_exit[1], "time_cross_no_open"
        if cur_close is not None and cur_sma is not None:
            prev_close, prev_sma = cur_close, cur_sma
        # 当日データ欠損(売買停止等)はprev_close/prev_smaを更新せず据え置く(欠損日をスキップ)
    return time_exit


def _walk_e4_trailing(
    fwd_days: list[str], price_series: dict, entry_price: float, atr20: float, last_seen_day: str,
    time_exit: tuple[str, float, str],
) -> tuple[str, float, str]:
    """E4: +1ATRで建値ストップ有効化→+2ATRで「最高終値-2ATR」のトレールに切替。

    ストップは前日終値までに確定した状態を使い、当日の約定判定はC1と同じギャップ規則
    (寄付が水準以下なら寄付約定・ザラ場タッチなら水準約定)。未発動(stage=0のまま)なら
    時間切れexit(20bd終値)にフォールバックする。
    """
    stage = 0  # 0=未発動 / 1=建値ストップ / 2=トレーリング
    floor_price: Optional[float] = None
    max_close_since_stage2: Optional[float] = None
    for i, d in enumerate(fwd_days):
        if d > last_seen_day:
            break
        rec = price_series.get(d)
        if rec is None:
            continue
        ao, al, ac = rec.get("AdjO"), rec.get("AdjL"), rec.get("AdjC")
        if stage > 0 and floor_price is not None:
            if ao is not None and ao <= floor_price:
                return d, ao, f"stage{stage}_gap"
            if al is not None and al <= floor_price:
                return d, floor_price, f"stage{stage}_touch"
        if ac is not None:
            gain = ac - entry_price
            if gain >= STAGE2_ATR_MULT * atr20:
                if stage < 2 or max_close_since_stage2 is None:
                    stage = 2
                    max_close_since_stage2 = ac
                elif ac > max_close_since_stage2:
                    max_close_since_stage2 = ac
                floor_price = max_close_since_stage2 - STAGE2_ATR_MULT * atr20
            elif gain >= STAGE1_ATR_MULT * atr20 and stage < 1:
                stage = 1
                floor_price = entry_price
    return time_exit


def walk_position(
    code: str, entry_date: str, entry_price: float, entry_idx: int, all_bdays: list[str],
    prices: dict, sma200_by_code: dict, topix_close: dict,
    atr20: Optional[float], beta: float,
) -> dict[str, tuple[str, float, str]]:
    """1ポジションについて7ルール全ての(exit_date, exit_price, reason)を1回の日次パスで返す。"""
    exit_idx_max = entry_idx + FORWARD_WINDOW_BD
    fwd_days = all_bdays[entry_idx : exit_idx_max + 1]  # 長さ21: index0=entry, index20=terminal
    price_series = prices.get(code, {})
    sma_series = sma200_by_code.get(code, {})

    # last_seen_day（上場廃止等の欠損判定用。全ルール共通の下限）
    last_seen_day = fwd_days[0]
    for d in fwd_days[1:]:
        rec = price_series.get(d)
        if rec is not None and rec.get("AdjC") is not None:
            last_seen_day = d

    time_exit = _time_based_exit(fwd_days, price_series)

    results: dict[str, tuple[str, float, str]] = {}
    results["C1"] = _walk_static_stop(fwd_days, price_series, entry_price * 0.92, last_seen_day, time_exit)
    results["C2"] = _walk_static_stop(fwd_days, price_series, entry_price * 0.90, last_seen_day, time_exit)
    results["C3"] = time_exit

    if atr20 is not None:
        results["E2"] = _walk_static_stop(
            fwd_days, price_series, entry_price - ATR_STOP_MULT * atr20, last_seen_day, time_exit
        )
        results["E4"] = _walk_e4_trailing(fwd_days, price_series, entry_price, atr20, last_seen_day, time_exit)
    else:
        results["E2"] = (time_exit[0], time_exit[1], "time_no_atr")
        results["E4"] = (time_exit[0], time_exit[1], "time_no_atr")

    prev_day = all_bdays[entry_idx - 1]
    results["E1"] = _walk_e1_scenario_break(fwd_days, price_series, sma_series, prev_day, time_exit)

    prev_topix = topix_close.get(prev_day)
    results["E3"] = _walk_e3_beta_adjusted(
        fwd_days, price_series, topix_close, entry_price, beta, prev_topix, time_exit
    )

    return results


def _walk_e3_beta_adjusted(
    fwd_days: list[str], price_series: dict, topix_close: dict, entry_price: float, beta: float,
    prev_topix: Optional[float], time_exit: tuple[str, float, str],
) -> tuple[str, float, str]:
    """E3: 累積超過リターンΣ(r_i-β*r_mkt)が-8%以下になった最初の日の終値で手仕舞い。

    day0(エントリー日)のr_stockはentry_price(寄付買い)基準、day0のr_mktは前営業日比の
    TOPIX終値リターン(市場は寄付基準を持たないため終値-終値で代用)。以降は終値-終値。
    売買停止等で当日終値が無い日はstock/market双方の基準を更新せずスキップし(次に価格が
    観測できた日にホライズンがそのまま引き継がれる)、超過リターンの二重計上・脱落を避ける。
    """
    cum_excess = 0.0
    last_close = entry_price
    last_topix = prev_topix
    for d in fwd_days:
        rec = price_series.get(d)
        cur_close = rec.get("AdjC") if rec else None
        cur_topix = topix_close.get(d)
        if cur_close is None or cur_topix is None or last_topix is None:
            continue
        r_stock = cur_close / last_close - 1
        r_mkt = cur_topix / last_topix - 1
        cum_excess += r_stock - beta * r_mkt
        last_close, last_topix = cur_close, cur_topix
        if cum_excess <= BETA_STOP_THRESHOLD:
            return d, cur_close, "beta_stop"
    return time_exit


TRIGGERED_REASONS = {
    "stop_gap", "stop_touch", "cross_exit", "beta_stop", "stage1_gap", "stage1_touch", "stage2_gap", "stage2_touch",
}


def process_population(
    returns_csv_path: Path, all_bdays: list[str], bday_index: dict[str, int],
    topix_close: dict[str, float], prices: dict, sma200_by_code: dict,
) -> tuple[pd.DataFrame, dict, dict]:
    """1母集団の全ポジションについて7ルールを計算し、position単位の明細DataFrameを返す。

    Returns:
        (detail_df, atr_diag, beta_diag)。detail_dfは1行1ポジション、列は
        signal_date/code/month/regime/entry_date/entry_price + ルールごとの
        exit_date_<rule>/exit_price_<rule>/reason_<rule>/ret_raw_<rule>/ret_costadj_<rule>/holding_days_<rule>。
    """
    positions = load_positions(returns_csv_path)
    atr_none_count = 0
    beta_fallback_count = 0
    rows = []
    for pos in positions.itertuples(index=False):
        entry_idx = bday_index[pos.entry_date]
        atr20 = compute_atr20(pos.code, entry_idx, all_bdays, prices)
        if atr20 is None:
            atr_none_count += 1
        beta, is_fallback, _n_obs = compute_beta(pos.code, entry_idx, all_bdays, prices, topix_close)
        if is_fallback:
            beta_fallback_count += 1

        rule_results = walk_position(
            pos.code, pos.entry_date, pos.entry_price, entry_idx, all_bdays,
            prices, sma200_by_code, topix_close, atr20, beta,
        )

        row = {
            "signal_date": pos.signal_date, "code": pos.code, "month": pos.month, "regime": pos.regime,
            "entry_date": pos.entry_date, "entry_price": pos.entry_price, "atr20": atr20, "beta": beta,
            "beta_fallback": is_fallback,
        }
        for rule in RULES:
            exit_date, exit_price, reason = rule_results[rule]
            if exit_price is None:
                raise SystemExit(
                    f"FATAL: code={pos.code} entry={pos.entry_date} rule={rule} でexit_priceが計算できませんでした"
                )
            ret_raw = exit_price / pos.entry_price - 1
            ret_costadj = ret_raw - ROUND_TRIP_COST
            holding_days = bday_index[exit_date] - entry_idx
            row[f"exit_date_{rule}"] = exit_date
            row[f"exit_price_{rule}"] = exit_price
            row[f"reason_{rule}"] = reason
            row[f"ret_raw_{rule}"] = ret_raw
            row[f"ret_costadj_{rule}"] = ret_costadj
            row[f"holding_days_{rule}"] = holding_days
        rows.append(row)

    detail_df = pd.DataFrame(rows)
    atr_diag = {"n": len(positions), "atr_none": atr_none_count}
    beta_diag = {"n": len(positions), "beta_fallback": beta_fallback_count}
    return detail_df, atr_diag, beta_diag


# --- 集計・レポート出力 ---------------------------------------------------------


def aggregate_rule_stats(detail_df: pd.DataFrame, rule: str) -> dict:
    n = len(detail_df)
    ret_raw = detail_df[f"ret_raw_{rule}"]
    ret_costadj = detail_df[f"ret_costadj_{rule}"]
    reason = detail_df[f"reason_{rule}"]
    triggered = reason.isin(TRIGGERED_REASONS)
    triggered_n = int(triggered.sum())
    if triggered_n > 0:
        c3_ret_of_triggered = detail_df.loc[triggered, "ret_raw_C3"]
        dropped_loss_rate = float((c3_ret_of_triggered > 0).mean())
    else:
        dropped_loss_rate = None
    return {
        "n": n,
        "ev": float(ret_costadj.mean()),
        "p20": float((ret_raw >= 0.20).mean()),
        "p30": float((ret_raw >= 0.30).mean()),
        "median_ret": float(ret_raw.median()),
        "avg_holding_days": float(detail_df[f"holding_days_{rule}"].mean()),
        "trigger_rate": float(triggered.mean()),
        "triggered_n": triggered_n,
        "dropped_loss_rate": dropped_loss_rate,
    }


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.2%}" if x is not None else "-"


def write_report(
    path: Path,
    pop_results: dict[str, tuple[pd.DataFrame, dict, dict, dict]],
    orig_stats_by_pop: dict[str, dict],
) -> None:
    lines = [
        "# KPI Exit設計ラウンド 比較レポート（第12周・カタログ§7-A）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降はロック中・本レポートの対象外）",
        "",
        "> **割引注記**: 本ラウンドは同一サンプル・同一エントリーに対する複数exitルールの"
        "**順位付け**が目的であり、各ルールの絶対値（EV・P20等）は「7通り試した中のベスト」を"
        "選ぶ多重比較バイアスを含む。ここでの勝者はあくまで次段階（ペーパートレード）での"
        "前向き確認の候補であり、本レポート単体をもって運用ルールとして確定させない。",
        "",
        "## ルール定義（§7-A事前登録仕様）",
        "",
        "| ルール | 定義 |",
        "|---|---|",
        "| C1 | 固定-8%損切り（ギャップダウン=寄付約定・ザラ場タッチ=水準約定） |",
        "| C2 | 固定-10%損切り（同上） |",
        "| C3 | 損切りなし（20営業日目終値で決済） |",
        f"| E1 | シナリオ崩壊: 前日終値>=SMA200(前日)かつ当日終値<SMA200(当日)で翌営業日寄付手仕舞い |",
        f"| E2 | ATR適応ストップ: entry-{ATR_STOP_MULT:.0f}×ATR20（ATR20=エントリー前20営業日TR平均） |",
        f"| E3 | β調整ストップ: 累積超過リターンΣ(r_i-β×r_mkt)<={BETA_STOP_THRESHOLD:.0%}で終値手仕舞い"
        f"（β不足時は1.0にフォールバック・n<{MIN_BETA_OBS}） |",
        f"| E4 | 3段階トレーリング: +{STAGE1_ATR_MULT:.0f}ATRで建値ストップ→+{STAGE2_ATR_MULT:.0f}ATRで"
        "「最高終値-2ATR」のトレールに切替 |",
        "",
    ]

    for pop_name, (detail_df, atr_diag, beta_diag, _) in pop_results.items():
        orig_stats = orig_stats_by_pop[pop_name]
        if orig_stats.get("point_lift") is not None:
            lift_line = (
                f"- リフト倍率（エントリー同一のため全ルール共通・C3=元の検証と同値）: "
                f"point={orig_stats['point_lift']:.2f} / "
                f"CI95%=[{orig_stats['ci_low']:.2f}, {orig_stats['ci_high']:.2f}]"
            )
        else:
            lift_line = "- リフト倍率: 計算不能（ベースレート欠測等）"
        lines += [
            f"## 母集団: {pop_name}（n={len(detail_df)}）",
            "",
            lift_line,
            f"- ATR20計算不能（データ不足でE2/E4が時間切れexitにフォールバック）: "
            f"{atr_diag['atr_none']}/{atr_diag['n']}件",
            f"- β回帰データ不足でβ=1.0にフォールバック（n<{MIN_BETA_OBS}）: "
            f"{beta_diag['beta_fallback']}/{beta_diag['n']}件",
            "",
            "| ルール | N | EV(コスト込) | P(+20%最終) | P(+30%) | 中央値 | 平均保有日数 | "
            "損切り発動率 | 降ろされ損率(発動時) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        rows_for_rank = []
        for rule in RULES:
            s = aggregate_rule_stats(detail_df, rule)
            rows_for_rank.append((rule, s))
            lines.append(
                f"| {rule} | {s['n']} | {_fmt_pct(s['ev'])} | {_fmt_pct(s['p20'])} | {_fmt_pct(s['p30'])} | "
                f"{_fmt_pct(s['median_ret'])} | {s['avg_holding_days']:.1f}日 | "
                f"{_fmt_pct(s['trigger_rate'])}（n={s['triggered_n']}） | "
                f"{_fmt_pct(s['dropped_loss_rate']) if s['dropped_loss_rate'] is not None else '-（発動0件）'} |"
            )
        ranked = sorted(rows_for_rank, key=lambda kv: kv[1]["ev"], reverse=True)
        lines += [
            "",
            f"**EV順ランキング（{pop_name}）**: " + " > ".join(
                f"{r}({_fmt_pct(s['ev'])})" for r, s in ranked
            ),
            "",
        ]

    lines += [
        "## 実装ノート",
        "- エントリー日・エントリー価格は既存の `output/kpi/<kpi>/returns.csv`（defer-entry込みで既に確定済み）を"
        "そのまま再利用し、本スクリプトでの再計算は一切行わない（team-lead指示）",
        "- フォワードリターン計算の日次バー読み込み・カレンダー・往復コスト定数(0.3%)は"
        "`scripts/measure_base_rate.py`（§6準拠・変更禁止）をそのまま再利用",
        "- リフト倍率・CIは `scripts/kpi_event_study.py` の `compute_stats`/`bootstrap_lift_ci` を"
        "元のreturns.csv（=C3相当）に対してそのまま再実行した値を全ルール共通で使用"
        "（エントリー不変のためexitルールを変えてもP20への寄与月構成・ベースレート比は変わらない"
        "という近似——本ラウンドの主眼はEVの順位比較であるためこれで十分と判断）",
        "- 「降ろされ損」率 = そのルールで損切り/手仕舞いが発動したポジションのうち、"
        "もし20営業日目終値まで持ち続けていたら(C3のret_raw)プラスだったものの割合",
        "- ATR20/β回帰データ不足時のフォールバック件数は上記diagとして母集団ごとに記録",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# --- 台帳追記（主母集団のみ・7行） ------------------------------------------------


def append_trials_for_population(
    detail_df: pd.DataFrame, orig_stats: dict, pop_name: str, atr_diag: dict, beta_diag: dict,
) -> None:
    for rule in RULES:
        s = aggregate_rule_stats(detail_df, rule)
        stats_for_judge = {
            "n": s["n"],
            "months_spanned": orig_stats["months_spanned"],
            "has_bull": orig_stats["has_bull"],
            "has_bear": orig_stats["has_bear"],
            "ci_low": orig_stats["ci_low"],
            "ev_stop8": s["ev"],
            "avg_monthly_n": orig_stats["avg_monthly_n"],
        }
        verdict, _reasons = kpi_event_study.judge(stats_for_judge)
        params = {
            "exit_rule": rule,
            "source_population": pop_name,
            "source_returns_csv": str(POPULATIONS[pop_name]),
            "round": "12_exit_study",
            "round_cost_note": "同一サンプルでの複数exit比較のため絶対値は割引評価（report.md冒頭注記参照）",
        }
        if rule in ("C1",):
            params["stop_pct"] = 0.08
        elif rule in ("C2",):
            params["stop_pct"] = 0.10
        elif rule == "E2":
            params["atr_window"] = ATR_WINDOW
            params["atr_stop_mult"] = ATR_STOP_MULT
            params["atr_none_count"] = atr_diag["atr_none"]
        elif rule == "E1":
            params["sma200_window"] = MA200_WINDOW
        elif rule == "E3":
            params["beta_window"] = BETA_WINDOW
            params["beta_stop_threshold"] = BETA_STOP_THRESHOLD
            params["min_beta_obs"] = MIN_BETA_OBS
            params["beta_fallback_count"] = beta_diag["beta_fallback"]
        elif rule == "E4":
            params["atr_window"] = ATR_WINDOW
            params["stage1_atr_mult"] = STAGE1_ATR_MULT
            params["stage2_atr_mult"] = STAGE2_ATR_MULT
            params["atr_none_count"] = atr_diag["atr_none"]

        record = {
            "run_id": uuid.uuid4().hex,
            "ts": jq_fetch.now_jst().isoformat(),
            "kpi_name": f"{pop_name}_exit_{rule}",
            "params": params,
            "period": {"start": PERIOD[0], "end": PERIOD[1]},
            "n": s["n"],
            "lift": orig_stats.get("point_lift"),
            "ci_low": orig_stats.get("ci_low"),
            "ci_high": orig_stats.get("ci_high"),
            "ev": s["ev"],
            "verdict": verdict,
            "entry_mode": "reused_from_existing_returns_csv",
            "regime_filter": None,
        }
        kpi_event_study.append_trial(record, TRIALS_PATH)


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    topix_series = measure_base_rate.load_topix_series()
    topix_close = dict(zip(topix_series.index, topix_series.values))

    positions_by_pop = {name: load_positions(path) for name, path in POPULATIONS.items()}
    all_codes: set[str] = set()
    all_entry_indices: list[int] = []
    for df in positions_by_pop.values():
        all_codes |= set(df["code"])
        all_entry_indices += [bday_index[e] for e in df["entry_date"]]

    earliest_idx = bday_index[_earliest_bars_date()]
    min_idx, max_idx = min(all_entry_indices), max(all_entry_indices)
    scan_start_idx = max(earliest_idx, min_idx - SCAN_BUFFER_BDAYS)
    scan_end_idx = min(len(all_bdays) - 1, max_idx + FORWARD_WINDOW_BD + 5)
    scan_days = all_bdays[scan_start_idx : scan_end_idx + 1]

    print(
        f"価格履歴プリロード開始: 銘柄数={len(all_codes)} 走査日数={len(scan_days)} "
        f"({scan_days[0]}〜{scan_days[-1]})",
        file=sys.stderr,
    )
    prices, sma200_by_code = build_price_history(all_codes, scan_days)
    print("価格履歴プリロード完了", file=sys.stderr)

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    pop_results = {}
    orig_stats_by_pop = {}
    for pop_name, returns_csv_path in POPULATIONS.items():
        detail_df, atr_diag, beta_diag = process_population(
            returns_csv_path, all_bdays, bday_index, topix_close, prices, sma200_by_code
        )
        pop_results[pop_name] = (detail_df, atr_diag, beta_diag, None)

        orig_df = pd.read_csv(
            returns_csv_path, dtype={"code": str, "signal_date": str, "month": str, "entry_date": str, "exit_date": str}
        )
        orig_df = orig_df[orig_df["in_universe"] == True].reset_index(drop=True)  # noqa: E712
        orig_stats = kpi_event_study.compute_stats(orig_df, base_rate_by_month, PERIOD)
        orig_stats_by_pop[pop_name] = orig_stats

        detail_path = OUTPUT_DIR / f"positions_{pop_name}.csv"
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        detail_df.to_csv(detail_path, index=False)
        print(f"{pop_name}: n={len(detail_df)} -> {detail_path}", file=sys.stderr)

    report_path = OUTPUT_DIR / "report.md"
    write_report(report_path, pop_results, orig_stats_by_pop)
    print(f"report: {report_path}")

    main_detail_df, main_atr_diag, main_beta_diag, _ = pop_results[LOG_TO_TRIALS_POPULATION]
    append_trials_for_population(
        main_detail_df, orig_stats_by_pop[LOG_TO_TRIALS_POPULATION], LOG_TO_TRIALS_POPULATION,
        main_atr_diag, main_beta_diag,
    )
    print(f"台帳追記完了: {LOG_TO_TRIALS_POPULATION} × {len(RULES)}ルール -> {TRIALS_PATH}")

    for pop_name in POPULATIONS:
        detail_df = pop_results[pop_name][0]
        print(f"\n=== {pop_name} (n={len(detail_df)}) ===")
        for rule in RULES:
            s = aggregate_rule_stats(detail_df, rule)
            dl = f"{s['dropped_loss_rate']:.1%}" if s["dropped_loss_rate"] is not None else "-"
            print(
                f"  {rule}: EV={s['ev']:.2%} P20={s['p20']:.2%} P30={s['p30']:.2%} "
                f"中央値={s['median_ret']:.2%} 保有={s['avg_holding_days']:.1f}日 "
                f"発動率={s['trigger_rate']:.1%}(n={s['triggered_n']}) 降ろされ損={dl}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
