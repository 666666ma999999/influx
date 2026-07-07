#!/usr/bin/env python3
"""第14周: TOP1000ユニバース感度検証（カタログ§7チャンピオン構成の追試）。

tasks/stock_algo_kpi_loop.md 第14周（事前登録・2026-07-07）を実装する。チャンピオン構成
【volshock×above200×quiet + E1 exit】（第13周・TOP500・n=73・リフト4.16[1.91,7.10]・
EV(なし)+3.48%・EV(E1)+2.84%）を、TOP1000ユニバース（`output/base_rate_top1000/`・
本スクリプト実行前に `measure_base_rate.py --top 1000` で作成済み）に対して再判定する。

前提（このスクリプト実行前に完了していること）:
    1. `measure_base_rate.py --start-month 2016-08 --end-month 2022-11 --top 1000
       --output-dir output/base_rate_top1000`
    2. `kpi_volshock_signals.py --start 2016-11 --end 2022-11 --filter-ma200 above
       --kpi-name va_top1000 --base-rate-dir output/base_rate_top1000`
       （T2=volshock×above200のTOP1000版。ハーネスが自動で台帳に記録する）

本スクリプトが行うこと（すべて既存 Canonical Module の再利用のみ・エントリー再計算なし）:
    1. T1=vaq_top1000（T2 に quiet_ratio>=1.2 を重ねた版）を生成し、台帳に記録する
       （quiet_ratio計算は `kpi_volshock_v2_amplifiers.compute_quiet_ratio` を再利用）
    2. T1/T2 双方の新ポジション集合に E1 シナリオ崩壊exitを適用する
       （`kpi_exit_study.build_price_history` / `_time_based_exit` / `_walk_e1_scenario_break` /
       `aggregate_rule_stats` を再利用。measure_base_rate.py 凍結のため exit判定ロジック自体は
       複製せず、判定対象ポジションが第12周の既存出力と異なる分だけ新規に日次パスを実行する）
    3. T1シグナルを「旧TOP500内(rank<=500) vs 501-1000位」に分割し、件数・EV・
       コスト感度(0.5%/1.0%)・売買代金分布を診断する（universes_w21.csv.gzのrank列を再利用。
       ランク再計算はしない）
    4. 事前登録3基準（tasks/stock_algo_kpi_loop.md 第14周）を機械判定する
    5. `output/kpi/universe_top1000/report.md` に一括レポートを出力する

Usage:
    python3 scripts/kpi_top1000_sensitivity.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_stats/judge/append_trial等を再利用)
import kpi_exit_study  # noqa: E402  (Canonical Module: build_price_history/_walk_e1_scenario_break等を再利用)
import kpi_volshock_v2_amplifiers as v2amp  # noqa: E402  (Canonical Module: compute_quiet_ratio を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読込・ROUND_TRIP_COSTを再利用)

BASE_RATE_DIR_TOP1000 = Path("output/base_rate_top1000")
UNIVERSE_WINDOW = 21
PERIOD = ("2016-11", "2022-11")  # 既存チャンピオンと同一のin-sample期間（§6凍結）

VA_TOP1000_RETURNS = Path("output/kpi/va_top1000/returns.csv")
OUTPUT_DIR = Path("output/kpi/universe_top1000")
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")

QUIET_RATIO_THRESHOLD = v2amp.QUIET_RATIO_THRESHOLD  # 1.2（閾値の事後チューニングはしない）

# チャンピオン(TOP500)基準線（第13周確定値・比較表示用。tasks/stock_algo_kpi_loop.md 第13周節）
CHAMPION_BASELINE = {
    "n": 73, "lift": 4.16, "ci_low": 1.91, "ci_high": 7.10,
    "ev_none": 0.0348, "ev_e1": 0.0284, "monthly_freq": 1.0,
}

# 第14周・事前登録3基準（tasks/stock_algo_kpi_loop.md 第14周節。変更禁止）
CRITERION_EV_E1_MIN = 0.02       # (a) EV(E1・コスト0.3%) >= +2%/回
CRITERION_FREQ_MULT_MIN = 2.0    # (b) 頻度 >= 現行の2倍
CRITERION_501_1000_COST = 0.005  # (c) 501-1000位帯がコスト0.5%でもEVプラス


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.2%}" if x is not None else "-"


def _fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "-"


# --- T1(vaq_top1000)生成: T2にquiet_ratio>=1.2を重ねる（quiet_ratio計算はv2ampを再利用） ------


def load_t2_positions() -> pd.DataFrame:
    """T2(va_top1000)のin_universeポジションを読み込む（ハーネス出力そのまま・再計算なし）。"""
    if not VA_TOP1000_RETURNS.exists():
        raise SystemExit(
            f"FATAL: {VA_TOP1000_RETURNS} が見つかりません。先に "
            f"`kpi_volshock_signals.py --filter-ma200 above --kpi-name va_top1000 "
            f"--base-rate-dir {BASE_RATE_DIR_TOP1000}` を実行してください。"
        )
    df = pd.read_csv(
        VA_TOP1000_RETURNS,
        dtype={
            "signal_date": str, "code": str, "month": str, "regime": str,
            "entry_date": str, "exit_date": str, "universe_month_used": str,
        },
    )
    df = df[df["in_universe"] == True].reset_index(drop=True)  # noqa: E712
    if df.empty:
        raise SystemExit(f"FATAL: {VA_TOP1000_RETURNS} に in_universe=True の行がありません")
    return df


def build_t1_vaq(t2_df: pd.DataFrame, bday_index: dict, all_bdays: list[str]) -> pd.DataFrame:
    """T2にquiet_ratio>=1.2を重ねてT1(vaq_top1000)を作る（compute_quiet_ratioを再利用）。"""
    t2_df = t2_df.copy()
    t2_df["quiet_ratio"] = t2_df.apply(
        lambda row: v2amp.compute_quiet_ratio(row["code"], row["signal_date"], bday_index, all_bdays), axis=1
    )
    t1_df = t2_df[t2_df["quiet_ratio"] >= QUIET_RATIO_THRESHOLD].reset_index(drop=True)
    return t1_df


# --- E1 exit適用（kpi_exit_study の日次ウォーク関数を再利用・ATR/β等の不要計算はスキップ） -------


def apply_e1_exit(positions_df: pd.DataFrame, all_bdays: list[str], bday_index: dict) -> pd.DataFrame:
    """positions_dfの各ポジションにE1シナリオ崩壊exitを適用し、E1列を付与したDataFrameを返す。

    kpi_exit_study.build_price_history / _time_based_exit / _walk_e1_scenario_break を
    そのまま再利用する（E1判定ロジック自体の複製はしない）。ATR20/βはE1に不要なため計算しない
    （kpi_exit_study.process_populationの全ルール一括計算とは異なり、E1単独の軽量版）。
    """
    if positions_df.empty:
        raise SystemExit("FATAL: E1適用対象のpositions_dfが空です")
    codes = set(positions_df["code"])
    entry_indices = [bday_index[d] for d in positions_df["entry_date"]]
    earliest_idx = bday_index[kpi_exit_study._earliest_bars_date()]
    scan_start_idx = max(earliest_idx, min(entry_indices) - kpi_exit_study.SCAN_BUFFER_BDAYS)
    scan_end_idx = min(len(all_bdays) - 1, max(entry_indices) + kpi_exit_study.FORWARD_WINDOW_BD + 5)
    scan_days = all_bdays[scan_start_idx : scan_end_idx + 1]

    prices, sma200_by_code = kpi_exit_study.build_price_history(codes, scan_days)

    rows = []
    for pos in positions_df.itertuples(index=False):
        entry_idx = bday_index[pos.entry_date]
        exit_idx_max = entry_idx + kpi_exit_study.FORWARD_WINDOW_BD
        fwd_days = all_bdays[entry_idx : exit_idx_max + 1]
        price_series = prices.get(pos.code, {})
        sma_series = sma200_by_code.get(pos.code, {})

        last_seen_day = fwd_days[0]
        for d in fwd_days[1:]:
            rec = price_series.get(d)
            if rec is not None and rec.get("AdjC") is not None:
                last_seen_day = d

        time_exit = kpi_exit_study._time_based_exit(fwd_days, price_series)
        prev_day = all_bdays[entry_idx - 1]
        exit_date, exit_price, reason = kpi_exit_study._walk_e1_scenario_break(
            fwd_days, price_series, sma_series, prev_day, time_exit
        )
        if exit_price is None:
            raise SystemExit(f"FATAL: code={pos.code} entry={pos.entry_date} でE1のexit_priceが計算できませんでした")
        ret_raw_e1 = exit_price / pos.entry_price - 1
        ret_costadj_e1 = ret_raw_e1 - measure_base_rate.ROUND_TRIP_COST
        holding_days_e1 = bday_index[exit_date] - entry_idx

        rows.append({
            "exit_date_E1": exit_date, "exit_price_E1": exit_price, "reason_E1": reason,
            "ret_raw_E1": ret_raw_e1, "ret_costadj_E1": ret_costadj_e1, "holding_days_E1": holding_days_e1,
        })

    e1_df = pd.DataFrame(rows)
    result = pd.concat([positions_df.reset_index(drop=True), e1_df], axis=1)
    result["ret_raw_C3"] = result["ret"]  # aggregate_rule_statsの「降ろされ損率」計算に必要（20bd終値=無停止版）
    return result


def e1_stats(detail_df: pd.DataFrame) -> dict:
    """kpi_exit_study.aggregate_rule_stats をそのまま再利用してE1の集計を返す。"""
    return kpi_exit_study.aggregate_rule_stats(detail_df, "E1")


# --- 501-1000位帯診断（rank列は既存universes_w21.csv.gzをそのまま参照・再計算なし） --------------


def load_rank_lookup() -> dict[tuple[str, str], int]:
    path = BASE_RATE_DIR_TOP1000 / f"universes_w{UNIVERSE_WINDOW}.csv.gz"
    df = pd.read_csv(path, dtype={"month": str, "code": str})
    return {(m, c): rk for m, c, rk in zip(df["month"], df["code"], df["rank"])}


def turnover_quartiles(codes_dates: list[tuple[str, str]]) -> dict:
    """(signal_date, code)ペアのリストについて、当日売買代金(Va)の中央値・四分位を返す。"""
    vas = []
    for signal_date, code in codes_dates:
        rec = measure_base_rate.load_bars_day(signal_date).get(code)
        if rec is not None and rec.get("Va"):
            vas.append(rec["Va"])
    if not vas:
        return {"n": 0, "median": None, "q1": None, "q3": None}
    s = pd.Series(vas)
    return {
        "n": len(vas),
        "median": float(s.median()),
        "q1": float(s.quantile(0.25)),
        "q3": float(s.quantile(0.75)),
    }


# --- 台帳記録 -------------------------------------------------------------------


def append_main_trial(kpi_name: str, stats: dict, verdict: str, params: dict) -> None:
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


def append_exit_e1_trial(kpi_name: str, base_stats: dict, e1: dict, source_pop: str) -> None:
    stats_for_judge = {
        "n": e1["n"],
        "months_spanned": base_stats["months_spanned"],
        "has_bull": base_stats["has_bull"],
        "has_bear": base_stats["has_bear"],
        "ci_low": base_stats["ci_low"],
        "ev_stop8": e1["ev"],
        "avg_monthly_n": base_stats["avg_monthly_n"],
    }
    verdict, _reasons = kpi_event_study.judge(stats_for_judge)
    record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": kpi_name,
        "params": {
            "exit_rule": "E1", "source_population": source_pop, "round": "14_universe_top1000_sensitivity",
            "sma200_window": kpi_exit_study.MA200_WINDOW,
            "round_cost_note": "第14周TOP1000感度検証・新ポジション集合へのE1新規適用（既存exit_studyの再利用はなし）",
        },
        "period": {"start": PERIOD[0], "end": PERIOD[1]},
        "n": e1["n"],
        "lift": base_stats.get("point_lift"),
        "ci_low": base_stats.get("ci_low"),
        "ci_high": base_stats.get("ci_high"),
        "ev": e1["ev"],
        "verdict": verdict,
        "entry_mode": "reused_from_existing_returns_csv",
        "regime_filter": None,
    }
    kpi_event_study.append_trial(record, TRIALS_PATH)


# --- レポート -------------------------------------------------------------------


def write_report(
    t1_stats: dict, t1_verdict: str, t1_reasons: list[str],
    t2_stats: dict,
    t1_e1: dict, t2_e1: dict,
    band_diag: dict,
    criteria: dict,
) -> None:
    lines = [
        "# 第14周: TOP1000ユニバース感度検証（カタログ§7チャンピオン構成の追試）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降は未着手・本検証の対象外）",
        "",
        "> **注記（規律）**: 本レポートは第13周チャンピオン構成【volshock×above200×quiet + E1 exit】"
        "のユニバース定義（TOP500→TOP1000）に対する**感度検証**である。良好な結果が出ても、"
        "それだけで本採用を意味しない——holdout(2023年以降)はロック中でありTOP1000版では未開封、"
        "また運用移管には前向き確認（ペーパートレード等の未来データでの再現）が別途必要になる。"
        "本採否判定はあくまで「TOP1000への切替を検討する価値があるか」の一次スクリーニングである。",
        "",
        "## 0. TOP1000ベースレート（in-sampleのみ・holdout 2023+は触っていない）",
        "",
        "| ユニバース | N | P(+20%終値) | 備考 |",
        "|---|---|---|---|",
        "| TOP500（既存） | 37,500 | 3.7% | `output/base_rate/`（第0周確定値） |",
        f"| TOP1000（本検証） | 75,000 | 4.5% | `output/base_rate_top1000/`（本検証で新規計測） |",
        "",
        "サニティ確認: 76ヶ月全てでユニバース件数=1000（過不足なし）。"
        "サンプル月(2018-11)でTOP1000のrank<=500集合とTOP500ユニバースが完全一致(差分0件)を確認。",
        "",
        "## 1. チャンピオン構成の再判定（TOP500 vs TOP1000）",
        "",
        "| 構成 | ユニバース | N | リフト(点推定) | リフトCI95% | EV(なし) | EV(-8%損切り) | EV(E1) | 月平均頻度 |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| チャンピオン基準線(第13周) | TOP500 | {CHAMPION_BASELINE['n']} | "
        f"{CHAMPION_BASELINE['lift']:.2f} | [{CHAMPION_BASELINE['ci_low']:.2f}, {CHAMPION_BASELINE['ci_high']:.2f}] | "
        f"{_fmt_pct(CHAMPION_BASELINE['ev_none'])} | - | {_fmt_pct(CHAMPION_BASELINE['ev_e1'])} | "
        f"{CHAMPION_BASELINE['monthly_freq']:.2f}件 |",
        f"| T1 vaq_top1000（volshock×above200×quiet） | TOP1000 | {t1_stats['n']} | "
        f"{_fmt_ratio(t1_stats['point_lift'])} | [{_fmt_ratio(t1_stats['ci_low'])}, {_fmt_ratio(t1_stats['ci_high'])}] | "
        f"{_fmt_pct(t1_stats['ev_none'])} | {_fmt_pct(t1_stats['ev_stop8'])} | {_fmt_pct(t1_e1['ev'])} | "
        f"{t1_stats['avg_monthly_n']:.2f}件 |",
        f"| T2 va_top1000（volshock×above200・参考） | TOP1000 | {t2_stats['n']} | "
        f"{_fmt_ratio(t2_stats['point_lift'])} | [{_fmt_ratio(t2_stats['ci_low'])}, {_fmt_ratio(t2_stats['ci_high'])}] | "
        f"{_fmt_pct(t2_stats['ev_none'])} | {_fmt_pct(t2_stats['ev_stop8'])} | {_fmt_pct(t2_e1['ev'])} | "
        f"{t2_stats['avg_monthly_n']:.2f}件 |",
        "",
        f"T1判定（§6評価プロトコル・in-sample基準）: **{kpi_event_study.VERDICT_LABELS_JA.get(t1_verdict, t1_verdict)}**",
        "",
    ] + [f"- {r}" for r in t1_reasons] + [
        "",
        "T1のE1発動診断: "
        f"発動率={_fmt_pct(t1_e1['trigger_rate'])}（n={t1_e1['triggered_n']}） / "
        f"降ろされ損率={_fmt_pct(t1_e1['dropped_loss_rate']) if t1_e1['dropped_loss_rate'] is not None else '-（発動0件）'} / "
        f"平均保有日数={t1_e1['avg_holding_days']:.1f}日",
        "",
        "## 2. 501-1000位帯の実行可能性診断（T1=vaq_top1000シグナルを分割）",
        "",
        "| 区分 | 件数 | P(+20%) | EV(なし・コスト0.3%) | EV(E1・コスト0.3%) |",
        "|---|---|---|---|---|",
    ]
    for label, s in (("旧TOP500内(rank<=500)", band_diag["old_top500"]), ("501-1000位", band_diag["rank501_1000"])):
        lines.append(
            f"| {label} | {s['n']} | {_fmt_pct(s.get('p20'))} | {_fmt_pct(s.get('ev_none'))} | {_fmt_pct(s.get('ev_e1'))} |"
        )
    lines += [
        "",
        "### (c) 501-1000位帯のコスト感度（往復コスト0.3%→0.5%/1.0%に引き上げた場合のEV）",
        "",
        "| コスト水準 | EV(なし・20bd終値) | EV(E1 exit) |",
        "|---|---|---|",
    ]
    r5 = band_diag["rank501_1000"]
    for cost_label, cost in (("0.3%(基準)", 0.003), ("0.5%", 0.005), ("1.0%", 0.010)):
        ev_none_c = r5["mean_ret_raw"] - cost if r5["mean_ret_raw"] is not None else None
        ev_e1_c = r5["mean_ret_raw_e1"] - cost if r5["mean_ret_raw_e1"] is not None else None
        lines.append(f"| {cost_label} | {_fmt_pct(ev_none_c)} | {_fmt_pct(ev_e1_c)} |")
    lines += [
        "",
        "### (d) 501-1000位帯シグナル銘柄の当日売買代金(Va)分布",
        "",
        f"- n={band_diag['turnover_501_1000']['n']} / "
        f"中央値={band_diag['turnover_501_1000']['median']/1e8:.2f}億円 / "
        f"Q1={band_diag['turnover_501_1000']['q1']/1e8:.2f}億円 / "
        f"Q3={band_diag['turnover_501_1000']['q3']/1e8:.2f}億円"
        if band_diag['turnover_501_1000']['n'] else "- n=0（501-1000位のシグナルなし）",
        "",
        "参考（第14周着手時の当日実測・タスク台帳より）: 500位=18.7億円/日 → 1000位=4.5億円/日 → 2000位=0.7億円/日",
        "",
        "## 3. 採否判定（事前登録3基準・機械判定）",
        "",
        "| # | 基準 | 実測値 | 判定 |",
        "|---|---|---|---|",
        f"| (a) | EV(E1・コスト0.3%) >= {CRITERION_EV_E1_MIN:.0%} | {_fmt_pct(t1_e1['ev'])} | "
        f"{'OK' if criteria['a_ev_e1_ok'] else 'NG'} |",
        f"| (b) | 頻度 >= 現行の{CRITERION_FREQ_MULT_MIN:.1f}倍（現行{CHAMPION_BASELINE['monthly_freq']:.1f}件"
        f"→閾値{CHAMPION_BASELINE['monthly_freq'] * CRITERION_FREQ_MULT_MIN:.1f}件） | "
        f"{t1_stats['avg_monthly_n']:.2f}件 | {'OK' if criteria['b_freq_ok'] else 'NG'} |",
        f"| (c) | 501-1000位帯がコスト0.5%でもEVプラス（E1基準） | "
        f"{_fmt_pct(criteria['c_rank501_1000_ev_at_cost5'])} | {'OK' if criteria['c_band_ok'] else 'NG'} |",
        "",
        f"**総合判定: {'3条件すべて充足 → TOP1000への切替を検討する価値あり（ユーザー判断+前向き確認が条件）' if criteria['all_ok'] else 'いずれか1条件以上が未達 → 事前登録基準によりTOP500を維持'}**",
        "",
        "## 実装ノート",
        "- T2(va_top1000)は`kpi_volshock_signals.py --filter-ma200 above --base-rate-dir "
        "output/base_rate_top1000`をそのまま実行（既存CLIオプションの再利用のみ・コード変更なし）",
        "- T1(vaq_top1000)はT2にquiet_ratio>=1.2を重ねた版。quiet_ratio計算は"
        "`kpi_volshock_v2_amplifiers.compute_quiet_ratio`を再利用（再実装なし）",
        "- E1 exitはT1/T2とも新規ポジション集合のため`kpi_exit_study`の日次ウォーク関数"
        "(`build_price_history`/`_time_based_exit`/`_walk_e1_scenario_break`)をそのまま再利用して"
        "新規に計算した（第12周の既存exit_study出力との単純結合はしていない。ポジション集合が"
        "TOP500版から変わっているため正しい計算には必須）",
        "- 501-1000位帯のrankは`output/base_rate_top1000/universes_w21.csv.gz`のrank列をそのまま参照"
        "（ランク再計算はしていない）",
        "- 台帳記録: vaq_top1000（主トライアル） / va_top1000（ハーネス実行時に自動記録済み） / "
        "vaq_top1000_exit_E1 / va_top1000_exit_E1 の計4行",
        "",
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR_TOP1000, UNIVERSE_WINDOW)

    print("T2(va_top1000)ポジション読込中...", file=sys.stderr)
    t2_df = load_t2_positions()
    t2_stats = kpi_event_study.compute_stats(t2_df, base_rate_by_month, PERIOD)
    print(f"T2: n={t2_stats['n']}", file=sys.stderr)

    print("T1(vaq_top1000)構築中(quiet_ratio計算)...", file=sys.stderr)
    t1_df = build_t1_vaq(t2_df, bday_index, all_bdays)
    t1_stats = kpi_event_study.compute_stats(t1_df, base_rate_by_month, PERIOD)
    t1_verdict, t1_reasons = kpi_event_study.judge(t1_stats)
    print(f"T1: n={t1_stats['n']} verdict={t1_verdict}", file=sys.stderr)

    append_main_trial(
        "vaq_top1000", t1_stats, t1_verdict,
        {
            "source_population": "va_top1000", "source_returns_csv": str(VA_TOP1000_RETURNS),
            "amplifier": "quiet_to_active_volume", "quiet_ratio_threshold": QUIET_RATIO_THRESHOLD,
            "quiet_recent_window": v2amp.QUIET_RECENT_WINDOW, "quiet_prior_window": v2amp.QUIET_PRIOR_WINDOW,
            "round": "14_universe_top1000_sensitivity", "universe_top": 1000,
        },
    )

    print("T1/T2へE1 exit適用中...", file=sys.stderr)
    t1_e1_df = apply_e1_exit(t1_df, all_bdays, bday_index)
    t2_e1_df = apply_e1_exit(t2_df, all_bdays, bday_index)
    t1_e1 = e1_stats(t1_e1_df)
    t2_e1 = e1_stats(t2_e1_df)
    print(f"T1 E1: EV={t1_e1['ev']:.2%} / T2 E1: EV={t2_e1['ev']:.2%}", file=sys.stderr)

    append_exit_e1_trial("vaq_top1000_exit_E1", t1_stats, t1_e1, "vaq_top1000")
    append_exit_e1_trial("va_top1000_exit_E1", t2_stats, t2_e1, "va_top1000")

    print("501-1000位帯診断中...", file=sys.stderr)
    rank_lookup = load_rank_lookup()
    t1_e1_df["rank"] = t1_e1_df.apply(
        lambda row: rank_lookup.get((row["universe_month_used"], row["code"])), axis=1
    )
    old_top500 = t1_e1_df[t1_e1_df["rank"] <= 500]
    rank501_1000 = t1_e1_df[t1_e1_df["rank"] > 500]

    def _band_summary(df: pd.DataFrame) -> dict:
        if df.empty:
            return {"n": 0, "p20": None, "ev_none": None, "ev_e1": None}
        return {
            "n": len(df),
            "p20": (df["ret"] >= 0.20).mean(),
            "ev_none": df["ret"].mean() - measure_base_rate.ROUND_TRIP_COST,
            "ev_e1": df["ret_costadj_E1"].mean(),
        }

    old_top500_summary = _band_summary(old_top500)
    rank501_1000_summary = _band_summary(rank501_1000)
    rank501_1000_summary["mean_ret_raw"] = float(rank501_1000["ret"].mean()) if len(rank501_1000) else None
    rank501_1000_summary["mean_ret_raw_e1"] = float(rank501_1000["ret_raw_E1"].mean()) if len(rank501_1000) else None

    turnover_diag = turnover_quartiles(
        list(zip(rank501_1000["signal_date"], rank501_1000["code"]))
    )

    band_diag = {
        "old_top500": old_top500_summary,
        "rank501_1000": rank501_1000_summary,
        "turnover_501_1000": turnover_diag,
    }

    ev_e1_at_cost5 = (
        rank501_1000_summary["mean_ret_raw_e1"] - CRITERION_501_1000_COST
        if rank501_1000_summary["mean_ret_raw_e1"] is not None else None
    )
    criteria = {
        "a_ev_e1_ok": t1_e1["ev"] >= CRITERION_EV_E1_MIN,
        "b_freq_ok": t1_stats["avg_monthly_n"] >= CHAMPION_BASELINE["monthly_freq"] * CRITERION_FREQ_MULT_MIN,
        "c_rank501_1000_ev_at_cost5": ev_e1_at_cost5,
        "c_band_ok": (ev_e1_at_cost5 is not None and ev_e1_at_cost5 > 0),
    }
    criteria["all_ok"] = criteria["a_ev_e1_ok"] and criteria["b_freq_ok"] and criteria["c_band_ok"]

    write_report(t1_stats, t1_verdict, t1_reasons, t2_stats, t1_e1, t2_e1, band_diag, criteria)

    # 手動照合用に明細を保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t1_e1_df.to_csv(OUTPUT_DIR / "vaq_top1000_positions_e1.csv", index=False)
    t2_e1_df.to_csv(OUTPUT_DIR / "va_top1000_positions_e1.csv", index=False)

    print(f"\n完了: report -> {OUTPUT_DIR / 'report.md'}")
    print(f"採否判定: a={criteria['a_ev_e1_ok']} b={criteria['b_freq_ok']} c={criteria['c_band_ok']} all={criteria['all_ok']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
