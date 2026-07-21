#!/usr/bin/env python3
"""online-FDR切替起案v2 §6 工程2: 5-arm統計体系比較シミュレーション・ハーネス。

**凍結審査R1（Codex・threadId 019f84eb）= NO-GO（9ブロッカー+MINOR5）を全件修正した版
（2026-07-21・team-lead確定設計判断に基づく）。凍結（Codex R2 GO）まで本実行は禁止。**
台帳(data/kpi_trials/*)・config/paper_watchlist.json・カタログ(docs/stock-algo-kpi-catalog.md)は
一切読み書きしない（工程2指示: 台帳/config一切不干渉）。出力は output/fdr_sim/ のみ（gitignore済み）。

## 位置づけ・出典

- 起案書: docs/fdr-switch-proposal.md §6（行81-106）+ 冒頭R1レビュー要旨（行1-9）
- R1修正指示: team-lead確定のB1〜B9ブロッカー対応+MINOR5件（2026-07-21）
- σ/n_eff校正の実測根拠: output/kpi_maturity/report.md §3-4, output/kpi_maturity/maturity.csv
- SAFFRON非同期版の一次資料: Zrnic, Ramdas & Jordan 2018 "Asynchronous Online Testing of
  Multiple Hypotheses" (arXiv:1812.05068) Algorithm 1(LORD++)/Algorithm 3(SAFFRON, 定数λ)
  + §3(async conflict set の定義)。本ハーネスのAsyncLORDArm/AsyncSAFFRONArmは同論文の
  Algorithm 1 / Algorithm 3 に1:1対応するようdocstring内に対応表を明記している（B3/B4対応）。

## モデル概要

1. **候補到着**: 年あたりlambda_per_year本を月次一様（homogeneous Poisson過程、月次rate=lambda/12）で
   近似（B9 s3では2ヶ月バーストに変更）。各候補は独立に確率p_trueで「真のエッジあり」。
   真エッジ候補の月次EVはev_true_pct_choicesの離散一様。偽候補はEV_true=0固定
   （B9 s4では偽候補のp値をBeta(0.9,1)から直接生成=軽微な反保守）。
2. **判定の遅延と非同期性**: 各候補はjudgment_delay_months（既定12ヶ月固定・B9 s2では
   一様{9..15}ヶ月に変更）後に判定される。AsyncLORD/AsyncSAFFRONの非同期適格性フィルタ
   （Zrnic, Ramdas & Jordan 2018型）は固定/可変いずれのdelay分布にも対応する設計。
3. **検出力モデル**: 片側z検定をp値シミュレーションとして実装。z_effect = EV_true/(sigma/sqrt(n_eff))、
   観測統計量 Z ~ N(z_effect, 1)、片側p値 = 1-Φ(Z)（norm_sfで数値的に安定に計算・MINOR対応）。
   ある候補にalphaが配分されたとき reject ⟺ p_value <= alpha。
4. **5 arm**: 全てArm基底クラスのdecide(candidates, sim_start_date, batch_cutoff_month) -> Decision
   を実装。A4/A5はB5確定により判定期間全体の一発バッチ方式（年次再発行を廃止）。
5. **B9ストレスシナリオ**: (p_true=0.10, lambda=10)固定で5種の頑健性チェックを追加実行し、
   gate1(採用可否のFDR上限判定)の対象セルに含める（gate2は主12セルのみ）。

## B1-B9対応の要点（詳細は各クラス/関数のdocstring）

- B1: FDP = V/max(R,1) を全run単純平均。発見ゼロrunをNaN除外しない。SE列を追加。
- B2: gate1 = 対象セル全てで fdr_2030_mean + 2.6383*SE <= 0.10（片側同時信頼上限）。n_sim=5000。
- B3: AsyncSAFFRONArmはZrnic et al. 2018 Algorithm 3に1:1対応（docstring内対応表）。
- B4: AsyncLORDArm はRamdas et al. 2017 LORD++の閉形式γ系列+Algorithm 1に忠実化。
- B5: A4/A5は判定期間全体一発バッチ（年次再発行廃止）。2028は別途reference変種として出力。
- B6: gate2は連続式 challenger_mean-A1_mean >= max(0.5, 0.5*A1_mean)（フォールバック廃止）。
- B7: --decide モードでgate1/gate2/gate3を機械適用しdecision.jsonを出力。
- B8: spec._meta.statusの3値遷移(DRAFT→FROZEN_CANDIDATE→FROZEN)をチェック。
- B9: ストレスシナリオ5種をgate1対象に追加。
- MINOR: p値をerfcで直接計算/γ系列のtailを正しく計算/A1 cohort_size・A4 n_families感度/
  n_eff_mode間のペア比較のためscenario seedをmode非依存化。

Usage:
    python3 scripts/fdr_regime_sim.py --spec config/fdr_sim_spec.draft.json --smoke
    python3 scripts/fdr_regime_sim.py --spec config/fdr_sim_spec.draft.json          # 本実行（FROZEN後のみ）
    python3 scripts/fdr_regime_sim.py --decide --summary-csv output/fdr_sim/summary.csv
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPEC_PATH = REPO_ROOT / "config" / "fdr_sim_spec.draft.json"
DEFAULT_OUT_DIR = REPO_ROOT / "output" / "fdr_sim"

# グレゴリオ暦の平均月長（365.2425日/12ヶ月）。月数⇔日数変換に一貫使用
# （scripts/kpi_maturity_power.py の AVG_DAYS_PER_MONTH と同一の慣行に統一）。
AVG_DAYS_PER_MONTH = 365.2425 / 12

N_EFF_MODE_ORDER = {"conservative": 0, "optimistic": 1}
P_VALUE_EPS = 1e-15  # p値の絶対0/1境界に対する数値ガード（安全弁。通常はnorm_sfで精度確保済み）

# SeedSequenceのentropyは整数のみ受理するため、run_grid(pl_index使用)と衝突しない専用の
# 名前空間定数を使う（文字列は不可・ValueError: unrecognized seed string になる）。
STRESS_SEED_NS = 900_001
NULLCHECK_SEED_NS = 900_002

# B2確定: gate1の片側同時信頼上限のBonferroni臨界値 z_{0.05/12}（12セル同時・事前固定）。
GATE1_Z = 2.6383
GATE1_FDR_TARGET = 0.10
# B6確定: gate2連続式の定数
GATE2_WIN_ABS_MARGIN = 0.5
GATE2_WIN_REL_MARGIN = 0.5
GATE2_NOREGRET_REL = 0.9
GATE2_NOREGRET_ABS = 0.05
GATE2_WIN_THRESHOLD = 8  # 12中8以上
GATE_LABEL_FINAL = "2030"

BASELINE_ARM = "a1_fwer_cohort_halving"
CANDIDATE_ARMS = ["a2_lord_async", "a3_saffron_async", "a4_family_hierarchical", "a5_fixed_batch_bh"]
# gate3事前宣言の複雑度順位（spec.acceptance_criteria.gate3_selection と同一）
COMPLEXITY_ORDER = ["a5_fixed_batch_bh", "a2_lord_async", "a3_saffron_async", "a4_family_hierarchical"]


# ============================================================================
# 基礎関数（正規分布・日付変換）
# ============================================================================

def norm_sf(x: float) -> float:
    """標準正規分布の上側確率 1-Φ(x) を erfc で直接計算する（MINOR対応: 1-norm_cdf(x) は
    x が大きい右裾で桁落ちしうるため、数学的に同一かつ数値的に安定な erfc 表現に統一）。"""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def date_to_month_index(sim_start: date, d: date) -> float:
    return (d - sim_start).days / AVG_DAYS_PER_MONTH


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
    family_raw: int  # n_families非依存の一様ラベル（A4のfamily=family_raw % n_familiesで決定）
    z_stat: float
    p_value: float


@dataclass(frozen=True)
class StressConfig:
    """B9確定: 5ストレス変種のオーバーライド（既定値=オフ=通常挙動と同一）。"""
    rho: float = 0.0                                        # s1: 月内共通因子相関
    delay_uniform_range: Optional[Tuple[int, int]] = None   # s2: 判定遅延 ~ 一様{lo..hi}ヶ月
    burst_arrivals: bool = False                             # s3: 年間流入を2ヶ月に集中
    null_p_beta_params: Optional[Tuple[float, float]] = None  # s4: null p ~ Beta(a,b)
    ev_override_ev_pct: Optional[float] = None                # s5: 当該EVの候補は常に保守n_eff


STRESS_IDS = [
    "s1_common_factor", "s2_variable_delay", "s3_burst_arrivals",
    "s4_anticonservative_null", "s5_effect_freq_inverse",
]


def build_stress_configs(spec: dict) -> Dict[str, StressConfig]:
    """B9確定: spec['stress_scenarios']から5変種のStressConfigを構築する（数値パラメータは
    specを唯一の外部入力とする本ハーネスの方針に合わせ、ハードコードしない）。"""
    ss = spec["stress_scenarios"]
    ev_max = max(spec["effect_model"]["ev_true_pct_choices"])
    return {
        "s1_common_factor": StressConfig(rho=float(ss["s1_common_factor"]["rho"])),
        "s2_variable_delay": StressConfig(
            delay_uniform_range=tuple(ss["s2_variable_delay"]["delay_months_range"])),
        "s3_burst_arrivals": StressConfig(burst_arrivals=True),
        "s4_anticonservative_null": StressConfig(
            null_p_beta_params=(float(ss["s4_anticonservative_null"]["beta_a"]),
                                 float(ss["s4_anticonservative_null"]["beta_b"]))),
        "s5_effect_freq_inverse": StressConfig(ev_override_ev_pct=ev_max),
    }


def generate_arrivals(rng: np.random.Generator, lambda_per_year: float, horizon_months: int,
                       burst: bool = False) -> np.ndarray:
    """年あたりlambda_per_year本の候補到着を生成する。

    既定(burst=False): 月次一様(homogeneous Poisson過程・月次rate=lambda/12)。
    B9 s3(burst=True): 年間流入を2ヶ月(各12ヶ月ブロックの1ヶ月目と7ヶ月目)に集中させる
    （burst_rate=lambda/2ずつ・年間期待到着数はburst=Falseと同一のlambda_per_yearを維持）。
    """
    if lambda_per_year <= 0:
        return np.array([], dtype=float)
    n_months = int(math.ceil(horizon_months))
    if not burst:
        monthly_rate = lambda_per_year / 12.0
        counts = rng.poisson(monthly_rate, size=n_months)
    else:
        counts = np.zeros(n_months, dtype=int)
        burst_rate = lambda_per_year / 2.0
        for month in range(n_months):
            if (month % 12) in (0, 6):
                counts[month] = rng.poisson(burst_rate)
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
    n_eff_conservative: Optional[float] = None,
    stress: Optional[StressConfig] = None,
) -> List[Candidate]:
    """候補集団を1本生成する。stress=Noneなら旧来のDRAFT挙動と等価（B9無効時の後方互換）。"""
    stress = stress or StressConfig()
    reg_months = generate_arrivals(rng, lambda_per_year, horizon_months, burst=stress.burst_arrivals)
    candidates: List[Candidate] = []
    common_shock: Dict[int, float] = {}  # s1: 月バケットごとの共有ショック Z_common ~ N(0,1)

    for i, rt in enumerate(reg_months):
        is_true = bool(rng.random() < p_true)
        ev_pct = float(rng.choice(ev_true_pct_choices)) if is_true else 0.0
        # family_rawはn_families非依存の一様ラベル(0..99999)。A4のfamily割当は
        # family_raw % n_familiesで決める(=n_families感度を候補再生成なしで評価するため)。
        family_raw = int(rng.integers(0, 100_000))

        # s5: 効果量×頻度の逆相関 — 指定EVの候補はn_eff_modeに関わらず保守n_effを使用
        eff_n_eff = n_eff
        if (stress.ev_override_ev_pct is not None and is_true
                and n_eff_conservative is not None
                and math.isclose(ev_pct, stress.ev_override_ev_pct)):
            eff_n_eff = n_eff_conservative
        z_effect = (ev_pct / 100.0) * math.sqrt(eff_n_eff) / sigma if is_true else 0.0

        # s1: 月内共通因子相関(rho)。eps=sqrt(rho)*Z_common+sqrt(1-rho)*Z_indiv は
        # 分散1を保つため、null候補の周辺p値分布はUniform(0,1)のまま(月内相関のみ導入)。
        if stress.rho > 0.0:
            month_bucket = int(math.floor(rt))
            if month_bucket not in common_shock:
                common_shock[month_bucket] = float(rng.normal())
            eps = (math.sqrt(stress.rho) * common_shock[month_bucket]
                   + math.sqrt(1.0 - stress.rho) * float(rng.normal()))
        else:
            eps = float(rng.normal())
        z_stat = float(z_effect + eps)

        # s2: 判定遅延 ~ 一様{lo..hi}ヶ月（順序逆転を許容）。既定は固定delay。
        if stress.delay_uniform_range is not None:
            lo, hi = stress.delay_uniform_range
            delay_i = float(rng.integers(lo, hi + 1))
        else:
            delay_i = judgment_delay_months

        # s4: null p値 ~ Beta(a,b)（軽微な反保守）。真エッジ候補は通常どおりnorm_sfで生成。
        if (not is_true) and stress.null_p_beta_params is not None:
            beta_a, beta_b = stress.null_p_beta_params
            p_value = float(rng.beta(beta_a, beta_b))
        else:
            p_value = norm_sf(z_stat)
        p_value = min(max(p_value, P_VALUE_EPS), 1.0 - P_VALUE_EPS)

        candidates.append(Candidate(
            cid=i, register_month=float(rt), decide_month=float(rt) + delay_i,
            is_true=is_true, ev_true_pct=ev_pct, family_raw=family_raw,
            z_stat=z_stat, p_value=p_value,
        ))
    return candidates


# ============================================================================
# gamma系列（B4確定: LORD++の閉形式・LORD/SAFFRON共通で使用）
# ============================================================================

def _lord_pp_gamma_unnormalized(j: np.ndarray) -> np.ndarray:
    """B4確定式(Ramdas et al. 2017 LORD++の閉形式・正規化前):
    gamma_j ∝ log(max(j,2)) / (j * exp(sqrt(log(max(j,2))))) 。
    j=1でlog(j)=0となるため max(j,2) で下駄を履かせる（team-lead確定式どおり）。"""
    j = np.asarray(j, dtype=float)
    j2 = np.maximum(j, 2.0)
    log_term = np.log(j2)
    denom = j * np.exp(np.sqrt(log_term))
    return log_term / denom


class LordPlusPlusGammaSeries:
    """B4確定: Ramdas et al. 2017 LORD++の閉形式gamma系列。旧DRAFT実装の多項式減衰(poly_decay)
    近似を廃止し、この閉形式に一本化(A2/A3で共通使用・A3固有のgamma系列は原論文でも特に
    指定がないため、B4で確定した同一のgamma系列を流用する設計判断)。

    正規化定数Zは大きな直接和(N_NORM項)で数値的に近似する（この系列は解析的な総和公式を
    持たないため）。__call__は任意のjに対し閉形式で値を返すため、旧実装にあった
    「n_max超のjで最後の値を据え置く」バグ(MINOR対応)を持たない。
    """

    N_NORM = 200_000

    def __init__(self) -> None:
        j = np.arange(1, self.N_NORM + 1, dtype=float)
        self._z = float(np.sum(_lord_pp_gamma_unnormalized(j)))

    def __call__(self, j: int) -> float:
        if j <= 0:
            return 0.0
        return float(_lord_pp_gamma_unnormalized(np.array([float(j)]))[0]) / self._z


# ============================================================================
# Arm基底クラス・5 arm実装
# ============================================================================

class Decision(NamedTuple):
    reject: Dict[int, bool]
    alpha: Dict[int, float]


class Arm:
    name: str = "base"

    def decide(self, candidates: List[Candidate], sim_start_date: date,
               batch_cutoff_month: Optional[float] = None) -> Decision:
        raise NotImplementedError


class FWERCohortHalvingArm(Arm):
    """A1: 現行FWER 陣別α半減（forward-only継続）。オンライン逐次armのためbatch_cutoff_monthは無視。"""

    name = "a1_fwer_cohort_halving"

    def __init__(self, total_budget: float, start_cohort_index: int, cohort_size: int):
        self.total_budget = total_budget
        self.start_cohort_index = start_cohort_index
        self.cohort_size = max(1, int(cohort_size))

    def decide(self, candidates: List[Candidate], sim_start_date: date,
               batch_cutoff_month: Optional[float] = None) -> Decision:
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
    """A2: LORD++（非同期・B4確定: Ramdas et al. 2017 Algorithm 1 + Zrnic, Ramdas & Jordan 2018
    の非同期conflict set X_t^async={i∈[t-1]:E_i>=t}を代入した忠実実装）。

    対応表(Algorithm 1・原論文 p.8 / async instantiation p.11):
      入力 W0, α(=q)         <-> self.W0, self.q
      γ_j (非負非増加・Σγ=1) <-> self.gamma = LordPlusPlusGammaSeries()（B4確定閉形式）
      t (t番目のテスト)      <-> k_i = pos+1（このarm内での登録順位・1-indexed）
      τ_j (=r_j, eq.4)       <-> resolved_at[j] = bisect_right(reg_months, decide_month_j)
                                 （「jの判定が登録カウント何番目の時点で可視化されるか」を
                                 global registration timelineへのbisectで求める。async conflict
                                 set の非conflict化時刻に相当。r_1<r_2<...はreward_taus.sort()で
                                 保証（固定delayでは登録順=可視化順だがs2可変delayでは崩れるため
                                 明示的にソートする必要がある）。
      α_{t+1}=γ_{t+1}W0+γ_{t+1-r1}(α-W0)+Σ_{j≥2}γ_{t+1-rj}α
                             <-> alpha[c.cid] = gamma(k_i)*W0
                                   + (reward_tausが空でなければ) gamma(k_i-tau1)*(q-W0)
                                   + Σ_{tau_j (j>=2)} gamma(k_i-tau_j)*q
      α_1=γ_1W0              <-> pos=0では reward_taus=[] のため a=gamma(1)*W0 に自動的に一致

    非同期性(R1ブロッカー①対応)は「候補iのalpha_iは、iの登録時点S_iまでに判定が確定した
    (decide_month<=S_i)候補のみを発見カウント対象とする」適格性フィルタで表現する
    （固定delay/s2可変delayいずれにも対応: decide_monthを直接比較するため delay分布に依らない）。
    """

    name = "a2_lord_async"

    def __init__(self, W0: float, q: float):
        self.W0 = W0
        self.q = q
        self.gamma = LordPlusPlusGammaSeries()

    def decide(self, candidates: List[Candidate], sim_start_date: date,
               batch_cutoff_month: Optional[float] = None) -> Decision:
        ordered = sorted(candidates, key=lambda c: c.register_month)
        reg_months = [c.register_month for c in ordered]
        resolved_at = [bisect.bisect_right(reg_months, c.decide_month) for c in ordered]

        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        for pos, c in enumerate(ordered):
            k_i = pos + 1
            reward_taus: List[int] = []
            for j in range(pos):
                if ordered[j].decide_month <= c.register_month and reject[ordered[j].cid]:
                    reward_taus.append(resolved_at[j])
            reward_taus.sort()

            a = self.gamma(k_i) * self.W0
            if reward_taus:
                tau1 = reward_taus[0]
                a += (self.q - self.W0) * self.gamma(k_i - tau1)
                for tau_j in reward_taus[1:]:
                    a += self.q * self.gamma(k_i - tau_j)
            alpha[c.cid] = a
            reject[c.cid] = c.p_value <= a
        return Decision(reject, alpha)


class AsyncSAFFRONArm(Arm):
    """A3: SAFFRON async（B3確定: Zrnic, Ramdas & Jordan 2018 Algorithm 3「SAFFRON* for
    constant λ under general conflict sets」に、非同期conflict set X_t^async を代入した実装）。
    一次資料: arXiv:1812.05068 p.9 Algorithm 3（画像で数式を直接確認・幻覚防止のため一次資料を取得済み）。

    対応表:
      input α(=q), γ_j, λ, W0        <-> self.q, self.gamma(=B4と共通のLordPlusPlusGammaSeries
                                          ・SAFFRON固有のγ系列は原論文でも特に指定がないため
                                          B4確定のLORD++閉形式を流用する設計判断), self.lambda_s, self.W0
      C_i (候補指標 1{P_i<=λ})        <-> is_candidate[j] = (p_value_j <= lambda_s)
                                          （alpha配分と独立に事前確定可能・既存DRAFTと同じ理由）
      τ_j (=r_j, eq.4)                <-> resolved_at[j]（AsyncLORDArmと同一の定義・同一のresolved_at）
      C_{j+}=Σ_{i=r_j+1}^{t} C_i・1{i∉X_t}
                                       <-> prefix累積和を用いた c_plus(r_j) = prefix[pos]-prefix[clamp(r_j)]
                                          （prefix[m]=先頭m件のcandidate∩eligible数。本実装のeligible
                                          判定はcandidate自身[pos]を含まない設計のため、論文の総和上限
                                          "i=t"が理論上自分自身を含みうる境界ケースは、非同期(delay>0)
                                          設定ではE_t>tで自分自身の判定は未確定=eligibleになり得ない、
                                          という一貫した解釈で自然に解消される。r_0:=0の慣行どおり
                                          c_plus(0)=prefix[pos]（先頭posの全candidate∩eligible数）とする）
      α_1=(1-λ)γ_1W0                  <-> pos=0では reward_taus=[] のため
                                          a_raw=(1-λ)*W0*gamma(1-c_plus(0))=(1-λ)*W0*gamma(1) に一致
      α_{t+1}=min{λ,(1-λ)[W0γ_{t+1-C0+}+(α-W0)γ_{t+1-r1-C1+}+Σ_{j≥2}αγ_{t+1-rj-Cj+}]}
                                       <-> a = min(lambda_s, (1-lambda_s)*term) （termの構成はコード参照）
    """

    name = "a3_saffron_async"

    def __init__(self, W0: float, q: float, lambda_s: float):
        self.W0 = W0
        self.q = q
        self.lambda_s = lambda_s
        self.gamma = LordPlusPlusGammaSeries()

    def decide(self, candidates: List[Candidate], sim_start_date: date,
               batch_cutoff_month: Optional[float] = None) -> Decision:
        ordered = sorted(candidates, key=lambda c: c.register_month)
        reg_months = [c.register_month for c in ordered]
        is_candidate = [c.p_value <= self.lambda_s for c in ordered]
        resolved_at = [bisect.bisect_right(reg_months, c.decide_month) for c in ordered]

        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        for pos, c in enumerate(ordered):
            k_i = pos + 1
            eligible = [ordered[j].decide_month <= c.register_month for j in range(pos)]
            prefix = [0] * (pos + 1)
            for j in range(pos):
                prefix[j + 1] = prefix[j] + (1 if (is_candidate[j] and eligible[j]) else 0)

            reward_taus: List[int] = []
            for j in range(pos):
                if eligible[j] and reject[ordered[j].cid]:
                    reward_taus.append(resolved_at[j])
            reward_taus.sort()

            def c_plus(r_j: int) -> int:
                lo = max(0, min(r_j, pos))
                return prefix[pos] - prefix[lo]

            term = self.W0 * self.gamma(k_i - c_plus(0))
            if reward_taus:
                tau1 = reward_taus[0]
                term += (self.q - self.W0) * self.gamma(k_i - tau1 - c_plus(tau1))
                for tau_j in reward_taus[1:]:
                    term += self.q * self.gamma(k_i - tau_j - c_plus(tau_j))
            a_raw = (1.0 - self.lambda_s) * term
            a = min(self.lambda_s, a_raw)
            alpha[c.cid] = a
            reject[c.cid] = c.p_value <= a
        return Decision(reject, alpha)


def _bh_apply(group: List[Candidate], q: float, reject: Dict[int, bool], alpha: Dict[int, float]) -> None:
    """Benjamini-Hochberg(1995) 手続きを1バッチに適用しreject/alpha辞書へ書き込む（A4/A5共通の
    canonical実装。旧DRAFTはA4/A5で同一ロジックを重複実装していたため本関数に統一）。"""
    m = len(group)
    if m == 0:
        return
    order = sorted(range(m), key=lambda i: group[i].p_value)
    p_sorted = [group[i].p_value for i in order]
    k_max = 0
    for k in range(m, 0, -1):
        if p_sorted[k - 1] <= (k / m) * q:
            k_max = k
            break
    cutoff = p_sorted[k_max - 1] if k_max > 0 else 0.0
    for i in order:
        c = group[i]
        alpha[c.cid] = cutoff
        reject[c.cid] = (k_max > 0) and (c.p_value <= cutoff)


class FamilyHierarchicalBHArm(Arm):
    """A4: family階層（family間Bonferroni×family内BH）。

    B5確定: 判定期間全体をfamily単位の一発バッチとし、batch_cutoff_monthまでに判定が確定した
    候補についてfamily内BH(alpha_family)を1回だけ適用する（年次再発行を廃止。旧DRAFTの
    (family,判定年)単位の年次再発行は--smoke自己検証でFWERを名目の約3倍に膨張させることが
    判明していたため、B5で一発バッチへ統一）。batch_cutoff_month=Noneなら候補全件を1バッチとする。
    family割当はcandidateのfamily_raw(生成時に固定・n_families非依存)をself.n_familiesで割った
    剰余で決める（n_families感度{2,4}を候補再生成なしでペア比較評価するための設計）。
    """

    name = "a4_family_hierarchical"

    def __init__(self, n_families: int, total_budget: float):
        self.n_families = n_families
        self.alpha_family = total_budget / n_families

    def decide(self, candidates: List[Candidate], sim_start_date: date,
               batch_cutoff_month: Optional[float] = None) -> Decision:
        by_family: Dict[int, List[Candidate]] = defaultdict(list)
        for c in candidates:
            if batch_cutoff_month is not None and c.decide_month > batch_cutoff_month:
                continue
            by_family[c.family_raw % self.n_families].append(c)
        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        for group in by_family.values():
            _bh_apply(group, self.alpha_family, reject, alpha)
        return Decision(reject, alpha)


class FixedBatchBHArm(Arm):
    """A5: 固定バッチBH（B5確定: 判定期間全体の一発バッチ・年次再発行を廃止）。
    batch_cutoff_monthまでに判定が確定した候補全体でBH(q)を1回だけ適用する。
    batch_cutoff_month=Noneなら候補全件を1バッチとする。
    """

    name = "a5_fixed_batch_bh"

    def __init__(self, q: float):
        self.q = q

    def decide(self, candidates: List[Candidate], sim_start_date: date,
               batch_cutoff_month: Optional[float] = None) -> Decision:
        group = [c for c in candidates
                 if batch_cutoff_month is None or c.decide_month <= batch_cutoff_month]
        reject: Dict[int, bool] = {}
        alpha: Dict[int, float] = {}
        _bh_apply(group, self.q, reject, alpha)
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
    factories: Dict[str, Callable[[], Arm]] = {
        "a1_fwer_cohort_halving": lambda: FWERCohortHalvingArm(
            total_budget=a["a1_fwer_cohort_halving"]["total_budget"],
            start_cohort_index=a["a1_fwer_cohort_halving"]["start_cohort_index"],
            cohort_size=a["a1_fwer_cohort_halving"]["cohort_size"],
        ),
        "a2_lord_async": lambda: AsyncLORDArm(
            W0=a["a2_lord_async"]["W0"], q=a["a2_lord_async"]["q"],
        ),
        "a3_saffron_async": lambda: AsyncSAFFRONArm(
            W0=a["a3_saffron_async"]["W0"], q=a["a3_saffron_async"]["q"],
            lambda_s=a["a3_saffron_async"]["lambda_s"],
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
                     metric_cutoffs: Dict[str, float],
                     labels: Optional[List[str]] = None) -> dict:
    """B1確定: FDP = V/max(R,1) を発見ゼロrunも含め計算する（NaN除外なし）。
    labelsを指定すると、そのラベルのみ実計算し他はNaN（A4/A5の一発バッチ変種で、
    batch_cutoff_monthの対象外ラベルを『未計算』と明示するために使用・B5対応）。
    """
    reject = decision.reject
    all_labels = list(metric_cutoffs.keys())
    active_labels = labels if labels is not None else all_labels
    rec: dict = {}
    for label in all_labels:
        if label not in active_labels:
            rec[f"true_disc_{label}"] = float("nan")
            rec[f"false_disc_{label}"] = float("nan")
            rec[f"total_disc_{label}"] = float("nan")
            rec[f"fdr_{label}"] = float("nan")
            rec[f"fwer_flag_{label}"] = float("nan")
            continue
        cutoff = metric_cutoffs[label]
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
        rec[f"fdr_{label}"] = false_d / max(total, 1)  # B1確定: FDP=V/max(R,1)・NaN除外廃止
        rec[f"fwer_flag_{label}"] = 1 if false_d >= 1 else 0
    disc_months = [c.decide_month for c in candidates if reject.get(c.cid, False)]
    rec["first_discovery_month"] = min(disc_months) if disc_months else float("nan")
    rec["n_candidates_total"] = len(candidates)
    return rec


# ============================================================================
# シミュレーション本体
# ============================================================================

GROUP_COLS = ["p_true", "lambda_per_year", "n_eff_mode", "arm", "scenario_kind", "stress_id", "variant"]


def make_scenario_list(spec: dict) -> List[Tuple[float, float, str]]:
    p_list = sorted(spec["grid"]["p_true"])
    lam_list = sorted(spec["grid"]["lambda_per_year"])
    mode_list = sorted(spec["grid"]["n_eff_mode"], key=lambda m: N_EFF_MODE_ORDER.get(m, 99))
    return list(itertools.product(p_list, lam_list, mode_list))


def make_pl_index(spec: dict) -> Dict[Tuple[float, float], int]:
    """MINOR確定: n_eff_mode間でpaired比較になるよう、rngシードを(p_true,lambda)のみで
    決める（modeを含めない）ためのインデックス。同じ(p_true,lambda,run_id)なら
    conservative/optimisticで同一の到着時刻・is_true・EV・family_raw・独立ノイズepsを
    共有し、z_effect(n_eff/sigma依存)のみがmode間で異なる、という真にpairedな設計になる。"""
    p_list = sorted(spec["grid"]["p_true"])
    lam_list = sorted(spec["grid"]["lambda_per_year"])
    pairs = list(itertools.product(p_list, lam_list))
    return {pl: idx for idx, pl in enumerate(pairs)}


def _label_final_early(metric_cutoffs: Dict[str, float]) -> Tuple[str, str]:
    if len(metric_cutoffs) != 2:
        raise NotImplementedError(
            "A4/A5の一発バッチ+2028reference設計はmetric_datesがちょうど2件であることを前提とする"
            f"（現在{len(metric_cutoffs)}件）。metric_dates変更時はB5関連ロジックの見直しが必要。"
        )
    label_final = max(metric_cutoffs, key=lambda k: metric_cutoffs[k])
    label_early = min(metric_cutoffs, key=lambda k: metric_cutoffs[k])
    return label_final, label_early


def run_grid(spec: dict, arms: Dict[str, Arm], n_sim: int, sim_start_date: date,
             metric_cutoffs: Dict[str, float], horizon_months: int) -> pd.DataFrame:
    """主グリッド(3×3×2=18シナリオ)を実行する。各(scenario,run)につき:
    - 5 arm本体(variant='main')
    - A1 cohort_size感度(spec['arms']['a1_fwer_cohort_halving']['cohort_size_sensitivity']・
      main以外の値。同一candidatesを再利用するpaired設計・MINOR対応)
    - A4 n_families感度(spec['arms']['a4_family_hierarchical']['n_families_sensitivity']・
      同一candidatesをfamily_raw%n_familiesで再割当するpaired設計)
    - A4/A5の2028一発バッチreference変種(B5確定・ゲート不使用・参考列)
    を記録する。
    """
    scenarios = make_scenario_list(spec)
    pl_index = make_pl_index(spec)
    delay = spec["judgment_delay_months"]
    ev_choices = spec["effect_model"]["ev_true_pct_choices"]
    n_eff_conservative = float(spec["calibration"]["n_eff_conservative"])
    label_final, label_early = _label_final_early(metric_cutoffs)
    cutoff_final = metric_cutoffs[label_final]
    cutoff_early = metric_cutoffs[label_early]

    a1_spec = spec["arms"]["a1_fwer_cohort_halving"]
    a4_spec = spec["arms"]["a4_family_hierarchical"]
    main_cohort_size = a1_spec["cohort_size"]
    main_n_families = a4_spec["n_families"]
    cohort_sizes_sensitivity = [cs for cs in a1_spec.get("cohort_size_sensitivity", [])
                                 if cs != main_cohort_size]
    n_families_sensitivity = [nf for nf in a4_spec.get("n_families_sensitivity", [])
                               if nf != main_n_families]

    sensitivity_a1_arms = [
        (FWERCohortHalvingArm(a1_spec["total_budget"], a1_spec["start_cohort_index"], cs),
         f"a1_cohort{cs}")
        for cs in cohort_sizes_sensitivity
    ]
    sensitivity_a4_arms = [
        (FamilyHierarchicalBHArm(nf, a4_spec["total_budget"]), f"a4_nfam{nf}")
        for nf in n_families_sensitivity
    ]

    records: List[dict] = []
    for (p_true, lam, mode) in scenarios:
        pl_idx = pl_index[(p_true, lam)]
        n_eff, sigma = resolve_calibration(spec, mode)
        for run_id in range(n_sim):
            rng = np.random.default_rng(np.random.SeedSequence([spec["seed"], pl_idx, run_id]))
            candidates = build_candidates(
                rng, lam, horizon_months, p_true, ev_choices, n_eff, sigma, delay,
                n_eff_conservative=n_eff_conservative,
            )
            base_tags = dict(p_true=p_true, lambda_per_year=lam, n_eff_mode=mode,
                              scenario_kind="grid", stress_id="")

            for arm_key, arm in arms.items():
                if arm_key in ("a4_family_hierarchical", "a5_fixed_batch_bh"):
                    decision = arm.decide(candidates, sim_start_date, batch_cutoff_month=cutoff_final)
                    rec = compute_metrics(candidates, decision, metric_cutoffs, labels=[label_final])
                else:
                    decision = arm.decide(candidates, sim_start_date)
                    rec = compute_metrics(candidates, decision, metric_cutoffs)
                rec.update(base_tags, arm=arm_key, variant="main")
                records.append(rec)

            for cohort_arm, variant_tag in sensitivity_a1_arms:
                decision = cohort_arm.decide(candidates, sim_start_date)
                rec = compute_metrics(candidates, decision, metric_cutoffs)
                rec.update(base_tags, arm="a1_fwer_cohort_halving", variant=variant_tag)
                records.append(rec)

            for nfam_arm, variant_tag in sensitivity_a4_arms:
                decision = nfam_arm.decide(candidates, sim_start_date, batch_cutoff_month=cutoff_final)
                rec = compute_metrics(candidates, decision, metric_cutoffs, labels=[label_final])
                rec.update(base_tags, arm="a4_family_hierarchical", variant=variant_tag)
                records.append(rec)

            # B5確定: A4/A5の「2028までの判定分への一発BH」reference（ゲート不使用・参考列）
            for arm_key in ("a4_family_hierarchical", "a5_fixed_batch_bh"):
                decision = arms[arm_key].decide(candidates, sim_start_date, batch_cutoff_month=cutoff_early)
                rec = compute_metrics(candidates, decision, metric_cutoffs, labels=[label_early])
                short = arm_key.split("_")[0]
                rec.update(base_tags, arm=arm_key, variant=f"{short}_ref{label_early}")
                records.append(rec)

    return pd.DataFrame(records)


def run_stress_grid(spec: dict, arms: Dict[str, Arm], n_sim: int, sim_start_date: date,
                     metric_cutoffs: Dict[str, float], horizon_months: int) -> pd.DataFrame:
    """B9確定: (p_true=0.10, lambda_per_year=10)固定で5ストレス変種×両n_eff_modeを実行し、
    5 arm本体(variant='main', scenario_kind='stress')を記録する（gate1対象・gate2は主12セルのみ）。
    """
    ss = spec["stress_scenarios"]
    p_true = float(ss["fixed_p_true"])
    lam = float(ss["fixed_lambda_per_year"])
    delay = spec["judgment_delay_months"]
    ev_choices = spec["effect_model"]["ev_true_pct_choices"]
    n_eff_conservative = float(spec["calibration"]["n_eff_conservative"])
    mode_list = sorted(spec["grid"]["n_eff_mode"], key=lambda m: N_EFF_MODE_ORDER.get(m, 99))
    label_final, _ = _label_final_early(metric_cutoffs)
    cutoff_final = metric_cutoffs[label_final]
    stress_configs = build_stress_configs(spec)

    records: List[dict] = []
    for s_idx, stress_id in enumerate(STRESS_IDS):
        stress = stress_configs[stress_id]
        for mode in mode_list:
            n_eff, sigma = resolve_calibration(spec, mode)
            for run_id in range(n_sim):
                # MINOR確定と同じ思想: n_eff_mode間でpairedになるようmodeをシードに含めない
                # （SeedSequenceの entropy は整数のみ受理するため、専用の名前空間定数を使う）
                rng = np.random.default_rng(
                    np.random.SeedSequence([spec["seed"], STRESS_SEED_NS, s_idx, run_id]))
                candidates = build_candidates(
                    rng, lam, horizon_months, p_true, ev_choices, n_eff, sigma, delay,
                    n_eff_conservative=n_eff_conservative, stress=stress,
                )
                base_tags = dict(p_true=p_true, lambda_per_year=lam, n_eff_mode=mode,
                                  scenario_kind="stress", stress_id=stress_id)
                for arm_key, arm in arms.items():
                    if arm_key in ("a4_family_hierarchical", "a5_fixed_batch_bh"):
                        decision = arm.decide(candidates, sim_start_date, batch_cutoff_month=cutoff_final)
                        rec = compute_metrics(candidates, decision, metric_cutoffs, labels=[label_final])
                    else:
                        decision = arm.decide(candidates, sim_start_date)
                        rec = compute_metrics(candidates, decision, metric_cutoffs)
                    rec.update(base_tags, arm=arm_key, variant="main")
                    records.append(rec)
    return pd.DataFrame(records)


def run_null_world_grid(spec: dict, arms: Dict[str, Arm], n_sim: int, sim_start_date: date,
                         metric_cutoffs: Dict[str, float], horizon_months: int) -> pd.DataFrame:
    """selfcheck③(B1)専用: p_true=0(全偽)世界を標準パイプラインで実行する（低n_simで十分）。"""
    lam = 10.0
    delay = spec["judgment_delay_months"]
    ev_choices = spec["effect_model"]["ev_true_pct_choices"]
    n_eff_conservative = float(spec["calibration"]["n_eff_conservative"])
    mode_list = sorted(spec["grid"]["n_eff_mode"], key=lambda m: N_EFF_MODE_ORDER.get(m, 99))
    label_final, _ = _label_final_early(metric_cutoffs)
    cutoff_final = metric_cutoffs[label_final]

    records: List[dict] = []
    for mode in mode_list:
        n_eff, sigma = resolve_calibration(spec, mode)
        for run_id in range(n_sim):
            rng = np.random.default_rng(
                np.random.SeedSequence([spec["seed"], NULLCHECK_SEED_NS, N_EFF_MODE_ORDER[mode], run_id]))
            candidates = build_candidates(
                rng, lam, horizon_months, 0.0, ev_choices, n_eff, sigma, delay,
                n_eff_conservative=n_eff_conservative,
            )
            for arm_key, arm in arms.items():
                if arm_key in ("a4_family_hierarchical", "a5_fixed_batch_bh"):
                    decision = arm.decide(candidates, sim_start_date, batch_cutoff_month=cutoff_final)
                else:
                    decision = arm.decide(candidates, sim_start_date)
                rec = compute_metrics(candidates, decision, metric_cutoffs, labels=[label_final])
                rec.update(p_true=0.0, lambda_per_year=lam, n_eff_mode=mode, arm=arm_key,
                           scenario_kind="nullcheck", stress_id="", variant="main")
                records.append(rec)
    return pd.DataFrame(records)


def aggregate(records_df: pd.DataFrame, metric_labels: List[str],
              group_cols: Optional[List[str]] = None) -> pd.DataFrame:
    """B1確定: fdr_{label}_mean は全run単純平均(NaN除外なし)。SE列を追加。
    fdr_{label}_n_valid_runsによるセル除外は廃止し、発見あり run数は診断列(n_disc_runs)として残す。
    """
    group_cols = group_cols or GROUP_COLS
    rows: List[dict] = []
    for keys, g in records_df.groupby(group_cols, dropna=False):
        row = dict(zip(group_cols, keys))
        row["n_sim"] = len(g)
        for label in metric_labels:
            fdr_col = g[f"fdr_{label}"].dropna()
            n_runs = len(fdr_col)
            row[f"true_disc_{label}_mean"] = g[f"true_disc_{label}"].mean()
            row[f"false_disc_{label}_mean"] = g[f"false_disc_{label}"].mean()
            row[f"total_disc_{label}_mean"] = g[f"total_disc_{label}"].mean()
            row[f"fdr_{label}_mean"] = fdr_col.mean() if n_runs > 0 else float("nan")
            row[f"fdr_{label}_se"] = (fdr_col.std(ddof=1) / math.sqrt(n_runs)) if n_runs > 1 else (
                0.0 if n_runs == 1 else float("nan"))
            row[f"fdr_{label}_n_runs"] = n_runs
            row[f"fdr_{label}_n_disc_runs"] = int((g[f"total_disc_{label}"].dropna() > 0).sum())
            row[f"fwer_{label}_mean"] = g[f"fwer_flag_{label}"].mean()
        disc_months = g["first_discovery_month"].dropna()
        row["first_discovery_month_mean"] = disc_months.mean() if len(disc_months) > 0 else float("nan")
        row["first_discovery_month_censored_frac"] = 1.0 - (len(disc_months) / len(g))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(group_cols).reset_index(drop=True)


# ============================================================================
# ユニット的検証（--smoke時に自動実行）
# ============================================================================

def selfcheck_a1_vs_a2_direction(main_summary_df: pd.DataFrame) -> Tuple[bool, str]:
    """検証①: 高流入シナリオ(p=0.20, lambda=10)で、2030時点の平均真発見数がA1<A2となる方向性を確認。
    main_summary_dfはvariant=='main' & scenario_kind=='grid'に絞り込み済みであること。
    """
    sub = main_summary_df[(main_summary_df["p_true"] == 0.20) & (main_summary_df["lambda_per_year"] == 10)]
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

    - A1/A2/A3（事前投資型）: p_true=0ではp_value~Uniform(0,1)が厳密に成り立ち、alpha_iはi自身の
      p_valueと独立なので tower propertyによりE[1(reject_i)]=E[alpha_i]が厳密に成立する。
    - A4/A5（一発バッチ型・B5確定でrun単位1回のみ適用）: BH(1995)の標準定理『独立p値・グローバル
      帰無の下で一発バッチBH(q)はP(1件以上棄却)<=q』を、A5=run全体1バッチ／A4=family(family_raw%
      n_families)単位のバッチとして直接検証する。
    """
    lam = 10.0
    n_eff, sigma = resolve_calibration(spec, "conservative")
    ev_choices = spec["effect_model"]["ev_true_pct_choices"]
    n_eff_conservative = float(spec["calibration"]["n_eff_conservative"])
    delay = spec["judgment_delay_months"]
    base_seed = spec["seed"] + 999_000_000

    alpha_identity_arms = {"a1_fwer_cohort_halving", "a2_lord_async", "a3_saffron_async"}
    identity_totals = {k: {"n_cand": 0, "n_false": 0, "sum_alpha": 0.0} for k in alpha_identity_arms}
    a4_arm = arms["a4_family_hierarchical"]
    batch_totals = {
        "a4_family_hierarchical": {"n_batches": 0, "n_batches_disc": 0,
                                    "q_level": a4_arm.alpha_family},
        "a5_fixed_batch_bh": {"n_batches": 0, "n_batches_disc": 0,
                               "q_level": spec["arms"]["a5_fixed_batch_bh"]["q"]},
    }

    for run_id in range(n_sim):
        rng = np.random.default_rng(np.random.SeedSequence([base_seed, run_id]))
        candidates = build_candidates(
            rng, lam, horizon_months, 0.0, ev_choices, n_eff, sigma, delay,
            n_eff_conservative=n_eff_conservative,
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
                t = batch_totals["a5_fixed_batch_bh"]
                t["n_batches"] += 1  # B5確定: run全体で1バッチ
                if any(decision.reject.get(c.cid, False) for c in candidates):
                    t["n_batches_disc"] += 1
            elif arm_key == "a4_family_hierarchical":
                by_family: Dict[int, List[Candidate]] = defaultdict(list)
                for c in candidates:
                    by_family[c.family_raw % arm.n_families].append(c)
                t = batch_totals["a4_family_hierarchical"]
                for group in by_family.values():
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


def selfcheck_b1_fdr_definition(null_df: pd.DataFrame, label_final: str) -> Tuple[bool, str]:
    """検証③(B1対応): p_true=0(全偽)世界では V=R が恒等的に成り立つため
    FDP=V/max(R,1)=1{R>0} となり、fdr_{label}_mean は経験的P(R>0)と厳密に一致するはず。
    標準パイプライン(compute_metrics/aggregate)の実装が正しく配線されていることの確認。
    """
    ok = True
    lines = []
    for arm_key in ARM_ORDER:
        for mode in sorted(null_df["n_eff_mode"].unique(), key=lambda m: N_EFF_MODE_ORDER.get(m, 99)):
            sub = null_df[(null_df["arm"] == arm_key) & (null_df["n_eff_mode"] == mode)]
            if sub.empty:
                continue
            fdr_mean = sub[f"fdr_{label_final}"].mean()
            p_r_gt0 = (sub[f"total_disc_{label_final}"] > 0).mean()
            diff = abs(fdr_mean - p_r_gt0)
            passed = diff < 1e-9
            ok = ok and passed
            lines.append(f"  [{arm_key}/{mode}] fdr_{label_final}_mean={fdr_mean:.6f} vs "
                          f"P(R>0)実測={p_r_gt0:.6f} (差={diff:.2e}) -> {'OK' if passed else 'NG'}")
    return ok, "\n".join(lines)


# ============================================================================
# B7確定: --decide（gate1/gate2/gate3の機械適用）
# ============================================================================

def decide_from_summary(summary_df: pd.DataFrame, label_final: str = GATE_LABEL_FINAL) -> dict:
    """summary_df(run_grid+run_stress_gridの結合結果)からgate1/gate2/gate3を機械適用する。

    gate1対象セル = variant=='main'かつ(scenario_kind=='grid'でlambda_per_year>0の12評価シナリオ
                    union scenario_kind=='stress'の10セル) = 22セル。B2確定のz=2.6383を
                    そのまま「対象セルすべて」に適用する(値の由来は12セルBonferroniだが、
                    B9でgate1対象セルが22に拡張された後もteam-lead確定の固定数値2.6383を
                    そのまま流用する設計判断・report内で明記)。
    gate2対象セル = variant=='main'かつscenario_kind=='grid'でlambda_per_year>0の12評価シナリオのみ。
    """
    fdr_col = f"fdr_{label_final}_mean"
    se_col = f"fdr_{label_final}_se"
    disc_col = f"true_disc_{label_final}_mean"

    main = summary_df[(summary_df["variant"] == "main") & (summary_df["scenario_kind"] == "grid")]
    evaluable = main[main["lambda_per_year"] > 0]
    stress = summary_df[(summary_df["variant"] == "main") & (summary_df["scenario_kind"] == "stress")]
    gate1_cells = pd.concat([evaluable, stress], ignore_index=True)

    result: dict = {
        "gate1_z": GATE1_Z, "gate1_fdr_target": GATE1_FDR_TARGET,
        "gate1_n_target_cells_declared": "evaluable(12) + stress(10) = 22（B9でgate1対象を拡張）",
        "arms": {},
    }

    for arm in CANDIDATE_ARMS:
        cells = gate1_cells[gate1_cells["arm"] == arm]
        gate1_pass = len(cells) > 0
        cell_results = []
        for _, row in cells.iterrows():
            fdr_mean = row.get(fdr_col)
            se = row.get(se_col)
            if pd.isna(fdr_mean) or pd.isna(se):
                cell_pass = False
                ucb = None
            else:
                ucb = float(fdr_mean) + GATE1_Z * float(se)
                cell_pass = ucb <= GATE1_FDR_TARGET
            gate1_pass = gate1_pass and cell_pass
            cell_results.append({
                "p_true": row["p_true"], "lambda_per_year": row["lambda_per_year"],
                "n_eff_mode": row["n_eff_mode"], "scenario_kind": row["scenario_kind"],
                "stress_id": row.get("stress_id", ""),
                "fdr_mean": None if pd.isna(fdr_mean) else float(fdr_mean),
                "se": None if pd.isna(se) else float(se),
                "ucb": ucb, "pass": bool(cell_pass),
            })

        arm_eval = evaluable[evaluable["arm"] == arm]
        base_eval = evaluable[evaluable["arm"] == BASELINE_ARM]
        merge_keys = ["p_true", "lambda_per_year", "n_eff_mode"]
        merged = arm_eval.merge(base_eval, on=merge_keys, suffixes=("_c", "_a1"))
        n_wins = 0
        no_regret_all = True
        scenario_results = []
        for _, row in merged.iterrows():
            c_mean = float(row[f"{disc_col}_c"])
            a1_mean = float(row[f"{disc_col}_a1"])
            win_margin = max(GATE2_WIN_ABS_MARGIN, GATE2_WIN_REL_MARGIN * a1_mean)
            is_win = (c_mean - a1_mean) >= win_margin
            no_regret = c_mean >= (GATE2_NOREGRET_REL * a1_mean - GATE2_NOREGRET_ABS)
            n_wins += int(is_win)
            no_regret_all = no_regret_all and no_regret
            scenario_results.append({
                "p_true": row["p_true"], "lambda_per_year": row["lambda_per_year"],
                "n_eff_mode": row["n_eff_mode"], "challenger_mean": c_mean, "a1_mean": a1_mean,
                "win": bool(is_win), "no_regret": bool(no_regret),
            })
        n_scenarios = len(merged)
        gate2_pass = (n_wins >= GATE2_WIN_THRESHOLD) and no_regret_all and (n_scenarios > 0)

        result["arms"][arm] = {
            "gate1_pass": bool(gate1_pass), "gate1_n_cells": len(cells), "gate1_cells": cell_results,
            "gate2_pass": bool(gate2_pass), "gate2_n_wins": n_wins, "gate2_n_scenarios": n_scenarios,
            "gate2_no_regret_all": bool(no_regret_all), "gate2_scenarios": scenario_results,
            "both_pass": bool(gate1_pass and gate2_pass),
        }

    passing = [a for a in CANDIDATE_ARMS if result["arms"][a]["both_pass"]]
    if not passing:
        final_arm = BASELINE_ARM
        selection_note = "gate1×gate2両方通過armゼロ→現行FWER(A1)維持"
    else:
        avg_map = {a: evaluable[evaluable["arm"] == a][disc_col].mean() for a in passing}
        max_avg = max(avg_map.values())
        near_max = [a for a in passing if avg_map[a] >= max_avg * 0.95]
        if len(near_max) == 1:
            final_arm = near_max[0]
            selection_note = "gate1×gate2通過arm中、12シナリオ平均true_disc_2030が最大"
        else:
            final_arm = min(near_max, key=lambda a: COMPLEXITY_ORDER.index(a))
            selection_note = f"5%以内タイ({near_max})→事前宣言の複雑度順位{COMPLEXITY_ORDER}で選択"

    result["final_arm"] = final_arm
    result["selection_note"] = selection_note
    result["passing_arms"] = passing
    return result


def run_decide(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = Path(args.summary_csv) if args.summary_csv else out_dir / "summary.csv"
    if not summary_path.exists():
        print(f"[FATAL] --decide: summary CSVが見つかりません: {summary_path}", file=sys.stderr)
        return 1
    summary_df = pd.read_csv(summary_path)
    result = decide_from_summary(summary_df, label_final=GATE_LABEL_FINAL)

    input_files = [summary_path]
    spec_p = Path(args.spec)
    if spec_p.exists():
        input_files.append(spec_p)
    result["input_files_sha256"] = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in input_files
    }
    result["generated_at"] = datetime.now(timezone.utc).isoformat()

    decision_path = out_dir / "decision.json"
    decision_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[decide] final_arm={result['final_arm']} passing_arms={result['passing_arms']} "
          f"-> {decision_path}", flush=True)
    return 0


