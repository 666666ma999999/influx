#!/usr/bin/env python3
"""online-FDR切替起案v2 §6 工程2: 5-arm統計体系比較シミュレーション・ハーネス（骨格・DRAFT）。

**本スクリプトはDRAFTである。凍結（数値・判定基準）はteam-lead + Codex敵対レビューが行う
（docs/fdr-switch-proposal.md §6 手順1）。本スクリプトの出力を意思決定・正式判定に使用しない。**
台帳(data/kpi_trials/*)・config/paper_watchlist.json・カタログ(docs/stock-algo-kpi-catalog.md)は
一切読み書きしない（工程2指示: 台帳/config一切不干渉）。出力は output/fdr_sim/ のみ（gitignore済み）。

## 位置づけ・出典

- 起案書: docs/fdr-switch-proposal.md §6（行81-106）+ 冒頭R1レビュー要旨（行1-9・NO-GOブロッカー10件）
- σ/n_eff校正の実測根拠: output/kpi_maturity/report.md §3-4, output/kpi_maturity/maturity.csv
- 数値パラメータ本体は config/fdr_sim_spec.draft.json（本スクリプトの唯一の外部入力）

## モデル概要

1. **候補到着**: 年あたりlambda_per_year本を月次一様（homogeneous Poisson過程、月次rate=lambda/12）で
   近似。各候補は独立に確率p_trueで「真のエッジあり」。真エッジ候補の月次EVはev_true_pct_choicesの
   離散一様。偽候補はEV_true=0固定。
2. **判定の遅延と非同期性**（R1ブロッカー①の核心）: 各候補はjudgment_delay_months（既定12ヶ月）後に
   判定される。複数候補が同時に走行し、判定順は登録順と一致するとは限らない（本モデルは固定delayの
   ため実際には判定順=登録順で決定論的に一致するが、A2/A3の非同期適格性フィルタは一般のdelay分布にも
   拡張可能な設計にしてある）。
3. **検出力モデル**: 判定時の片側z検定を、実際にはp値シミュレーションとして実装する。
   z_effect = EV_true/(sigma/sqrt(n_eff))、観測統計量 Z ~ N(z_effect, 1)、片側p値 = 1-Φ(Z)。
   ある候補にalphaが配分されたとき reject ⟺ p_value <= alpha。これは
   P(reject) = Φ(z_effect - Φ^{-1}(1-alpha)) と数学的に同一（proposal.md §6のpower式と整合）であり、
   かつ偽候補(z_effect=0)では p_value ~ Uniform(0,1) 正確に成り立つため P(reject) = alpha が厳密に
   成立する（--smoke時のユニット的検証②で利用）。
4. **5 arm**: 全てArm基底クラスのdecide(candidates, sim_start_date) -> Decision(reject, alpha) を実装。
   arm追加はARM_REGISTRYに1エントリ追加するだけで良い構造にしてある。

## 既知の簡略化（DRAFT・凍結前にteam-lead/Codexの確認を要する点）

- A1の陣（cohort）形成規則: 実際の陣サイズは可変（5,3,1,2,1）だったが、本skeletonは固定cohort_sizeで
  単純化している（spec json内にDRAFT注記）。
- A2/A3のwealth更新式: 原論文の厳密なclosed-form（log項を含むgamma系列等）ではなく、多項式減衰gamma
  系列によるrunning-balance近似。非同期適格性フィルタ（登録時点までに判定確定済みの発見のみを
  カウント対象とする）自体はZrnic et al. 2018型async online-FDRの核心的発想を反映している。
- A4のfamily内BH: (family, 判定日の暦年)をバッチ単位とし、バッチ確定時に一度だけBHを適用する
  一発バッチ方式（A5と同一機構をfamily単位に細分化）。当初は判定発生ごとの逐次再計算方式を検討したが、
  --smoke自己検証でp_true=0世界のFWERを名目の約3倍に膨張させることが判明したため現行方式に修正した
  （詳細はFamilyHierarchicalBHArmのdocstring参照）。

Usage:
    python3 scripts/fdr_regime_sim.py --spec config/fdr_sim_spec.draft.json --smoke
    python3 scripts/fdr_regime_sim.py --spec config/fdr_sim_spec.draft.json          # 本実行（凍結後のみ）
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPEC_PATH = REPO_ROOT / "config" / "fdr_sim_spec.draft.json"
DEFAULT_OUT_DIR = REPO_ROOT / "output" / "fdr_sim"

# グレゴリオ暦の平均月長（365.2425日/12ヶ月）。月数⇔日数変換に一貫使用
# （scripts/kpi_maturity_power.py の AVG_DAYS_PER_MONTH と同一の慣行に統一）。
AVG_DAYS_PER_MONTH = 365.2425 / 12

N_EFF_MODE_ORDER = {"conservative": 0, "optimistic": 1}


# ============================================================================
# 基礎関数（正規分布・日付変換）
# ============================================================================

def norm_cdf(x: float) -> float:
    """標準正規分布の累積分布関数（scipy不使用・math.erfのみで実装）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def date_to_month_index(sim_start: date, d: date) -> float:
    return (d - sim_start).days / AVG_DAYS_PER_MONTH


