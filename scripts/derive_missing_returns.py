#!/usr/bin/env python3
"""sue_x_above200 / volshock_x_above200_quiet の returns.csv を既存フルスキーマから決定論的に導出する。

再走査ではない（2026-08-01 ユーザーGO・設計確定: 両系統とも既存 returns.csv の部分集合）:
- sue_x_above200 = output/kpi/sue_beat/returns.csv × signals_features_h1.csv の (signal_date, code) 内部結合
  （§7-J 凍結手順 kpi_sue_exit_study.py と同一の母集団再現法・FATAL assert n=516）
- volshock_x_above200_quiet = output/kpi/volshock_x_above200/returns.csv ×
  signals_features_volshock_x_above200.csv の quiet_ratio>=1.2 行（§7-B v2-2 凍結定義・FATAL assert n=73）

同一性ゲート（1つでも不一致なら書き込まず異常終了・fail-closed）:
- in_universe 行数が watchlist 凍結 n と完全一致
- プールEV(なし)=mean(ret)-0.003 が watchlist 凍結 ev_none と一致（小数4桁）
- quiet は per-signal で features 側 ret と returns 側 ret の一致も検査（結合の取り違え検出）

出力は output/kpi/<kpi_name>/returns.csv（gitignore 域・seedなしの決定論的導出＝再現可能）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
COST = 0.003

DERIVATIONS = {
    "sue_x_above200": {
        "expected_n": 516, "frozen_ev_none": 0.0188,
        "parent": "output/kpi/sue_beat/returns.csv",
        "signals": "output/kpi/sue_champion_composite/signals_features_h1.csv",
    },
    "volshock_x_above200_quiet": {
        "expected_n": 73, "frozen_ev_none": 0.0348,
        "parent": "output/kpi/volshock_x_above200/returns.csv",
        "signals": "output/kpi/volshock_v2_amplifiers/signals_features_volshock_x_above200.csv",
        "signal_filter": ("quiet_ratio", 1.2),
    },
}


def derive(kpi: str, spec: dict) -> None:
    parent = pd.read_csv(REPO / spec["parent"], dtype={"signal_date": str, "code": str})
    signals = pd.read_csv(REPO / spec["signals"], dtype={"signal_date": str, "code": str})
    if "signal_filter" in spec:
        col, threshold = spec["signal_filter"]
        signals = signals[signals[col] >= threshold]
    keys = signals[["signal_date", "code"]].drop_duplicates()
    merged = parent.merge(keys, on=["signal_date", "code"], how="inner")

    # 親returnsは全体では重複キーを持ちうる（sue_beat実測48行・Codexレビュー指摘）。
    # 部分集合側で1件でも重複が混入したらn照合前に即FATAL（膨張の構造検出）。
    if merged.duplicated(["signal_date", "code"]).any():
        raise SystemExit(f"FATAL {kpi}: 結合結果に重複キーが混入（親returnsの重複キーがヒット・書き込み中止）")

    in_uni = merged[merged["in_universe"].astype(str) == "True"]
    if len(in_uni) != spec["expected_n"]:
        raise SystemExit(f"FATAL {kpi}: in_universe {len(in_uni)} != 凍結n {spec['expected_n']}（書き込み中止）")
    ev_none = in_uni["ret"].mean() - COST
    if round(ev_none, 4) != round(spec["frozen_ev_none"], 4):
        raise SystemExit(f"FATAL {kpi}: プールEV(なし) {ev_none:.4f} != 凍結 {spec['frozen_ev_none']}（書き込み中止）")
    # per-signal 照合は features 側に ret があれば両系統とも必須（n+平均だけでは取り違えを検出できない・Codexレビュー指摘）
    if "ret" in signals.columns:
        chk = in_uni.merge(signals[["signal_date", "code", "ret"]], on=["signal_date", "code"],
                           suffixes=("", "_feat"))
        if len(chk) != len(in_uni) or (chk["ret"] - chk["ret_feat"]).abs().max() > 1e-9:
            raise SystemExit(f"FATAL {kpi}: per-signal ret が features と不一致（結合取り違え・書き込み中止）")

    out_dir = REPO / "output/kpi" / kpi
    out_dir.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_dir / "returns.csv", index=False)
    print(f"{kpi}: derived {len(merged)}行 (in_universe {len(in_uni)}={spec['expected_n']}✓ "
          f"ev_none {ev_none:.4f}={spec['frozen_ev_none']}✓) -> {out_dir.relative_to(REPO)}/returns.csv")


def main() -> None:
    for kpi, spec in DERIVATIONS.items():
        derive(kpi, spec)


if __name__ == "__main__":
    main()
