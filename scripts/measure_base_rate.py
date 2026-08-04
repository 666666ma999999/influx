#!/usr/bin/env python3
"""日本株「1ヶ月+20%」ベースレート計測（Phase B）。

docs/stock-algo-kpi-catalog.md の §0（ゴール定義）・§6（計測手順）を実装する。
`data/jquants/` 配下のローカルキャッシュのみを読み、API 呼び出しは一切行わない
（バックグラウンドで進行中の `jq_fetch.py` の取得状況に依存する）。

定義は §6 に厳密に準拠する（全KPI検証との比較可能性を保つため変更禁止）:
    - T = 各月の最終営業日
    - ユニバース: T を含む直近 N 営業日の売買代金(Va)合計で ProdCat=="011"（内国株券）を
      売買代金降順に上位 --top 件選抜
    - エントリー: T+1営業日の AdjO / イグジット: entry_day から20営業日後の AdjC
      （上場廃止等でその日に価格が無い場合は窓内最後の AdjC で決済・delisted_flag=1）
    - MFE/MAE は窓（entry_day〜exit_day、両端含む）の AdjH/AdjL から算出
    - 損切りシミュレーション（-8%/-10%）は全バリアントに往復コスト0.3%を控除
    - レジームは TOPIX Close(T) vs SMA200(T時点まで、ルックアヘッドなし)
    - メンバーシップは当月含め3ヶ月連続在籍で existing、それ未満は new

look-ahead 禁止: ユニバース構築に使う営業日は全て T 以前（assert で保証。下記
build_universe 内）。

Usage:
    python3 scripts/measure_base_rate.py --start-month 2016-11 --end-month 2017-02
    python3 scripts/measure_base_rate.py --start-month 2016-08 --end-month 2026-06 \
        --universe-window 126 --output-dir output/base_rate
"""
from __future__ import annotations

import argparse
import functools
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: read_json_gz / DATA_ROOT / カレンダー変換を再利用)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

FORWARD_WINDOW_BD = 20  # T+1エントリーから何営業日後の終値で決済するか
STOP_LEVELS = {"stop8": 0.08, "stop10": 0.10}
ROUND_TRIP_COST = 0.003  # 往復売買コスト（§6手順6）
MAE_TOUCH_LEVELS = (("touch_-8", -0.08), ("touch_-10", -0.10), ("touch_-15", -0.15))
MFE_TOUCH_LEVELS = (("mfe20", 0.20), ("mfe30", 0.30))
MIN_MEMBERSHIP_MONTHS = 3  # 当月含め3ヶ月連続在籍で existing
SMA200_WINDOW = 200
PROD_CAT_STOCK = "011"  # 内国株券（カタログ §0 確定定義）


# --- カレンダー・キャッシュ読み込み（jq_fetch の流儀を再利用） -----------------


def load_calendar_days() -> list[tuple[str, str]]:
    """calendar.json.gz を読み込む（未取得なら明確なエラーで停止・API呼び出しはしない）。"""
    path = jq_fetch.DATA_ROOT / "calendar.json.gz"
    if not path.exists():
        raise SystemExit(
            f"FATAL: カレンダーキャッシュが見つかりません: {path}\n"
            f"先に `docker compose run --rm xstock python scripts/jq_fetch.py --only calendar` を実行してください。"
        )
    # ファイルが既に存在するため api_key/run_id は使われない（jq_fetch.load_calendar_days の
    # 「既存ならフェッチしない」分岐のみを通る。Canonical Module を再利用しつつオフライン専用の
    # 事前チェックだけをこちらで追加している）。
    return jq_fetch.load_calendar_days(api_key=None, run_id=None)  # type: ignore[arg-type]


def all_business_days(calendar_days: list[tuple[str, str]]) -> list[str]:
    """全期間の営業日（HolDiv 1/2）を日付昇順で返す。"""
    return jq_fetch.business_days_in_range(calendar_days, "00010101", "99991231")


def month_ends_in_range(calendar_days: list[tuple[str, str]], start_month: str, end_month: str) -> list[str]:
    """[start_month, end_month]（YYYY-MM）に含まれる各月の最終営業日を返す。"""
    start_bound = start_month.replace("-", "") + "01"
    end_bound = end_month.replace("-", "") + "31"  # 実在しない日付でも文字列上限として機能
    return jq_fetch.month_end_business_days_in_range(calendar_days, start_bound, end_bound)