def month_index_to_date(sim_start: date, month_index: float) -> date:
    return sim_start + timedelta(days=month_index * AVG_DAYS_PER_MONTH)


# ============================================================================
# 候補（Candidate）生成
# ============================================================================

@dataclass
class Candidate:
    cid: int
    register_month: float
    decide_month: float
    is_true: bool
    ev_true_pct: float
    family: int
    z_stat: float
    p_value: float


def generate_arrivals(rng: np.random.Generator, lambda_per_year: float, horizon_months: int) -> np.ndarray:
    """年あたりlambda_per_year本の候補到着を月次一様(homogeneous Poisson過程)で生成する。

    月次rate = lambda_per_year/12。各月内での到着時刻はUniform(0,1)（月内一様）。
    """
    if lambda_per_year <= 0:
        return np.array([], dtype=float)
    monthly_rate = lambda_per_year / 12.0
    n_months = int(math.ceil(horizon_months))
    counts = rng.poisson(monthly_rate, size=n_months)
    times: List[float] = []
    for m, c in enumerate(counts):
        if c <= 0:
            continue
        offsets = rng.uniform(0.0, 1.0, size=int(c))
        times.extend(float(m) + float(o) for o in offsets)
    return np.sort(np.array(times, dtype=float))


def build_candidates(
    rng: np.random.Generator,
    lambda_per_year: float,
    horizon_months: int,
    p_true: float,
    ev_true_pct_choices: List[float],
    n_eff: float,
    sigma: float,
    judgment_delay_months: float,
    n_families: int,
) -> List[Candidate]:
    reg_months = generate_arrivals(rng, lambda_per_year, horizon_months)
    candidates: List[Candidate] = []
    for i, rt in enumerate(reg_months):
        is_true = bool(rng.random() < p_true)
        ev_pct = float(rng.choice(ev_true_pct_choices)) if is_true else 0.0
        family = int(rng.integers(0, n_families))
        z_effect = (ev_pct / 100.0) * math.sqrt(n_eff) / sigma if is_true else 0.0
        z_stat = float(z_effect + rng.normal())
        p_value = 1.0 - norm_cdf(z_stat)
        p_value = min(max(p_value, 1e-15), 1.0 - 1e-15)  # 数値ガード
        candidates.append(Candidate(
            cid=i, register_month=float(rt), decide_month=float(rt) + judgment_delay_months,
            is_true=is_true, ev_true_pct=ev_pct, family=family, z_stat=z_stat, p_value=p_value,
        ))
    return candidates


# ============================================================================
# gamma系列（online-FDR系armで共通使用）
# ============================================================================

class GammaSeries:
    """LORD++/SAFFRON型armで使う減衰系列 gamma_j (j=1,2,...・sum=1に正規化)。

    DRAFT: 多項式減衰 gamma_j ∝ j^-exponent のみ実装（Ramdas et al. 2017の実装で慣行的に
    使われる近似形）。原論文の厳密な閉形式（log項含む）はteam-lead/Codex確認後に確定する。
    """

    def __init__(self, exponent: float, n_max: int = 4000):
        j = np.arange(1, n_max + 1, dtype=float)
        raw = 1.0 / np.power(j, exponent)
        raw = raw / raw.sum()
        self._vals = raw

    def __call__(self, j: int) -> float:
        if j <= 0:
            return 0.0
        idx = j - 1
        if idx >= len(self._vals):
            return float(self._vals[-1])
        return float(self._vals[idx])


def build_gamma_series(gamma_spec: dict) -> GammaSeries:
    gtype = gamma_spec.get("type", "poly_decay")
    if gtype == "poly_decay":
        return GammaSeries(exponent=float(gamma_spec.get("exponent", 1.6)))
    raise ValueError(f"未対応のgamma_series type: {gtype}（DRAFT skeletonはpoly_decayのみ実装）")


# ============================================================================
# Arm基底クラス・5 arm実装
# ============================================================================

