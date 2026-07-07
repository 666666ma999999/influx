#!/usr/bin/env python3
"""第13周A: 超小型時価総額アンプ(v2-1) + 出来高「静→動」パターン(v2-2)（カタログ§7-B）。

volshock_x_above200（主・n=180）と volshock_5x（対照・n=292）の既存
`output/kpi/<kpi>/returns.csv` の in_universe ポジションを再利用し、**エントリー再計算は一切
行わない**（signal_date/code/entry_date/entry_price/ret/mfe/mae/regime/month等は既存出力のまま）。
各シグナルへ2つの増幅特徴（時価総額バケット・quiet_ratio）を付与し、両母集団を分割して比較する。
正式トライアル（台帳記録）は above200 母集団のみの2本（team-lead指示・§7-B v2-1/v2-2）。

## v2-1 超小型時価総額アンプ: 時価総額の算出方法（実データで確定・カタログ§7-B原案から変更）
カタログ原案は「株式数×AdjC」だが、これは実データ検証の結果**誤り**と判明したため変更した
（本モジュール§0検証セクション、または output/kpi/volshock_v2_amplifiers/report.md 参照）。
根拠: J-Quants fins開示（`data/jquants/fins/`）に発行済株式数(ShOutFY)・自己株式数(TrShFY)が
直接存在する（proxy不要）。これは各開示時点での**未調整の実数**であり、一方AdjCは**将来の分割も
遡及適用済み**の価格である。両者をそのまま乗じると、判定基準日から"現在"までの間に分割があった
銘柄の時価総額が分割比率倍に過小評価される。実測例: 任天堂(79740)は2022-09-29に10:1分割、
2022-09-30時点のShOutFY（8月開示・分割前）×AdjCでは0.68兆円と算出されるが、正しい方法
（後述の式）では6.8兆円となり実際の市場観測（当時報道の時価総額水準）と整合する。
ソフトバンクG(99840)は逆に「未来の分割（本データセットの範囲外）」により2022-09-30時点のAdjCが
実際の当日終値の1/4に遡及調整されており、これも同様の過小評価を招く。

採用式: market_cap(x) = net_shares(D) * C(D) * (AdjC(x) / AdjC(D))
    D = code の x 以前で直近の開示日（net_shares = ShOutFY(D) - TrShFY(D)）
    net_shares(D) * C(D) は D時点の生の時価総額（分割整合済み・常に正しい）
    AdjC(x)/AdjC(D) は split中立な「D→xの純価格変化」倍率（分割前後どちらでも打ち消し合う）
ランクは各シグナルの universe_month_used（前月末確定ユニバース・look-ahead回避）の
TOP500内で計算し、下位30%を「超小型」と定義する。

## v2-2 出来高「静→動」パターン
quiet_ratio = mean(Va[D-10..D-1]) / mean(Va[D-30..D-11])（D=シグナル日を含まない・PIT安全）。
quiet_ratio >= 1.2 を「静かな漸増あり」と定義する。

## Canonical Module再利用
- フォワードリターン列（ret/mfe/mae/ret_stop8等）は既存 returns.csv をそのまま使用（再計算なし）
- 分割サブセットの n/lift/CI/EV は `kpi_event_study.compute_stats`/`bootstrap_lift_ci` を再利用
- 台帳記録は `kpi_event_study.judge`/`append_trial` を再利用

Usage:
    python3 scripts/kpi_volshock_v2_amplifiers.py
"""
from __future__ import annotations

import math
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_stats/judge/append_trial等を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: load_fins_day・IN_SAMPLE_START/END を再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: FINS_HISTORY_START_BD を再利用・二重定義しない)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/universe読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)