def month_starts_in_range(calendar_days: list[tuple[str, str]], start_month: str, end_month: str) -> list[str]:
    """[start_month, end_month]（YYYY-MM）に含まれる各月の最初の営業日を返す（§7-AE用）。"""
    start_bound = start_month.replace("-", "") + "01"
    end_bound = end_month.replace("-", "") + "31"
    return jq_fetch.month_start_business_days_in_range(calendar_days, start_bound, end_bound)


@functools.lru_cache(maxsize=600)
def load_bars_day(date_str: str) -> dict[str, dict]:
    """指定営業日の全銘柄四本値を Code -> record の dict で返す（未取得なら明確なエラーで停止）。"""
    path = jq_fetch.DATA_ROOT / "bars" / f"{date_str}.json.gz"
    if not path.exists():
        raise SystemExit(
            f"FATAL: bars キャッシュが見つかりません: {path}\n"
            f"バックグラウンドの jq_fetch.py がこの日付までまだ到達していない可能性があります。\n"
            f"`docker compose run --rm xstock python scripts/jq_fetch.py --status` で進捗を確認してください。"
        )
    obj = jq_fetch.read_json_gz(path)
    return {rec["Code"]: rec for rec in obj["data"]}


@functools.lru_cache(maxsize=200)
def load_master_day(date_str: str) -> dict[str, dict]:
    """指定月末営業日の銘柄マスタを Code -> record の dict で返す（未取得なら明確なエラーで停止）。"""
    path = jq_fetch.DATA_ROOT / "master" / f"{date_str}.json.gz"
    if not path.exists():
        raise SystemExit(
            f"FATAL: master キャッシュが見つかりません: {path}\n"
            f"バックグラウンドの jq_fetch.py がこの日付までまだ到達していない可能性があります。\n"
            f"`docker compose run --rm xstock python scripts/jq_fetch.py --status` で進捗を確認してください。"
        )
    obj = jq_fetch.read_json_gz(path)
    return {rec["Code"]: rec for rec in obj["data"]}


def load_topix_series() -> pd.Series:
    """TOPIX 終値を Date(YYYYMMDD) 昇順の pandas Series で返す。"""
    path = jq_fetch.DATA_ROOT / "topix.json.gz"
    if not path.exists():
        raise SystemExit(f"FATAL: topix キャッシュが見つかりません: {path}")
    obj = jq_fetch.read_json_gz(path)
    dates = [rec["Date"].replace("-", "") for rec in obj["data"]]
    closes = [rec["C"] for rec in obj["data"]]
    s = pd.Series(closes, index=dates, dtype="float64").sort_index()
    return s


def build_regime_series(topix_close: pd.Series) -> dict[str, str]:
    """各営業日の地合いレジームを返す（TOPIX Close(T) vs SMA200(T時点まで)）。

    rolling(window=200) は末尾整列（当日を含む直近200件平均）なので、
    T より後のデータは一切参照しない（ルックアヘッドなし）。
    200件に満たない初期の日付は "unknown" とする。
    """
    sma200 = topix_close.rolling(window=SMA200_WINDOW, min_periods=SMA200_WINDOW).mean()
    regime: dict[str, str] = {}
    for date_str, close in topix_close.items():
        sma = sma200.loc[date_str]
        if pd.isna(sma):
            regime[date_str] = "unknown"
        else:
            regime[date_str] = "bull" if close > sma else "bear"
    return regime


# --- ユニバース構築 -----------------------------------------------------------