# ============================================================================
# B8確定: spec._meta.status の3値遷移チェック
# ============================================================================

def resolve_run_mode(spec: dict, smoke: bool, n_sim_override: Optional[int]) -> Tuple[bool, str]:
    """B8確定: spec._meta.statusの3値遷移(DRAFT→FROZEN_CANDIDATE→FROZEN)をチェックする。
    FROZEN_CANDIDATE=smokeのみ許可・FROZEN=本実行のみ許可（かつ--n-sim上書きはFATAL）。
    """
    status = spec.get("_meta", {}).get("status", "")
    if status.startswith("FROZEN_CANDIDATE"):
        if not smoke:
            return False, "spec._meta.status=FROZEN_CANDIDATE は --smoke のみ許可（本実行は status=FROZEN 化後）。"
        return True, ""
    if status.startswith("FROZEN"):
        if smoke:
            return False, "spec._meta.status=FROZEN は本実行専用（--smoke は不可・凍結前のFROZEN_CANDIDATEで実施済みのはず）。"
        if n_sim_override is not None:
            return False, "spec._meta.status=FROZEN では --n-sim によるn_sim上書きは禁止（凍結値をそのまま使用すること）。"
        return True, ""
    return False, (f"spec._meta.status='{status}' は未対応。3値遷移(DRAFT→FROZEN_CANDIDATE→FROZEN)の"
                    "うちFROZEN_CANDIDATEまたはFROZENのみ本ハーネスで実行可能です。")