POPULATIONS = {
    "volshock_x_above200": Path("output/kpi/volshock_x_above200/returns.csv"),
    "volshock_5x": Path("output/kpi/volshock_5x/returns.csv"),
}
FORMAL_TRIAL_POPULATION = "volshock_x_above200"  # team-lead指示: 正式トライアルはabove200のみ
BASELINE_NOTE = "volshock_x_above200基準線: n=180 lift=2.52[1.38,4.17] EV(なし)+1.12%（第11周既存レポート）"

OUTPUT_DIR = Path("output/kpi/volshock_v2_amplifiers")
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21

MICRO_CAP_PERCENTILE = 0.30  # 下位30%帯
QUIET_RATIO_THRESHOLD = 1.2
QUIET_RECENT_WINDOW = 10  # D-10..D-1
QUIET_PRIOR_WINDOW = 20  # D-30..D-11

FINS_HISTORY_START_BD = kpi_uprev_signals.FINS_HISTORY_START_BD  # "20160706"（finsキャッシュ実データ先頭日）


def _parse_numeric(raw) -> Optional[float]:
    """finsの数値フィールド（文字列 or 空文字 "" or None）をfloatにパースする。"""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# --- 発行済株式数PIT履歴構築（v2-1用） -------------------------------------------


def build_shares_history(
    hist_start_bd: str, hist_end_bd: str, all_bdays: list[str]
) -> tuple[dict[str, list[tuple[str, float, float, float]]], dict]:
    """[hist_start_bd, hist_end_bd] の全fins開示からCode別の株式数履歴を構築する。

    種別（DocType）不問（ShOutFYが数値のものはFY/1Q/2Q/3Q決算短信のいずれにも存在する）。
    各エントリは (disc_date, net_shares, c_disc, adjc_disc)。c_disc/adjc_disc は開示日当日の
    生終値/調整済み終値（market_cap算出時のロールフォワードに必要・§0式参照）。

    kpi_uprev_signals.build_fop_history と同型のパターン（対象フィールドが異なるため
    同一関数の再利用はできない・意図的な複製）。
    """
    history: dict[str, list[tuple[str, float, float, float]]] = defaultdict(list)
    stats = {
        "scanned_days": 0, "total_records": 0, "shout_numeric_records": 0,
        "no_bars_on_disc_day": 0, "net_shares_nonpositive": 0,
    }
    scan_days = [d for d in all_bdays if hist_start_bd <= d <= hist_end_bd]
    stats["scanned_days"] = len(scan_days)
    for d in scan_days:
        records = kpi_pead_signals.load_fins_day(d)
        bars_d: Optional[dict] = None
        for rec in records:
            stats["total_records"] += 1
            code = rec.get("Code")
            if not code:
                continue
            shout = _parse_numeric(rec.get("ShOutFY"))
            if shout is None:
                continue
            trsh = _parse_numeric(rec.get("TrShFY")) or 0.0
            net_shares = shout - trsh
            if net_shares <= 0:
                stats["net_shares_nonpositive"] += 1
                continue
            if bars_d is None:
                bars_d = measure_base_rate.load_bars_day(d)
            bars_rec = bars_d.get(code)
            if bars_rec is None or not bars_rec.get("C") or not bars_rec.get("AdjC"):
                stats["no_bars_on_disc_day"] += 1
                continue
            stats["shout_numeric_records"] += 1
            history[code].append((d, net_shares, bars_rec["C"], bars_rec["AdjC"]))
    for code in history:
        history[code].sort(key=lambda t: t[0])
    return history, stats


def shares_asof(code: str, asof_date: str, history: dict) -> Optional[tuple[str, float, float, float]]:
    """asof_date以前（含む）で最も直近の(disc_date, net_shares, c_disc, adjc_disc)を返す。"""
    recs = [t for t in history.get(code, []) if t[0] <= asof_date]
    if not recs:
        return None
    return max(recs, key=lambda t: t[0])