def build_universe(
    t_date: str,
    bday_index: dict[str, int],
    all_bdays: list[str],
    window_n: int,
    top_n: int,
    master_date: Optional[str] = None,
) -> tuple[list[tuple[str, float]], dict]:
    """営業日 T のユニバースを構築する（既定は月末営業日 T。§7-AE等の月初起点呼び出しにも対応）。

    Args:
        master_date: ProdCat分類（内国株券フィルタ）に使うmasterスナップショットの日付を
            t_date から分離指定したい場合に渡す（既定 None は t_date と同一＝全既存呼び出し元
            の挙動を完全互換で維持）。data/jquants/master/ は月末営業日にしか取得されない
            設計のため、t_date が月末以外（§7-AE の月初第1営業日等）だと load_master_day(t_date)
            は失敗する。呼び出し側で「t_date より前で直近の既存masterスナップショット日」を
            解決して渡すことで、売買代金トレーリング窓・look-aheadガードはt_date基準のまま、
            ProdCat分類だけ既存の直近masterを使う（2026-07-18 team lead裁定・§7-AE用）。
            master_date <= t_date であること（未来のmasterを使うlook-ahead違反を防ぐ）。

    Returns:
        (selected, stats): selected は [(code, turnover_sum), ...] を売買代金降順で
        top_n 件まで。stats はフィルタ前後の件数（検証レポート用）。
    """
    idx = bday_index[t_date]
    if idx < window_n - 1:
        raise SystemExit(
            f"FATAL: T={t_date} には直近{window_n}営業日分のカレンダー履歴がありません"
            f"（データ開始日に近すぎます）。"
        )
    win_days = all_bdays[idx - window_n + 1 : idx + 1]
    # look-ahead 禁止の明示的ガード: ユニバース構築に使う営業日は全て T 以前でなければならない。
    assert win_days[-1] == t_date
    assert max(win_days) <= t_date, f"look-ahead 違反: window day {max(win_days)} > T {t_date}"

    turnover: dict[str, float] = defaultdict(float)
    for d in win_days:
        bars = load_bars_day(d)
        for code, rec in bars.items():
            va = rec.get("Va")
            if va:
                turnover[code] += va

    effective_master_date = master_date if master_date is not None else t_date
    assert effective_master_date <= t_date, (
        f"look-ahead 違反: master_date {effective_master_date} > t_date {t_date}"
    )
    master = load_master_day(effective_master_date)
    master_011_codes = {code for code, rec in master.items() if rec.get("ProdCat") == PROD_CAT_STOCK}

    candidates = [(code, tv) for code, tv in turnover.items() if code in master_011_codes]
    candidates.sort(key=lambda x: -x[1])
    selected = candidates[:top_n]

    stats = {
        "n_turnover_codes": len(turnover),
        "n_master_011": len(master_011_codes),
        "n_intersect": len(candidates),
        "n_selected": len(selected),
        "master_date_used": effective_master_date,
    }
    if len(selected) < top_n:
        print(f"WARN: [{t_date}] ユニバースが{top_n}件未満: {len(selected)}件", file=sys.stderr)
    return selected, stats


def _membership(code: str, m1: Optional[set], m2: Optional[set]) -> str:
    """当月含め3ヶ月連続在籍かどうか（m1=前月, m2=前々月のユニバース集合）。"""
    if m1 is not None and m2 is not None and code in m1 and code in m2:
        return "existing"
    return "new"


# --- フォワードリターン計算 ----------------------------------------------------