# ============================================================================
# main
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="online-FDR切替起案v2 §6 工程2: 5-arm統計体系比較シミュレーション・ハーネス",
    )
    ap.add_argument("--spec", default=str(DEFAULT_SPEC_PATH), help="spec jsonのパス")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--smoke", action="store_true",
                     help="smoke_n_sim（既定200程度）で全arm動作確認+ユニット的検証を実行")
    ap.add_argument("--n-sim", type=int, default=None, help="n_simを上書き（--smoke未指定時のみ有効・FROZEN時はFATAL）")
    ap.add_argument("--decide", action="store_true",
                     help="B7確定: --summary-csvからgate1/gate2/gate3を機械適用しdecision.jsonを出力（specのstatusゲート非依存）")
    ap.add_argument("--summary-csv", default=None, help="--decide時の入力summary CSVパス（既定: out-dir/summary.csv）")
    args = ap.parse_args()

    if args.decide:
        return run_decide(args)

    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    ok, err = resolve_run_mode(spec, args.smoke, args.n_sim)
    if not ok:
        print(f"[FATAL] {err}", file=sys.stderr)
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
    grid_df = run_grid(spec, arms, n_sim, sim_start_date, metric_cutoffs, horizon_months)
    stress_df = run_stress_grid(spec, arms, n_sim, sim_start_date, metric_cutoffs, horizon_months)
    records_df = pd.concat([grid_df, stress_df], ignore_index=True)
    summary_df = aggregate(records_df, metric_labels)
    elapsed = time.monotonic() - t0

    out_path = out_dir / out_name
    summary_df.to_csv(out_path, index=False)
    print(f"[done] 実行時間={elapsed:.1f}秒 rows={len(records_df)} -> {out_path}", flush=True)

    if not args.smoke:
        return 0

    # --smoke: ユニット的検証を自動実行
    main_summary_df = summary_df[(summary_df["variant"] == "main") & (summary_df["scenario_kind"] == "grid")]

    print("\n[selfcheck 1] A1(陣別α半減) vs A2(LORD++) 方向性（p=0.20,lambda=10,2030時点）", flush=True)
    ok1, msg1 = selfcheck_a1_vs_a2_direction(main_summary_df)
    print(msg1, flush=True)

    print("\n[selfcheck 2] p_true=0（全偽）世界での経験的偽発見率 vs 理論値mean(alpha)/BH定理", flush=True)
    ok2, msg2 = selfcheck_null_alpha_consistency(spec, arms, sim_start_date, horizon_months, n_sim)
    print(msg2, flush=True)

    print("\n[selfcheck 3] B1確定: p_true=0世界でfdr_2030_mean ≈ 経験的P(R>0)（標準パイプライン配線確認）", flush=True)
    label_final, _ = _label_final_early(metric_cutoffs)
    null_df = run_null_world_grid(spec, arms, n_sim, sim_start_date, metric_cutoffs, horizon_months)
    ok3, msg3 = selfcheck_b1_fdr_definition(null_df, label_final)
    print(msg3, flush=True)

    selfcheck_path = out_dir / "smoke_selfcheck.txt"
    selfcheck_path.write_text(
        f"selfcheck1_pass={ok1}\n{msg1}\n\nselfcheck2_pass={ok2}\n{msg2}\n\n"
        f"selfcheck3_pass={ok3}\n{msg3}\n",
        encoding="utf-8",
    )
    print(f"\n[selfcheck] 結果: check1={'PASS' if ok1 else 'FAIL'} check2={'PASS' if ok2 else 'FAIL'} "
          f"check3={'PASS' if ok3 else 'FAIL'} -> {selfcheck_path}", flush=True)

    if not (ok1 and ok2 and ok3):
        print("[FATAL] ユニット的検証に失敗しました。ハーネス実装を確認してください。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
