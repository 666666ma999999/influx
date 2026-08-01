#!/usr/bin/env python3
"""市場保有ベースライン v2（同一執行・stop8対応）と KPI 別の市場超過EVの算出。

設計（2026-08-01 ユーザーGO・チャット設計1枚が正本）:
- ベースライン = output/base_rate/returns_w21.csv.gz（毎月・TOP500全銘柄等ウェイト・t+1寄付→20営業日）
  に kpi_event_study.ev_v2_summary をそのまま適用（Dual-Path禁止・§6付記IVコスト規約と同一）
- 超過 = 月ペア差（共通暦月ごとに KPI月内平均 − ベースライン月内平均）の系列を、
  「1月1行のdf」に変換して ev_v2_summary に通す（two-stage=差系列の月ブロックbootstrap・
  共分散を保った超過CI。点の単純控除はしない）。コストは差で相殺されるため raw 列同士で差を取る
  （none: ret−ret / stop8: ret_stop8−ret_stop8[両者控除済み]）
- 出力: output/base_rate/market_baseline_v2.json（凍結値）＋ tasks/ev_estimand_v2_results.md の
  マーカー区間に超過表を追補（記述・α非消費・判定不使用・verdict不変更）
- 既知の限界: stop8 はスリッページゼロ仮定（measure_base_rate.py:311）。両側共通のため差では部分相殺。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from ev_estimand_v2 import RETURNS_ALIASES  # noqa: E402  (別名ディレクトリ対応の正本を共有)
from kpi_event_study import ev_v2_summary  # noqa: E402

BASE_PATH = REPO / "output/base_rate/returns_w21.csv.gz"
OUT_JSON = REPO / "output/base_rate/market_baseline_v2.json"
RESULTS_PATH = REPO / "tasks/ev_estimand_v2_results.md"
MARKER_BEGIN = "<!-- market_baseline_v2:begin -->"
MARKER_END = "<!-- market_baseline_v2:end -->"
IN_SAMPLE = (201611, 202211)  # KPI in-sample窓（2016-11〜2022-11）
EXITS = {"none": ("ret", 0.003), "stop8": ("ret_stop8", 0.0)}
KPI_NAMES = [
    "volshock_5x", "volshock_x_above200", "shortcover_x_bear", "sue_beat",
    "sell_reg_trigger_rebound", "sh_dip_reentry", "turnover_rank_surge",
    "margin_expand_yoy", "raw_strev_entry", "gap_hold_close_strong",
    "engulf_reversal_day", "three_up_ignition", "sales_beat",
    "guidance_fy_strong", "cfo_margin_improve", "earnings_spillover",
    "pead_gap8_vol3",  # 2026-08-01 正誤訂正でv2算出可能に（returns実体=defer3別名・参照系統）
    "sue_x_above200", "volshock_x_above200_quiet",  # 2026-08-01 derive_missing_returns.py 導出（凍結n/EV照合ゲート付き）
]


def baseline_stats(df: pd.DataFrame) -> dict:
    out = {}
    for name, (col, cost) in EXITS.items():
        out[name] = ev_v2_summary(df, ev_column=col, cost=cost)
    return out


def excess_summary(kpi_df: pd.DataFrame, base_df: pd.DataFrame, col: str) -> dict:
    """共通暦月の月内平均差の系列を「1月1行df」にして ev_v2_summary に通す（cost=0・差で相殺済み）。"""
    kpi_m = kpi_df.groupby("month")[col].mean()
    base_m = base_df.groupby("month")[col].mean()
    common = sorted(set(kpi_m.index) & set(base_m.index))
    if not common:
        return {"status": "not_computed", "reason": "no_common_months"}
    diff = pd.DataFrame({"month": common, "excess": [kpi_m[m] - base_m[m] for m in common]})
    r = ev_v2_summary(diff, ev_column="excess", cost=0.0)
    r["common_months"] = len(common)
    return r


def fmt(v) -> str:
    return f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "—"


def main() -> None:
    base = pd.read_csv(BASE_PATH, dtype={"code": str})
    base_is = base[(base["month"] >= IN_SAMPLE[0]) & (base["month"] <= IN_SAMPLE[1])]

    result = {
        "computed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(BASE_PATH.relative_to(REPO)),
        "note": "market holding baseline, estimand v2 (two-stage month-equal, one-sided 95% primary). "
                "stop8 slippage-zero assumption shared with KPI side (partially cancels in excess).",
        "full_window": {"months": f"{base['month'].min()}-{base['month'].max()}", **baseline_stats(base)},
        "in_sample_window": {"months": f"{IN_SAMPLE[0]}-{IN_SAMPLE[1]}", **baseline_stats(base_is)},
    }

    rows = []
    for kpi in KPI_NAMES:
        path = REPO / "output/kpi" / RETURNS_ALIASES.get(kpi, kpi) / "returns.csv"
        df = pd.read_csv(path)
        df = df[df["in_universe"].astype(str) == "True"]
        cells = [kpi]
        for name, (col, _cost) in EXITS.items():
            ex = excess_summary(df, base, col)
            if ex.get("status") == "computed":
                cells.append(f"{fmt(ex['ev_v2'])} [下限{fmt(ex['ci1s_low'])}] ({ex['common_months']}月)")
            else:
                cells.append(f"未算出({ex.get('reason')})")
        rows.append("| " + " | ".join(cells) + " |")

    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    section = [
        MARKER_BEGIN,
        "",
        "## 市場超過EV v2（同一執行・月ペア差・記述専用）",
        "",
        f"- 生成時刻: {result['computed_at']} / ベースライン源: `{result['source']}`（凍結値= `output/base_rate/market_baseline_v2.json`）",
        f"- 市場保有ベースライン v2（全期間 {result['full_window']['months']}）: "
        f"none {fmt(result['full_window']['none']['ev_v2'])} [下限{fmt(result['full_window']['none']['ci1s_low'])}] / "
        f"stop8 {fmt(result['full_window']['stop8']['ev_v2'])} [下限{fmt(result['full_window']['stop8']['ci1s_low'])}]",
        f"- 同（in-sample窓 {result['in_sample_window']['months']}）: "
        f"none {fmt(result['in_sample_window']['none']['ev_v2'])} [下限{fmt(result['in_sample_window']['none']['ci1s_low'])}] / "
        f"stop8 {fmt(result['in_sample_window']['stop8']['ev_v2'])} [下限{fmt(result['in_sample_window']['stop8']['ci1s_low'])}]",
        "",
        "超過 = 共通暦月ごとの〈KPI月内平均 − 市場月内平均〉差系列に estimand v2 を適用（共分散保持・コストは差で相殺）。",
        "**目標（§0付記II）が要求する量はこの表の片側95%下限**。α非消費・判定不使用・verdict不変更。",
        "",
        "| KPI | 超過EV(none) [片側95%下限] | 超過EV(stop8) [片側95%下限] |",
        "|---|---|---|",
        *rows,
        "",
        MARKER_END,
    ]
    text = RESULTS_PATH.read_text(encoding="utf-8")
    if MARKER_BEGIN in text:
        pre = text.split(MARKER_BEGIN)[0]
        post = text.split(MARKER_END)[-1]
        text = pre + "\n".join(section) + post
    else:
        text = text.rstrip() + "\n\n" + "\n".join(section) + "\n"
    RESULTS_PATH.write_text(text, encoding="utf-8")
    print(f"baseline -> {OUT_JSON.relative_to(REPO)}")
    print(f"excess table -> {RESULTS_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
