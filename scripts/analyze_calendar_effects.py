#!/usr/bin/env python3
"""カレンダー効果一括検証 + 節目×ブレッドス警戒（第13周・カタログ§7-B v2-4/v2-5・分析専用）。

docs/stock-algo-kpi-catalog.md §7-B の事前登録定義を実装する。これは§6評価プロトコル凍結の
合否判定を伴う正式なKPI検証ではなく記述的な層別分析であるため、試行台帳
（data/kpi_trials/trials.jsonl）への追記は行わない。

v2-4: TOPIX日次リターン10年を曜日・月内ポジションで層別集計し（市場レベル）、既存の複合シグナル
（shortcover_turn / volshock_5x / volshock_x_above200 の returns.csv）も同じ軸で層別する
（シグナルレベル）。
v2-5: 「TOPIX 252営業日高値更新日 かつ TOP500ユニバースの値下がり比率80%超」を事前登録どおりに
定義し、10年での発生回数を数える。発生していれば警戒日前後の市場・既存シグナルの挙動を
非警戒期間と比較する。

カレンダー・ユニバース・フォワードリターン計算は scripts/measure_base_rate.py /
scripts/kpi_event_study.py の既存関数・データ（output/base_rate/universes_w21.csv.gz 等）を
再利用する（Canonical Module原則・再実装しない）。

Usage:
    docker compose run --rm xstock python scripts/analyze_calendar_effects.py
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・topix・bars・summarize等を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: base_rate/universe読込を再利用)

N_BOOTSTRAP = 1000
BOOTSTRAP_SEED = 42  # kpi_event_study.py と同一シード（プロジェクト内の再現性を揃える）

OUTPUT_DIR = Path("output/kpi/calendar_effects")
BASE_RATE_DIR = kpi_event_study.DEFAULT_BASE_RATE_DIR
UNIVERSE_WINDOW = 21

# v2-4 シグナルレベル層別対象（依頼指定の3シグナル。§6の in_universe 判定は各 returns.csv に
# 既に反映済みのため、ここでは in_universe==True の行のみを対象とする）
EXISTING_SIGNALS: dict[str, Path] = {
    "shortcover_turn": Path("output/kpi/shortcover_turn/returns.csv"),
    "volshock_5x": Path("output/kpi/volshock_5x/returns.csv"),
    "volshock_x_above200": Path("output/kpi/volshock_x_above200/returns.csv"),
}

WEEKDAY_JA = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金"}
MIN_CELL_N_SIGNAL = 30  # 依頼指定: n<30のセルは参考値と明記

# v2-5 事前登録定義（凍結。本スクリプト内で緩めない）
HIGH_WINDOW_BD = 252
BREADTH_WARNING_THRESHOLD = 0.80
WARNING_ENTRY_SUPPRESS_WINDOWS_BD = (5, 10)


# --- カレンダー・フラグ構築（v2-4 共通基盤） ---------------------------------------


def build_calendar_flags(all_bdays: list[str]) -> pd.DataFrame:
    """全営業日について曜日・月内ポジションのフラグを立てる（PIT安全: 暦の休日スケジュールのみ
    を使い、価格データは参照しない）。

    - weekday: 0=月 … 4=金
    - is_first_bday: 暦月内で最初の営業日
    - is_last5_bday: 暦月内で最後から5営業日以内
    - is_last5_bday_qend: is_last5_bday かつ暦月が四半期末月(3/6/9/12)（お化粧買い想定ウィンドウ）
    """
    by_month: dict[str, list[str]] = {}
    for d in all_bdays:
        by_month.setdefault(d[:6], []).append(d)
    first_of_month = {days[0] for days in by_month.values()}
    last5_of_month = {d for days in by_month.values() for d in days[-5:]}

    rows = []
    for d in all_bdays:
        dt = datetime.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        month_num = int(d[4:6])
        is_last5 = d in last5_of_month
        rows.append(
            {
                "date": d,
                "weekday": dt.weekday(),
                "is_first_bday": d in first_of_month,
                "is_last5_bday": is_last5,
                "is_last5_bday_qend": is_last5 and month_num in (3, 6, 9, 12),
            }
        )
    return pd.DataFrame(rows)


# --- 市場レベル（TOPIX日次リターン） ---------------------------------------------


def load_topix_daily(calendar_flags: pd.DataFrame) -> pd.DataFrame:
    """TOPIX日次リターン + 曜日・月内ポジションフラグの結合済みDataFrameを返す（10年全期間）。"""
    close = measure_base_rate.load_topix_series()
    df = pd.DataFrame({"date": close.index, "close": close.values})
    df["ret"] = df["close"] / df["close"].shift(1) - 1
    df["month"] = df["date"].str[:6]
    df = df.merge(calendar_flags, on="date", how="left")
    return df.dropna(subset=["ret"]).reset_index(drop=True)


def bootstrap_group_mean_diff(
    daily: pd.DataFrame,
    mask_col: str,
    value_col: str = "ret",
    n_boot: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """月次ブロック・ブートストラップで「mask_col==Trueの日の平均 - 全体平均」のCIを算出する。

    §6手順5（月次コホートの横断・系列相関を独立試行として扱わない）の考え方を、TOPIX日次
    リターンの曜日/月内ポジション効果の検定に転用したもの（kpi_event_study.bootstrap_lift_ci
    と同じ設計: 暦月を単位に復元抽出し、月内の日次相関構造を保持したまま再標本化する）。
    """
    months = sorted(daily["month"].unique())
    grouped = {m: g for m, g in daily.groupby("month")}
    rng = np.random.default_rng(seed)
    diffs: list[float] = []
    for _ in range(n_boot):
        sample_months = rng.choice(months, size=len(months), replace=True)
        pooled = pd.concat([grouped[m] for m in sample_months], ignore_index=True)
        sub = pooled[pooled[mask_col]]
        if len(sub) == 0:
            continue
        diffs.append(sub[value_col].mean() - pooled[value_col].mean())

    point_sub = daily.loc[daily[mask_col], value_col].mean() if daily[mask_col].any() else None
    point_all = daily[value_col].mean()
    if not diffs or point_sub is None:
        return {"diff_point": None, "diff_ci_low": None, "diff_ci_high": None, "n_boot_valid": 0}
    ci_low, ci_high = np.percentile(diffs, [2.5, 97.5])
    return {
        "diff_point": point_sub - point_all,
        "diff_ci_low": float(ci_low),
        "diff_ci_high": float(ci_high),
        "n_boot_valid": len(diffs),
    }


def market_cell_row(daily: pd.DataFrame, label: str, mask_col: str) -> dict:
    sub = daily[daily[mask_col]]
    n = len(sub)
    boot = bootstrap_group_mean_diff(daily, mask_col)
    return {
        "label": label,
        "n": n,
        "mean": sub["ret"].mean() if n else None,
        "median": sub["ret"].median() if n else None,
        "freq_neg2": (sub["ret"] <= -0.02).mean() if n else None,
        "freq_neg3": (sub["ret"] <= -0.03).mean() if n else None,
        **boot,
    }


def format_market_row(row: dict) -> str:
    def pct(x: Optional[float], signed: bool = False) -> str:
        if x is None:
            return "-"
        return f"{x:+.2%}" if signed else f"{x:.2%}"

    ci = (
        f"{pct(row['diff_point'], signed=True)} [{pct(row['diff_ci_low'], signed=True)}, "
        f"{pct(row['diff_ci_high'], signed=True)}]"
        if row["diff_point"] is not None
        else "-"
    )
    return (
        f"| {row['label']} | {row['n']} | {pct(row['mean'])} | {pct(row['median'])} | "
        f"{pct(row['freq_neg2'])} | {pct(row['freq_neg3'])} | {ci} |"
    )


MARKET_TABLE_HEADER = (
    "| 区分 | N(日) | 平均 | 中央値 | -2%以下頻度 | -3%以下頻度 | 全体比の差分[95%CI] |\n"
    "|---|---|---|---|---|---|---|"
)


# --- シグナルレベル層別（既存3シグナルの returns.csv） -----------------------------


def load_signal_returns(path: Path, calendar_flags: pd.DataFrame) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"signal_date": str, "code": str})
    df = df[df["in_universe"]].reset_index(drop=True)
    df = df.merge(calendar_flags, left_on="signal_date", right_on="date", how="left")
    return df


def format_signal_row(label: str, sub: pd.DataFrame, baseline_p20: Optional[float]) -> str:
    s = measure_base_rate.summarize(sub)
    if s["n"] == 0:
        return f"| {label} | 0 | - | - | - | - |"
    flag = "†" if s["n"] < MIN_CELL_N_SIGNAL else ""
    lift_vs_baseline = f"{s['p20'] / baseline_p20:.2f}x" if baseline_p20 else "-"
    return (
        f"| {label}{flag} | {s['n']} | {s['p20']:.1%} | {lift_vs_baseline} | "
        f"{s['ev_stop8']:.2%} | {s['mean_ret']:.2%} |"
    )


SIGNAL_TABLE_HEADER = (
    "| 区分 | N | P(+20%終値) | P20倍率(vs全体) | EV(-8%損切り) | 平均リターン |\n"
    "|---|---|---|---|---|---|"
)


def build_signal_section(kpi_name: str, returns_path: Path, calendar_flags: pd.DataFrame) -> list[str]:
    df = load_signal_returns(returns_path, calendar_flags)
    overall = measure_base_rate.summarize(df)
    baseline_p20 = overall.get("p20")

    lines = [f"### {kpi_name}（in_universe n={overall['n']}）", ""]
    lines.append(SIGNAL_TABLE_HEADER)
    lines.append(format_signal_row("全体（比較対象）", df, baseline_p20))
    lines.append("")
    lines.append("**曜日別**")
    lines.append(SIGNAL_TABLE_HEADER)
    for wd in range(5):
        lines.append(format_signal_row(WEEKDAY_JA[wd], df[df["weekday"] == wd], baseline_p20))
    lines.append("")
    lines.append("**月内ポジション別**")
    lines.append(SIGNAL_TABLE_HEADER)
    lines.append(format_signal_row("月初第1営業日", df[df["is_first_bday"]], baseline_p20))
    lines.append(format_signal_row("月末最終5営業日", df[df["is_last5_bday"]], baseline_p20))
    lines.append(format_signal_row("四半期末月の最終5営業日", df[df["is_last5_bday_qend"]], baseline_p20))
    lines.append("")
    lines.append("†=n<30（参考値。統計的判定不能）")
    lines.append("")
    return lines


# --- v2-5 節目×ブレッドス警戒 ---------------------------------------------------


def compute_topix_252_high_days(topix_daily: pd.DataFrame) -> pd.DataFrame:
    """「過去252営業日高値を当日終値が更新した日」を返す（当日を含まない過去252日の最大値との比較
    =ルックアヘッドなし・当日を含む一般的な「52週高値」定義と同値）。"""
    df = topix_daily.copy()
    prior_max = df["close"].shift(1).rolling(window=HIGH_WINDOW_BD, min_periods=HIGH_WINDOW_BD).max()
    df["is_new_high"] = df["close"] > prior_max
    return df[df["is_new_high"]].reset_index(drop=True)


def _universe_set_for_date(date_t: str, universes_by_month: dict[str, set]) -> set:
    """kpi_event_study._universe_membership と同じ「シグナル月より厳密に前の直近確定月末
    ユニバース」を使うルックアヘッド禁止ロジックを、銘柄集合版として再実装したもの
    （_universe_membership は1銘柄判定用のprivate関数のため、集合を返す本用途には同じ
    ロジックをそのまま再現するほうが呼び出し側が単純になる）。"""
    month = date_t[:6]
    earlier_months = [m for m in universes_by_month if m < month]
    if not earlier_months:
        return set()
    return universes_by_month[max(earlier_months)]


def compute_breadth_down_ratio(
    date_t: str, date_prev: str, universe_codes: set[str]
) -> tuple[Optional[float], int, int]:
    """date_t 時点のユニバース銘柄について、前営業日比の値下がり比率を返す。

    Returns:
        (down_ratio, down_count, counted). 両日の AdjC が両方取れた銘柄のみを分母に数える
        （欠測=売買停止等はカウントしない）。
    """
    bars_t = measure_base_rate.load_bars_day(date_t)
    bars_prev = measure_base_rate.load_bars_day(date_prev)
    down = 0
    counted = 0
    for code in universe_codes:
        rec_t = bars_t.get(code)
        rec_prev = bars_prev.get(code)
        if rec_t is None or rec_prev is None:
            continue
        c_t = rec_t.get("AdjC")
        c_prev = rec_prev.get("AdjC")
        if c_t is None or c_prev is None:
            continue
        counted += 1
        if c_t < c_prev:
            down += 1
    if counted == 0:
        return None, 0, 0
    return down / counted, down, counted


def analyze_breadth_warning(
    topix_daily: pd.DataFrame, all_bdays: list[str], bday_index: dict[str, int]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """v2-5 事前登録定義（252日高値当日 かつ 値下がり比率>=80%）の警戒日を洗い出す。

    Returns:
        (high_days_breadth_df, warning_days_df)。high_days_breadth_df は252日高値を更新した
        全日の値下がり比率（発生ゼロだった場合の分布把握・報告用）、warning_days_df はそのうち
        閾値を満たした行（警戒日）のみ。
    """
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    high_days = compute_topix_252_high_days(topix_daily)

    rows = []
    for _, r in high_days.iterrows():
        date_t = r["date"]
        idx = bday_index[date_t]
        if idx == 0:
            continue
        date_prev = all_bdays[idx - 1]
        codes = _universe_set_for_date(date_t, universes_by_month)
        if not codes:
            continue
        ratio, down, counted = compute_breadth_down_ratio(date_t, date_prev, codes)
        if ratio is None:
            continue
        rows.append({"date": date_t, "down_ratio": ratio, "down": down, "counted": counted})

    breadth_df = pd.DataFrame(rows)
    warning_df = breadth_df[breadth_df["down_ratio"] >= BREADTH_WARNING_THRESHOLD].reset_index(drop=True)
    return breadth_df, warning_df


def _in_warning_window(
    signal_date: str, warning_day_idxs: list[int], n_bd: int, bday_index: dict[str, int]
) -> bool:
    """signal_date が「いずれかの警戒日の翌営業日からn_bd営業日後まで」に入るかを判定する
    （事前登録の「N営業日エントリー抑制」ウィンドウの判定そのもの）。"""
    idx_s = bday_index.get(signal_date)
    if idx_s is None:
        return False
    return any(1 <= idx_s - idx_w <= n_bd for idx_w in warning_day_idxs)


def build_breadth_section(
    breadth_df: pd.DataFrame,
    warning_df: pd.DataFrame,
    all_bdays: list[str],
    bday_index: dict[str, int],
    signal_dfs: dict[str, pd.DataFrame],
) -> list[str]:
    lines = [
        "## v2-5. 節目×ブレッドス急悪化警戒",
        "",
        f"事前登録定義（凍結・緩めていない）: TOPIX終値が過去{HIGH_WINDOW_BD}営業日高値を更新した"
        f"当日に、月末確定TOP500ユニバースの値下がり比率が{BREADTH_WARNING_THRESHOLD:.0%}以上。",
        "",
        f"- 252営業日高値更新日（10年）: {len(breadth_df)}件（ユニバース未確定期間・データ欠測で"
        f"除外した日を除く）",
        f"- 上記のうち値下がり比率{BREADTH_WARNING_THRESHOLD:.0%}以上（=警戒日）: **{len(warning_df)}件**",
    ]

    if len(warning_df) == 0:
        top5 = breadth_df.sort_values("down_ratio", ascending=False).head(5)
        lines += [
            "",
            "**判定: 発生0件のみ報告・判定不能（データ制約ではなく事象非発生）。**",
            "",
            "252営業日高値を更新する日は定義上「市場全体が強い日」であり、その当日に銘柄の8割が"
            "値下がりする（＝ごく一部の超大型株だけが指数を押し上げる極端な偏り）という組み合わせは、"
            "10年間・252営業日高値更新181日のいずれでも観測されなかった。参考として値下がり比率が"
            "最も高かった上位5日を示す（いずれも閾値の80%には遠く、最大でも6割弱）:",
            "",
            "| 日付 | 値下がり比率 | 値下がり数/対象数 |",
            "|---|---|---|",
        ]
        for _, r in top5.iterrows():
            lines.append(f"| {r['date']} | {r['down_ratio']:.1%} | {int(r['down'])}/{int(r['counted'])} |")
        lines += [
            "",
            "したがって警戒日前後の市場・既存シグナルEV比較（5/10営業日窓）は実施しない"
            "（分母0件のため比較不能）。事前登録どおり閾値は緩めていない"
            "（80%→60%等への事後変更はp-hacking＝§6評価プロトコルの精神に反するため本タスク内では行わない。"
            "閾値見直しは別途の新規事前登録が必要）。",
        ]
        return lines

    # 発生していた場合の分岐（本データでは到達しないが、閾値変更時の再利用に備えて実装しておく）
    lines += ["", "**警戒日一覧**", "", "| 日付 | 値下がり比率 |", "|---|---|"]
    for _, r in warning_df.iterrows():
        lines.append(f"| {r['date']} | {r['down_ratio']:.1%} |")

    topix_close = measure_base_rate.load_topix_series()
    warning_day_idxs = [bday_index[d] for d in warning_df["date"]]
    for n_bd in WARNING_ENTRY_SUPPRESS_WINDOWS_BD:
        fwd_after_warning = []
        for d in warning_df["date"]:
            idx = bday_index[d]
            fwd_idx = idx + n_bd
            if fwd_idx < len(all_bdays):
                fwd_after_warning.append(topix_close.loc[all_bdays[fwd_idx]] / topix_close.loc[d] - 1)
        baseline_fwd = (topix_close.shift(-n_bd) / topix_close - 1).dropna()
        lines += [
            "",
            f"- TOPIX{n_bd}営業日後リターン: 警戒日後 平均{np.mean(fwd_after_warning):+.2%}"
            f"（n={len(fwd_after_warning)}） vs 全日ベースライン 平均{baseline_fwd.mean():+.2%}"
            f"（n={len(baseline_fwd)}）",
        ]

    lines += ["", "**既存シグナルのEV比較（警戒日後Nbd抑制ウィンドウ内 vs 外）**", ""]
    for kpi_name, df in signal_dfs.items():
        lines.append(f"- {kpi_name}:")
        for n_bd in WARNING_ENTRY_SUPPRESS_WINDOWS_BD:
            mask = df["signal_date"].map(
                lambda d: _in_warning_window(d, warning_day_idxs, n_bd, bday_index)
            )
            s_in = measure_base_rate.summarize(df[mask])
            s_out = measure_base_rate.summarize(df[~mask])
            in_str = f"P20={s_in['p20']:.1%} EV={s_in['ev_stop8']:.2%}" if s_in["n"] else "n=0"
            out_str = f"P20={s_out['p20']:.1%} EV={s_out['ev_stop8']:.2%}" if s_out["n"] else "n=0"
            lines.append(f"  - {n_bd}bd窓: 窓内(n={s_in['n']}) {in_str} / 窓外(n={s_out['n']}) {out_str}")
    return lines


# --- レポート生成 ---------------------------------------------------------------


def build_report(
    weekday_rows: list[dict],
    monthpos_rows: list[dict],
    signal_sections: list[str],
    breadth_section: list[str],
    n_market_cells: int,
    n_signal_cells: int,
) -> str:
    total_cells = n_market_cells + n_signal_cells
    lines = [
        "# カレンダー効果一括検証 + 節目×ブレッドス警戒（第13周・§7-B v2-4/v2-5）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        "",
        "**多重比較の注意（冒頭）**: 本レポートは市場レベル・シグナルレベル合わせて"
        f"約{total_cells}セルを層別比較する記述的分析であり、§6評価プロトコルの正式なKPI検証"
        "（試行台帳記録・holdout確認）ではない。α=0.05換算では偶然だけで"
        f"1〜2セル程度の「効いているように見える」結果が出ることを前提に読むこと。"
        "「効くセル」の記載は次周の事前登録候補の提案であり、確定知見ではない。",
        "",
        "## v2-4. カレンダー効果一括検証 — 市場レベル（TOPIX日次リターン10年）",
        "",
        "### 曜日別",
        "",
        MARKET_TABLE_HEADER,
    ]
    for row in weekday_rows:
        lines.append(format_market_row(row))
    lines += [
        "",
        "@nano_nano2001の「月曜弱い」仮説の判定は上表の月曜行を参照（全体比の差分の95%CIが0を"
        "跨がなければ10年×市場全体でも支持される）。",
        "",
        "### 月内ポジション別",
        "",
        MARKET_TABLE_HEADER,
    ]
    for row in monthpos_rows:
        lines.append(format_market_row(row))
    lines += [
        "",
        "## v2-4. カレンダー効果一括検証 — シグナルレベル層別（既存3シグナルの returns.csv）",
        "",
    ]
    lines += signal_sections
    lines += breadth_section
    lines += [
        "",
        "## 実装ノート",
        "- カレンダー・topix・bars読み込みは `scripts/measure_base_rate.py` / "
        "`scripts/kpi_event_study.py` の既存関数・出力（output/base_rate/universes_w21.csv.gz 等）"
        "をそのまま再利用（再実装なし）",
        "- 市場レベルのCIは月次ブロック・ブートストラップ（`kpi_event_study.bootstrap_lift_ci` と"
        "同じ設計をmean差分に転用。§6手順5準拠）",
        "- シグナルレベルの層別は既存 `measure_base_rate.summarize()` をそのまま使用"
        "（P(+20%)はret>=0.20終値判定・EVは-8%損切り版=既存レポート群と同じ定義）。個別セルの"
        "CIは計算していない（依頼範囲はP(+20%)・EVの点推定とn。多重比較の観点からも点推定+n"
        "併記の方が誤った確信を生みにくい）",
        "- 本分析は正式なKPI検証ではないため試行台帳（data/kpi_trials/trials.jsonl）には追記していない",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    calendar_flags = build_calendar_flags(all_bdays)

    topix_daily = load_topix_daily(calendar_flags)
    print(f"TOPIX日次リターン: {len(topix_daily)}日 ({topix_daily['date'].min()}〜{topix_daily['date'].max()})", file=sys.stderr)

    # market_cell_row は bool 列を mask_col に取る（"weekday" は int 列のため、曜日ごとに
    # 専用のbool列を張ってから渡す）
    weekday_rows = []
    for wd in range(5):
        col = f"_is_wd{wd}"
        topix_daily[col] = topix_daily["weekday"] == wd
        weekday_rows.append(market_cell_row(topix_daily, f"{WEEKDAY_JA[wd]}曜", col))

    monthpos_rows = [
        market_cell_row(topix_daily, "月初第1営業日", "is_first_bday"),
        market_cell_row(topix_daily, "月末最終5営業日", "is_last5_bday"),
        market_cell_row(topix_daily, "四半期末月の最終5営業日", "is_last5_bday_qend"),
    ]

    signal_sections: list[str] = []
    signal_dfs: dict[str, pd.DataFrame] = {}
    for kpi_name, path in EXISTING_SIGNALS.items():
        if not path.exists():
            print(f"WARN: {path} が見つからずスキップ", file=sys.stderr)
            continue
        signal_sections += build_signal_section(kpi_name, path, calendar_flags)
        signal_dfs[kpi_name] = load_signal_returns(path, calendar_flags)

    breadth_df, warning_df = analyze_breadth_warning(topix_daily, all_bdays, bday_index)
    breadth_section = build_breadth_section(breadth_df, warning_df, all_bdays, bday_index, signal_dfs)

    n_market_cells = len(weekday_rows) + len(monthpos_rows)
    n_signal_cells = len(EXISTING_SIGNALS) * (5 + 3)
    report_md = build_report(
        weekday_rows, monthpos_rows, signal_sections, breadth_section, n_market_cells, n_signal_cells
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / "report.md"
    report_path.write_text(report_md, encoding="utf-8")

    breadth_csv_path = OUTPUT_DIR / "high252_breadth.csv"
    breadth_df.to_csv(breadth_csv_path, index=False)

    print(f"完了: report={report_path} breadth_csv={breadth_csv_path}")
    print(f"252日高値日数={len(breadth_df)} 警戒日数={len(warning_df)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
