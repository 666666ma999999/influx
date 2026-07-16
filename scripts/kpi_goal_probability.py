#!/usr/bin/env python3
"""P7/BF（レビュー合意事項）: 「勝ちレシピ10本」到達確率のモンテカルロ・シミュレーション。

**経営計画/運用監視用。合否判定には一切使用しない。**
本スクリプトが算出する確率は、既存の正式判定フロー（judge()・枠S/枠F運用開始ライン・
§7-*系の凍結事前登録ルール）を一切変更・代替しない。台帳(data/kpi_trials/trials.jsonl)・
ペーパートレード台帳(data/paper_trades/)・観察リスト(config/paper_watchlist.json)・
カタログ(docs/stock-algo-kpi-catalog.md)・タスク管理(tasks/*.md) への書き込みは一切行わない
（本スクリプトはこれらを読むだけの読み取り専用集計であり、成功基準ハックの余地を作らない）。

## モデル概要

1. **候補集合**: config/paper_watchlist.json の status が "observation" の系統（"reference"=
   holdout棄却済み参照系統・"hoos_rejected"=棄却済みは除外。実読で17系統・下記§家族分類参照）。
2. **家族クラスタ**: kpi_name から機械的に5分類（volshock系/fins決算系/リバーサル系/需給系/その他。
   分類規則は classify_family() が正）。
3. **シナリオ格子**: 真の独立エッジ率 p∈{0.05,0.10,0.20} × 共通因子採用確率 rho∈{0.3,0.7} ×
   新規候補流入 0/5/10 本/年 = 3×2×3=18シナリオ。
   各系統の「真に有効か」は共通潜在因子混合モデルで生成する:
   クラスタcの共通因子 Z_c ~ Bernoulli(p)。系統iは確率rhoでZ_{cluster(i)}に従い、確率(1-rho)で
   独立にBernoulli(p)を引く。周辺確率は常にpに保たれ、rho→1で同クラスタ内完全連動、rho→0で
   完全独立となる（相関構造を持つが周辺分布はpから動かないという最小仮定のモデル）。
   rhoは「共通因子に従う確率」であり、系統ペア間の実効相関そのものではない点に注意
   （2値変数の混合モデルでは同クラスタ内2系統の実効相関=rho²になる。rho=0.3→実効相関0.09、
   rho=0.7→実効相関0.49。詳細はモデル仮定7参照）。
4. **判定可能日**: 12完全暦月 と n≥100到達（月間頻度で外挿）の遅い方。ペーパートレード運用は
   全17系統が2026-07-07〜2026-07-15に順次登録されているが、本シミュレーションでは12完全暦月の
   フロアをレビュー合意どおり一律 PROGRAM_START=2026-08-01 起点（→2027-08-01）で簡略化する
   （個別registered_dateの数日〜1週間の差は無視。系統ごとの頻度差はn≥100到達日の方に反映される）。
   月間頻度の出典優先順位（P3規則を踏襲・kpi_maturity_power.pyと分母規則を統一・2026-07-16
   Codexレビュー指摘反映）: ① output/kpi/<kpi_name>/returns.csv の in_universe実測（凍結された
   in_sample.period 全体の月数で n を割る。ゼロ発火月があっても脱落させず分母に含める）→
   ② watchlist記載の in_sample.avg_monthly_signals（カタログ記載値）→ ③ in_sample.n /
   in_sample.period の月数（カタログ記載からの導出、watchlist内の既存系統が採用している
   n/73ヶ月 と同型の計算）。①の分母は「最初のシグナル月〜最後のシグナル月」の実測スパンでは
   ない（旧版はこの実測スパンを分母にしており、ゼロ発火月が脱落して頻度が上方バイアスする
   問題があった）。
5. **新規候補流入**: 0/5/10 本/年。流入候補は「平均的クラスタ・平均頻度」と仮定するため、実クラスタ
   には属さず専用の仮想クラスタ FAMILY_INFLOW に割り当て、頻度は17系統の月間頻度の単純平均を使う。
   到着タイミングは決定論的な均等間隔（Poissonランダム過程ではなく、頻度シナリオの簡略化に合わせた
   再現可能な近似）。
6. **判定確率のモデル化（重要な簡略化・限界）**: 判定可能日に到達した系統は、その系統が真に有効
   （latent success=1）であれば即座に正式合格すると仮定する（検出力100%の簡略化）。実際の凍結判定
   ルール（前向きEV片側p値・lift>1・頻度条件等）の統計的検出力は100%未満であるため、本モデルは
   「検出力100%を仮定した楽観シナリオ」を示す。ただし偽陽性ゼロ・観察継続中の対象からの脱落
   （admission脱落）なし等の別方向の単純化も混在しており、これらは逆方向にも作用しうるため
   厳密な意味での「上限」ではない点に注意（本モデルの限界の一つ・詳細はモデル仮定8参照）。
   経営計画上の目安として利用し、個別系統の合否判定には使わない。

Usage:
    docker compose run --rm xstock python scripts/kpi_goal_probability.py
    docker compose run --rm xstock python scripts/kpi_goal_probability.py --n-sim 10000
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = REPO_ROOT / "config" / "paper_watchlist.json"
OUTPUT_KPI_DIR = REPO_ROOT / "output" / "kpi"
REPORT_DIR = REPO_ROOT / "output" / "kpi_goal_prob"
# P3頻度突合自己チェックの突合先（scripts/kpi_maturity_power.py の出力・読み取り専用）。
MATURITY_CSV_PATH = REPO_ROOT / "output" / "kpi_maturity" / "maturity.csv"
FREQ_MISMATCH_TOLERANCE = 0.01  # 件/月。丸め誤差を超える差異のみ「不一致」として報告する

# --- モデル定数 ------------------------------------------------------------------

SEED = 20260716
N_SIM_DEFAULT = 100_000
MIN_N_FOR_JUDGE = 100  # §7-J等の凍結判定ルール: n>=100 or 12完全暦月の遅い方（共通ルール）

# 全17観察系統の in_sample.period はいずれも "2016-11〜2022-11"（73ヶ月）で共通。
# watchlist に avg_monthly_signals が無い系統はこの月数で n/months 換算する
# （他系統のnoteが明記する既存慣行「avg_monthly_signalsはn/73ヶ月の導出値」と同型）。

# ペーパートレード運用の起点。個別registered_dateの数日差は本シミュレーションでは簡略化して
# 無視し、全系統・全シナリオで一律この起点から「12完全暦月」を数える（→2027-08-01）。
PROGRAM_START = date(2026, 8, 1)

DEADLINES: dict[str, date] = {
    "2027末": date(2027, 12, 31),
    "2028末": date(2028, 12, 31),
    "2030末": date(2030, 12, 31),
}
THRESHOLDS = [1, 3, 10]

P_GRID = [0.05, 0.10, 0.20]
RHO_GRID = [0.3, 0.7]
INFLOW_GRID = [0, 5, 10]  # 新規候補流入 本/年

EXCLUDED_STATUSES = {"reference", "hoos_rejected"}  # 有効系統から除外（棄却済み/参考のみ）

# --- 家族クラスタ分類（kpi_name からの機械的分類。分類表はここがSSoT） -------------

FAMILY_VOLSHOCK = "volshock系"
FAMILY_FINS = "fins決算系"
FAMILY_REVERSAL = "リバーサル系"
FAMILY_SUPPLY = "需給系"
FAMILY_OTHER = "その他"
FAMILY_INFLOW = "新規流入(平均)"  # 新規候補流入専用の仮想クラスタ（実クラスタには属さない）

FAMILIES_REAL = [FAMILY_VOLSHOCK, FAMILY_FINS, FAMILY_REVERSAL, FAMILY_SUPPLY, FAMILY_OTHER]

FINS_KEYWORDS = ("sue_", "sales_beat", "guidance_fy", "cfo_margin", "margin_expand", "earnings_spillover")
REVERSAL_KEYWORDS = ("strev", "reversal", "rebound", "dip_reentry")
SUPPLY_KEYWORDS = ("shortcover", "turnover_rank", "margin_pampan", "margin_urinaga")


def classify_family(kpi_name: str) -> str:
    """watchlistのkpi_nameから機械的に家族クラスタへ割り当てる（判定順序=先勝ち）。

    1. "volshock"で始まる → volshock系（出来高ショック系）
    2. sue_/sales_beat/guidance_fy/cfo_margin/margin_expand/earnings_spillover を含む
       → fins決算系（決算・財務開示由来。margin_expand/cfo_marginは信用取引ではなく
       利益率(マージン)の意であることに注意）
    3. strev/reversal/rebound/dip_reentry を含む → リバーサル系
    4. shortcover/turnover_rank/margin_pampan/margin_urinaga を含む → 需給系
       （空売り残高・回転率ランク・信用残高＝需給フロー由来）
    5. 上記いずれにも該当しない → その他
    """
    if kpi_name.startswith("volshock"):
        return FAMILY_VOLSHOCK
    if any(k in kpi_name for k in FINS_KEYWORDS):
        return FAMILY_FINS
    if any(k in kpi_name for k in REVERSAL_KEYWORDS):
        return FAMILY_REVERSAL
    if any(k in kpi_name for k in SUPPLY_KEYWORDS):
        return FAMILY_SUPPLY
    return FAMILY_OTHER


# --- 日付ユーティリティ -----------------------------------------------------------


def add_months(d: date, months: int) -> date:
    """d の月初から months ヶ月後の月初日付を返す。"""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def parse_period_months(period: str) -> int:
    """"2016-11〜2022-11" 形式の in_sample.period 文字列から月数(両端含む)を返す。"""
    start_s, end_s = period.split("〜")  # U+301C WAVE DASH
    y0, m0 = (int(x) for x in start_s.split("-"))
    y1, m1 = (int(x) for x in end_s.split("-"))
    return (y1 - y0) * 12 + (m1 - m0) + 1


def readiness_date(t0: date, freq_monthly: float) -> date:
    """判定可能日 = 12完全暦月 と n>=100到達（月間頻度で外挿）の遅い方。"""
    d12 = add_months(t0, 12)
    if freq_monthly <= 0:
        return date(9999, 12, 31)  # 事実上到達不能（本モデルの表現上の上限）
    months_needed = math.ceil(MIN_N_FOR_JUDGE / freq_monthly)
    d100 = add_months(t0, months_needed)
    return max(d12, d100)


# --- 月間頻度（P3規則: returns.csv実測 → カタログ記載 → カタログ記載からの導出） -----


def measure_actual_frequency(returns_path: Path, period: str) -> tuple[float, int, int]:
    """output/kpi/<kpi_name>/returns.csv の in_universe 行から実測月間頻度を計算する。

    分母は「最初のシグナル月〜最後のシグナル月」の実測スパンではなく、watchlist の凍結
    in_sample.period 全体の月数を使う（kpi_maturity_power.py の load_returns_frequency_sigma()
    と同一規則）。シグナルがゼロ発火の月があっても分母から脱落させないための修正
    （2026-07-16 Codexレビュー指摘: 旧版は実測スパンを分母にしており頻度が上方バイアスしていた）。
    集計対象も signal_date を in_sample.period 範囲内に限定する（kpi_maturity_power.pyと同様、
    period範囲外の行が万一混入していても頻度に影響しないようにする）。

    Returns:
        (freq_monthly, n, total_months)
    """
    df = pd.read_csv(returns_path, dtype={"signal_date": str})
    if "in_universe" in df.columns:
        df = df[df["in_universe"]]
    total_months = parse_period_months(period)
    start_s, end_s = period.split("〜")  # U+301C WAVE DASH
    y0, m0 = (int(x) for x in start_s.split("-"))
    y1, m1 = (int(x) for x in end_s.split("-"))
    ym_start, ym_end = y0 * 100 + m0, y1 * 100 + m1
    ym = df["signal_date"].str[:6].astype(int)
    sub = df[(ym >= ym_start) & (ym <= ym_end)]
    n = len(sub)
    if n == 0:
        raise SystemExit(f"FATAL: {returns_path} にperiod={period}範囲内のin_universe行が0件（実測不能）")
    return n / total_months, n, total_months


def compute_monthly_frequency(kpi_name: str, in_sample: dict) -> tuple[float, str]:
    """月間頻度とその出典を返す（優先順位: returns.csv実測 → カタログ記載 → カタログ記載からの導出）。"""
    returns_path = OUTPUT_KPI_DIR / kpi_name / "returns.csv"
    period = in_sample["period"]
    if returns_path.exists():
        freq, n, total_months = measure_actual_frequency(returns_path, period)
        return freq, (
            f"実測(output/kpi/{kpi_name}/returns.csv・in_universe・period={period}を分母="
            f"{total_months}ヶ月・n={n})"
        )
    avg = in_sample.get("avg_monthly_signals")
    if avg is not None:
        return float(avg), "カタログ記載(config/paper_watchlist.json in_sample.avg_monthly_signals)"
    n = in_sample["n"]
    months = parse_period_months(in_sample["period"])
    return n / months, f"カタログ記載からの導出(n={n}/{months}ヶ月・period={in_sample['period']})"


# --- 候補集合の構築 ---------------------------------------------------------------


def load_candidates(watchlist_path: Path = WATCHLIST_PATH) -> list[dict]:
    """config/paper_watchlist.json の有効系統(status=="observation")を実読して候補リストを返す。"""
    data = json.loads(watchlist_path.read_text(encoding="utf-8"))
    candidates = []
    for entry in data["watchlist"]:
        status = entry.get("status")
        if status in EXCLUDED_STATUSES:
            continue
        kpi_name = entry["kpi_name"]
        in_sample = entry.get("in_sample") or {}
        freq, freq_source = compute_monthly_frequency(kpi_name, in_sample)
        family = classify_family(kpi_name)
        ready = readiness_date(PROGRAM_START, freq)
        candidates.append({
            "kpi_name": kpi_name,
            "status": status,
            "family": family,
            "freq_monthly": freq,
            "freq_source": freq_source,
            "n_insample": in_sample.get("n"),
            "t0": PROGRAM_START,
            "readiness_date": ready,
            "months_to_100": math.ceil(MIN_N_FOR_JUDGE / freq) if freq > 0 else None,
        })
    candidates.sort(key=lambda c: c["kpi_name"])
    return candidates


def generate_inflow_candidates(rate_per_year: int, avg_freq: float, horizon_end: date) -> list[dict]:
    """新規候補流入を決定論的な均等間隔で生成する（「平均的クラスタ・平均頻度」の簡略化）。

    到着時刻は k本目 = PROGRAM_START + round(k * 12/rate_per_year)ヶ月（k=1,2,...）。
    horizon_end を超える到着は生成しない（それより先に到着しても12完全暦月フロアだけで
    horizon_endを超えてしまい、いずれの締切にも寄与できないため）。
    """
    if rate_per_year <= 0:
        return []
    horizon_months = (horizon_end.year - PROGRAM_START.year) * 12 + (horizon_end.month - PROGRAM_START.month)
    out = []
    k = 1
    while True:
        offset = round(k * 12.0 / rate_per_year)
        if offset > horizon_months:
            break
        t0 = add_months(PROGRAM_START, offset)
        ready = readiness_date(t0, avg_freq)
        out.append({
            "kpi_name": f"inflow_{rate_per_year}per_year_{k:03d}",
            "status": "inflow(simulated)",
            "family": FAMILY_INFLOW,
            "freq_monthly": avg_freq,
            "freq_source": "新規流入は既存17系統の月間頻度の単純平均を仮定",
            "n_insample": None,
            "t0": t0,
            "readiness_date": ready,
            "months_to_100": math.ceil(MIN_N_FOR_JUDGE / avg_freq) if avg_freq > 0 else None,
        })
        k += 1
    return out


# --- モンテカルロ本体 -------------------------------------------------------------


def run_scenario(candidates: list[dict], p: float, rho: float, rng: np.random.Generator,
                  n_sim: int) -> dict[tuple[str, int], float]:
    """1シナリオ(p, rho, 候補集合)についてn_sim回シミュレーションし、締切×閾値ごとの確率を返す。

    共通潜在因子混合モデル: クラスタcの共通因子 Z_c ~ Bernoulli(p)。系統iは確率rhoで
    Z_{cluster(i)} に従い（W_i=1）、確率(1-rho)で独立に E_i~Bernoulli(p) を引く（W_i=0）。
    success_i = W_i ? Z_{cluster(i)} : E_i。周辺確率は常にpに保たれる。
    """
    clusters = sorted({c["family"] for c in candidates})
    cluster_idx = {c: i for i, c in enumerate(clusters)}
    n_c = len(candidates)
    n_clusters = len(clusters)

    z = rng.random((n_sim, n_clusters)) < p
    w = rng.random((n_sim, n_c)) < rho
    e = rng.random((n_sim, n_c)) < p

    cidx = np.array([cluster_idx[c["family"]] for c in candidates])
    z_sel = z[:, cidx]
    success = np.where(w, z_sel, e)  # (n_sim, n_c) bool

    result: dict[tuple[str, int], float] = {}
    for dl_name, dl_date in DEADLINES.items():
        ready_mask = np.array([c["readiness_date"] <= dl_date for c in candidates])
        counted = success & ready_mask[None, :]
        counts = counted.sum(axis=1)
        for k in THRESHOLDS:
            result[(dl_name, k)] = float((counts >= k).mean())
    return result


def run_all_scenarios(base_candidates: list[dict], n_sim: int, seed: int = SEED) -> dict:
    """18シナリオ(p×rho×inflow)全てを実行する。

    Returns:
        {inflow_rate: {(p, rho): {(deadline_name, threshold): prob}}}
    """
    avg_freq = float(np.mean([c["freq_monthly"] for c in base_candidates]))
    horizon_end = DEADLINES["2030末"]
    rng = np.random.default_rng(seed)

    out: dict = {}
    for inflow_rate in INFLOW_GRID:
        inflow_candidates = generate_inflow_candidates(inflow_rate, avg_freq, horizon_end)
        candidates = base_candidates + inflow_candidates
        out[inflow_rate] = {"n_inflow": len(inflow_candidates), "scenarios": {}}
        for p in P_GRID:
            for rho in RHO_GRID:
                probs = run_scenario(candidates, p, rho, rng, n_sim)
                out[inflow_rate]["scenarios"][(p, rho)] = probs
    return out, avg_freq


# --- 自己チェック（P3頻度突合） ---------------------------------------------------


def cross_check_frequency_with_maturity_csv(
    candidates: list[dict], maturity_csv_path: Path = MATURITY_CSV_PATH
) -> list[str]:
    """本スクリプトの freq_monthly と scripts/kpi_maturity_power.py の出力
    （output/kpi_maturity/maturity.csv）の freq_monthly 列を突合する自己チェック。

    両スクリプトとも returns.csv実測時の分母を「凍結in_sample.period全体の月数」に統一した
    （2026-07-16 Codexレビュー指摘対応）ため、returns.csvが存在する系統は基本的に一致する
    はずである。avg_monthly_signals/導出値にフォールバックする2系統
    （volshock_x_above200_quiet・sue_x_above200）は、両スクリプトのフォールバック優先順位が
    異なる場合があり、一致しないことがある（これは本チェックが検出すべき既知の差異であり、
    バグではない。理由は report.md 側に個別に明記する）。

    kpi_maturity_power.py を先に実行していない場合は maturity.csv が存在せず、その旨を
    1件のメッセージとして返す（読み取り専用チェックであり、本スクリプトからmaturity.csvを
    生成することはしない）。

    Returns:
        不一致（またはmaturity.csv不在）の説明文字列のリスト（空なら全一致）。
    """
    if not maturity_csv_path.exists():
        return [
            f"{maturity_csv_path} が存在しないため頻度突合をスキップしました"
            "（先に scripts/kpi_maturity_power.py を実行してください）"
        ]
    with open(maturity_csv_path, encoding="utf-8") as f:
        by_name = {row["kpi_name"]: row["freq_monthly"] for row in csv.DictReader(f)}

    mismatches: list[str] = []
    for c in candidates:
        ref_raw = by_name.get(c["kpi_name"])
        if ref_raw is None:
            mismatches.append(f"{c['kpi_name']}: maturity.csvに該当行なし（突合不能）")
            continue
        try:
            ref = float(ref_raw)
        except ValueError:
            mismatches.append(f"{c['kpi_name']}: maturity.csvのfreq_monthly値が数値でない({ref_raw!r})")
            continue
        diff = abs(ref - c["freq_monthly"])
        if diff > FREQ_MISMATCH_TOLERANCE:
            mismatches.append(
                f"{c['kpi_name']}: maturity.csv={ref:.3f}件/月 vs 本スクリプト="
                f"{c['freq_monthly']:.3f}件/月（差分{diff:.3f}）"
            )
    return mismatches


# --- 自己チェック（単調性） -------------------------------------------------------


def check_monotonicity(results: dict) -> list[str]:
    """(a) 同一締切でthreshold=1>=3>=10 (b) 同一thresholdで締切が延びると非減少、を検証する。

    Returns:
        違反があれば説明文字列のリスト（空リスト=OK）。
    """
    violations = []
    deadline_names = list(DEADLINES.keys())
    for inflow_rate, block in results.items():
        for (p, rho), probs in block["scenarios"].items():
            for dl in deadline_names:
                p1 = probs[(dl, 1)]
                p3 = probs[(dl, 3)]
                p10 = probs[(dl, 10)]
                if not (p1 >= p3 - 1e-12 >= p10 - 1e-12) or not (p1 + 1e-12 >= p3 and p3 + 1e-12 >= p10):
                    violations.append(
                        f"閾値単調性違反: inflow={inflow_rate} p={p} rho={rho} {dl}: "
                        f"P(>=1)={p1:.4f} P(>=3)={p3:.4f} P(>=10)={p10:.4f}"
                    )
            for k in THRESHOLDS:
                seq = [probs[(dl, k)] for dl in deadline_names]
                for a, b in zip(seq, seq[1:]):
                    if b + 1e-12 < a:
                        violations.append(
                            f"締切単調性違反: inflow={inflow_rate} p={p} rho={rho} k={k}: {seq}"
                        )
    return violations


# --- レポート出力 -----------------------------------------------------------------


def _fmt_pct(x: float) -> str:
    return f"{x:.2%}"


def write_report(path: Path, base_candidates: list[dict], avg_freq: float, results: dict,
                  n_sim: int, elapsed_sec: float, violations: list[str],
                  freq_mismatches: list[str]) -> None:
    lines = [
        "# 「勝ちレシピ10本」到達確率モンテカルロ・シミュレーション",
        "",
        "**経営計画/運用監視用。合否判定には一切使用しない。**"
        "（既存の正式判定フロー judge()・枠S/枠F運用開始ライン・各§7-*凍結ルールを一切代替しない）",
        "",
        f"生成: scripts/kpi_goal_probability.py（seed={SEED}・n_sim={n_sim:,}・実行時間{elapsed_sec:.1f}秒）",
        "",
        "## 候補集合（config/paper_watchlist.json 実読・status==\"observation\"のみ・17系統）",
        "",
        "除外: `pead_gap8_vol3`(status=reference・holdout棄却済み参照のみ) / "
        "`sh_dip_reentry`(status=hoos_rejected・棄却済み)",
        "",
        "### 家族クラスタ分類表（kpi_nameから機械的に分類・classify_family()がSSoT）",
        "",
        "| kpi_name | 家族クラスタ | 月間頻度(推定) | 頻度出典 | 判定可能日 |",
        "|---|---|---|---|---|",
    ]
    for c in base_candidates:
        lines.append(
            f"| {c['kpi_name']} | {c['family']} | {c['freq_monthly']:.2f}件/月 | "
            f"{c['freq_source']} | {c['readiness_date'].isoformat()} |"
        )

    fam_counts: dict[str, int] = {}
    for c in base_candidates:
        fam_counts[c["family"]] = fam_counts.get(c["family"], 0) + 1
    lines += ["", "家族クラスタ別内訳: " + " / ".join(f"{k}={v}本" for k, v in fam_counts.items())]

    lines += [
        "",
        "## P3頻度突合自己チェック",
        "",
        "本スクリプトの freq_monthly と `scripts/kpi_maturity_power.py` の出力"
        "（`output/kpi_maturity/maturity.csv`）の freq_monthly を突合する（両スクリプトとも"
        "returns.csv実測時の分母を凍結in_sample.period全体の月数に統一済み・2026-07-16"
        "Codexレビュー指摘対応）。",
        "",
    ]
    if not freq_mismatches:
        lines.append("**OK（全17系統で一致・許容誤差0.01件/月以内）**")
    else:
        lines.append(f"**不一致 {len(freq_mismatches)}件**")
        lines += [f"- {m}" for m in freq_mismatches]
        lines.append("")
        lines.append(
            "上記のうち returns.csv不在の2系統（volshock_x_above200_quiet・sue_x_above200）は、"
            "本スクリプトが優先順位②（watchlist記載のavg_monthly_signals）または③"
            "（in_sample.n/period月数からの導出）にフォールバックするのに対し、"
            "kpi_maturity_power.py はcatalog引用値をFREQ_OVERRIDE定数として直接ハードコードして"
            "おり、フォールバック先の情報源が異なるため一致しないことがある"
            "（分母バイアスの修正とは別の、既存の設計差異。catalog記載値をどちらの優先順位に"
            "揃えるかは別途要検討）。"
        )
    lines += [
        "",
        "## モデル仮定（全列挙）",
        "",
        "1. 候補集合は config/paper_watchlist.json の status==\"observation\" の17系統（実読）。"
        "reference/hoos_rejectedは除外。",
        "2. 家族クラスタは5分類（volshock系/fins決算系/リバーサル系/需給系/その他）。"
        "分類はkpi_nameの部分文字列マッチによる機械的判定（本表参照）。",
        f"3. 判定可能日 = 12完全暦月 と n>=100到達（月間頻度で外挿）の遅い方。"
        f"12完全暦月フロアは全系統一律 PROGRAM_START={PROGRAM_START.isoformat()} 起点"
        f"（→{add_months(PROGRAM_START, 12).isoformat()}）で簡略化。実際のregistered_date"
        "（2026-07-07〜2026-07-15）の数日〜1週間の差は無視する。",
        "4. 月間頻度の出典優先順位（P3規則踏襲・kpi_maturity_power.pyと分母規則を統一）: "
        "①output/kpi/<kpi_name>/returns.csvのin_universe実測（分母は凍結in_sample.period "
        "全体の月数。ゼロ発火月があっても分母から脱落させない） → "
        "②watchlist記載のin_sample.avg_monthly_signals(カタログ記載) → "
        "③in_sample.n/in_sample.periodの月数（カタログ記載からの導出）。"
        "①の分母修正の詳細は「## P3頻度突合自己チェック」節を参照。",
        "5. 各系統の「真に有効か」は共通潜在因子混合モデルで生成: クラスタ共通因子"
        "Z_c〜Bernoulli(p)。系統iは確率rhoでZ_{cluster(i)}に従い、確率(1-rho)で独立に"
        "Bernoulli(p)を引く。周辺確率は常にp（rho非依存）。",
        "6. rhoは「系統が共通因子に従う確率」であり、系統ペア間の実効相関そのものではない。"
        "この2値混合モデルでは同クラスタ内2系統間の実効相関=rho²になる"
        "（rho=0.3→実効ペア相関0.09／rho=0.7→実効ペア相関0.49）。下表の列名を「rho(クラスタ内相関)」"
        "から「共通因子採用確率rho」に改称したのはこのため（2026-07-16 Codexレビュー指摘）。",
        f"7. 新規候補流入(0/5/10本/年)は「平均的クラスタ・平均頻度」と仮定し、実クラスタに属さない"
        f"仮想クラスタ({FAMILY_INFLOW})に割り当てる。頻度=既存17系統の単純平均="
        f"{avg_freq:.2f}件/月。到着タイミングは決定論的な均等間隔近似（真のPoisson過程ではない）。",
        "8. 判定可能日に到達し、かつ真に有効(latent success=1)であれば即座に正式合格すると仮定"
        "（検出力100%の簡略化）。実際の凍結判定ルールの統計的検出力は100%未満のため、"
        "**本モデルは検出力100%を仮定した楽観シナリオ**を示す。ただし偽陽性ゼロ・観察継続中の"
        "対象がreadiness到達前に脱落しない（admission脱落なし）等の別方向の単純化も混在しており、"
        "これらは逆方向にも作用しうるため厳密な意味での「上限」ではない点に注意"
        "（2026-07-16 Codexレビュー指摘）。",
        f"9. n_sim={n_sim:,}・seed={SEED}固定。乱数生成器はnumpy.random.default_rng(seed)を"
        "18シナリオ(p×rho×inflow)の固定順序（inflow外側→p→rho内側）で逐次消費し、再現可能。",
    ]

    lines += ["", "## シナリオ×締切×閾値の確率表", ""]
    for inflow_rate in INFLOW_GRID:
        block = results[inflow_rate]
        lines += [
            f"### 新規候補流入 {inflow_rate}本/年（シミュレーション上の追加候補数={block['n_inflow']}本、"
            f"頻度={avg_freq:.2f}件/月と仮定）",
            "",
            "| p(独立エッジ率) | 共通因子採用確率rho | "
            + " | ".join(f"{dl}P(>={k})" for dl in DEADLINES for k in THRESHOLDS) + " |",
            "|---|---" + "|---" * (len(DEADLINES) * len(THRESHOLDS)) + "|",
        ]
        for p in P_GRID:
            for rho in RHO_GRID:
                probs = block["scenarios"][(p, rho)]
                cells = " | ".join(_fmt_pct(probs[(dl, k)]) for dl in DEADLINES for k in THRESHOLDS)
                lines.append(f"| {p:.2f} | {rho:.1f} | {cells} |")
        lines.append("")

    lines += [
        "## 自己チェック（単調性）",
        "",
        "検証項目: (a) 同一締切内で P(>=1) >= P(>=3) >= P(>=10) "
        "(b) 同一閾値で締切が延びると非減少（2027末<=2028末<=2030末）",
        "",
    ]
    if violations:
        lines.append(f"**NG（{len(violations)}件の違反）**")
        lines += [f"- {v}" for v in violations[:50]]
    else:
        lines.append("**OK（全シナリオ・全締切・全閾値の組で単調性を満たす）**")

    lines += [
        "",
        "## 実装ノート",
        "- 読み取り専用: data/kpi_trials/trials.jsonl・data/paper_trades/配下・"
        "config/paper_watchlist.json・docs/stock-algo-kpi-catalog.md・tasks/*.md への書き込みなし",
        "- 出力: 本レポートのみ（output/kpi_goal_prob/report.md）。台帳追記なし",
        "- モデルの限界: 検出力100%簡略化・12完全暦月フロアの一律起点簡略化・"
        "新規流入の到着タイミング決定論的近似・家族間相関=0（クラスタ横断の共通ショックなし）",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- main --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="「勝ちレシピ10本」到達確率モンテカルロ・シミュレーション"
                                              "（経営計画用・合否判定には不使用）")
    ap.add_argument("--n-sim", type=int, default=N_SIM_DEFAULT,
                     help=f"シミュレーション回数（既定{N_SIM_DEFAULT:,}。実行が長い場合は10000等に低減可）")
    ap.add_argument("--watchlist", default=str(WATCHLIST_PATH), help="config/paper_watchlist.json のパス")
    ap.add_argument("--output-dir", default=str(REPORT_DIR))
    args = ap.parse_args()

    t0 = time.monotonic()
    candidates = load_candidates(Path(args.watchlist))
    if len(candidates) != 17:
        print(f"[warn] 有効系統数が想定の17と異なります: {len(candidates)}件", flush=True)

    results, avg_freq = run_all_scenarios(candidates, n_sim=args.n_sim, seed=SEED)
    elapsed = time.monotonic() - t0

    violations = check_monotonicity(results)
    freq_mismatches = cross_check_frequency_with_maturity_csv(candidates)

    out_dir = Path(args.output_dir)
    report_path = out_dir / "report.md"
    write_report(
        report_path, candidates, avg_freq, results, args.n_sim, elapsed, violations,
        freq_mismatches,
    )

    print(f"[done] 候補={len(candidates)}系統 n_sim={args.n_sim:,} 実行時間={elapsed:.1f}秒 "
          f"単調性違反={len(violations)}件 P3頻度突合不一致={len(freq_mismatches)}件", flush=True)
    print(f"[report] {report_path}", flush=True)
    if freq_mismatches:
        print(
            f"[warn] P3頻度突合で{len(freq_mismatches)}件の不一致（report.mdの"
            "「P3頻度突合自己チェック」節を確認してください）", flush=True,
        )

    if violations:
        print("[FATAL] 単調性の自己チェックに違反しています。report.mdを確認してください。", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
