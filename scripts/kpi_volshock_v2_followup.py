#!/usr/bin/env python3
"""第13周A フォローアップ（team-lead指示3点・カタログ§7-B v2-1/v2-2の続き）。

1. E1シナリオ崩壊exit適用: volshock_x_above200_micro(n=97)/volshock_x_above200_quiet(n=73)の
   各ポジションに `output/kpi/exit_study/positions_volshock_x_above200.csv`（第12周・既存出力）の
   ret_costadj_E1をそのまま突合してEV(E1)を算出する（再計算なし。exit_studyはE1を含む7ルールを
   volshock_x_above200の全180ポジションについて既に計算済みのため、サブセットの行を突合するだけで済む）。
2. 正式トライアル1本: volshock_quiet（volshock_5x母集団×quiet_ratio>=1.2・above200フィルタなし）。
   quiet_ratio自体は `scripts/kpi_volshock_v2_amplifiers.py` が既に計算・保存済みの
   `signals_features_volshock_5x.csv` のquiet_bucket列を再利用する（再計算なし）。
3. フィルタ重複診断（分析のみ・台帳記録なし）: volshock_5x母集団(n=292・above200フィルタを
   含まないためabove200が実際に変動する唯一の母集団)上で above200/micro/quiet の3フィルタの
   ジャッカード係数・3元クロス表を算出する。above200フラグ(dev200)はvolshock_5x生成時点の
   出力に含まれていない（第5周生成・dev200列追加は第11周のため）ため、
   `kpi_volshock_signals.generate_volshock_signals`（Canonical Module・同一パラメータで
   決定的に再現可能）を再実行してdev200を復元し、signal_date+codeで突合する。

**閾値の追加チューニングは禁止**（team-lead指示）: quiet_ratio>=1.2・micro=下位30%ランクの
既存定義をそのまま使う。

Usage:
    python3 scripts/kpi_volshock_v2_followup.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_stats/judge/append_trial を再利用)
import kpi_volshock_signals  # noqa: E402  (Canonical Module: generate_volshock_signals を再利用・dev200復元用)
import kpi_volshock_v2_amplifiers as v2  # noqa: E402  (Canonical Module: load_positions/PERIOD/定数を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー読込を再利用)

EXIT_STUDY_DIR = Path("output/kpi/exit_study")
FEATURES_DIR = v2.OUTPUT_DIR
TRIALS_PATH = v2.TRIALS_PATH
BASE_RATE_DIR = v2.BASE_RATE_DIR
UNIVERSE_WINDOW = v2.UNIVERSE_WINDOW
PERIOD = v2.PERIOD

VOLSHOCK_5X_RETURNS = Path("output/kpi/volshock_5x/returns.csv")


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.2%}" if x is not None else "-"


def _fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "-"


# --- 1. E1シナリオ崩壊exitの突合（above200_micro/above200_quiet） -----------------


def load_e1_lookup(positions_csv: Path) -> dict[tuple[str, str], float]:
    """exit_study済みのpositions_<pop>.csvからret_costadj_E1をsignal_date+codeキーで返す。"""
    df = pd.read_csv(positions_csv, dtype={"signal_date": str, "code": str})
    return dict(zip(zip(df["signal_date"], df["code"]), df["ret_costadj_E1"]))


def attach_e1_ev(subset_df: pd.DataFrame, e1_lookup: dict) -> tuple[Optional[float], int]:
    """subset_dfの各行にE1突合し、EV(E1)=mean(ret_costadj_E1)と突合不能件数を返す。"""
    vals = []
    missing = 0
    for row in subset_df.itertuples(index=False):
        v = e1_lookup.get((row.signal_date, row.code))
        if v is None:
            missing += 1
        else:
            vals.append(v)
    ev_e1 = (sum(vals) / len(vals)) if vals else None
    return ev_e1, missing


# --- 2. 正式トライアル: volshock_quiet（5x母集団×quiet、above200なし） --------------


def build_volshock_quiet_trial(e1_lookup_5x: dict) -> tuple[dict, str, list[str], Optional[float]]:
    features_path = FEATURES_DIR / "signals_features_volshock_5x.csv"
    features_df = pd.read_csv(features_path, dtype={"signal_date": str, "code": str})
    quiet_keys = set(
        zip(
            features_df.loc[features_df["quiet_bucket"] == "quiet", "signal_date"],
            features_df.loc[features_df["quiet_bucket"] == "quiet", "code"],
        )
    )

    full_df = v2.load_positions(VOLSHOCK_5X_RETURNS)
    mask = full_df.apply(lambda r: (r["signal_date"], r["code"]) in quiet_keys, axis=1)
    subset_df = full_df[mask].reset_index(drop=True)

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    stats = kpi_event_study.compute_stats(subset_df, base_rate_by_month, PERIOD)
    verdict, reasons = kpi_event_study.judge(stats)

    ev_e1, e1_missing = attach_e1_ev(subset_df, e1_lookup_5x)

    params = {
        "source_population": "volshock_5x",
        "source_returns_csv": str(VOLSHOCK_5X_RETURNS),
        "amplifier": "quiet_to_active_volume",
        "quiet_ratio_threshold": v2.QUIET_RATIO_THRESHOLD,
        "quiet_recent_window": v2.QUIET_RECENT_WINDOW,
        "quiet_prior_window": v2.QUIET_PRIOR_WINDOW,
        "above200_filter": False,
        "ev_e1_scenario_break": ev_e1,
        "ev_e1_missing_positions": e1_missing,
        "round": "13A_followup",
        "note": "above200フィルタなしでnを稼げるか・quiet単独がabove200×quietを包含するかの確認(team-lead指示)",
    }
    record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": "volshock_quiet",
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
    print(
        f"台帳記録: volshock_quiet n={stats['n']} lift={_fmt_ratio(stats['point_lift'])} "
        f"ci95=[{_fmt_ratio(stats['ci_low'])},{_fmt_ratio(stats['ci_high'])}] verdict={verdict} "
        f"EV(E1)={_fmt_pct(ev_e1)}"
    )
    return stats, verdict, reasons, ev_e1


# --- 3. フィルタ重複診断（volshock_5x母集団・above200/micro/quiet） ----------------


def restore_dev200_for_5x(all_bdays_all: list[str]) -> pd.DataFrame:
    """volshock_5x生成時(第5周)にはdev200列が無かったため、同一パラメータでの再生成
    (Canonical Module・決定的に再現可能)によりdev200を復元する。ハーネス再実行はしない
    (--skip-harness相当・シグナル生成のみ)。
    """
    start_bound = PERIOD[0].replace("-", "") + "01"
    end_bound = PERIOD[1].replace("-", "") + "31"
    start_bd = next((d for d in all_bdays_all if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays_all) if d <= end_bound), None)
    signals_df, _diag = kpi_volshock_signals.generate_volshock_signals(start_bd, end_bd)
    return signals_df[["signal_date", "code", "dev200"]].copy()


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def build_overlap_diagnostics(all_bdays_all: list[str]) -> dict:
    features_df = pd.read_csv(
        FEATURES_DIR / "signals_features_volshock_5x.csv", dtype={"signal_date": str, "code": str}
    )
    dev200_df = restore_dev200_for_5x(all_bdays_all)
    dev200_df["signal_date"] = dev200_df["signal_date"].astype(str)
    dev200_df["code"] = dev200_df["code"].astype(str)
    merged = features_df.merge(dev200_df, on=["signal_date", "code"], how="left")

    n_total = len(merged)
    n_dev200_missing = int(merged["dev200"].isna().sum())
    merged["above200"] = merged["dev200"] > 0

    above200_set = set(zip(merged.loc[merged["above200"], "signal_date"], merged.loc[merged["above200"], "code"]))
    micro_set = set(
        zip(
            merged.loc[merged["market_cap_bucket"] == "micro", "signal_date"],
            merged.loc[merged["market_cap_bucket"] == "micro", "code"],
        )
    )
    quiet_set = set(
        zip(merged.loc[merged["quiet_bucket"] == "quiet", "signal_date"], merged.loc[merged["quiet_bucket"] == "quiet", "code"])
    )
    all_keys = set(zip(merged["signal_date"], merged["code"]))

    jaccards = {
        "above200 vs micro": jaccard(above200_set, micro_set),
        "above200 vs quiet": jaccard(above200_set, quiet_set),
        "micro vs quiet": jaccard(micro_set, quiet_set),
    }

    cross_tab = {}
    for a in (True, False):
        for m in (True, False):
            for q in (True, False):
                key_set = all_keys
                key_set = key_set & (above200_set if a else (all_keys - above200_set))
                key_set = key_set & (micro_set if m else (all_keys - micro_set))
                key_set = key_set & (quiet_set if q else (all_keys - quiet_set))
                cross_tab[(a, m, q)] = len(key_set)

    return {
        "n_total": n_total,
        "n_dev200_missing": n_dev200_missing,
        "n_above200": len(above200_set),
        "n_micro": len(micro_set),
        "n_quiet": len(quiet_set),
        "jaccards": jaccards,
        "cross_tab": cross_tab,
    }


# --- レポート出力 ---------------------------------------------------------------


def write_followup_report(
    micro_e1: tuple[Optional[float], int],
    quiet_e1: tuple[Optional[float], int],
    quiet_trial: tuple[dict, str, list[str], Optional[float]],
    overlap: dict,
) -> None:
    lines = [
        "# 第13周A フォローアップレポート（team-lead指示3点）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        "",
        "## 1. E1シナリオ崩壊exitの適用（volshock_x_above200_micro / _quiet）",
        "",
        "既存の `output/kpi/exit_study/positions_volshock_x_above200.csv`（第12周）の"
        "`ret_costadj_E1`列をsignal_date+codeで突合（再計算なし）。",
        "",
        "| KPI | N | EV(なし) | EV(-8%損切り) | EV(E1シナリオ崩壊) | E1突合不能件数 |",
        "|---|---|---|---|---|---|",
        f"| volshock_x_above200_micro | 97 | 0.16% | -1.74% | {_fmt_pct(micro_e1[0])} | {micro_e1[1]} |",
        f"| volshock_x_above200_quiet | 73 | 3.48% | 0.14% | {_fmt_pct(quiet_e1[0])} | {quiet_e1[1]} |",
        "",
        "（EV(なし)/EV(-8%損切り)は前回レポート `output/kpi/volshock_v2_amplifiers/report.md` の値を再掲）",
        "",
        "## 2. 正式トライアル: volshock_quiet（volshock_5x母集団×quiet_ratio>=1.2・above200なし）",
        "",
    ]
    stats, verdict, reasons, ev_e1 = quiet_trial
    lines += [
        v2.STATS_TABLE_HEADER,
        v2._stats_row("volshock_quiet", stats),
        "",
        f"EV(E1シナリオ崩壊) = {_fmt_pct(ev_e1)}",
        "",
        f"判定: **{kpi_event_study.VERDICT_LABELS_JA.get(verdict, verdict)}**",
        "",
    ]
    lines += [f"- {r}" for r in reasons]
    lines += [
        "",
        f"above200×quiet(n=73)との関係: quiet単独(above200なし)はn={stats['n']}"
        f"（above200×quietの73件は原理的にこのn件の部分集合——quietがabove200を包含する形かどうかは"
        "下記§3のクロス表・ジャッカード係数で確認）。",
        "",
        "## 3. フィルタ重複診断（volshock_5x母集団・n={}・above200/micro/quietの重なり）".format(overlap["n_total"]),
        "",
        f"above200フラグはvolshock_5x生成時に未保存だったため、`kpi_volshock_signals."
        f"generate_volshock_signals`（同一パラメータ・決定的）を再実行してdev200を復元し突合した"
        f"（突合不能={overlap['n_dev200_missing']}件）。",
        "",
        f"- above200(dev200>0): n={overlap['n_above200']}",
        f"- micro(時価総額下位30%): n={overlap['n_micro']}",
        f"- quiet(quiet_ratio>=1.2): n={overlap['n_quiet']}",
        "",
        "### ジャッカード係数（|A∩B|/|A∪B|。0=完全独立、1=完全一致）",
        "",
        "| フィルタペア | ジャッカード係数 |",
        "|---|---|",
    ]
    for pair, j in overlap["jaccards"].items():
        lines.append(f"| {pair} | {j:.3f} |")
    lines += [
        "",
        "### 3元クロス表（above200 × micro × quiet）",
        "",
        "| above200 | micro | quiet | N |",
        "|---|---|---|---|",
    ]
    for (a, m, q), n in sorted(overlap["cross_tab"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {a} | {m} | {q} | {n} |")
    lines += [
        "",
        "## 実装ノート",
        "- E1のEVは既存exit_study出力（`positions_volshock_x_above200.csv`）の再利用のみで再計算していない",
        "- volshock_quietトライアルのquiet判定は `signals_features_volshock_5x.csv` のquiet_bucket列"
        "（`kpi_volshock_v2_amplifiers.py` が既に算出済み）を再利用し、quiet_ratio自体は再計算していない",
        "- above200フラグ（dev200）はvolshock_5x生成時に未保存だったため、"
        "`kpi_volshock_signals.generate_volshock_signals` を同一パラメータで再実行し復元した"
        "（決定的関数のため再現性に問題なし。ハーネス再実行はしていない=シグナル生成のみ）",
        "- 閾値の追加チューニングは行っていない（team-lead指示・quiet_ratio>=1.2・micro=下位30%の既存定義を維持）",
        "",
    ]
    out_path = FEATURES_DIR / "followup_report.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"report: {out_path}")


def main() -> int:
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays_all = measure_base_rate.all_business_days(calendar_days)

    # --- 1. E1 EV突合 ---
    above200_features = pd.read_csv(
        FEATURES_DIR / "signals_features_volshock_x_above200.csv", dtype={"signal_date": str, "code": str}
    )
    e1_lookup_above200 = load_e1_lookup(EXIT_STUDY_DIR / "positions_volshock_x_above200.csv")
    micro_subset = above200_features[above200_features["market_cap_bucket"] == "micro"]
    quiet_subset = above200_features[above200_features["quiet_bucket"] == "quiet"]
    micro_e1 = attach_e1_ev(micro_subset, e1_lookup_above200)
    quiet_e1 = attach_e1_ev(quiet_subset, e1_lookup_above200)
    print(f"above200_micro EV(E1)={_fmt_pct(micro_e1[0])} (突合不能={micro_e1[1]})")
    print(f"above200_quiet EV(E1)={_fmt_pct(quiet_e1[0])} (突合不能={quiet_e1[1]})")

    # --- 2. volshock_quiet トライアル ---
    e1_lookup_5x = load_e1_lookup(EXIT_STUDY_DIR / "positions_volshock_5x.csv")
    quiet_trial = build_volshock_quiet_trial(e1_lookup_5x)

    # --- 3. 重複診断 ---
    overlap = build_overlap_diagnostics(all_bdays_all)
    print(
        f"重複診断: above200={overlap['n_above200']} micro={overlap['n_micro']} quiet={overlap['n_quiet']} "
        f"jaccards={overlap['jaccards']}"
    )

    write_followup_report(micro_e1, quiet_e1, quiet_trial, overlap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