def compute_forward_return_for_code(
    code: str,
    t_date: str,
    bday_index: dict[str, int],
    all_bdays: list[str],
) -> Optional[dict]:
    """T(シグナル確定日)の翌営業日エントリー・20営業日後イグジットのリターン等を1銘柄分計算する。

    ベースレート計測（月次ユニバース一括）と個別KPIのイベントスタディ（銘柄×任意日）の
    両方から呼ばれる Canonical Module（`scripts/kpi_event_study.py` が re-export 経由で再利用）。
    T はユニバースの月末営業日に限らず、カレンダー上の任意の営業日でよい。

    Returns:
        entry_date/exit_date/entry_price/ret/mfe/mae/delisted_flag/ret_stop8/ret_stop10/
        touch_-8/touch_-10/touch_-15/mfe20/mfe30 を含む dict。
        エントリー日に約定可能な始値(AdjO)が無い場合は None（呼び出し側で entry_missing 集計）。
    """
    idx = bday_index[t_date]
    entry_idx = idx + 1
    exit_idx = idx + 1 + FORWARD_WINDOW_BD
    if exit_idx >= len(all_bdays):
        raise SystemExit(
            f"FATAL: T={t_date} の{FORWARD_WINDOW_BD}営業日後がカレンダー範囲外です"
            f"（カレンダーキャッシュの延長が必要）。"
        )
    entry_day = all_bdays[entry_idx]
    fwd_days = all_bdays[entry_idx : exit_idx + 1]  # 長さ21: index0=entry, index20=exit目標日

    fwd_bars = {d: load_bars_day(d) for d in fwd_days}

    entry_rec = fwd_bars[entry_day].get(code)
    if entry_rec is None or not entry_rec.get("AdjO"):
        return None
    entry_price = entry_rec["AdjO"]

    max_h = entry_rec.get("AdjH") or entry_price
    min_l = entry_rec.get("AdjL") or entry_price
    last_seen_adjc = entry_rec.get("AdjC") or entry_price
    last_seen_day = entry_day

    for d in fwd_days[1:]:
        rec = fwd_bars[d].get(code)
        if rec is None:
            continue  # 売買停止等の欠損日はスキップ（MFE/MAE・last_seenの更新をしない）
        if rec.get("AdjH") is not None:
            max_h = max(max_h, rec["AdjH"])
        if rec.get("AdjL") is not None:
            min_l = min(min_l, rec["AdjL"])
        if rec.get("AdjC") is not None:
            last_seen_adjc = rec["AdjC"]
            last_seen_day = d

    exit_target_day = fwd_days[-1]
    exit_target_rec = fwd_bars[exit_target_day].get(code)
    if exit_target_rec is not None and exit_target_rec.get("AdjC"):
        exit_price = exit_target_rec["AdjC"]
        exit_date = exit_target_day
        delisted_flag = 0
    else:
        # 上場廃止等でイグジット目標日に価格が無い -> 窓内最後の AdjC で決済
        exit_price = last_seen_adjc
        exit_date = last_seen_day
        delisted_flag = 1

    ret = exit_price / entry_price - 1
    mfe = max_h / entry_price - 1
    mae = min_l / entry_price - 1

    stop_rets = {}
    for name, s in STOP_LEVELS.items():
        threshold = entry_price * (1 - s)
        stop_price = None
        for d in fwd_days:
            if d > last_seen_day:
                break  # 上場廃止後は約定しえない
            rec = fwd_bars[d].get(code)
            if rec is None:
                continue
            ao = rec.get("AdjO")
            al = rec.get("AdjL")
            if ao is not None and ao <= threshold:
                stop_price = ao  # ギャップダウン: 始値で決済
                break
            if al is not None and al <= threshold:
                stop_price = threshold  # 指値相当: 損切り水準そのもので決済
                break
        if stop_price is None:
            stop_price = exit_price  # 発動なし: 通常のイグジット価格
        stop_rets[name] = (stop_price / entry_price - 1) - ROUND_TRIP_COST

    row = {
        "entry_date": entry_day,
        "exit_date": exit_date,
        "entry_price": entry_price,
        "ret": ret,
        "mfe": mfe,
        "mae": mae,
        "delisted_flag": delisted_flag,
        "ret_stop8": stop_rets["stop8"],
        "ret_stop10": stop_rets["stop10"],
    }
    for name, level in MAE_TOUCH_LEVELS:
        row[name] = int(mae <= level)
    for name, level in MFE_TOUCH_LEVELS:
        row[name] = int(mfe >= level)
    return row


def compute_returns_for_month(
    t_date: str,
    selected: list[tuple[str, float]],
    bday_index: dict[str, int],
    all_bdays: list[str],
    regime: str,
    membership_of: dict[str, str],
) -> tuple[list[dict], int]:
    """T の翌営業日エントリー・20営業日後イグジットのリターン等を銘柄ごとに計算する。

    Returns:
        (rows, entry_missing_count)
    """
    rows = []
    entry_missing_count = 0
    for code, _turnover in selected:
        result = compute_forward_return_for_code(code, t_date, bday_index, all_bdays)
        if result is None:
            entry_missing_count += 1
            continue
        # 既存CLIの出力列順（month,code,...,membership,regime,ret_stop8,...）を維持するため
        # dict マージではなく明示的に組み立てる（compute_forward_return_for_code の戻り値は
        # membership/regime を持たないため挿入順が変わってしまう）。
        row = {
            "month": t_date[:6],
            "code": code,
            "entry_date": result["entry_date"],
            "exit_date": result["exit_date"],
            "entry_price": result["entry_price"],
            "ret": result["ret"],
            "mfe": result["mfe"],
            "mae": result["mae"],
            "delisted_flag": result["delisted_flag"],
            "membership": membership_of[code],
            "regime": regime,
            "ret_stop8": result["ret_stop8"],
            "ret_stop10": result["ret_stop10"],
        }
        for name, _level in MAE_TOUCH_LEVELS:
            row[name] = result[name]
        for name, _level in MFE_TOUCH_LEVELS:
            row[name] = result[name]
        rows.append(row)

    return rows, entry_missing_count