def compute_market_cap(
    code: str, asof_date: str, history: dict, bars_asof_day: dict
) -> Optional[float]:
    """market_cap(asof_date) = net_shares(D) * c_disc(D) * (AdjC(asof_date)/AdjC(D))（§0式）。"""
    entry = shares_asof(code, asof_date, history)
    if entry is None:
        return None
    _disc_date, net_shares, c_disc, adjc_disc = entry
    if not adjc_disc:
        return None
    rec_x = bars_asof_day.get(code)
    if rec_x is None or not rec_x.get("AdjC"):
        return None
    return net_shares * c_disc * (rec_x["AdjC"] / adjc_disc)


def build_micro_sets(
    universe_months: list[str],
    universes_by_month: dict[str, set],
    calendar_days: list[tuple[str, str]],
    history: dict,
) -> tuple[dict[str, set], dict[str, set], dict[tuple[str, str], float], dict]:
    """各universe_monthについて、TOP500内の時価総額下位30%コード集合(micro)と、
    時価総額が計算できたコード集合(valid)、コードごとの時価総額値(cap_lookup)を返す。
    """
    micro_sets: dict[str, set] = {}
    valid_sets: dict[str, set] = {}
    cap_lookup: dict[tuple[str, str], float] = {}
    diag = {"months_processed": 0, "market_cap_unknown_total": 0, "universe_members_total": 0}
    for m in universe_months:
        month_ends = measure_base_rate.month_ends_in_range(calendar_days, m, m)
        if not month_ends:
            micro_sets[m] = set()
            valid_sets[m] = set()
            continue
        t_date = month_ends[0]
        bars_t = measure_base_rate.load_bars_day(t_date)
        codes = universes_by_month.get(m, set())
        diag["universe_members_total"] += len(codes)
        caps: list[tuple[str, float]] = []
        unknown = 0
        for code in codes:
            mc = compute_market_cap(code, t_date, history, bars_t)
            if mc is None:
                unknown += 1
                continue
            caps.append((code, mc))
            cap_lookup[(m, code)] = mc
        diag["market_cap_unknown_total"] += unknown
        caps.sort(key=lambda x: x[1])
        cutoff = math.ceil(len(caps) * MICRO_CAP_PERCENTILE) if caps else 0
        micro_sets[m] = {c for c, _ in caps[:cutoff]}
        valid_sets[m] = {c for c, _ in caps}
        diag["months_processed"] += 1
    return micro_sets, valid_sets, cap_lookup, diag


# --- quiet_ratio計算（v2-2用） ---------------------------------------------------


def compute_quiet_ratio(
    code: str, signal_date: str, bday_index: dict[str, int], all_bdays: list[str]
) -> Optional[float]:
    """quiet_ratio = mean(Va[D-10..D-1]) / mean(Va[D-30..D-11])（D=signal_date、Dを含まない）。"""
    idx = bday_index[signal_date]
    if idx - QUIET_PRIOR_WINDOW - QUIET_RECENT_WINDOW < 0:
        return None
    recent_days = all_bdays[idx - QUIET_RECENT_WINDOW : idx]
    prior_days = all_bdays[idx - QUIET_RECENT_WINDOW - QUIET_PRIOR_WINDOW : idx - QUIET_RECENT_WINDOW]
    va_recent = [
        v for d in recent_days
        if (v := measure_base_rate.load_bars_day(d).get(code, {}).get("Va"))
    ]
    va_prior = [
        v for d in prior_days
        if (v := measure_base_rate.load_bars_day(d).get(code, {}).get("Va"))
    ]
    if not va_recent or not va_prior:
        return None
    mean_prior = sum(va_prior) / len(va_prior)
    if mean_prior == 0:
        return None
    return (sum(va_recent) / len(va_recent)) / mean_prior


# --- データ読み込み --------------------------------------------------------------


def load_positions(returns_csv_path: Path) -> pd.DataFrame:
    """既存returns.csvからin_universe行を全列そのまま読み込む（再計算なし）。"""
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
    return df


# --- 集計・レポート --------------------------------------------------------------


