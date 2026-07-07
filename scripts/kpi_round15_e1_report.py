#!/usr/bin/env python3
"""第15周: 頻度直交セット T1-T4 への E1 exit 適用 + 横断比較レポート。

tasks指示（第15周・頻度直交セット）の実行・出力手順2「全4本にE1 exit（200日線割れ・
kpi_exit_studyエンジン再利用）を適用しEV(E1)を算出」・手順3「report: 横断比較表」を実装する。

前提（本スクリプト実行前に完了していること）:
    各KPIについて `scripts/kpi_<name>_signals.py --start 2016-11 --end 2022-11` が完走し、
    `output/kpi/<name>/returns.csv` が生成済み（run_event_study()の標準出力・台帳にも記録済み）。

本スクリプトが行うこと（すべて既存 Canonical Module の再利用のみ・エントリー再計算なし。
scripts/kpi_top1000_sensitivity.py 第14周の apply_e1_exit パターンをそのまま踏襲）:
    1. 各KPIの in_universe=True ポジション集合に E1シナリオ崩壊exitを適用する
       （kpi_exit_study.build_price_history / _time_based_exit / _walk_e1_scenario_break /
       aggregate_rule_stats を再利用。4本とも第12周チャンピオン集合とは別の新規ポジション集合
       のため、既存exit_study出力の使い回しはできず新規に日次パスを実行する）
    2. EV4変種（なし/-8%損切り/-10%損切り/E1）を集計し、台帳に <name>_exit_E1 として記録する
    3. output/kpi/round15_freq_orthogonal/report.md に横断比較表を出力する
       （チャンピオン基準線: n=73・リフト4.16[1.91,7.10]・EV(E1)+2.84%・月1.0件 と対比）

Usage:
    python3 scripts/kpi_round15_e1_report.py
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
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・ROUND_TRIP_COSTを再利用)

BASE_RATE_DIR = kpi_event_study.DEFAULT_BASE_RATE_DIR
UNIVERSE_WINDOW = 21
PERIOD = ("2016-11", "2022-11")  # §6凍結のin-sample期間（他KPIと同一）
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_DIR = Path("output/kpi/round15_freq_orthogonal")

# チャンピオン基準線（第13周確定値・比較表示用。tasks/stock_algo_kpi_loop.md 第13周節）
CHAMPION_BASELINE = {
    "n": 73, "lift": 4.16, "ci_low": 1.91, "ci_high": 7.10,
    "ev_none": None, "ev_stop8": None, "ev_e1": 0.0284, "monthly_freq": 1.0,
}

# 第15周・4本の定義(tasks指示どおり。順序=T1〜T4)
KPIS: list[tuple[str, str, str]] = [
    ("sector_momentum_laggard", "T1", "業種モメンタム×業種内出遅れ"),
    ("range_breakout", "T2", "レンジ収縮→上放れ×出来高"),
    ("ma200_reclaim", "T3", "200日線奪回クロス"),
    ("sh_dip_reentry", "T4", "ストップ高：初押し再上昇"),
]

# tasks指示の合否基準: 月5件以上 かつ EV(E1)プラス
FREQ_CRITERION = 5.0
EV_E1_CRITERION = 0.0


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.2%}" if x is not None else "-"


def _fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "-"


def load_positions(kpi_name: str) -> pd.DataFrame:
    path = Path("output/kpi") / kpi_name / "returns.csv"
    if not path.exists():
        raise SystemExit(f"FATAL: {path} が見つかりません。先に kpi_{kpi_name}_signals.py を実行してください。")
    df = pd.read_csv(
        path,
        dtype={
            "signal_date": str, "code": str, "month": str, "regime": str,
            "entry_date": str, "exit_date": str, "universe_month_used": str,
        },
    )
    df = df[df["in_universe"] == True].reset_index(drop=True)  # noqa: E712
    if df.empty:
        raise SystemExit(f"FATAL: {path} に in_universe=True の行がありません")
    return df


def apply_e1_exit(positions_df: pd.DataFrame, all_bdays: list[str], bday_index: dict) -> pd.DataFrame:
    """positions_dfの各ポジションにE1シナリオ崩壊exitを適用する。

    scripts/kpi_top1000_sensitivity.py の apply_e1_exit と同一実装（Canonical Module の
    kpi_exit_study.build_price_history / _time_based_exit / _walk_e1_scenario_break を再利用。
    E1判定ロジック自体は複製しない）。
    """
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
    result["ret_raw_C3"] = result["ret"]  # aggregate_rule_statsの「降ろされ損率」計算に必要
    return result


def e1_stats(detail_df: pd.DataFrame) -> dict:
    return kpi_exit_study.aggregate_rule_stats(detail_df, "E1")


def append_exit_e1_trial(kpi_name: str, base_stats: dict, e1: dict) -> str:
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
        "kpi_name": f"{kpi_name}_exit_E1",
        "params": {
            "exit_rule": "E1", "source_population": kpi_name, "round": "15_freq_orthogonal_set",
            "sma200_window": kpi_exit_study.MA200_WINDOW,
            "round_cost_note": "第15周頻度直交セット・新規ポジション集合へのE1新規適用（既存exit_studyの再利用はなし）",
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
    return verdict


def main() -> int:
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    results = []
    for kpi_name, label, jp_name in KPIS:
        print(f"[{label}] {kpi_name}: ポジション読込中...", file=sys.stderr)
        pos_df = load_positions(kpi_name)
        stats = kpi_event_study.compute_stats(pos_df, base_rate_by_month, PERIOD)
        verdict_main, reasons_main = kpi_event_study.judge(stats)
        print(f"[{label}] n={stats['n']} verdict={verdict_main}", file=sys.stderr)

        print(f"[{label}] E1 exit適用中...", file=sys.stderr)
        e1_df = apply_e1_exit(pos_df, all_bdays, bday_index)
        e1 = e1_stats(e1_df)
        verdict_e1 = append_exit_e1_trial(kpi_name, stats, e1)
        print(f"[{label}] E1: EV={e1['ev']:.2%} verdict={verdict_e1}", file=sys.stderr)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        e1_df.to_csv(OUTPUT_DIR / f"{kpi_name}_positions_e1.csv", index=False)

        freq_ok = stats["avg_monthly_n"] >= FREQ_CRITERION
        ev_e1_ok = e1["ev"] is not None and e1["ev"] > EV_E1_CRITERION
        results.append({
            "kpi_name": kpi_name, "label": label, "jp_name": jp_name,
            "stats": stats, "verdict_main": verdict_main, "reasons_main": reasons_main,
            "e1": e1, "verdict_e1": verdict_e1,
            "freq_ok": freq_ok, "ev_e1_ok": ev_e1_ok, "both_ok": freq_ok and ev_e1_ok,
        })

    write_report(results)
    print(f"\n完了: report -> {OUTPUT_DIR / 'report.md'}")
    for r in results:
        print(
            f"  {r['label']}({r['kpi_name']}): n={r['stats']['n']} lift={_fmt_ratio(r['stats']['point_lift'])} "
            f"freq={r['stats']['avg_monthly_n']:.2f}/月 EV(E1)={_fmt_pct(r['e1']['ev'])} "
            f"月5件+EV(E1)プラス={r['both_ok']}"
        )
    return 0


def write_report(results: list[dict]) -> None:
    lines = [
        "# 第15周: 頻度直交セット(T1-T4) 横断比較レポート",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降はロック中・対象外）",
        "",
        "> **焦点**: 「月5件以上出て、EV(E1)がプラスの入口が生まれるか」"
        f"（チャンピオン基準線: n={CHAMPION_BASELINE['n']}・リフト{CHAMPION_BASELINE['lift']:.2f}"
        f"[{CHAMPION_BASELINE['ci_low']:.2f}, {CHAMPION_BASELINE['ci_high']:.2f}]・"
        f"EV(E1){CHAMPION_BASELINE['ev_e1']:+.2%}・月{CHAMPION_BASELINE['monthly_freq']:.1f}件 と対比）",
        "",
        "## 横断比較表（4変種: なし/-8%損切り/-10%損切り/E1）",
        "",
        "| KPI | N | リフト(点推定) | CI95% | 月平均頻度 | EV(なし) | EV(-8%) | EV(-10%) | EV(E1) | 月5件+EV(E1)>0 |",
        "|---|---|---|---|---|---|---|---|---|---|",
        f"| チャンピオン(第13周・参考) | {CHAMPION_BASELINE['n']} | {CHAMPION_BASELINE['lift']:.2f} | "
        f"[{CHAMPION_BASELINE['ci_low']:.2f}, {CHAMPION_BASELINE['ci_high']:.2f}] | "
        f"{CHAMPION_BASELINE['monthly_freq']:.2f}件 | - | +3.48% | - | "
        f"{CHAMPION_BASELINE['ev_e1']:+.2%} | (基準線) |",
    ]
    for r in results:
        s, e1 = r["stats"], r["e1"]
        lines.append(
            f"| {r['label']}: {r['kpi_name']} | {s['n']} | {_fmt_ratio(s['point_lift'])} | "
            f"[{_fmt_ratio(s['ci_low'])}, {_fmt_ratio(s['ci_high'])}] | {s['avg_monthly_n']:.2f}件 | "
            f"{_fmt_pct(s['ev_none'])} | {_fmt_pct(s['ev_stop8'])} | {_fmt_pct(s['ev_stop10'])} | "
            f"{_fmt_pct(e1['ev'])} | {'YES' if r['both_ok'] else 'no'} |"
        )
    lines += ["", "## 個別判定根拠", ""]
    for r in results:
        s = r["stats"]
        lines.append(f"### {r['label']}: {r['kpi_name']}（{r['jp_name']}）")
        lines.append("")
        lines.append(f"- §6本判定（in-sample・point_lift基準）: **{kpi_event_study.VERDICT_LABELS_JA.get(r['verdict_main'], r['verdict_main'])}**")
        for reason in r["reasons_main"]:
            lines.append(f"  - {reason}")
        lines.append(
            f"- E1発動診断: 発動率={_fmt_pct(r['e1']['trigger_rate'])}（n={r['e1']['triggered_n']}） / "
            f"降ろされ損率={_fmt_pct(r['e1']['dropped_loss_rate']) if r['e1']['dropped_loss_rate'] is not None else '-（発動0件）'} / "
            f"平均保有日数={r['e1']['avg_holding_days']:.1f}日"
        )
        lines.append(
            f"- 第15周焦点基準（月5件以上 かつ EV(E1)プラス）: "
            f"月平均頻度{s['avg_monthly_n']:.2f}件({'OK' if r['freq_ok'] else 'NG'}) / "
            f"EV(E1){_fmt_pct(r['e1']['ev'])}({'OK' if r['ev_e1_ok'] else 'NG'}) "
            f"→ **{'両条件充足' if r['both_ok'] else '未充足'}**"
        )
        lines.append("")

    all_fail = all(not r["both_ok"] for r in results)
    lines += [
        "## 総合結論",
        "",
        (
            "4本すべてが「月5件以上 かつ EV(E1)プラス」を同時に満たさなかった。"
            if all_fail else
            "以下のKPIが「月5件以上 かつ EV(E1)プラス」の両条件を満たした: "
            + ", ".join(r["label"] for r in results if r["both_ok"])
        ),
        "",
        "詳細な考察・実装ノートは各KPI個別レポート(`output/kpi/<name>/report.md`)を参照。",
        "",
        "## 実装ノート",
        "- E1 exitは4本とも新規ポジション集合のため`kpi_exit_study`の日次ウォーク関数"
        "(`build_price_history`/`_time_based_exit`/`_walk_e1_scenario_break`)をそのまま再利用して"
        "新規に計算した（第12周の既存exit_study出力との単純結合はしていない）",
        "- 台帳記録: 各KPIの主トライアル(4行・各生成スクリプト実行時にハーネスが自動記録済み) + "
        "<name>_exit_E1（本スクリプトが記録・計4行）",
        "- 月平均頻度は検証期間全体(73ヶ月)を分母とする定義（§3「年3シグナルでは運用不能」の趣旨に整合。"
        "kpi_event_study.compute_statsのavg_monthly_nをそのまま使用）",
        "",
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