# --- 集計 ---------------------------------------------------------------------


def summarize(df: pd.DataFrame) -> dict:
    """returns の部分集合から summary 用の統計一式を計算する（全breakdownで共通利用）。"""
    n = len(df)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "p20": (df["ret"] >= 0.20).mean(),
        "p30": (df["ret"] >= 0.30).mean(),
        "p_mfe20": (df["mfe"] >= 0.20).mean(),
        "p_neg5": (df["ret"] <= -0.05).mean(),
        "median_ret": df["ret"].median(),
        "mean_ret": df["ret"].mean(),
        "mae_median": df["mae"].median(),
        "touch8": df["touch_-8"].mean(),
        "touch10": df["touch_-10"].mean(),
        "touch15": df["touch_-15"].mean(),
        "ev_none": df["ret"].mean() - ROUND_TRIP_COST,
        "ev_stop8": df["ret_stop8"].mean(),
        "ev_stop10": df["ret_stop10"].mean(),
    }


def format_stats_row(label: str, s: dict) -> str:
    if s["n"] == 0:
        return f"| {label} | 0 | - | - | - | - | - | - | - | - | - |"
    return (
        f"| {label} | {s['n']} | {s['p20']:.1%} | {s['p30']:.1%} | {s['p_mfe20']:.1%} | "
        f"{s['p_neg5']:.1%} | {s['median_ret']:.1%} | {s['mean_ret']:.1%} | {s['mae_median']:.1%} | "
        f"{s['touch8']:.1%}/{s['touch10']:.1%}/{s['touch15']:.1%} | "
        f"{s['ev_none']:.1%}/{s['ev_stop8']:.1%}/{s['ev_stop10']:.1%} |"
    )


