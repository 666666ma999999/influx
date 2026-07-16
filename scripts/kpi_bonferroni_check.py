#!/usr/bin/env python3
"""§6「多重比較の実計算」の恒久・再現可能スクリプト（監査C-2 / 敵対的レビュー⑨CRITICAL対応）。

生存3系統（チャンピオン volshock_x_above200_quiet のlift・ULフェード ul_fade_standalone のEV・
SUE sue_beat のEV）を Bonferroni 調整 CI（ci_level = 1 - 0.05/累積試行数）で再評価し、
カタログ§6の記載数値を誰でも再現できるようにする。

n_boot は **10000 固定**とする（既定の N_BOOTSTRAP=1000 では 99.918% の裾（1000本中の最極端
1本近傍）を解像できず、CI端点が seed 依存で 0.70〜1.17 程度暴れることを敵対的レビュー⑨が実測。
極端分位の感度確認には裾の解像度が必須のため、本スクリプトに限り 10000 を正とする。
95%CI の歴史的比較可能性を守るため、モジュール既定値 N_BOOTSTRAP=1000 は変更しない）。

既存出力の再利用のみ（シグナルの再生成・再定義はしない）:
- チャンピオン: output/kpi/volshock_x_above200/returns.csv（in_universe行）に
  output/kpi/volshock_v2_amplifiers/signals_features_volshock_x_above200.csv の quiet_bucket を
  結合し =="quiet" の n=73（kpi_volshock_v3_ul_earnings.py と同一の母集団再現手順・n不一致はFATAL）
- ULフェード: output/kpi/ul_fade_standalone/returns.csv の in_universe 行（n=105）
- SUE: output/kpi/sue_beat/returns.csv の in_universe 行（n=818）

Usage:
    docker compose run --rm xstock python scripts/kpi_bonferroni_check.py [--n-boot 10000]
    # 累積試行数は既定で data/kpi_trials/trials.jsonl の非空行数から機械算出する（§6付記4項・
    # Codexレビュー⑬M2対応）。--n-trials は過去数値の再現用の明示上書きに限定する
    # （例: §6表の初出値を再現するには --n-trials 61）。--show-n で分母のみ表示して終了。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_study  # noqa: E402  (Canonical: bootstrap_lift_ci/bootstrap_ev_ci を再利用)

CHAMPION_RETURNS = Path("output/kpi/volshock_x_above200/returns.csv")
CHAMPION_FEATURES = Path("output/kpi/volshock_v2_amplifiers/signals_features_volshock_x_above200.csv")
CHAMPION_EXPECTED_N = 73
UL_FADE_RETURNS = Path("output/kpi/ul_fade_standalone/returns.csv")
SUE_RETURNS = Path("output/kpi/sue_beat/returns.csv")
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")


def load_in_universe(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"signal_date": str, "code": str, "month": str})
    if "in_universe" in df.columns:
        df = df[df["in_universe"]].reset_index(drop=True)
    return df


def effective_trial_count(trials_path: Path = TRIALS_PATH) -> int:
    """累積Bonferroni分母の正本: trials.jsonl の非空行数（§6付記4項で凍結・保守側の単純カウント。

    歴史的経緯の注記行[重複・縮退]も分母に含める＝保守側。screening_batches.jsonl の
    screenセルは§6付記4項どおり算入しない（バッチメタ行と検証段の生存セル行が trials.jsonl
    側に既に含まれる）。
    """
    if not trials_path.exists():
        raise SystemExit(f"FATAL: 台帳が見つかりません: {trials_path}")
    with open(trials_path, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Bonferroni調整CIの再現計算（§6多重比較・監査C-2）")
    parser.add_argument("--n-trials", type=int, default=None,
                        help="累積試行数の明示上書き（過去数値の再現用に限定。省略時は台帳から機械算出）")
    parser.add_argument("--n-boot", type=int, default=10000,
                        help="ブートストラップ本数（既定10000。docstring参照・1000では裾が解像できない）")
    parser.add_argument("--show-n", action="store_true", help="台帳由来の累積試行数を表示して終了")
    args = parser.parse_args()

    n_trials = args.n_trials if args.n_trials is not None else effective_trial_count()
    source = "手動上書き（再現用）" if args.n_trials is not None else f"台帳 {TRIALS_PATH} の非空行数"
    if args.show_n:
        print(f"累積試行数={n_trials}（出所: {source}）")
        return 0

    ci_level = 1 - 0.05 / n_trials
    print(f"Bonferroni調整: alpha=0.05/{n_trials}（出所: {source}） → CI水準={ci_level:.5%} / n_boot={args.n_boot}")

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    # --- チャンピオン lift（quiet母集団n=73の再現・kpi_volshock_v3_ul_earnings.pyと同手順） ---
    champ = load_in_universe(CHAMPION_RETURNS)
    feats = pd.read_csv(CHAMPION_FEATURES, dtype={"signal_date": str, "code": str},
                        usecols=["signal_date", "code", "quiet_bucket"])
    champ = champ.merge(feats, on=["signal_date", "code"], how="left", validate="one_to_one")
    champ = champ[champ["quiet_bucket"] == "quiet"].reset_index(drop=True)
    if len(champ) != CHAMPION_EXPECTED_N:
        raise SystemExit(f"FATAL: champion母集団n={len(champ)}が既知値{CHAMPION_EXPECTED_N}と不一致")
    lift95 = kpi_event_study.bootstrap_lift_ci(champ, base_rate_by_month, n_boot=args.n_boot)
    lift_adj = kpi_event_study.bootstrap_lift_ci(champ, base_rate_by_month, n_boot=args.n_boot,
                                                 ci_level=ci_level)
    print(f"champion lift: point={lift_adj['point_lift']:.2f} "
          f"95%CI=[{lift95['ci_low']:.2f},{lift95['ci_high']:.2f}] "
          f"調整後CI=[{lift_adj['ci_low']:.2f},{lift_adj['ci_high']:.2f}]")

    # --- ULフェード EV(なし) ---
    ul = load_in_universe(UL_FADE_RETURNS)
    ul95 = kpi_event_study.bootstrap_ev_ci(ul, n_boot=args.n_boot)
    ul_adj = kpi_event_study.bootstrap_ev_ci(ul, n_boot=args.n_boot, ci_level=ci_level)
    print(f"ul_fade EV: n={len(ul)} point={ul_adj['point_ev']:.4%} "
          f"95%CI=[{ul95['ci_low']:.4%},{ul95['ci_high']:.4%}] "
          f"調整後CI=[{ul_adj['ci_low']:.4%},{ul_adj['ci_high']:.4%}]")

    # --- SUE EV(なし) ---
    sue = load_in_universe(SUE_RETURNS)
    sue95 = kpi_event_study.bootstrap_ev_ci(sue, n_boot=args.n_boot)
    sue_adj = kpi_event_study.bootstrap_ev_ci(sue, n_boot=args.n_boot, ci_level=ci_level)
    print(f"sue_beat EV: n={len(sue)} point={sue_adj['point_ev']:.4%} "
          f"95%CI=[{sue95['ci_low']:.4%},{sue95['ci_high']:.4%}] "
          f"調整後CI=[{sue_adj['ci_low']:.4%},{sue_adj['ci_high']:.4%}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