def _fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "-"


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.2%}" if x is not None else "-"


def _stats_row(label: str, stats: dict) -> str:
    ci = f"[{_fmt_ratio(stats['ci_low'])}, {_fmt_ratio(stats['ci_high'])}]"
    return (
        f"| {label} | {stats['n']} | {_fmt_pct(stats.get('p20'))} | {_fmt_ratio(stats.get('point_lift'))} | "
        f"{ci} | {_fmt_pct(stats.get('ev_none'))} | {_fmt_pct(stats.get('ev_stop8'))} | "
        f"{stats.get('months_spanned', 0)} |"
    )


STATS_TABLE_HEADER = (
    "| 区分 | N | P(+20%) | リフト(点推定) | リフトCI95% | EV(なし) | EV(-8%損切り) | 分散月数 |\n"
    "|---|---|---|---|---|---|---|---|"
)


def main() -> int:
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    positions_by_pop = {name: load_positions(path) for name, path in POPULATIONS.items()}

    universe_months: set[str] = set()
    for df in positions_by_pop.values():
        universe_months |= set(df["universe_month_used"])
    universe_months = sorted(universe_months)
    print(f"対象universe_month: {len(universe_months)}ヶ月 ({universe_months[0]}〜{universe_months[-1]})", file=sys.stderr)

    hist_end_month = kpi_pead_signals.IN_SAMPLE_END
    hist_end_bd = measure_base_rate.month_ends_in_range(calendar_days, hist_end_month, hist_end_month)[0]
    print("株式数PIT履歴構築中...", file=sys.stderr)
    shares_history, shares_diag = build_shares_history(FINS_HISTORY_START_BD, hist_end_bd, all_bdays)
    print(
        f"株式数履歴構築完了: 銘柄数={len(shares_history)} "
        f"(走査日数={shares_diag['scanned_days']}, ShOutFY数値あり={shares_diag['shout_numeric_records']}, "
        f"開示日bars欠測={shares_diag['no_bars_on_disc_day']}, 非正値除外={shares_diag['net_shares_nonpositive']})",
        file=sys.stderr,
    )

    print("月次「超小型」帯構築中...", file=sys.stderr)
    micro_sets, valid_sets, cap_lookup, micro_diag = build_micro_sets(
        universe_months, universes_by_month, calendar_days, shares_history
    )
    print(
        f"時価総額ランク構築完了: {micro_diag['months_processed']}ヶ月分 "
        f"(ユニバース延べ{micro_diag['universe_members_total']}件中 "
        f"時価総額不明={micro_diag['market_cap_unknown_total']}件"
        f"={micro_diag['market_cap_unknown_total']/max(1, micro_diag['universe_members_total']):.1%})",
        file=sys.stderr,
    )

    def assign_market_cap_bucket(row) -> str:
        m, code = row["universe_month_used"], row["code"]
        if code in micro_sets.get(m, set()):
            return "micro"
        if code in valid_sets.get(m, set()):
            return "non_micro"
        return "unknown"

    def assign_market_cap(row) -> Optional[float]:
        return cap_lookup.get((row["universe_month_used"], row["code"]))

    print("quiet_ratio計算中...", file=sys.stderr)
    for name, df in positions_by_pop.items():
        df["market_cap"] = df.apply(assign_market_cap, axis=1)
        df["market_cap_bucket"] = df.apply(assign_market_cap_bucket, axis=1)
        df["quiet_ratio"] = df.apply(
            lambda row: compute_quiet_ratio(row["code"], row["signal_date"], bday_index, all_bdays), axis=1
        )
        df["quiet_bucket"] = df["quiet_ratio"].apply(
            lambda q: "unknown" if q is None or pd.isna(q) else ("quiet" if q >= QUIET_RATIO_THRESHOLD else "active")
        )
        print(
            f"  {name}: market_cap_bucket "
            f"{df['market_cap_bucket'].value_counts().to_dict()} / "
            f"quiet_bucket {df['quiet_bucket'].value_counts().to_dict()}",
            file=sys.stderr,
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, df in positions_by_pop.items():
        features_path = OUTPUT_DIR / f"signals_features_{name}.csv"
        df[[
            "signal_date", "code", "month", "regime", "universe_month_used",
            "market_cap", "market_cap_bucket", "quiet_ratio", "quiet_bucket", "ret",
        ]].to_csv(features_path, index=False)
        print(f"features -> {features_path}", file=sys.stderr)

    # --- 分割分析(両母集団×両アンプ) ---------------------------------------------
    split_results: dict[str, dict[str, dict]] = {}
    for name, df in positions_by_pop.items():
        baseline_stats = kpi_event_study.compute_stats(df, base_rate_by_month, PERIOD)
        micro_stats = kpi_event_study.compute_stats(df[df["market_cap_bucket"] == "micro"], base_rate_by_month, PERIOD)
        non_micro_stats = kpi_event_study.compute_stats(
            df[df["market_cap_bucket"] == "non_micro"], base_rate_by_month, PERIOD
        )
        quiet_stats = kpi_event_study.compute_stats(df[df["quiet_bucket"] == "quiet"], base_rate_by_month, PERIOD)
        active_stats = kpi_event_study.compute_stats(df[df["quiet_bucket"] == "active"], base_rate_by_month, PERIOD)
        split_results[name] = {
            "baseline": baseline_stats, "micro": micro_stats, "non_micro": non_micro_stats,
            "quiet": quiet_stats, "active": active_stats,
        }

    # --- 正式トライアル2本（above200のみ） ------------------------------------------
    above_df = positions_by_pop[FORMAL_TRIAL_POPULATION]
    trial_defs = [
        (
            "volshock_x_above200_micro",
            above_df[above_df["market_cap_bucket"] == "micro"],
            {
                "source_population": FORMAL_TRIAL_POPULATION,
                "source_returns_csv": str(POPULATIONS[FORMAL_TRIAL_POPULATION]),
                "amplifier": "micro_cap_bottom30pct",
                "market_cap_formula": "net_shares(D)*C(D)*(AdjC(x)/AdjC(D))",
                "micro_cap_percentile": MICRO_CAP_PERCENTILE,
                "market_cap_unknown_rate": micro_diag["market_cap_unknown_total"] / max(1, micro_diag["universe_members_total"]),
                "round": "13A_v2_amplifiers",
                "power_warning": "n極小の見込み。§6合格基準(n>=100等)は未達前提で登録する",
            },
        ),
        (
            "volshock_x_above200_quiet",
            above_df[above_df["quiet_bucket"] == "quiet"],
            {
                "source_population": FORMAL_TRIAL_POPULATION,
                "source_returns_csv": str(POPULATIONS[FORMAL_TRIAL_POPULATION]),
                "amplifier": "quiet_to_active_volume",
                "quiet_ratio_threshold": QUIET_RATIO_THRESHOLD,
                "quiet_recent_window": QUIET_RECENT_WINDOW,
                "quiet_prior_window": QUIET_PRIOR_WINDOW,
                "round": "13A_v2_amplifiers",
            },
        ),
    ]

    trial_stats: dict[str, tuple[dict, str, list[str]]] = {}
    for kpi_name, subset_df, params in trial_defs:
        stats = kpi_event_study.compute_stats(subset_df, base_rate_by_month, PERIOD)
        verdict, reasons = kpi_event_study.judge(stats)
        record = {
            "run_id": uuid.uuid4().hex,
            "ts": jq_fetch.now_jst().isoformat(),
            "kpi_name": kpi_name,
            "params": params,
            "period": {"start": PERIOD[0], "end": PERIOD[1]},
            "n": stats["n"],
            "lift": stats["point_lift"],
            "ci_low": stats["ci_low"],
            "ci_high": stats["ci_high"],
            "ev": stats["ev_stop8"],
            "verdict": verdict,
            "entry_mode": "reused_from_existing_returns_csv",
            "regime_filter": None,
        }
        kpi_event_study.append_trial(record, TRIALS_PATH)
        trial_stats[kpi_name] = (stats, verdict, reasons)
        print(
            f"台帳記録: {kpi_name} n={stats['n']} lift={_fmt_ratio(stats['point_lift'])} "
            f"ci95=[{_fmt_ratio(stats['ci_low'])},{_fmt_ratio(stats['ci_high'])}] verdict={verdict}"
        )

    write_report(split_results, trial_stats, shares_diag, micro_diag)
    print(f"report: {OUTPUT_DIR / 'report.md'}")
    return 0


def write_report(
    split_results: dict[str, dict[str, dict]],
    trial_stats: dict[str, tuple[dict, str, list[str]]],
    shares_diag: dict,
    micro_diag: dict,
) -> None:
    unknown_rate = micro_diag["market_cap_unknown_total"] / max(1, micro_diag["universe_members_total"])
    lines = [
        "# 第13周A: 超小型時価総額アンプ + 出来高「静→動」パターン 検証レポート（カタログ§7-B v2-1/v2-2）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降はロック中・本レポートの対象外）",
        f"比較基準線: {BASELINE_NOTE}",
        "",
        "## 0. 時価総額算出方法の実データ検証（v2-1着手前の必須確認）",
        "",
        "カタログ§7-B原案『株式数×AdjC』は実データ検証の結果、split銘柄で最大10倍規模の過小評価を",
        "招くことが判明したため、以下の式に変更した:",
        "",
        "`market_cap(x) = net_shares(D) * C(D) * (AdjC(x) / AdjC(D))`",
        "（D=xより前で直近の開示日、net_shares(D)=ShOutFY(D)-TrShFY(D)、C/AdjCは開示日Dの生/調整済み終値）",
        "",
        "根拠となった実測（本データセット内の実例）:",
        "- 発行済株式数(ShOutFY)・自己株式数(TrShFY)は `data/jquants/fins/` 開示に**直接存在**"
        "（proxy不要。J-Quants財務情報サマリーの一部フィールド）",
        "- 任天堂(79740)は2022-09-29に10:1分割。2022-09-30時点でShOutFY(8月開示・分割前)×AdjCを"
        "単純計算すると0.68兆円だが、上式（分割後C×開示日AdjC比率でロールフォワード）では6.8兆円"
        "となる（当時の実勢と整合する水準は後者）",
        "- ソフトバンクG(99840)は本データセット期間より後（2024年）に1:4分割。2022-09-30時点で"
        "AdjCは既にその将来分割を遡及調整済み（AdjC=1225 vs 生終値C=4900）のため、"
        "『生株数×AdjC』では時価総額が実際の1/4に過小評価される"
        "（生株数×生終値Cでは7.79兆円、これが正しい水準）",
        "",
        f"株式数PIT履歴構築: 走査日数={shares_diag['scanned_days']} / "
        f"ShOutFY数値あり開示={shares_diag['shout_numeric_records']} / "
        f"開示日のbars欠測で除外={shares_diag['no_bars_on_disc_day']} / "
        f"非正値(net_shares<=0)で除外={shares_diag['net_shares_nonpositive']}",
        f"月次ユニバース時価総額カバレッジ: 延べ{micro_diag['universe_members_total']}件中 "
        f"不明={micro_diag['market_cap_unknown_total']}件（{unknown_rate:.1%}）"
        "（発行済株式数の開示が一度もない新規上場直後銘柄等が主因。ランキング母数から除外）",
        "",
        "## 1. v2-1 超小型時価総額アンプ",
        "",
        "定義: シグナル月の前月末確定ユニバース（`universe_month_used`・look-ahead回避）のTOP500内で"
        f"時価総額を上記式で算出し、下位{MICRO_CAP_PERCENTILE:.0%}を「超小型」と定義。",
        "",
    ]
    for pop_name, cells in split_results.items():
        lines += [
            f"### {pop_name}（n={cells['baseline']['n']}）",
            "",
            STATS_TABLE_HEADER,
            _stats_row("全体(baseline)", cells["baseline"]),
            _stats_row("超小型(micro)", cells["micro"]),
            _stats_row("非超小型(non_micro)", cells["non_micro"]),
            "",
        ]

    micro_trial_stats, micro_verdict, micro_reasons = trial_stats["volshock_x_above200_micro"]
    lines += [
        "**正式トライアル: `volshock_x_above200_micro`（above200 かつ 超小型）**",
        "",
        f"> ⚠️ **検定力警告**: above200母集団(n=180)の30%規模想定でn={micro_trial_stats['n']}。"
        f"§6合格基準（n>=100 等）は構造的に未達の見込みで登録する（判定は判定保留/不合格が濃厚）。",
        "",
        STATS_TABLE_HEADER,
        _stats_row("volshock_x_above200_micro", micro_trial_stats),
        "",
        f"判定: **{kpi_event_study.VERDICT_LABELS_JA.get(micro_verdict, micro_verdict)}**",
        "",
    ]
    lines += [f"- {r}" for r in micro_reasons]
    lines += [
        "",
        "## 2. v2-2 出来高「静→動」パターン",
        "",
        f"定義: quiet_ratio = mean(Va[D-10..D-1]) / mean(Va[D-30..D-11])（D=シグナル日を含まない）。"
        f"quiet_ratio >= {QUIET_RATIO_THRESHOLD} を「静かな漸増あり(quiet)」と定義。",
        "",
    ]
    for pop_name, cells in split_results.items():
        lines += [
            f"### {pop_name}（n={cells['baseline']['n']}）",
            "",
            STATS_TABLE_HEADER,
            _stats_row("全体(baseline)", cells["baseline"]),
            _stats_row("静→動(quiet)", cells["quiet"]),
            _stats_row("非quiet(active)", cells["active"]),
            "",
        ]

    quiet_trial_stats, quiet_verdict, quiet_reasons = trial_stats["volshock_x_above200_quiet"]
    lines += [
        "**正式トライアル: `volshock_x_above200_quiet`（above200 かつ quiet_ratio>=1.2）**",
        "",
        STATS_TABLE_HEADER,
        _stats_row("volshock_x_above200_quiet", quiet_trial_stats),
        "",
        f"判定: **{kpi_event_study.VERDICT_LABELS_JA.get(quiet_verdict, quiet_verdict)}**",
        "",
    ]
    lines += [f"- {r}" for r in quiet_reasons]

    lines += [
        "",
        "## 3. 実装ノート",
        "- signals_raw.csv/returns.csv の in_universe 行を再利用し、entry_date/entry_price/ret等は"
        "一切再計算していない（既存出力の列をそのまま使用）",
        "- 分割サブセットのn/リフト/CI/EVは `kpi_event_study.compute_stats`/`bootstrap_lift_ci` を"
        "そのまま再利用（再実装なし）",
        "- 時価総額不明（`market_cap_bucket`='unknown'）・quiet_ratio不明（`quiet_bucket`='unknown'）"
        "の行は各分割セルの対象から自然に除外される（micro/non_microのいずれにも属さない）",
        f"- 台帳記録は{FORMAL_TRIAL_POPULATION}のみ（team-lead指示）。もう一方の母集団(volshock_5x)は"
        "比較用の分割分析のみで台帳には記録しない",
        "- per-signal特徴量は `output/kpi/volshock_v2_amplifiers/signals_features_<pop>.csv` に保存"
        "（手動照合・追加分析用）",
        "",
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