STATS_HEADER = (
    "| 区分 | N | P(+20%終値) | P(+30%) | P(MFE+20%) | P(-5%以下) | 中央値 | 平均 | MAE中央値 | "
    "タッチ率(-8/-10/-15) | 損切り別EV(なし/-8%/-10%・コスト込) |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def write_summary_md(path: Path, returns_df: pd.DataFrame, window_n: int, top_n: int, n_months: int) -> None:
    overall = summarize(returns_df)
    headline = overall.get("p20", 0.0)
    lines = [
        f"# ベースレート計測サマリー（window={window_n}, top={top_n}, 対象月数={n_months}）",
        "",
        "コスト規約: ret列は生値、stop列とEVは往復0.3%控除済み",
        "上場廃止は窓内最終AdjC決済（TOB買付価格決済は未実装・データ制約）",
        "",
        f"**素の+20%/20営業日到達率 = {headline:.1%}**"
        + (f"（n={overall['n']}）" if overall["n"] else "（n=0・シグナルなし）"),
        "",
        "## 全体",
        STATS_HEADER,
        format_stats_row("全体", overall),
        "",
        "## 年別",
        STATS_HEADER,
    ]
    if len(returns_df) > 0:
        for year, g in returns_df.groupby(returns_df["month"].str[:4]):
            lines.append(format_stats_row(year, summarize(g)))
    lines += ["", "## レジーム別", STATS_HEADER]
    if len(returns_df) > 0:
        for regime, g in returns_df.groupby("regime"):
            lines.append(format_stats_row(regime, summarize(g)))
    lines += ["", "## メンバーシップ別", STATS_HEADER]
    if len(returns_df) > 0:
        for membership, g in returns_df.groupby("membership"):
            lines.append(format_stats_row(membership, summarize(g)))
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# --- メイン処理 -----------------------------------------------------------------


def run(start_month: str, end_month: str, window_n: int, top_n: int, output_dir: Path) -> None:
    calendar_days = load_calendar_days()
    all_bdays = all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    month_ends = month_ends_in_range(calendar_days, start_month, end_month)
    if not month_ends:
        raise SystemExit(f"FATAL: 指定範囲 {start_month}〜{end_month} に月末営業日が見つかりません。")

    topix_close = load_topix_series()
    regime_by_day = build_regime_series(topix_close)

    universe_history: dict[str, set] = {}  # T -> 選抜銘柄集合（挿入順=時系列順）
    prev_universe_set: Optional[set] = None

    universe_rows: list[dict] = []
    return_rows: list[dict] = []
    monthly_rows: list[dict] = []
    total_entry_missing = 0

    for t_date in month_ends:
        selected, ustats = build_universe(t_date, bday_index, all_bdays, window_n, top_n)
        universe_codes_this_month = {code for code, _ in selected}

        churn_pct = None
        if prev_universe_set is not None and universe_codes_this_month:
            new_count = len(universe_codes_this_month - prev_universe_set)
            churn_pct = new_count / len(universe_codes_this_month) * 100
        prev_universe_set = universe_codes_this_month

        hist_keys = list(universe_history.keys())
        m1 = universe_history[hist_keys[-1]] if len(hist_keys) >= 1 else None
        m2 = universe_history[hist_keys[-2]] if len(hist_keys) >= 2 else None
        membership_of = {code: _membership(code, m1, m2) for code, _ in selected}
        universe_history[t_date] = universe_codes_this_month

        for rank, (code, tv) in enumerate(selected, start=1):
            universe_rows.append(
                {
                    "month": t_date[:6],
                    "code": code,
                    "rank": rank,
                    "turnover_sum": tv,
                    "membership": membership_of[code],
                }
            )

        regime = regime_by_day.get(t_date, "unknown")
        month_rows, entry_missing = compute_returns_for_month(
            t_date, selected, bday_index, all_bdays, regime, membership_of
        )
        total_entry_missing += entry_missing
        return_rows.extend(month_rows)

        month_df = pd.DataFrame(month_rows)
        month_stats = summarize(month_df)
        monthly_rows.append(
            {
                "month": t_date[:6],
                "n": month_stats["n"],
                "p20": month_stats.get("p20"),
                "p30": month_stats.get("p30"),
                "p_neg5": month_stats.get("p_neg5"),
                "median_ret": month_stats.get("median_ret"),
                "churn_pct": churn_pct,
                "regime": regime,
            }
        )

        print(
            f"[{t_date}] universe={ustats['n_selected']} "
            f"(turnover_codes={ustats['n_turnover_codes']}, master011={ustats['n_master_011']}, "
            f"intersect={ustats['n_intersect']}) regime={regime} churn_pct="
            f"{'%.1f' % churn_pct if churn_pct is not None else '-'} entry_missing={entry_missing}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    universes_df = pd.DataFrame(universe_rows)
    returns_df = pd.DataFrame(return_rows)
    monthly_df = pd.DataFrame(monthly_rows)

    universes_path = output_dir / f"universes_w{window_n}.csv.gz"
    returns_path = output_dir / f"returns_w{window_n}.csv.gz"
    monthly_path = output_dir / f"base_rate_monthly_w{window_n}.csv"
    summary_path = output_dir / f"summary_w{window_n}.md"

    universes_df.to_csv(universes_path, index=False, compression="gzip")
    returns_df.to_csv(returns_path, index=False, compression="gzip")
    monthly_df.to_csv(monthly_path, index=False)
    write_summary_md(summary_path, returns_df, window_n, top_n, len(month_ends))

    print(f"\n完了: {len(month_ends)}ヶ月分・returns={len(returns_df)}行・entry_missing累計={total_entry_missing}")
    print(f"出力: {universes_path}\n出力: {returns_path}\n出力: {monthly_path}\n出力: {summary_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="日本株「1ヶ月+20%」ベースレート計測（Phase B）")
    parser.add_argument("--start-month", required=True, help="計測開始月 YYYY-MM")
    parser.add_argument("--end-month", required=True, help="計測終了月 YYYY-MM（両端含む）")
    parser.add_argument(
        "--universe-window", type=int, default=21, choices=[21, 126],
        help="ユニバース構築の売買代金合計に使う営業日数（既定21・126は6ヶ月平均の感度チェック用）",
    )
    parser.add_argument("--top", type=int, default=500, help="ユニバースの上位選抜件数（既定500）")
    parser.add_argument("--output-dir", default="output/base_rate", help="出力先ディレクトリ")
    args = parser.parse_args()

    if not MONTH_RE.match(args.start_month) or not MONTH_RE.match(args.end_month):
        raise SystemExit("FATAL: --start-month / --end-month は YYYY-MM 形式で指定してください")
    if args.start_month > args.end_month:
        raise SystemExit("FATAL: --start-month は --end-month 以前である必要があります")

    run(args.start_month, args.end_month, args.universe_window, args.top, Path(args.output_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