class Decision(NamedTuple):
    reject: Dict[int, bool]
    alpha: Dict[int, float]


class Arm:
    name: str = "base"

    def decide(self, candidates: List[Candidate], sim_start_date: date) -> Decision:
        raise NotImplementedError


class FWERCohortHalvingArm(Arm):
    """A1: 現行FWER 陣別α半減（forward-only継続）。"""

    name = "a1_fwer_cohort_halving"

    def __init__(self, total_budget: float, start_cohort_index: int, cohort_size: int):
        self.total_budget = total_budget
        self.start_cohort_index = start_cohort_index
        self.cohort_size = max(1, int(cohort_size))

    def decide(self, candidates: List[Candidate], sim_start_date: date) -> Decision:
        ordered = sorted(candidates, key=lambda c: c.register_month)
        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        for pos, c in enumerate(ordered):
            cohort_b = self.start_cohort_index + (pos // self.cohort_size)
            a = (self.total_budget / (2 ** cohort_b)) / self.cohort_size
            alpha[c.cid] = a
            reject[c.cid] = c.p_value <= a
        return Decision(reject, alpha)


class AsyncLORDArm(Arm):
    """A2: LORD++（非同期対応・Zrnic et al.型のDRAFT簡略実装）。

    非同期適格性フィルタ（R1ブロッカー①対応）: 候補iのalpha_iは『iの登録時点S_iまでに判定が
    確定した(decide_month<=S_i)候補』のみを発見カウント対象とする。
    """

    name = "a2_lord_async"

    def __init__(self, W0: float, reward: float, gamma_spec: dict, judgment_delay_months: float):
        self.W0 = W0
        self.reward = reward
        self.gamma = build_gamma_series(gamma_spec)
        self.judgment_delay_months = judgment_delay_months

    def decide(self, candidates: List[Candidate], sim_start_date: date) -> Decision:
        ordered = sorted(candidates, key=lambda c: c.register_month)
        reg_months = [c.register_month for c in ordered]
        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        for pos, c in enumerate(ordered):
            k_i = pos + 1
            threshold = c.register_month - self.judgment_delay_months
            eligible_upto = bisect.bisect_right(reg_months, threshold)
            reward_sum = 0.0
            for j in range(eligible_upto):
                jc = ordered[j]
                if reject[jc.cid]:
                    k_j = j + 1
                    reward_sum += self.gamma(k_i - k_j)
            a = self.gamma(k_i) * self.W0 + self.reward * reward_sum
            alpha[c.cid] = a
            reject[c.cid] = c.p_value <= a
        return Decision(reject, alpha)


class AsyncSAFFRONArm(Arm):
    """A3: SAFFRON async（DRAFT簡略実装）。A2と同一の非同期適格性フィルタ+candidate閾値lambda_s。"""

    name = "a3_saffron_async"

    def __init__(self, W0: float, reward: float, lambda_s: float, gamma_spec: dict,
                 judgment_delay_months: float):
        self.W0 = W0
        self.reward = reward
        self.lambda_s = lambda_s
        self.gamma = build_gamma_series(gamma_spec)
        self.judgment_delay_months = judgment_delay_months

    def decide(self, candidates: List[Candidate], sim_start_date: date) -> Decision:
        ordered = sorted(candidates, key=lambda c: c.register_month)
        reg_months = [c.register_month for c in ordered]
        # is_candidate は alpha配分と独立（p_valueは登録順の他候補の判定結果に依存しないため事前確定可）
        is_candidate = [c.p_value <= self.lambda_s for c in ordered]
        prefix = [0] * (len(ordered) + 1)
        for idx, flag in enumerate(is_candidate):
            prefix[idx + 1] = prefix[idx] + (1 if flag else 0)

        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        k_index: Dict[int, int] = {}
        for pos, c in enumerate(ordered):
            threshold = c.register_month - self.judgment_delay_months
            eligible_upto = bisect.bisect_right(reg_months, threshold)
            k_i = prefix[eligible_upto] + 1
            reward_sum = 0.0
            for j in range(eligible_upto):
                jc = ordered[j]
                if is_candidate[j] and reject[jc.cid]:
                    k_j = k_index[jc.cid]
                    reward_sum += self.gamma(k_i - k_j)
            a_raw = self.gamma(k_i) * self.W0 + self.reward * reward_sum
            a = min(a_raw, self.lambda_s)
            alpha[c.cid] = a
            reject[c.cid] = c.p_value <= a
            k_index[c.cid] = k_i
        return Decision(reject, alpha)


class FamilyHierarchicalBHArm(Arm):
    """A4: family階層（family間Bonferroni×family内BH）。

    family内は (family, 判定日の暦年) の組をバッチ単位とし、そのバッチが確定した時点で
    一度だけBH(alpha_family)を適用する一発バッチ方式（A5と同一の年次バッチ機構をfamily単位に
    細分化したもの）。

    [設計変更の経緯・DRAFT] 当初案は『判定発生ごとにそのfamilyの判定済み全候補で毎回BHを
    再計算する』前向き逐次再計算方式だったが、--smoke自己検証で該当方式がp_true=0(全偽)世界の
    family内FWERを名目のalpha_family水準の約3倍(実測0.0397 vs 名目0.0125)に押し上げる
    ことが判明した（BHの一発バッチ保証は『同一バッチへの繰り返しpeeking』の下では成立しない
    ため。standalone実験で確認済み）。これはコードのバグではなくオンライン化していない
    素朴な逐次再計算という設計選択そのものの統計的欠陥であり、A5と同じ一発バッチ機構に
    揃えることで解消した（一発バッチBHのFWER<=q保証はグローバル帰無下で数値検証済み・
    standalone実験でP(any rejection)≈0.100 (q=0.10)を確認）。alpha_family水準での
    真の『online』family内BH（peeking耐性を持つ厳密な逐次手続き）が必要かはteam-lead/Codex
    確認事項として残る。
    """

    name = "a4_family_hierarchical"

    def __init__(self, n_families: int, total_budget: float):
        self.n_families = n_families
        self.alpha_family = total_budget / n_families

    def decide(self, candidates: List[Candidate], sim_start_date: date) -> Decision:
        by_batch: Dict[Tuple[int, int], List[Candidate]] = defaultdict(list)
        for c in candidates:
            yr = month_index_to_date(sim_start_date, c.decide_month).year
            by_batch[(c.family, yr)].append(c)

        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        for _, group in by_batch.items():
            m = len(group)
            order = sorted(range(m), key=lambda i: group[i].p_value)
            p_sorted = [group[i].p_value for i in order]
            k_max = 0
            for k in range(m, 0, -1):
                if p_sorted[k - 1] <= (k / m) * self.alpha_family:
                    k_max = k
                    break
            cutoff = p_sorted[k_max - 1] if k_max > 0 else 0.0
            for i in order:
                c = group[i]
                alpha[c.cid] = cutoff
                reject[c.cid] = (k_max > 0) and (c.p_value <= cutoff)
        return Decision(reject, alpha)


class FixedBatchBHArm(Arm):
    """A5: 固定バッチBH（年次バッチ）。判定日が属する暦年ごとにまとめてBH(q)を1回適用。"""

    name = "a5_fixed_batch_bh"

    def __init__(self, q: float):
        self.q = q

    def decide(self, candidates: List[Candidate], sim_start_date: date) -> Decision:
        by_year: Dict[int, List[Candidate]] = defaultdict(list)
        for c in candidates:
            yr = month_index_to_date(sim_start_date, c.decide_month).year
            by_year[yr].append(c)

        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        for yr, group in by_year.items():
            m = len(group)
            order = sorted(range(m), key=lambda i: group[i].p_value)
            p_sorted = [group[i].p_value for i in order]
            k_max = 0
            for k in range(m, 0, -1):
                if p_sorted[k - 1] <= (k / m) * self.q:
                    k_max = k
                    break
            cutoff = p_sorted[k_max - 1] if k_max > 0 else 0.0
            for i in order:
                c = group[i]
                alpha[c.cid] = cutoff
                reject[c.cid] = (k_max > 0) and (c.p_value <= cutoff)
        return Decision(reject, alpha)


ARM_ORDER = [
    "a1_fwer_cohort_halving",
    "a2_lord_async",
    "a3_saffron_async",
    "a4_family_hierarchical",
    "a5_fixed_batch_bh",
]


def build_arms(spec: dict) -> Dict[str, Arm]:
    """spec['arms']からArmインスタンス辞書を組み立てる（レジストリ・arm追加はここに1行足すだけ）。"""
    a = spec["arms"]
    delay = spec["judgment_delay_months"]
    factories: Dict[str, Callable[[], Arm]] = {
        "a1_fwer_cohort_halving": lambda: FWERCohortHalvingArm(
            total_budget=a["a1_fwer_cohort_halving"]["total_budget"],
            start_cohort_index=a["a1_fwer_cohort_halving"]["start_cohort_index"],
            cohort_size=a["a1_fwer_cohort_halving"]["cohort_size"],
        ),
        "a2_lord_async": lambda: AsyncLORDArm(
            W0=a["a2_lord_async"]["W0"], reward=a["a2_lord_async"]["reward"],
            gamma_spec=a["a2_lord_async"]["gamma_series"], judgment_delay_months=delay,
        ),
        "a3_saffron_async": lambda: AsyncSAFFRONArm(
            W0=a["a3_saffron_async"]["W0"], reward=a["a3_saffron_async"]["reward"],
            lambda_s=a["a3_saffron_async"]["lambda_s"],
            gamma_spec=a["a3_saffron_async"]["gamma_series"], judgment_delay_months=delay,
        ),
        "a4_family_hierarchical": lambda: FamilyHierarchicalBHArm(
            n_families=a["a4_family_hierarchical"]["n_families"],
            total_budget=a["a4_family_hierarchical"]["total_budget"],
        ),
        "a5_fixed_batch_bh": lambda: FixedBatchBHArm(q=a["a5_fixed_batch_bh"]["q"]),
    }
    return {key: factories[key]() for key in ARM_ORDER}


# ============================================================================
# 校正値解決・メトリクス
# ============================================================================

def resolve_calibration(spec: dict, n_eff_mode: str) -> Tuple[float, float]:
    calib = spec["calibration"]
    if n_eff_mode == "conservative":
        return float(calib["n_eff_conservative"]), float(calib["sigma_month"])
    if n_eff_mode == "optimistic":
        return float(calib["n_eff_optimistic"]), float(calib["sigma_trade"])
    raise ValueError(f"未知のn_eff_mode: {n_eff_mode}")


def compute_metrics(candidates: List[Candidate], decision: Decision,
                     metric_cutoffs: Dict[str, float]) -> dict:
    reject = decision.reject
    rec: dict = {}
    for label, cutoff in metric_cutoffs.items():
        true_d = false_d = 0
        for c in candidates:
            if c.decide_month <= cutoff and reject.get(c.cid, False):
                if c.is_true:
                    true_d += 1
                else:
                    false_d += 1
        total = true_d + false_d
        rec[f"true_disc_{label}"] = true_d
        rec[f"false_disc_{label}"] = false_d
        rec[f"total_disc_{label}"] = total
        rec[f"fdr_{label}"] = (false_d / total) if total > 0 else float("nan")
        rec[f"fwer_flag_{label}"] = 1 if false_d >= 1 else 0
    disc_months = [c.decide_month for c in candidates if reject.get(c.cid, False)]
    rec["first_discovery_month"] = min(disc_months) if disc_months else float("nan")
    rec["n_candidates_total"] = len(candidates)
    return rec


# ============================================================================
# シミュレーション本体
# ============================================================================

def make_scenario_list(spec: dict) -> List[Tuple[float, float, str]]:
    p_list = sorted(spec["grid"]["p_true"])
    lam_list = sorted(spec["grid"]["lambda_per_year"])
    mode_list = sorted(spec["grid"]["n_eff_mode"], key=lambda m: N_EFF_MODE_ORDER.get(m, 99))
    return list(itertools.product(p_list, lam_list, mode_list))


def run_grid(spec: dict, arms: Dict[str, Arm], n_sim: int, sim_start_date: date,
             metric_cutoffs: Dict[str, float], horizon_months: int) -> pd.DataFrame:
    scenarios = make_scenario_list(spec)
    scenario_index = {s: idx for idx, s in enumerate(scenarios)}
    delay = spec["judgment_delay_months"]
    ev_choices = spec["effect_model"]["ev_true_pct_choices"]
    n_families = spec["arms"]["a4_family_hierarchical"]["n_families"]

    records: List[dict] = []
    for (p_true, lam, mode) in scenarios:
        sidx = scenario_index[(p_true, lam, mode)]
        n_eff, sigma = resolve_calibration(spec, mode)
        for run_id in range(n_sim):
            rng = np.random.default_rng(np.random.SeedSequence([spec["seed"], sidx, run_id]))
            candidates = build_candidates(
                rng, lam, horizon_months, p_true, ev_choices, n_eff, sigma, delay, n_families,
            )
            for arm_key, arm in arms.items():
                decision = arm.decide(candidates, sim_start_date)
                rec = compute_metrics(candidates, decision, metric_cutoffs)
                rec.update(p_true=p_true, lambda_per_year=lam, n_eff_mode=mode,
                           arm=arm_key, run_id=run_id)
                records.append(rec)
    return pd.DataFrame(records)


def aggregate(records_df: pd.DataFrame, metric_labels: List[str]) -> pd.DataFrame:
    group_cols = ["p_true", "lambda_per_year", "n_eff_mode", "arm"]
    rows: List[dict] = []
    for keys, g in records_df.groupby(group_cols):
        row = dict(zip(group_cols, keys))
        row["n_sim"] = len(g)
        for label in metric_labels:
            row[f"true_disc_{label}_mean"] = g[f"true_disc_{label}"].mean()
            row[f"false_disc_{label}_mean"] = g[f"false_disc_{label}"].mean()
            row[f"total_disc_{label}_mean"] = g[f"total_disc_{label}"].mean()
            valid = g[f"fdr_{label}"].dropna()
            row[f"fdr_{label}_mean"] = valid.mean() if len(valid) > 0 else float("nan")
            row[f"fdr_{label}_n_valid_runs"] = len(valid)
            row[f"fwer_{label}_mean"] = g[f"fwer_flag_{label}"].mean()
        disc_months = g["first_discovery_month"].dropna()
        row["first_discovery_month_mean"] = disc_months.mean() if len(disc_months) > 0 else float("nan")
        row["first_discovery_month_censored_frac"] = 1.0 - (len(disc_months) / len(g))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(group_cols).reset_index(drop=True)


# ============================================================================
# ユニット的検証（--smoke時に自動実行）
# ============================================================================

def selfcheck_a1_vs_a2_direction(summary_df: pd.DataFrame) -> Tuple[bool, str]:
    """検証①: 高流入シナリオ(p=0.20, lambda=10)で、2030時点の平均真発見数がA1<A2となる方向性を確認。

    A1(陣別α半減)は後発ほどαが指数的に枯死する構造上、資産還付のあるA2(LORD++)より真発見数が
    少なくなるはずである（proposal.md §1-2 の核心的主張の方向性チェック）。
    """
    sub = summary_df[(summary_df["p_true"] == 0.20) & (summary_df["lambda_per_year"] == 10)]
    lines = []
    ok = True
    modes = sorted(sub["n_eff_mode"].unique(), key=lambda m: N_EFF_MODE_ORDER.get(m, 99))
    if not modes:
        return False, "p=0.20,lambda=10 シナリオがsummaryに存在しない（grid不足）"
    for mode in modes:
        s = sub[sub["n_eff_mode"] == mode]
        a1 = s[s["arm"] == "a1_fwer_cohort_halving"]["true_disc_2030_mean"]
        a2 = s[s["arm"] == "a2_lord_async"]["true_disc_2030_mean"]
        if a1.empty or a2.empty:
            ok = False
            lines.append(f"  [{mode}] データ欠落")
            continue
        a1v, a2v = float(a1.iloc[0]), float(a2.iloc[0])
        passed = a1v < a2v
        ok = ok and passed
        lines.append(f"  [{mode}] A1 true_disc_2030_mean={a1v:.4f} < A2 true_disc_2030_mean={a2v:.4f} "
                      f"-> {'OK' if passed else 'NG'}")
    return ok, "\n".join(lines)


def selfcheck_null_alpha_consistency(spec: dict, arms: Dict[str, Arm], sim_start_date: date,
                                      horizon_months: int, n_sim: int) -> Tuple[bool, str]:
    """検証②: p_true=0（全候補が偽）の世界で、各armがFDR/FWERをq/alpha水準以下に抑えることを確認。

    armの型により根拠となる定理を使い分ける（1本の指標に無理に統一しない）:
    - A1/A2/A3（登録時に候補自身のp値と独立にalphaが事前確定する『事前投資型』）: p_true=0では
      p_value~Uniform(0,1)が厳密に成り立ち、alpha_iはi自身のp_valueと独立（他候補の登録前確定済み
      判定のみに依存）なので、tower propertyによりE[1(reject_i)]=E[alpha_i]が厳密に成立する。
      経験的偽発見率(=false_disc/n_candidates)と理論値mean(alpha)の差が5SE以内かを確認する。
    - A4/A5（バッチ内で一括BHを適用する『一発バッチ型』）: 各候補のalphaはバッチ内の他候補
      （自分自身のp値を含む）に依存する自己参照的な実現値であり、上記のtower property論法は
      適用できない。代わりにBenjamini-Hochberg(1995)の標準定理『独立p値・グローバル帰無の下で
      一発バッチBH(q)はP(1件以上棄却)<=q』をバッチ単位（A5=年次、A4=family×年次）で直接検証する
      （standalone実験で理論値との一致を事前確認済み: m=10,q=0.10でP(any rejection)実測0.1007
      /--smoke開発中に判明した『A4当初案=判定発生ごと逐次再計算方式』はこの定理の前提=一発バッチ
      を満たさずFWERが名目の約3倍に膨張することが分かったため、A4もA5と同じ一発バッチ機構に
      修正済み。詳細はFamilyHierarchicalBHArmのdocstring参照）。
    """
    lam = 10.0  # 候補数を稼ぐため最大流入を使用
    n_eff, sigma = resolve_calibration(spec, "conservative")
    ev_choices = spec["effect_model"]["ev_true_pct_choices"]
    n_families = spec["arms"]["a4_family_hierarchical"]["n_families"]
    delay = spec["judgment_delay_months"]
    base_seed = spec["seed"] + 999_000_000  # メイングリッドと衝突しない別ストリーム

    alpha_identity_arms = {"a1_fwer_cohort_halving", "a2_lord_async", "a3_saffron_async"}
    identity_totals = {k: {"n_cand": 0, "n_false": 0, "sum_alpha": 0.0} for k in alpha_identity_arms}
    batch_totals = {
        "a4_family_hierarchical": {"n_batches": 0, "n_batches_disc": 0,
                                    "q_level": spec["arms"]["a4_family_hierarchical"]["total_budget"] / n_families},
        "a5_fixed_batch_bh": {"n_batches": 0, "n_batches_disc": 0,
                               "q_level": spec["arms"]["a5_fixed_batch_bh"]["q"]},
    }

    for run_id in range(n_sim):
        rng = np.random.default_rng(np.random.SeedSequence([base_seed, run_id]))
        candidates = build_candidates(
            rng, lam, horizon_months, 0.0, ev_choices, n_eff, sigma, delay, n_families,
        )
        for arm_key, arm in arms.items():
            decision = arm.decide(candidates, sim_start_date)
            if arm_key in alpha_identity_arms:
                t = identity_totals[arm_key]
                for c in candidates:
                    t["sum_alpha"] += decision.alpha.get(c.cid, 0.0)
                    t["n_cand"] += 1
                    if decision.reject.get(c.cid, False):
                        t["n_false"] += 1  # p_true=0なので発見は必ず偽発見
            elif arm_key == "a5_fixed_batch_bh":
                by_year: Dict[int, List[Candidate]] = defaultdict(list)
                for c in candidates:
                    yr = month_index_to_date(sim_start_date, c.decide_month).year
                    by_year[yr].append(c)
                t = batch_totals["a5_fixed_batch_bh"]
                for group in by_year.values():
                    t["n_batches"] += 1
                    if any(decision.reject.get(c.cid, False) for c in group):
                        t["n_batches_disc"] += 1
            elif arm_key == "a4_family_hierarchical":
                by_fam_year: Dict[Tuple[int, int], List[Candidate]] = defaultdict(list)
                for c in candidates:
                    yr = month_index_to_date(sim_start_date, c.decide_month).year
                    by_fam_year[(c.family, yr)].append(c)
                t = batch_totals["a4_family_hierarchical"]
                for group in by_fam_year.values():
                    t["n_batches"] += 1
                    if any(decision.reject.get(c.cid, False) for c in group):
                        t["n_batches_disc"] += 1

    ok = True
    lines = []
    for arm_key in ["a1_fwer_cohort_halving", "a2_lord_async", "a3_saffron_async"]:
        t = identity_totals[arm_key]
        if t["n_cand"] == 0:
            lines.append(f"  [{arm_key}] 候補0件のためskip")
            continue
        emp_rate = t["n_false"] / t["n_cand"]
        mean_alpha = t["sum_alpha"] / t["n_cand"]
        se = math.sqrt(max(mean_alpha * (1 - mean_alpha), 1e-12) / t["n_cand"])
        diff = abs(emp_rate - mean_alpha)
        tol = 5 * se + 1e-6
        passed = diff <= tol
        ok = ok and passed
        lines.append(
            f"  [{arm_key}] (事前投資型・alpha恒等式チェック) 経験的偽発見率={emp_rate:.5f} vs "
            f"理論値mean(alpha)={mean_alpha:.5f} (差={diff:.5f}, 許容5SE={tol:.5f}) "
            f"-> {'OK' if passed else 'NG'} / n_candidates_total={t['n_cand']}"
        )
    for arm_key in ["a4_family_hierarchical", "a5_fixed_batch_bh"]:
        t = batch_totals[arm_key]
        if t["n_batches"] == 0:
            lines.append(f"  [{arm_key}] バッチ0件のためskip")
            continue
        fwer_hat = t["n_batches_disc"] / t["n_batches"]
        q = t["q_level"]
        se = math.sqrt(max(q * (1 - q), 1e-12) / t["n_batches"])
        tol = 5 * se + 1e-6
        passed = fwer_hat <= q + tol
        ok = ok and passed
        lines.append(
            f"  [{arm_key}] (一発バッチ型・BH定理チェック) バッチ単位FWER実測={fwer_hat:.5f} vs "
            f"名目q水準={q:.5f} (許容+5SE={tol:.5f}) -> {'OK' if passed else 'NG'} / "
            f"n_batches={t['n_batches']}"
        )
    return ok, "\n".join(lines)


# ============================================================================
# main
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="online-FDR切替起案v2 §6 工程2: 5-arm統計体系比較シミュレーション・ハーネス（骨格・DRAFT）",
    )
    ap.add_argument("--spec", default=str(DEFAULT_SPEC_PATH), help="spec json（DRAFT）のパス")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--smoke", action="store_true",
                     help="smoke_n_sim（既定200程度）で全arm動作確認+ユニット的検証を実行")
    ap.add_argument("--n-sim", type=int, default=None, help="n_simを上書き（--smoke未指定時のみ有効）")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not spec.get("_meta", {}).get("status", "").startswith("DRAFT"):
        print("[FATAL] spec._meta.status が DRAFT で始まっていません。凍結前のspecのみ本ハーネスで実行可能です。",
              file=sys.stderr)
        return 1

    sim_start_date = date.fromisoformat(spec["sim_start_date"])
    metric_dates = [date.fromisoformat(s) for s in spec["metric_dates"]]
    metric_labels = [str(d.year) for d in metric_dates]
    metric_cutoffs = {label: date_to_month_index(sim_start_date, d)
                       for label, d in zip(metric_labels, metric_dates)}

    judgment_delay = spec["judgment_delay_months"]
    min_horizon_months = max(metric_cutoffs.values()) + judgment_delay + 1
    horizon_months = int(math.ceil(max(spec["horizon_years"] * 12, min_horizon_months)))
    if spec["horizon_years"] * 12 < min_horizon_months:
        print(f"[warn] spec.horizon_years({spec['horizon_years']})はmetric_datesを賄うのに不足。"
              f"{horizon_months}ヶ月に自動延長しました。", flush=True)

    arms = build_arms(spec)

    if args.smoke:
        n_sim = spec["smoke_n_sim"]
        out_name = "smoke_summary.csv"
    else:
        n_sim = args.n_sim if args.n_sim is not None else spec["n_sim"]
        out_name = "summary.csv"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    scenarios = make_scenario_list(spec)
    print(f"[run] scenarios={len(scenarios)} n_sim={n_sim} arms={len(arms)} "
          f"horizon_months={horizon_months} spec={spec_path}", flush=True)
    records_df = run_grid(spec, arms, n_sim, sim_start_date, metric_cutoffs, horizon_months)
    summary_df = aggregate(records_df, metric_labels)
    elapsed = time.monotonic() - t0

    out_path = out_dir / out_name
    summary_df.to_csv(out_path, index=False)
    print(f"[done] 実行時間={elapsed:.1f}秒 rows={len(records_df)} -> {out_path}", flush=True)

    if not args.smoke:
        return 0

    # --smoke: ユニット的検証を自動実行
    print("\n[selfcheck 1] A1(陣別α半減) vs A2(LORD++) 方向性（p=0.20,lambda=10,2030時点）", flush=True)
    ok1, msg1 = selfcheck_a1_vs_a2_direction(summary_df)
    print(msg1, flush=True)

    print("\n[selfcheck 2] p_true=0（全偽）世界での経験的偽発見率 vs 理論値mean(alpha)", flush=True)
    ok2, msg2 = selfcheck_null_alpha_consistency(spec, arms, sim_start_date, horizon_months, n_sim)
    print(msg2, flush=True)

    selfcheck_path = out_dir / "smoke_selfcheck.txt"
    selfcheck_path.write_text(
        f"selfcheck1_pass={ok1}\n{msg1}\n\nselfcheck2_pass={ok2}\n{msg2}\n", encoding="utf-8",
    )
    print(f"\n[selfcheck] 結果: check1={'PASS' if ok1 else 'FAIL'} check2={'PASS' if ok2 else 'FAIL'} "
          f"-> {selfcheck_path}", flush=True)

    if not (ok1 and ok2):
        print("[FATAL] ユニット的検証に失敗しました。ハーネス実装を確認してください。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
