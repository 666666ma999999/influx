#!/usr/bin/env python3
"""合格基準 v2（pass_rule_v2）の正式判定計算器 — v1（出典の器）に、絶対EVと対TOPIX超過EVの頑健条件を AND で足す。

正本: docs/stock-algo-kpi-catalog.md §6付記V ／ 事前登録: tasks/topix_excess_criterion_v2.md（確定版）。

判定の形（1日1票・系統×signal_date）:
- v1（不変・記録）: 出典の「月次ブロック bootstrap」。§7-Q 型=帰無中心化の片側 p ≤ α／§7-J 型=非中心化 percentile 95%CI 下限>0
- v2 で足す: 絶対EVと超過EVのそれぞれに、連続2か月クラスタ頑健 t（CR1）の片側 p ≤ α_eff（系統別に T0b で較正）
- formal_pass = v1 ∩ 絶対の頑健条件 ∩ 超過の頑健条件（同じ入力・SUE のペア採択前の系統別条件として v1 より狭い）

なぜ新しい経路か（既存 canonical を拡張しない理由）:
- kpi_event_study.bootstrap_ev_ci / ev_v2_summary は in-sample 用の「行加重・非中心化 CI」で、凍結対象
  （judge()・ev_v2_summary は変更禁止ファイル）。本書の「1日1票・前向き正式判定」は別物。
- paper_eval.compute_scoreboard は単純平均のみ（前向き正式判定の p 機構は repo に存在しなかった）。
- tob_drift_v1_stats は凍結ハッシュ付きの別レーン専用。別レーン tob の v2 条件ではそれを**そのまま呼ぶ**。
- 業種33の PIT 参照は influencer_rescore.load_master_index / master_asof を呼ぶ（再実装しない）。
本モジュールは読み取り専用（台帳・trials.jsonl・ledger に一切書かない）。表示側（build_recipe_shelf /
build_daily_reco）は count_formal_passes() だけを使う（numpy 不要の経路）。

使い方:
  python scripts/formal_excess_v2.py --selftest     # 自己検査（SELFTEST OK）
  python scripts/formal_excess_v2.py --dry-run      # 実データで現時点の状態を件数つきで表示（書き込みなし・損益値は出さない）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

REPO = Path(__file__).resolve().parents[1]
TOPIX_PATH = REPO / "data/jquants/topix.json.gz"
LEDGER_PATH = REPO / "data/paper_trades/ledger.jsonl"
TRIALS_PATH = REPO / "data/kpi_trials/trials.jsonl"
TOB_LEDGER_PATH = REPO / "data/paper_trades/tob_ledger.jsonl"
PAIR_LEDGER_PATH = REPO / "data/paper_trades/pair_forward_ledger.jsonl"
INFLUENCER_FORWARD = {
    "fxnia_kabu": REPO / "output/influencer_candidates/fxnia_forward/mentions.csv",
    "okasira_kanki": REPO / "output/influencer_candidates/okasira_forward/mentions.csv",
}
# 監視開始（fxnia= Batch3 実装 2026-07-24・okasira= vault ノート §6 H1「監視開始 2026-08-30」・同じ凍結仕様）
INFLUENCER_START = {"fxnia_kabu": "20260724", "okasira_kanki": "20260830"}

PASS_RULE_VERSION = "pass_rule_v2_abs_and_topix_excess"
COST = 0.003                      # 往復コスト（株側 ret は控除済み・TOPIX 側も同額を引く＝確認2 裁定）
SEED = 20261004
CHUNK = 20000                     # 別レーン fxnia の cluster bootstrap の乱数生成のまとまり
MIN_MONTHS = 6
HOLD_BDAYS = 20                   # nostop の保有営業日数（TOPIX 暦で entry から 20 営業日後）
WINDOW_START = "20260801"         # 初回窓（確認1: 今の窓から適用）
WINDOW_END = "20270731"
HIT_THRESHOLD = 0.20              # lift の当たり（gross +20%）

QUIET = "volshock_x_above200_quiet"
SUE_PAIR = ("sue_x_above200", "sue_beat")

# E-20 の 14 系統と α（第2陣は丸めず 0.0125/3）
E20_ALPHA: dict[str, float] = {
    QUIET: 0.025,
    "sue_x_above200": 0.025,
    "sue_beat": 0.025,
    "sell_reg_trigger_rebound": 0.025 / 5,
    "turnover_rank_surge": 0.025 / 5,
    "margin_expand_yoy": 0.025 / 5,
    "raw_strev_entry": 0.025 / 5,
    "gap_hold_close_strong": 0.0125 / 3,
    "engulf_reversal_day": 0.0125 / 3,
    "three_up_ignition": 0.0125 / 3,
    "sales_beat": 0.05 / 8,
    "guidance_fy_strong": 0.05 / 16 / 2,
    "cfo_margin_improve": 0.05 / 16 / 2,
    "earnings_spillover": 0.05 / 32,
}
# v1 絶対EV検定の種類: "ci_lower"（§7-J: 95%CI 下限>0）／"p"（§7-Q 型: 片側 p ≤ α）
V1_KIND: dict[str, str] = {k: ("ci_lower" if k in SUE_PAIR else "p") for k in E20_ALPHA}
# 頻度の下限（件/月）: 月5件=5.0・年6件=0.5
FREQ_MIN: dict[str, float] = {k: 5.0 for k in E20_ALPHA}
FREQ_MIN.update({"sales_beat": 0.5, QUIET: 0.5})
# 最低有効月: 共通 6・quiet は専用条件 Q7 の 24
MIN_MONTHS_BY_KPI: dict[str, int] = {k: (24 if k == QUIET else MIN_MONTHS) for k in E20_ALPHA}

LABELS = ("formal_pass", "formal_fail_excess", "formal_fail", "formal_pending_topix", "window_expired")
COUNTED_LABEL = "formal_pass"


# --------------------------------------------------------------------------------------------
# 入力: TOPIX
# --------------------------------------------------------------------------------------------
class InputError(ValueError):
    """入力検査に落ちた（判定に使わない）。"""


class Topix:
    """TOPIX 日足（O/C）。日付は YYYYMMDD。"""

    def __init__(self, rows: list[dict], sha256: str = "", source: str = ""):
        o: dict[str, float] = {}
        c: dict[str, float] = {}
        for r in rows:
            d = str(r.get("Date", "")).replace("-", "")
            if len(d) != 8 or not d.isdigit():
                raise InputError(f"TOPIX: 日付が不正 {r.get('Date')!r}")
            if d in o:
                raise InputError(f"TOPIX: 日付の重複 {d}")
            try:
                ov, cv = float(r["O"]), float(r["C"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InputError(f"TOPIX: {d} の O/C が数値でない") from exc
            if not (math.isfinite(ov) and math.isfinite(cv)):
                raise InputError(f"TOPIX: {d} の O/C が有限でない")
            if ov <= 0 or cv <= 0:
                raise InputError(f"TOPIX: {d} の O/C が正でない")
            o[d], c[d] = ov, cv
        self.o, self.c = o, c
        self.dates = sorted(o)
        self.index = {d: i for i, d in enumerate(self.dates)}
        self.sha256 = sha256
        self.source = source

    def ret(self, entry: str, exit_: str) -> Optional[float]:
        """TOPIX_C[exit]/TOPIX_O[entry] − 1。どちらかが無ければ None（＝保留）。"""
        if entry in self.o and exit_ in self.c:
            return self.c[exit_] / self.o[entry] - 1.0
        return None

    def add_bdays(self, d: str, n: int) -> Optional[str]:
        i = self.index.get(d)
        if i is None or i + n >= len(self.dates):
            return None
        return self.dates[i + n]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_topix(path: Path = TOPIX_PATH) -> Topix:
    raw = json.load(gzip.open(path, "rt"))
    rows = raw if isinstance(raw, list) else (raw.get("data") or raw.get("topix") or [])
    return Topix(rows, sha256=sha256_file(path), source=str(path))


# --------------------------------------------------------------------------------------------
# 行の値
# --------------------------------------------------------------------------------------------
def excess_value(ret_net: float, topix_ret: float) -> float:
    """超過 = 株の ret（往復0.3%控除済み） − (TOPIX リターン − 0.003)。"""
    return ret_net - (topix_ret - COST)


def is_judgeable_row(row: dict) -> bool:
    """正式判定の対象行か（nostop 出口確定・分割の偽合図でない）。stop8 終了でも nostop 未満期なら対象外。"""
    if row.get("split_false_signal"):
        return False
    return row.get("ret_nostop") is not None and bool(row.get("entry_date")) and bool(row.get("exit_date_nostop"))


def row_excess(row: dict, topix: Topix) -> Optional[float]:
    """ledger 1行の超過。TOPIX が引けなければ None（保留）。"""
    tr = topix.ret(str(row["entry_date"]), str(row["exit_date_nostop"]))
    if tr is None:
        return None
    return excess_value(float(row["ret_nostop"]), tr)


# --------------------------------------------------------------------------------------------
# 1日1票と月ブロック
# --------------------------------------------------------------------------------------------
def day_votes(items: Iterable[tuple[str, float]]) -> dict[str, float]:
    """(signal_date, 値) の列 → {signal_date: その日の値の平均}。"""
    acc: dict[str, list[float]] = defaultdict(list)
    for d, v in items:
        acc[str(d)].append(float(v))
    return {d: sum(vs) / len(vs) for d, vs in sorted(acc.items())}


def month_blocks(votes: dict[str, float]) -> tuple[list[str], list[list[float]]]:
    """票のある暦月（YYYYMM）ごとの日票リスト（空月はブロックにしない）。"""
    by: dict[str, list[float]] = defaultdict(list)
    for d, v in sorted(votes.items()):
        by[d[:6]].append(v)
    months = sorted(by)
    return months, [by[m] for m in months]


def _np():
    import numpy as np  # 遅延 import（表示側の count_formal_passes は numpy 不要）

    return np


def _boot_index(rng, n_boot: int, m: int):
    """乱数列（別レーン fxnia の cluster bootstrap 用）: CHUNK 行ずつ integers(0, m, size=(k, m))。"""
    np = _np()
    parts = []
    for s in range(0, n_boot, CHUNK):
        k = min(CHUNK, n_boot - s)
        parts.append(rng.integers(0, m, size=(k, m)))
    return np.vstack(parts)


# ---- v1（出典の「月次ブロック bootstrap」・細部は本版で固定・判定は不変のまま記録） -------------------
V1_N_BOOT = 20000
V1_N_BOOT_BOUNDARY = 200000


def _block_arrays(votes: dict[str, float], center: bool):
    """票のある暦月ごとの (日票の和, 日票数)。center=True なら x_d − X で和を取る。"""
    np = _np()
    months, blocks = month_blocks(votes)
    X = sum(votes.values()) / len(votes)
    sums = np.array([sum((v - X) if center else v for v in b) for b in blocks], dtype=float)
    cnts = np.array([len(b) for b in blocks], dtype=float)
    return X, sums, cnts


def _boot_means(sums, cnts, n_boot: int, seed: int):
    """暦月ブロックを月数と同数だけ復元抽出 → 抽出月の日票を連結した平均（Σ和/Σ数）。"""
    np = _np()
    idx = _boot_index(np.random.default_rng(seed), n_boot, len(sums))
    return sums[idx].sum(1) / cnts[idx].sum(1)


def v1_centered_p(votes: dict[str, float], alpha: float, seed: int = SEED) -> dict:
    """§7-Q 型 v1:「前向きEVの月次ブロック bootstrap 片側 p」（帰無中心化・§7-K L1406 の式）。
    p=(1+#{X*≥X})/(n_boot+1)・n_boot=20000・境界 |p−α|<2·MCSE のときだけ同 seed で 200000 に1回。"""
    X, sums, cnts = _block_arrays(votes, center=True)
    n_boot = V1_N_BOOT
    xs = _boot_means(sums, cnts, n_boot, seed)
    p = (1 + int((xs >= X).sum())) / (n_boot + 1)
    mcse = math.sqrt(p * (1 - p) / n_boot)
    boundary = abs(p - alpha) < 2 * mcse
    if boundary:
        n_boot = V1_N_BOOT_BOUNDARY
        xs = _boot_means(sums, cnts, n_boot, seed)
        p = (1 + int((xs >= X).sum())) / (n_boot + 1)
        mcse = math.sqrt(p * (1 - p) / n_boot)
    return {"p": p, "mcse": mcse, "n_boot": n_boot, "boundary_recalc": boundary}


def v1_percentile_ci(votes: dict[str, float], seed: int = SEED, n_boot: int = V1_N_BOOT) -> dict:
    """§7-J 型 v1:「月次ブロック・ブートストラップ95%CI」（非中心化 percentile）。
    下限 = 昇順 [floor(0.025·n_boot)]・上限 = 昇順 [n_boot−1−floor(0.025·n_boot)]（0 始まり）。"""
    np = _np()
    _, sums, cnts = _block_arrays(votes, center=False)
    xs = np.sort(_boot_means(sums, cnts, n_boot, seed))
    k = int(math.floor(0.025 * n_boot))
    return {"lower": float(xs[k]), "upper": float(xs[n_boot - 1 - k]), "n_boot": n_boot}


# ---- v2 で足す頑健条件（T0b で較正した 2か月クラスタ CR1 t） ---------------------------------------
P_METHOD = "cluster_robust_t_2month_cr1"
CLUSTER_ANCHOR = "202608"                  # 2か月クラスタの起点（窓の初月）: 8-9月・10-11月…（7月は単独）
# 系統別の閾値 α_eff（T0b 第7段の固定手順で選んだ値: 較正用乱数で 12 変種×絶対/超過の p の α 分位点の最小値を
# 3 桁に切り下げ・α で上限。別乱数の検証で全変種 α＋2·MCSE 以内＝許容線の検査であって厳密なサイズ保証ではない）
# α を超える較正値は α に切る（緩める方向の較正はしない）。値の出所= tasks/topix_excess_criterion_v2.md「サイズ検証」第7段。
ALPHA_EFF: dict[str, float] = {
    QUIET: 0.025,                          # 較正 0.028 → α に切る
    "sue_x_above200": 0.0225,
    "sue_beat": 0.0233,
    "sell_reg_trigger_rebound": 0.00253,
    "turnover_rank_surge": 0.00343,
    "margin_expand_yoy": 0.00327,
    "raw_strev_entry": 0.005,              # 較正 0.00848 → α に切る
    "gap_hold_close_strong": 0.00237,
    "engulf_reversal_day": 0.00221,
    "three_up_ignition": 0.00206,
    "sales_beat": 0.0059,
    "guidance_fy_strong": 0.0015625,       # 較正 0.00242 → α に切る
    "cfo_margin_improve": 0.00131,
    "earnings_spillover": 0.0015625,       # 較正 0.00192 → α に切る
}


def _month_index(ym: str) -> int:
    return int(ym[:4]) * 12 + int(ym[4:6]) - 1


def cluster_key(ym: str) -> int:
    """暦月 YYYYMM → 2か月クラスタ番号 floor((月 − 起点)/2)。起点より前は負の番号（例: 2026-07 → −1）。"""
    return (_month_index(ym) - _month_index(CLUSTER_ANCHOR)) // 2


def cluster_t(votes: dict[str, float]) -> dict:
    """1日1票の平均 X と、連続2か月クラスタの頑健標準誤差（CR1: G/(G−1)）・t 値・自由度 G−1。

    X = Σx_d / D、各クラスタ g の残差和 e_g = Σ_{d∈g}(x_d − X)、V = G/(G−1)·Σe_g² / D²、SE = √V。
    """
    D = len(votes)
    if D == 0:
        return {"X": None, "SE": None, "t": None, "G": 0, "df": 0, "D": 0}
    X = sum(votes.values()) / D
    e: dict[int, float] = defaultdict(float)
    for d, v in votes.items():
        e[cluster_key(d[:6])] += v - X
    G = len(e)
    if G < 2:
        return {"X": X, "SE": None, "t": None, "G": G, "df": G - 1, "D": D}
    V = G / (G - 1) * sum(x * x for x in e.values()) / (D * D)
    SE = math.sqrt(V)
    if SE == 0:
        t = math.inf if X > 0 else -math.inf   # X≤0 かつ SE=0 は p=1（帰無を棄却しない）
    else:
        t = X / SE
    return {"X": X, "SE": SE, "t": t, "G": G, "df": G - 1, "D": D}


def _t_sf(t: float, df: int) -> float:
    from scipy import stats as st  # 遅延 import

    if math.isinf(t):
        return 0.0 if t > 0 else 1.0
    return float(st.t.sf(t, df))


def pvalue(votes: dict[str, float]) -> float:
    """片側 p = P(T_{G−1} ≥ X/SE)（帰無: 平均 ≤ 0）。SE=0 のとき X>0 なら 0、X≤0 なら 1。"""
    ct = cluster_t(votes)
    if ct["t"] is None:
        raise ValueError(f"p を計算できない（クラスタ数 G={ct['G']}）")
    return _t_sf(ct["t"], ct["df"])


def robust_condition(votes: dict[str, float], kpi: str, min_months: int) -> dict:
    """v2 の頑健条件（絶対・超過で共通）: 有効月 ≥ min_months かつ 2か月クラスタ t の片側 p ≤ α_eff[kpi]。"""
    if kpi not in ALPHA_EFF:
        raise KeyError(f"α_eff が未登録の系統: {kpi}")
    months, _ = month_blocks(votes)
    M = len(months)
    res: dict[str, Any] = {"method": P_METHOD, "alpha_eff": ALPHA_EFF[kpi], "M": M, "n_days": len(votes),
                           "X": (sum(votes.values()) / len(votes)) if votes else None}
    if M < min_months:
        res.update(status="unmet", reason=f"fail_min_months(M={M}<{min_months})")
        return res
    ct = cluster_t(votes)
    p = pvalue(votes)
    res.update(SE=ct["SE"], t=ct["t"], G=ct["G"], df=ct["df"], p=p,
               status="met" if p <= ALPHA_EFF[kpi] else "unmet")
    return res


def v1_condition(votes: dict[str, float], kpi: str, min_months: int) -> dict:
    """v1 の絶対EV検定（出典の型どおり）: SUE=95%CI 下限>0／他=中心化 p ≤ α。有効月 < min_months は未達。"""
    months, _ = month_blocks(votes)
    M = len(months)
    res: dict[str, Any] = {"kind": V1_KIND[kpi], "M": M, "n_days": len(votes)}
    if M < min_months:
        res.update(status="unmet", reason=f"fail_min_months(M={M}<{min_months})")
        return res
    if V1_KIND[kpi] == "ci_lower":
        ci = v1_percentile_ci(votes)
        res.update(ci_lower=ci["lower"], status="met" if ci["lower"] > 0 else "unmet")
    else:
        r = v1_centered_p(votes, E20_ALPHA[kpi])
        res.update(r, status="met" if r["p"] <= E20_ALPHA[kpi] else "unmet")
    return res


def interim_reject(votes: dict[str, float], kpi: str) -> dict:
    """中間判定（早期棄却のみ・v1 値）: 月次ブロック bootstrap（非中心化）95%CI 上限 < 0。
    共通規則: 有効月 M < 2（ブロック 1 個）では計算しない。quiet（Q6）: M ≥ 1 で計算する（30 行到達が
    1 か月に収まっても一意に判定）。時点規則（30 行 or 3か月〔quiet は 3か月∧M≥6〕の早い方で1回だけ）と
    1回だけ消費する記録はランナー側の契約。"""
    months, _ = month_blocks(votes)
    min_m = 1 if kpi == QUIET else 2
    if len(months) < min_m:
        return {"status": "not_computable", "M": len(months)}
    ci = v1_percentile_ci(votes)
    return {"status": "reject" if ci["upper"] < 0 else "continue", "upper": ci["upper"], "M": len(months)}


# --------------------------------------------------------------------------------------------
# lift・頻度・欠測の不利固定値
# --------------------------------------------------------------------------------------------
def lift_point(rows: list[dict], base_rate_by_month: dict[str, float]) -> dict:
    """lift = (Σhit/N) / (Σ_m n_m·b_m / Σ_m n_m)。hit = 1[ret_nostop + 0.003 ≥ 0.20]（gross）。
    b_m が引けない月があれば status="pending_base_rate"（保留→20営業日で失効）。"""
    if not rows:
        return {"status": "unmet", "reason": "no_rows"}
    n_by_m: dict[str, int] = defaultdict(int)
    hits = 0
    for r in rows:
        n_by_m[str(r["signal_date"])[:6]] += 1
        hits += 1 if float(r["ret_nostop"]) + COST >= HIT_THRESHOLD else 0
    missing = sorted(m for m in n_by_m if base_rate_by_month.get(m) is None)
    if missing:
        return {"status": "pending_base_rate", "missing_months": missing}
    N = sum(n_by_m.values())
    base = sum(n * base_rate_by_month[m] for m, n in n_by_m.items()) / N
    if base <= 0:
        return {"status": "unmet", "reason": "base_rate_nonpositive"}
    lift = (hits / N) / base
    return {"status": "met" if lift > 1.0 else "unmet", "lift": lift, "hits": hits, "N": N}


def frequency(rows: list[dict], qualified_months: list[str]) -> Optional[float]:
    """件/月 = qualified 完全暦月にある行数 / その月数。月が0なら None。"""
    q = set(qualified_months)
    if not q:
        return None
    return sum(1 for r in rows if str(r["signal_date"])[:6] in q) / len(q)


def adverse_fixed_value(values: list[float]) -> float:
    """欠測シナリオ3の不利固定値: 昇順 sorted[floor(0.1·(n−1))]（補間なし）。"""
    if not values:
        raise ValueError("adverse_fixed_value: 値が無い")
    s = sorted(values)
    return s[int(math.floor(0.1 * (len(s) - 1)))]


# --------------------------------------------------------------------------------------------
# ラベル
# --------------------------------------------------------------------------------------------
def combine_label(*, completeness_ok: bool, pending: bool, pending_deadline_passed: bool,
                  early_rejected: bool, scenario_labels: list[str]) -> str:
    """判定の順序（本書「判定の順序」）: 完全性→保留→中間棄却→3シナリオ一致→ラベル。
    scenario_labels はシナリオごとの "formal_pass"/"formal_fail_excess"/"formal_fail"。"""
    if not completeness_ok:
        return "window_expired"
    if pending:
        return "window_expired" if pending_deadline_passed else "formal_pending_topix"
    if early_rejected:
        return "formal_fail"
    if not scenario_labels:
        raise ValueError("scenario_labels が空")
    if len(set(scenario_labels)) > 1:
        return "window_expired"
    return scenario_labels[0]


def scenario_label(v1_met: bool, abs_robust_met: bool, excess_met: bool) -> str:
    """1シナリオのラベル。v2 合格 = v1（出典の器）∩ 絶対EVの頑健条件 ∩ 超過の頑健条件。
    超過だけが欠けた時に限り formal_fail_excess（v1 未達・絶対の頑健条件未達は formal_fail）。"""
    if not (v1_met and abs_robust_met):
        return "formal_fail"
    return "formal_pass" if excess_met else "formal_fail_excess"


def sue_pair_decision(state_h1: str, state_raw: str, delta_ev_abs: Optional[float]) -> dict:
    """§7-J ペア判定表の読み替え。state ∈ {met, unmet, pending, expired}（expired は unmet 扱い）。
    戻り値 {"decision": "pending"|"h1"|"raw"|"none", "count": 0|1}。"""
    s1 = "unmet" if state_h1 == "expired" else state_h1
    s2 = "unmet" if state_raw == "expired" else state_raw
    if "pending" in (s1, s2):
        return {"decision": "pending", "count": 0}
    if s1 == "met" and s2 == "unmet":
        return {"decision": "h1", "count": 1}
    if s1 == "unmet" and s2 == "met":
        return {"decision": "raw", "count": 1}
    if s1 == "met" and s2 == "met":
        if delta_ev_abs is None:
            raise ValueError("両方充足の時は絶対 ΔEV が要る")
        return {"decision": "h1" if delta_ev_abs > 0 else "raw", "count": 1}
    return {"decision": "none", "count": 0}


# --------------------------------------------------------------------------------------------
# 表示側の数え方（numpy 不要）
# --------------------------------------------------------------------------------------------
def load_trials(path: Path = TRIALS_PATH) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def is_counted_formal_pass(row: dict) -> bool:
    """正式合格として数える行か: verdict==formal_pass ∧ 版一致 ∧ 登録系統（E20_ALPHA と ALPHA_EFF の両方に載る系統）。
    SUE 2系統は、ペア判定が確定して採択された側の行（sue_pair_adopted is True）だけを数える
    （ペアが保留の間はランナーが正式行を書かない契約・片側だけ formal_pass でも採択印が無ければ数えない）。"""
    kpi = str(row.get("kpi_name", ""))
    if not (str(row.get("verdict", "")) == COUNTED_LABEL
            and row.get("pass_rule_version") == PASS_RULE_VERSION
            and kpi in E20_ALPHA and kpi in ALPHA_EFF):
        return False
    if kpi in SUE_PAIR:
        return row.get("sue_pair_adopted") is True
    return True


def count_formal_passes(trials: list[dict]) -> int:
    """正式合格（合格基準 v2）の本数。SUE ペアは採択された1系統だけ＝最大1本。旧 pass / confirm_pass / 版なしは数えない。"""
    kpis = {str(r["kpi_name"]) for r in trials if is_counted_formal_pass(r)}
    n = len(kpis - set(SUE_PAIR))
    if kpis & set(SUE_PAIR):
        n += 1
    return n


# --------------------------------------------------------------------------------------------
# 別レーン（tob / fxnia / pair）
# --------------------------------------------------------------------------------------------
def tob_deals(events: list[dict]) -> list[dict]:
    """tob_ledger の order/fill/exit を deal_key で束ねる。"""
    by: dict[str, dict] = {}
    for e in events:
        k = e.get("deal_key")
        if not k:
            continue
        d = by.setdefault(k, {"deal_key": k})
        if e["event"] == "order":
            d.update(code=e.get("code"), entry_day=e.get("entry_day"))
        elif e["event"] == "fill":
            d.update(exit_target=e.get("exit_target"), filled=True)
        elif e["event"] == "exit":
            d.update(net=e.get("net"), censored=bool(e.get("censored")), closed=True)
    return list(by.values())


def tob_excess_by_month(deals: list[dict], topix: Topix,
                        censored_exit_date: Optional[Callable[[dict], Optional[str]]] = None) -> dict:
    """v2 条件(3)の入力: 月（entry 月）→ deal 超過のリスト。censored は実際の最終取引日（引けない時は保留）。"""
    by: dict[str, list[float]] = defaultdict(list)
    pending: list[str] = []
    n_closed = 0
    for d in deals:
        if not d.get("closed") or d.get("net") is None:
            continue
        n_closed += 1
        exit_day = d.get("exit_target")
        if d.get("censored"):
            exit_day = censored_exit_date(d) if censored_exit_date else None
        tr = topix.ret(str(d.get("entry_day")), str(exit_day)) if exit_day else None
        if tr is None:
            pending.append(d["deal_key"])
            continue
        by[str(d["entry_day"])[:6]].append(excess_value(float(d["net"]), tr))
    return {"by_month": dict(by), "pending": pending, "n_closed": n_closed}


def tob_v2_condition(excess_by_month: dict[str, list[float]]) -> dict:
    """tob_drift_v1 の v2 条件(3): 凍結統計 tob_drift_v1_stats.bootstrap_lower をそのまま超過に当てる。"""
    import tob_drift_v1_stats as S  # 凍結ハッシュ付きの正本（呼ぶだけ・変更しない）

    if not excess_by_month:
        return {"status": "unmet", "reason": "no_deals"}
    lo = S.bootstrap_lower(excess_by_month)
    return {"status": "met" if lo > 0 else "unmet", "lower95": lo}


def iso_week(d: str) -> str:
    y, w, _ = dt.datetime.strptime(d, "%Y%m%d").isocalendar()
    return f"{y}-W{w:02d}"


def influencer_excess(rows: list[dict], topix: Topix, start: str,
                      s33_of: Optional[Callable[[str, str], str]] = None) -> dict:
    """fxnia/okasira mentions.csv → 独立コールの超過（Batch3 補強1「クラスタ内は最初の適格コールのみ」）。

    対象 = prospective が真 ∧ post_date ≥ start の行。クラスタ = (銘柄, post_date の ISO 週)。クラスタ内を
    (post_date, CSV の出現順) で並べて先頭から見る:
      - status=excluded（約定不可・価格欠測＝補強3で除外）→ 適格でないので次の行へ
      - status=censored（未成熟: signal/entry 未到来・20bd 未満期）→ そのクラスタは保留（後続で置き換えない）
      - status=scored → この行を採用して終わり
    返り値 items は (主クラスタ=銘柄|ISO週, 副クラスタ=signal 月|業種33, 超過)。"""
    by: dict[str, list[tuple[str, int, dict]]] = defaultdict(list)
    n = 0
    for i, r in enumerate(rows):
        if str(r.get("prospective", "")).lower() not in ("true", "1") or str(r.get("post_date", "")) < start:
            continue
        n += 1
        by[f"{r['code']}|{iso_week(str(r['post_date']))}"].append((str(r["post_date"]), i, r))
    items: list[tuple[str, str, float]] = []
    pending_clusters = 0
    pending_topix = 0
    no_eligible = 0
    for k, lst in by.items():
        chosen = None
        state = "none"
        for _, _, r in sorted(lst, key=lambda x: (x[0], x[1])):
            st_ = r.get("status")
            if st_ == "excluded":
                continue
            if st_ == "censored":
                state = "pending"
                break
            if st_ == "scored":
                chosen, state = r, "scored"
                break
        if state == "pending":
            pending_clusters += 1
            continue
        if chosen is None:
            no_eligible += 1
            continue
        tr = topix.ret(str(chosen.get("entry_date", "")), str(chosen.get("exit_date", "")))
        if tr is None or chosen.get("net_return") in ("", None):
            pending_topix += 1
            continue
        s33 = s33_of(str(chosen["code"]), str(chosen["post_date"])) if s33_of else "?"
        sig = str(chosen.get("signal_date") or chosen["post_date"])
        items.append((k, f"{sig[:6]}|{s33}", excess_value(float(chosen["net_return"]), tr)))
    return {"items": items, "pending": pending_clusters + pending_topix, "pending_clusters": pending_clusters,
            "pending_topix": pending_topix, "no_eligible": no_eligible, "n_prospective": n,
            "n_independent": len(by)}


def cluster_bootstrap_lower(items: list[tuple[str, float]], n_boot: int = 1000, seed: int = SEED) -> Optional[float]:
    """fxnia 補強1の型: クラスタ単位の復元抽出（クラスタ数と同数）→ 行のプール平均 → 昇順 floor(0.05·n) 番目。"""
    np = _np()
    if not items:
        return None
    by: dict[str, list[float]] = defaultdict(list)
    for k, v in items:
        by[k].append(v)
    keys = sorted(by)
    sums = np.array([sum(by[k]) for k in keys], dtype=float)
    cnts = np.array([len(by[k]) for k in keys], dtype=float)
    idx = _boot_index(np.random.default_rng(seed), n_boot, len(keys))
    xs = np.sort(sums[idx].sum(1) / cnts[idx].sum(1))
    return float(xs[int(math.floor(0.05 * n_boot))])


def influencer_v2_condition(items: list[tuple[str, str, float]]) -> dict:
    """別レーン fxnia/okasira の条件(4): 主クラスタ（銘柄|ISO週）と副クラスタ（月|業種33）の両方で
    cluster bootstrap 片側95%下限 > 0（補強1 の「主」と「月×業種でも束ねる」を両方満たした時だけ充足）。"""
    if not items:
        return {"status": "unmet", "reason": "no_calls"}
    lo_main = cluster_bootstrap_lower([(a, v) for a, _, v in items])
    lo_sub = cluster_bootstrap_lower([(b, v) for _, b, v in items])
    return {"status": "met" if (lo_main > 0 and lo_sub > 0) else "unmet",
            "lower95_code_week": lo_main, "lower95_month_s33": lo_sub, "n_calls": len(items)}


def s33_lookup() -> Callable[[str, str], str]:
    """業種33の PIT 参照（既存 influencer_rescore.load_master_index / master_asof を再利用）。"""
    import influencer_rescore as IR

    masters = IR.load_master_index(REPO / "data/jquants/master")

    def f(code: str, ymd: str) -> str:
        m = IR.master_asof(masters, ymd)
        c5 = code if len(code) == 5 else code + "0"
        return str((m.get(c5) or m.get(code) or ("?", "?"))[0])

    return f


def pair_excess(events: list[dict], topix: Topix) -> dict:
    """pair_forward の matured 行 → (pair_id, signal_date, 超過)。exit は entry から TOPIX 暦で 20 営業日後。"""
    out: list[tuple[str, str, float]] = []
    pending = 0
    n = 0
    for e in events:
        if e.get("event") != "matured":
            continue
        o = e.get("outcome") or {}
        if o.get("primary_ret") is None or not o.get("entry_date"):
            continue
        n += 1
        exit_day = topix.add_bdays(str(o["entry_date"]), HOLD_BDAYS)
        tr = topix.ret(str(o["entry_date"]), exit_day) if exit_day else None
        if tr is None:
            pending += 1
            continue
        out.append((str(e["pair_id"]), str(e["signal_date"]), excess_value(float(o["primary_ret"]), tr)))
    return {"items": out, "pending": pending, "n_matured": n}


# --------------------------------------------------------------------------------------------
# 試走（書き込みなし・損益値は出さない）
# --------------------------------------------------------------------------------------------
def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def dry_run(asof: Optional[str] = None) -> dict:
    """現時点の状態を件数で返す。判定（p 値）は計算しない＝途中成績を新たに見ない。"""
    asof = asof or dt.date.today().strftime("%Y%m%d")
    topix = load_topix()
    ledger = _read_jsonl(LEDGER_PATH)
    trials = load_trials()
    rep: dict[str, Any] = {"asof": asof, "pass_rule_version": PASS_RULE_VERSION,
                           "topix_sha256": topix.sha256, "ledger_sha256": sha256_file(LEDGER_PATH),
                           "topix_last": topix.dates[-1], "systems": {}, "lanes": {}}
    for kpi in E20_ALPHA:
        rows = [r for r in ledger if r.get("kpi_name") == kpi]
        split_false = sum(1 for r in rows if r.get("split_false_signal"))
        judgeable = [r for r in rows if is_judgeable_row(r)]
        missing_topix = [r for r in judgeable if row_excess(r, topix) is None]
        immature = [r for r in rows if not r.get("split_false_signal") and not is_judgeable_row(r)]
        state = "not_mature" if asof <= WINDOW_END else "needs_runner"
        rep["systems"][kpi] = {
            "alpha": E20_ALPHA[kpi], "rows": len(rows), "split_false_excluded": split_false,
            "nostop_confirmed": len(judgeable), "excess_computable": len(judgeable) - len(missing_topix),
            "excess_pending_topix": len(missing_topix), "nostop_unconfirmed": len(immature),
            "days": len({r["signal_date"] for r in judgeable}),
            "state": state,
        }
    # tob
    deals = tob_deals(_read_jsonl(TOB_LEDGER_PATH))
    tx = tob_excess_by_month(deals, topix)
    rep["lanes"]["tob_drift_v1"] = {"deals": len(deals), "closed": tx["n_closed"],
                                    "excess_computable": sum(len(v) for v in tx["by_month"].values()),
                                    "excess_pending": len(tx["pending"]),
                                    "state": "not_mature（最終判定=2028-07-28 か n=150 の早い方）"}
    pe = pair_excess(_read_jsonl(PAIR_LEDGER_PATH), topix)
    rep["lanes"]["pair_forward_v1"] = {"matured": pe["n_matured"], "excess_computable": len(pe["items"]),
                                       "excess_pending": pe["pending"],
                                       "state": "observation_only（合格線なし・記述評価に超過を併記）"}
    for acc, path in INFLUENCER_FORWARD.items():
        rows = list(csv.DictReader(open(path, encoding="utf-8"))) if path.exists() else []
        ie = influencer_excess(rows, topix, INFLUENCER_START[acc])
        rep["lanes"][f"{acc}_forward"] = {"prospective_rows": ie["n_prospective"], "independent_clusters": ie["n_independent"],
                                          "pending_clusters": ie["pending_clusters"], "no_eligible": ie["no_eligible"],
                                          "excess_computable": len(ie["items"]),
                                          "excess_pending": ie["pending"],
                                          "state": "accruing（停止則=独立60 or 4か月）"}
    rep["formal_pass_count_v2"] = count_formal_passes(trials)
    rep["trials_rows"] = len(trials)
    return rep


# --------------------------------------------------------------------------------------------
# 自己検査
# --------------------------------------------------------------------------------------------
def selftest() -> int:
    t = Topix([{"Date": "2026-08-03", "O": 100, "C": 101}, {"Date": "2026-08-04", "O": 102, "C": 110}])
    assert abs(t.ret("20260803", "20260804") - 0.10) < 1e-12
    assert abs(excess_value(0.05, 0.10) - (0.05 - 0.097)) < 1e-12
    assert t.ret("20260803", "20260805") is None
    for bad in ([{"Date": "2026-08-03", "O": 0, "C": 1}],
                [{"Date": "2026-08-03", "O": 1, "C": 1}, {"Date": "2026-08-03", "O": 1, "C": 1}],
                [{"Date": "2026-08-03", "O": float("nan"), "C": 1}]):
        try:
            Topix(bad)
        except InputError:
            pass
        else:
            raise AssertionError("入力検査が通ってしまった")
    v = day_votes([("20260803", 0.1), ("20260803", 0.3), ("20260901", -0.1)])
    assert v == {"20260803": 0.2, "20260901": -0.1}
    assert adverse_fixed_value([5, 1, 3, 2, 4, 6, 7, 8, 9, 10, 11]) == 2
    assert count_formal_passes([
        {"kpi_name": "sue_beat", "verdict": "formal_pass", "pass_rule_version": PASS_RULE_VERSION,
         "sue_pair_adopted": True},
        {"kpi_name": "sue_x_above200", "verdict": "formal_pass", "pass_rule_version": PASS_RULE_VERSION},
        {"kpi_name": "sales_beat", "verdict": "formal_pass"},
        {"kpi_name": "sales_beat", "verdict": "pass", "pass_rule_version": PASS_RULE_VERSION},
    ]) == 1
    assert set(ALPHA_EFF) == set(E20_ALPHA) and all(ALPHA_EFF[k] <= E20_ALPHA[k] for k in E20_ALPHA)
    print("SELFTEST OK")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--asof", default=None)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.dry_run:
        print(json.dumps(dry_run(a.asof), ensure_ascii=False, indent=1))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
