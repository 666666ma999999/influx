#!/usr/bin/env python3
"""第22周: 候補ファクトリー v1 — 既存母集団×文脈特徴のFDRスクリーニング（カタログ§7-K）。

グリッド仕様の正本は `config/screening_grid_v1.json`（凍結済み・実行後の変更禁止）。統計規律の
正本は `docs/stock-algo-kpi-catalog.md` §6「スクリーニング台帳と多重性の凍結ルール」（2026-07-11
新設）。本スクリプトはその実装であり、グリッド・カタログの記述と食い違う場合は正本を優先する。

段階（--phase）:
    screen  — 発見半期（既定 2016-11〜2019-12）で全セルを評価し、BH-FDR（+BY補正併記）で
              生存セル（上限8）を選抜する。全セル（ineligible/reference含む）を
              `data/kpi_trials/screening_batches.jsonl` に append する。
    confirm — screenの生存セルを検証半期（既定 2020-01〜2022-11）で評価し、Bonferroni同時補正
              （p<=0.05/S）で合否判定する。合否によらず全生存セルを `data/kpi_trials/trials.jsonl`
              に正式行として記録する（+バッチメタ1行）。--phase confirm 単独実行時は
              screening_batches.jsonl から当該batch_idの生存セルを読み込む。
    both    — screen→confirmを1プロセス内で連続実行する（生存セルの受け渡しはメモリ内で行う）。

Canonical Module再利用（新規ロジックはグリッド適用・帰無中心化ブートストラップp値・BH/BY補正・
セル構築・レポート出力のみ）:
    - dev200:        kpi_sue_champion_signals.compute_dev200
    - quiet_ratio:    kpi_volshock_v2_amplifiers.compute_quiet_ratio
    - ul_count_10bd:  kpi_volshock_v3_ul_earnings.compute_ul_count_10bd
    - dev25/max20:    kpi_pead_signals.compute_dev25/compute_max20
    - リフト点推定/CI: kpi_event_study.bootstrap_lift_ci
    - EV参考CI:       kpi_event_study.bootstrap_ev_ci
    - 台帳追記:       kpi_event_study.append_trial（trials_path引数で screening_batches.jsonl にも転用）

Usage:
    docker compose run --rm xstock python scripts/kpi_screen_batch.py --phase both
    docker compose run --rm xstock python scripts/kpi_screen_batch.py --phase screen
    docker compose run --rm xstock python scripts/kpi_screen_batch.py --phase confirm
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical: load_base_rate_by_month/bootstrap_lift_ci/bootstrap_ev_ci/append_trial)
import kpi_pead_signals as pead_mod  # noqa: E402  (Canonical: compute_dev25/compute_max20)
import kpi_sue_champion_signals as sue_mod  # noqa: E402  (Canonical: compute_dev200)
import kpi_volshock_v2_amplifiers as v2_amp  # noqa: E402  (Canonical: compute_quiet_ratio)
import kpi_volshock_v3_ul_earnings as v3_ul  # noqa: E402  (Canonical: compute_ul_count_10bd)
import measure_base_rate  # noqa: E402  (Canonical: カレンダー・bars/regime読込・ROUND_TRIP_COST)

DEFAULT_GRID_PATH = Path("config/screening_grid_v1.json")
DEFAULT_OUTPUT_DIR = Path("output/kpi_screening")
DEFAULT_SCREENING_LEDGER = Path("data/kpi_trials/screening_batches.jsonl")
DEFAULT_TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
ROUND_TRIP_COST = measure_base_rate.ROUND_TRIP_COST

# --- 凍結パラメータ（grid JSON screen_rules/confirm_rulesのテキスト定義を数値化。
#     min_n/min_months_spanned のみ grid から動的読取＝正本と自動整合、他は自然文のため
#     module定数として固定する。他KPIスクリプトの§6凍結値ハードコード慣行と同型） ---
N_BOOT_PRIMARY = 20000
N_BOOT_ESCALATED = 50000
SEED_PRIMARY = 42
SEED_SECONDARY = 43
BH_Q = 0.10
SELECTION_LIMIT = 8
CONFIRM_MIN_N = 30
SENSITIVITY_N_BOOT = 10000
LIFT_N_BOOT = 1000  # 点推定のみ使用（CIは参考併記）。§6既定N_BOOTSTRAP=1000と同一

# 凍結グリッドの sha256 先頭16桁の登録簿（Codexレビュー⑬M1対応）。
# 新バッチはここへの追記＝事前登録の一部。実行時に実ファイルのハッシュと照合し、
# 不一致（=凍結後のグリッド変更）は FATAL 停止する。--grid-override（smoke用）は対象外だが、
# その場合は --no-trials-append と --screening-ledger の併用を強制して本番台帳を保護する。
FROZEN_GRID_HASHES = {"batch_v1": "73489b3b23a229b4"}


def meta_kpi_name(batch_id: str) -> str:
    """trials.jsonl のバッチメタ行 kpi_name の唯一の導出点（Canonical・二重append判定と記録の両方で使用）。

    batch_id="batch_v1" → "screening_batch_v1"（§6付記4項の kpi_name=screening_batch_<id> 表記に一致）。
    """
    return f"screening_{batch_id}"


# --- グリッド読込・整合性検証 ----------------------------------------------------


def sha256_16(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_grid(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"FATAL: グリッドJSONが見つかりません: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def included_populations(grid: dict) -> list[dict]:
    return [e for e in grid["population_inventory"] if e.get("included")]


def verify_population_integrity(pops: list[dict]) -> None:
    """各 returns.csv の sha256 先頭16桁が grid の sha256_16 と一致するか検査する（凍結時点との同一性保証）。"""
    for pop in pops:
        path = Path(pop["returns_csv"])
        if not path.exists():
            raise SystemExit(f"FATAL: returns.csv が見つかりません: {path}（population={pop['kpi_name']}）")
        actual = sha256_16(path)
        expected = pop["sha256_16"]
        if actual != expected:
            raise SystemExit(
                f"FATAL: sha256不一致 population={pop['kpi_name']} expected={expected} actual={actual}。"
                f"凍結時点からreturns.csvが変更されている可能性があります（グリッドJSONまたはデータの再確認要）。"
            )


def check_batch_replay_fatal(ledger_path: Path, batch_id: str) -> None:
    """同一batch_idの行が既にscreening_batches.jsonlに存在すればFATAL（batch_replay_rules）。

    screen/both（=screening_batches.jsonlへの新規書き込みを伴うフェーズ）の実行前にのみ呼ぶ。
    confirm単独実行はscreening_batches.jsonlを読むだけで書かないため対象外
    （むしろ既存batch_idの行が必要＝load_survivors_from_ledgerの前提）。
    """
    if not ledger_path.exists():
        return
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("batch_id") == batch_id:
                raise SystemExit(
                    f"FATAL: {ledger_path} に既存の batch_id={batch_id} の行が存在します"
                    "（batch_replay_rules違反・同一batch_idへの再appendは禁止）。"
                )


# --- 特徴量計算（ユニーク(code, signal_date)単位で1回だけ計算し全セル共有） -------------


def compute_computed_features(pairs_df: pd.DataFrame, bday_index: dict, all_bdays: list[str]) -> pd.DataFrame:
    """dev200/quiet_ratio/ul_count_10bd/dev25/max20 を日付昇順に計算する（load_bars_dayのlru_cache
    ヒット率を上げるため呼び出し順は signal_date 昇順を維持すること）。"""
    dev200_vals: list[Optional[float]] = []
    quiet_vals: list[Optional[float]] = []
    ul_vals: list[Optional[int]] = []
    dev25_vals: list[Optional[float]] = []
    max20_vals: list[Optional[float]] = []
    for row in pairs_df.itertuples(index=False):
        code, sd = row.code, row.signal_date
        dev200_vals.append(sue_mod.compute_dev200(code, sd, bday_index, all_bdays))
        quiet_vals.append(v2_amp.compute_quiet_ratio(code, sd, bday_index, all_bdays))
        ul_vals.append(v3_ul.compute_ul_count_10bd(code, sd, bday_index, all_bdays))
        idx_g = bday_index[sd]
        adjc_prev = None
        if idx_g >= 1:
            adjc_prev = measure_base_rate.load_bars_day(all_bdays[idx_g - 1]).get(code, {}).get("AdjC")
        dev25_vals.append(pead_mod.compute_dev25(code, idx_g, all_bdays, adjc_prev) if adjc_prev is not None else None)
        max20_vals.append(pead_mod.compute_max20(code, idx_g, all_bdays))
    out = pairs_df.copy()
    out["dev200"] = dev200_vals
    out["quiet_ratio"] = quiet_vals
    out["ul_count_10bd"] = ul_vals
    out["dev25"] = dev25_vals
    out["max20"] = max20_vals
    return out


def attach_features(combined: pd.DataFrame, bday_index: dict, all_bdays: list[str], universes_df: pd.DataFrame) -> pd.DataFrame:
    """combined（population列付きの複数母集団プール済みDataFrame）に7特徴量を全て付与する。"""
    if combined.empty:
        for col in ("dev200", "quiet_ratio", "ul_count_10bd", "dev25", "max20", "regime_feat", "membership"):
            combined[col] = pd.Series(dtype="object")
        return combined
    pairs = combined[["code", "signal_date"]].drop_duplicates().sort_values("signal_date").reset_index(drop=True)
    pairs_features = compute_computed_features(pairs, bday_index, all_bdays)
    combined = combined.merge(pairs_features, on=["code", "signal_date"], how="left", validate="many_to_one")
    combined["regime_feat"] = combined["regime"].where(combined["regime"] != "unknown", None)
    umap = universes_df.rename(columns={"month": "universe_month_used"})[["universe_month_used", "code", "membership"]]
    combined = combined.merge(umap, on=["universe_month_used", "code"], how="left", validate="many_to_one")
    return combined


def feature_colname(feature_name: str) -> str:
    return "regime_feat" if feature_name == "regime" else feature_name


def load_population_period_df(pop: dict, period: tuple[str, str]) -> pd.DataFrame:
    df = pd.read_csv(
        pop["returns_csv"],
        dtype={"signal_date": str, "code": str, "month": str, "regime": str, "universe_month_used": str},
    )
    df = df[df["in_universe"] == True].reset_index(drop=True)  # noqa: E712 (CSV読込のbool比較)
    df = df[(df["month"] >= period[0]) & (df["month"] <= period[1])].reset_index(drop=True)
    df = df.copy()
    df["population"] = pop["kpi_name"]
    return df


# --- 帰無中心化・月次ブロック・ブートストラップの片側p値（ベクトル化） --------------------


def compute_null_centered_pvalue(df: pd.DataFrame, n_boot: int, seed: int) -> dict:
    """EV_obs = mean(ret) - cost。ret_centered = ret - mean(ret) + cost で中心化した系列に
    月ブロック復元抽出（kpi_event_study.bootstrap_ev_ciと同一方式）を適用し、
    p = (1 + #{EV*_centered >= EV_obs}) / (n_boot + 1) を計算する。

    月ごとの合計・件数を先に集約し、n_boot×月数のインデックス行列で一括抽出することで
    1セルあたり1秒未満を狙う（Pythonループでの逐次リサンプルを回避）。
    """
    global_mean = float(df["ret"].mean())
    ev_obs = global_mean - ROUND_TRIP_COST
    ret_centered = df["ret"].to_numpy() - global_mean + ROUND_TRIP_COST
    tmp = pd.DataFrame({"month": df["month"].to_numpy(), "ret_centered": ret_centered})
    grouped = tmp.groupby("month")["ret_centered"].agg(["sum", "count"])
    sums = grouped["sum"].to_numpy()
    counts = grouped["count"].to_numpy()
    n_months = len(sums)

    rng = np.random.default_rng(seed)
    idx_matrix = rng.integers(0, n_months, size=(n_boot, n_months))
    sampled_sums = sums[idx_matrix].sum(axis=1)
    sampled_counts = counts[idx_matrix].sum(axis=1)
    ev_star = sampled_sums / sampled_counts - ROUND_TRIP_COST

    exceed = int(np.sum(ev_star >= ev_obs))
    p = (1 + exceed) / (n_boot + 1)
    mc_se = math.sqrt(p * (1 - p) / n_boot)
    return {"ev_obs": ev_obs, "p": p, "mc_se": mc_se, "n_boot": n_boot, "n_months": n_months}


def bh_correction(pvals: np.ndarray, q: float, correction_factor: float = 1.0) -> np.ndarray:
    """BH-FDR（correction_factor=1.0）またはBY補正（correction_factor=c(m)）のstep-up手続き。"""
    m = len(pvals)
    if m == 0:
        return np.array([], dtype=bool)
    order = np.argsort(pvals)
    sorted_p = pvals[order]
    ranks = np.arange(1, m + 1)
    thresh = ranks / m * q / correction_factor
    passed_sorted = sorted_p <= thresh
    reject_sorted = np.zeros(m, dtype=bool)
    where_passed = np.where(passed_sorted)[0]
    if len(where_passed) > 0:
        reject_sorted[: where_passed.max() + 1] = True
    reject = np.zeros(m, dtype=bool)
    reject[order] = reject_sorted
    return reject


def harmonic_sum(m: int) -> float:
    return sum(1.0 / k for k in range(1, m + 1)) if m else 1.0


# --- セル構築 --------------------------------------------------------------------


def build_cells(
    combined: pd.DataFrame,
    pops: list[dict],
    feature_defs: list[dict],
    reference_set: set[tuple[str, str]],
    min_n: int,
    min_months: int,
) -> list[dict]:
    cells: list[dict] = []
    for pop in pops:
        pop_name = pop["kpi_name"]
        pop_df = combined[combined["population"] == pop_name]
        pop_n = len(pop_df)
        for feat in feature_defs:
            fname = feat["name"]
            colname = feature_colname(fname)
            missing_count = int(pop_df[colname].isna().sum()) if pop_n else 0
            is_ref = (pop_name, fname) in reference_set
            valid_df = pop_df[pop_df[colname].notna()]
            for bucket in feat["buckets"]:
                cond = bucket["cond"]
                if len(valid_df):
                    mask = eval(cond, {"__builtins__": {}}, {fname: valid_df[colname]})  # noqa: S307 (grid JSONのみが供給する信頼済み条件式)
                    bucket_df = valid_df[mask]
                else:
                    bucket_df = valid_df
                n = len(bucket_df)
                months = int(bucket_df["month"].nunique()) if n else 0
                eligible = n >= min_n and months >= min_months
                cells.append(
                    {
                        "population": pop_name,
                        "feature": fname,
                        "bucket": bucket["label"],
                        "cond": cond,
                        "is_reference": is_ref,
                        "population_period_n": pop_n,
                        "missing_count": missing_count,
                        "missing_rate": (missing_count / pop_n) if pop_n else None,
                        "n": n,
                        "months": months,
                        "eligible": eligible,
                        "df": bucket_df,
                        "status": "ineligible" if not eligible else ("reference" if is_ref else "tested"),
                    }
                )
    return cells


# --- screenフェーズ ---------------------------------------------------------------


def score_family(cells: list[dict], n_boot: int, seed: int) -> None:
    """eligibleな全セル（reference含む）にp値・lift点推定を計算しcellに書き込む。"""
    for c in cells:
        if not c["eligible"]:
            c["ev_obs"] = c["p"] = c["mc_se"] = c["lift_point"] = c["lift_ci_low"] = c["lift_ci_high"] = None
            continue
        pval_res = compute_null_centered_pvalue(c["df"], n_boot=n_boot, seed=seed)
        c["ev_obs"], c["p"], c["mc_se"] = pval_res["ev_obs"], pval_res["p"], pval_res["mc_se"]
        lift_res = kpi_event_study.bootstrap_lift_ci(c["df"], c["_base_rate_by_month"], n_boot=LIFT_N_BOOT, seed=SEED_PRIMARY)
        c["lift_point"], c["lift_ci_low"], c["lift_ci_high"] = (
            lift_res["point_lift"], lift_res["ci_low"], lift_res["ci_high"],
        )


def evaluate_family(family: list[dict], pvals: list[float], q: float, limit: int) -> dict:
    """family（eligible∩非reference）の p 値配列から BH/BY 判定と生存セル選抜を行う。

    cellへの書き込みは行わない（seed感度確認のため同一familyに対しseed違いのp値で2回呼ぶ必要が
    あり、呼び出しのたびにcellのbh_pass/by_passを上書きすると最終的にどちらのseedの判定が
    cellに残ったか不定になるため、呼び出し側が明示的に書き込みタイミングを選べるようにする）。
    """
    pvals_arr = np.array(pvals)
    m = len(family)
    bh_pass = bh_correction(pvals_arr, q)
    by_pass = bh_correction(pvals_arr, q, correction_factor=harmonic_sum(m))
    candidates = [
        (c, p) for c, p, bh in zip(family, pvals, bh_pass)
        if bh and c["lift_point"] is not None and c["lift_point"] > 1.0
    ]
    candidates.sort(key=lambda cp: (cp[1], cp[0]["population"], cp[0]["feature"], cp[0]["bucket"]))
    survivors = [c for c, _p in candidates[:limit]]
    keys = {(c["population"], c["feature"], c["bucket"]) for c in survivors}
    return {"pvals": pvals, "bh_pass": bh_pass, "by_pass": by_pass, "survivors": survivors, "keys": keys}


def run_screen_phase(
    grid: dict,
    pops: list[dict],
    feature_defs: list[dict],
    reference_set: set[tuple[str, str]],
    bday_index: dict,
    all_bdays: list[str],
    base_rate_by_month: dict,
    universes_df: pd.DataFrame,
) -> dict:
    period = (grid["periods"]["screen"]["start_month"], grid["periods"]["screen"]["end_month"])
    min_n = grid["screen_rules"]["eligibility"]["min_n"]
    min_months = grid["screen_rules"]["eligibility"]["min_months_spanned"]

    frames = [load_population_period_df(pop, period) for pop in pops]
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    combined = attach_features(combined, bday_index, all_bdays, universes_df)

    cells = build_cells(combined, pops, feature_defs, reference_set, min_n, min_months)
    for c in cells:
        c["_base_rate_by_month"] = base_rate_by_month

    score_family(cells, n_boot=N_BOOT_PRIMARY, seed=SEED_PRIMARY)
    family = [c for c in cells if c["eligible"] and not c["is_reference"]]
    eval42 = evaluate_family(family, [c["p"] for c in family], BH_Q, SELECTION_LIMIT)

    # --- seed感度確認（seed=43で生存集合が一致するかを機械確認。cellのbh_pass/by_passは
    #     ここでは書き込まない＝最終的にどちらのseedの判定が残ったか不定になる事故を防ぐ） ---
    pvals_43 = [compute_null_centered_pvalue(c["df"], n_boot=N_BOOT_PRIMARY, seed=SEED_SECONDARY)["p"] for c in family]
    eval43 = evaluate_family(family, pvals_43, BH_Q, SELECTION_LIMIT)

    n_boot_used = N_BOOT_PRIMARY
    escalated = False
    if eval42["keys"] != eval43["keys"]:
        escalated = True
        n_boot_used = N_BOOT_ESCALATED
        score_family(cells, n_boot=n_boot_used, seed=SEED_PRIMARY)
        family = [c for c in cells if c["eligible"] and not c["is_reference"]]
        eval42 = evaluate_family(family, [c["p"] for c in family], BH_Q, SELECTION_LIMIT)
        pvals_43 = [compute_null_centered_pvalue(c["df"], n_boot=n_boot_used, seed=SEED_SECONDARY)["p"] for c in family]
        eval43 = evaluate_family(family, pvals_43, BH_Q, SELECTION_LIMIT)
        if eval42["keys"] != eval43["keys"]:
            raise SystemExit(
                "FATAL: seed=42/43 の生存セル集合が n_boot=50000 でも不一致です。"
                "グリッド仕様の凍結手順（seed_sensitivity）により結果の採用を禁止し停止します。"
                f" seed42={sorted(eval42['keys'])} / seed43={sorted(eval43['keys'])}"
            )

    # 最終的にcellへ書き込むbh_pass/by_passはseed=42（primary）の判定のみ
    for c, bh, by in zip(family, eval42["bh_pass"], eval42["by_pass"]):
        c["bh_pass"], c["by_pass"] = bool(bh), bool(by)
    survivors = eval42["survivors"]
    survivor_keys = eval42["keys"]

    for c in cells:
        key = (c["population"], c["feature"], c["bucket"])
        c["selected"] = key in survivor_keys
        c.pop("_base_rate_by_month", None)

    return {
        "cells": cells,
        "family_size": len(family),
        "survivors": survivors,
        "survivor_keys": survivor_keys,
        "n_boot_used": n_boot_used,
        "escalated": escalated,
        "period": period,
        "eligible_count": sum(1 for c in cells if c["eligible"]),
        "reference_count": sum(1 for c in cells if c["is_reference"]),
        "bh_pass_count": sum(1 for c in family if c["bh_pass"]),
        "by_pass_count": sum(1 for c in family if c["by_pass"]),
    }


def cell_to_ledger_record(cell: dict, batch_id: str, grid_sha: str, n_boot_used: int) -> dict:
    return {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": f"screen_v1_{cell['population']}_{cell['feature']}_{cell['bucket']}",
        "batch_id": batch_id,
        "grid_sha256_16": grid_sha,
        "population": cell["population"],
        "feature": cell["feature"],
        "bucket": cell["bucket"],
        "cond": cell["cond"],
        "is_reference": cell["is_reference"],
        "status": cell["status"],
        "n": cell["n"],
        "months": cell["months"],
        "population_period_n": cell["population_period_n"],
        "missing_count": cell["missing_count"],
        "missing_rate": cell["missing_rate"],
        "eligible": cell["eligible"],
        "ev_obs": cell.get("ev_obs"),
        "p": cell.get("p"),
        "mc_se": cell.get("mc_se"),
        "n_boot_used": n_boot_used if cell["eligible"] else None,
        "seed": SEED_PRIMARY,
        "bh_pass": cell.get("bh_pass"),
        "by_pass": cell.get("by_pass"),
        "lift_point": cell.get("lift_point"),
        "lift_ci_low": cell.get("lift_ci_low"),
        "lift_ci_high": cell.get("lift_ci_high"),
        "selected": cell.get("selected", False),
    }


# --- confirmフェーズ ---------------------------------------------------------------


def verify_grid_prose_constants(grid: dict) -> None:
    """凍結グリッドJSONの自然文パラメータとコード定数の一致を FATAL 検査する（Codexレビュー⑬M1）。

    min_n/min_months_spanned は grid から動的読取のため対象外。自然文にしか存在しない
    n_boot/seed/q/生存上限/Bonferroni/confirm床 を部分文字列照合で検査する。
    """
    checks = [
        (f"n_boot={N_BOOT_PRIMARY}" in grid["screen_rules"]["p_value"], "screen_rules.p_value の n_boot"),
        (f"seed={SEED_PRIMARY}" in grid["screen_rules"]["p_value"], "screen_rules.p_value の seed"),
        ("q=0.10" in grid["screen_rules"]["selection_primary"], "screen_rules.selection_primary の q"),
        ("上限8" in grid["screen_rules"]["selection_primary"], "screen_rules.selection_primary の生存上限"),
        ("0.05/S" in grid["confirm_rules"]["criteria"], "confirm_rules.criteria の Bonferroni 同時補正"),
        (f"n_confirm >= {CONFIRM_MIN_N}" in grid["confirm_rules"]["criteria"], "confirm_rules.criteria の n_confirm 床"),
    ]
    for ok, label in checks:
        if not ok:
            raise SystemExit(f"FATAL: グリッドJSONとコード定数の不一致: {label}（凍結仕様の照合失敗）")


def load_screen_summary_from_ledger(ledger_path: Path, batch_id: str) -> dict:
    """confirmフェーズ単独実行用: screening_batches.jsonlから当該batch_idの全行を読み、
    (a) 生存セルのscreen統計（n/months/ev_obs/p/mc_se/lift_point/lift_ci_low/lift_ci_high/
    bh_pass/by_pass） (b) バッチ集計（n_cells=全行数・eligible/reference/bh_pass/by_pass の
    各カウント）を再構築する。build_confirm_trial_records が要求する screen_summary の形
    （cell_count/eligible_count/reference_count/bh_pass_count/by_pass_count/survivors）に
    合わせて返す（phase="both"時にscreen_summaryへ本来入る値と同一のキー集合）。
    """
    if not ledger_path.exists():
        raise SystemExit(f"FATAL: confirmフェーズ単独実行にはscreening_batches.jsonlが必要です: {ledger_path}")
    rows = []
    with open(ledger_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("batch_id") == batch_id:
                rows.append(rec)
    if not rows:
        raise SystemExit(f"FATAL: batch_id={batch_id} の行が {ledger_path} に見つかりません。")

    keys = [(r.get("population"), r.get("feature"), r.get("bucket")) for r in rows]
    if len(keys) != len(set(keys)):
        raise SystemExit(
            f"FATAL: screening台帳にセルキー重複あり（batch_id={batch_id}・二重appendまたはscreen中断の疑い）"
        )

    survivors = [
        {
            "population": rec["population"], "feature": rec["feature"], "bucket": rec["bucket"], "cond": rec["cond"],
            "n": rec.get("n"), "months": rec.get("months"), "ev_obs": rec.get("ev_obs"), "p": rec.get("p"),
            "mc_se": rec.get("mc_se"), "lift_point": rec.get("lift_point"), "lift_ci_low": rec.get("lift_ci_low"),
            "lift_ci_high": rec.get("lift_ci_high"), "bh_pass": rec.get("bh_pass"), "by_pass": rec.get("by_pass"),
        }
        for rec in rows
        if rec.get("status") == "tested" and rec.get("selected")
    ]
    if not survivors:
        raise SystemExit(f"FATAL: batch_id={batch_id} の生存セルが {ledger_path} に見つかりません。")

    return {
        "cell_count": len(rows),
        "eligible_count": sum(1 for r in rows if r.get("eligible")),
        "reference_count": sum(1 for r in rows if r.get("is_reference")),
        "bh_pass_count": sum(1 for r in rows if r.get("bh_pass")),
        "by_pass_count": sum(1 for r in rows if r.get("by_pass")),
        "survivors": survivors,
    }


def run_confirm_phase(
    grid: dict,
    survivors: list[dict],
    pops_by_name: dict[str, dict],
    bday_index: dict,
    all_bdays: list[str],
    base_rate_by_month: dict,
    universes_df: pd.DataFrame,
) -> dict:
    period = (grid["periods"]["confirm"]["start_month"], grid["periods"]["confirm"]["end_month"])
    S = len(survivors)
    bonferroni_alpha = 0.05 / S if S else None

    needed_pop_names = sorted({s["population"] for s in survivors})
    frames = [load_population_period_df(pops_by_name[name], period) for name in needed_pop_names]
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    combined = attach_features(combined, bday_index, all_bdays, universes_df)

    results = []
    for s in survivors:
        colname = feature_colname(s["feature"])
        pop_df = combined[combined["population"] == s["population"]]
        valid_df = pop_df[pop_df[colname].notna()] if len(pop_df) else pop_df
        if len(valid_df):
            mask = eval(s["cond"], {"__builtins__": {}}, {s["feature"]: valid_df[colname]})  # noqa: S307
            cell_df = valid_df[mask]
        else:
            cell_df = valid_df
        n_confirm = len(cell_df)

        if n_confirm > 0:
            pval_res = compute_null_centered_pvalue(cell_df, n_boot=N_BOOT_PRIMARY, seed=SEED_PRIMARY)
            lift_res = kpi_event_study.bootstrap_lift_ci(cell_df, base_rate_by_month, n_boot=LIFT_N_BOOT, seed=SEED_PRIMARY)
            ev_ci95 = kpi_event_study.bootstrap_ev_ci(cell_df, n_boot=LIFT_N_BOOT, seed=SEED_PRIMARY)
        else:
            pval_res = {"ev_obs": None, "p": None, "mc_se": None}
            lift_res = {"point_lift": None, "ci_low": None, "ci_high": None}
            ev_ci95 = {"point_ev": None, "ci_low": None, "ci_high": None}

        passed = bool(
            n_confirm >= CONFIRM_MIN_N
            and pval_res["p"] is not None
            and bonferroni_alpha is not None
            and pval_res["p"] <= bonferroni_alpha
            and lift_res["point_lift"] is not None
            and lift_res["point_lift"] > 1.0
        )

        bull_df = cell_df[cell_df["regime"] == "bull"] if n_confirm else cell_df
        bear_df = cell_df[cell_df["regime"] == "bear"] if n_confirm else cell_df
        bull_ev = float(bull_df["ret"].mean() - ROUND_TRIP_COST) if len(bull_df) else None
        bear_ev = float(bear_df["ret"].mean() - ROUND_TRIP_COST) if len(bear_df) else None
        excl2020_df = cell_df[~cell_df["month"].between("202001", "202012")] if n_confirm else cell_df
        ev_excl2020 = float(excl2020_df["ret"].mean() - ROUND_TRIP_COST) if len(excl2020_df) else None

        results.append(
            {
                **s,
                "n_confirm": n_confirm,
                "confirm_ev_obs": pval_res["ev_obs"],
                "confirm_p": pval_res["p"],
                "confirm_mc_se": pval_res["mc_se"],
                "confirm_lift_point": lift_res["point_lift"],
                "confirm_lift_ci_low": lift_res["ci_low"],
                "confirm_lift_ci_high": lift_res["ci_high"],
                "confirm_ev_ci95_low": ev_ci95["ci_low"],
                "confirm_ev_ci95_high": ev_ci95["ci_high"],
                "bonferroni_alpha": bonferroni_alpha,
                "confirm_pass": passed,
                "bull_ev": bull_ev,
                "bear_ev": bear_ev,
                "ev_excl2020": ev_excl2020,
            }
        )
    return {"period": period, "S": S, "bonferroni_alpha": bonferroni_alpha, "results": results}


def build_confirm_trial_records(
    grid: dict, grid_sha: str, confirm: dict, screen_summary: Optional[dict], trials_path: Path,
) -> tuple[list[dict], dict]:
    """confirm結果からtrials.jsonl用のレコード群（生存セル各1行+バッチメタ1行）を構築する。"""
    n_before = 0
    if trials_path.exists():
        with open(trials_path, "r", encoding="utf-8") as f:
            n_before = sum(1 for _ in f)
    S = confirm["S"]
    n_new = n_before + 1 + S
    ci_level = 1 - 0.05 / n_new if n_new else 0.95

    cell_records = []
    for r in confirm["results"]:
        sens_ci = (
            kpi_event_study.bootstrap_ev_ci(
                # cell_dfは confirm フェーズ内でしか保持していないため、ここでは再計算しない。
                # 代わりに confirm_ev_ci95 と同じ n_boot=10000/ci_level=ci_level を
                # run_confirm_phase 側で計算済みの値として渡す設計にできないため、
                # ここでは r に埋め込まれた cell_df 参照を使う。
                r["_cell_df"], n_boot=SENSITIVITY_N_BOOT, seed=SEED_PRIMARY, ci_level=ci_level,
            )
            if r.get("_cell_df") is not None and len(r["_cell_df"])
            else {"point_ev": None, "ci_low": None, "ci_high": None}
        )
        params = {
            "grid_ref": "docs/stock-algo-kpi-catalog.md §7-K",
            "grid_sha256_16": grid_sha,
            "batch_id": grid["batch_id"],
            "population": r["population"],
            "feature": r["feature"],
            "bucket": r["bucket"],
            "cond": r["cond"],
            "screen_p": None,
            "screen_lift": None,
            "screen_ev_obs": None,
            "confirm_p": r["confirm_p"],
            "confirm_mc_se": r["confirm_mc_se"],
            "confirm_ev_obs": r["confirm_ev_obs"],
            "confirm_lift_ci95": [r["confirm_lift_ci_low"], r["confirm_lift_ci_high"]],
            "confirm_ev_ci95": [r["confirm_ev_ci95_low"], r["confirm_ev_ci95_high"]],
            "bonferroni_alpha": r["bonferroni_alpha"],
            "n_confirm": r["n_confirm"],
            "confirm_pass": r["confirm_pass"],
            "bull_ev": r["bull_ev"],
            "bear_ev": r["bear_ev"],
            "ev_excl2020_reference": r["ev_excl2020"],
            "sensitivity_ci_level": ci_level,
            "sensitivity_ev_point": sens_ci["point_ev"],
            "sensitivity_ev_ci95": [sens_ci["ci_low"], sens_ci["ci_high"]],
            "framing": "historical validation（内部検証・独立確認ではない）。合格の意味は前向き検証への優先順位付け資格に限定",
        }
        if screen_summary is not None:
            key = (r["population"], r["feature"], r["bucket"])
            match = next((c for c in screen_summary["survivors"] if (c["population"], c["feature"], c["bucket"]) == key), None)
            if match is not None:
                params["screen_p"] = match["p"]
                params["screen_lift"] = match["lift_point"]
                params["screen_ev_obs"] = match["ev_obs"]
        cell_records.append(
            {
                "run_id": uuid.uuid4().hex,
                "ts": jq_fetch.now_jst().isoformat(),
                "kpi_name": f"screen_v1_{r['population']}_{r['feature']}_{r['bucket']}",
                "params": params,
                "period": {"start": confirm["period"][0], "end": confirm["period"][1]},
                "n": r["n_confirm"],
                "lift": r["confirm_lift_point"],
                "ci_low": r["confirm_lift_ci_low"],
                "ci_high": r["confirm_lift_ci_high"],
                "ev": r["confirm_ev_obs"],
                "verdict": "confirm_pass" if r["confirm_pass"] else "confirm_fail",
                "entry_mode": "screening_batch_v1_reused_returns",
                "regime_filter": None,
            }
        )

    meta_record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": meta_kpi_name(grid["batch_id"]),
        "params": {
            "grid_sha256_16": grid_sha,
            "batch_id": grid["batch_id"],
            "n_cells": screen_summary["cell_count"] if screen_summary else None,
            "eligible_count": screen_summary["eligible_count"] if screen_summary else None,
            "reference_count": screen_summary["reference_count"] if screen_summary else None,
            "bh_pass_count": screen_summary["bh_pass_count"] if screen_summary else None,
            "by_pass_count": screen_summary["by_pass_count"] if screen_summary else None,
            "survivor_count": S,
            "n_before": n_before,
            "n_after": n_new,
        },
        "period": {"start": confirm["period"][0], "end": confirm["period"][1]},
        "n": None,
        "lift": None,
        "ci_low": None,
        "ci_high": None,
        "ev": None,
        "verdict": "batch_meta",
        "entry_mode": "screening_batch_v1_meta",
        "regime_filter": None,
    }
    return cell_records, {"n_before": n_before, "n_after": n_new, "ci_level": ci_level, "meta_record": meta_record}


# --- レポート出力 ------------------------------------------------------------------


def _fmt(x, spec: str = ".4f") -> str:
    return format(x, spec) if x is not None else "-"


def write_report(
    path: Path,
    grid: dict,
    grid_sha: str,
    screen_summary: Optional[dict],
    confirm: Optional[dict],
    trials_meta: Optional[dict],
) -> None:
    lines = [
        f"# 第22周: 候補ファクトリー v1 スクリーニングレポート（batch_id={grid['batch_id']}）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"グリッド: `{DEFAULT_GRID_PATH}` (sha256_16={grid_sha})",
        f"発見半期: {grid['periods']['screen']['start_month']} 〜 {grid['periods']['screen']['end_month']} / "
        f"検証半期: {grid['periods']['confirm']['start_month']} 〜 {grid['periods']['confirm']['end_month']}"
        "（historical validation・独立確認ではない）",
        "",
    ]

    if screen_summary is not None:
        cells = screen_summary["cells"]
        lines += [
            "## screenフェーズ",
            "",
            f"- 総セル数: {len(cells)}",
            f"- eligible: {screen_summary['eligible_count']} / reference_only: {screen_summary['reference_count']} / "
            f"family(eligible∩非reference): {screen_summary['family_size']}",
            f"- BH-FDR(q=0.10)通過: {screen_summary['bh_pass_count']} / BY補正通過: {screen_summary['by_pass_count']}",
            f"- n_boot使用値: {screen_summary['n_boot_used']}"
            f"{'（seed感度不一致によりn_boot=50000へエスカレーション済み）' if screen_summary['escalated'] else '（seed=42/43生存集合一致・エスカレーションなし）'}",
            f"- 生存セル（上限8・p昇順）: {len(screen_summary['survivors'])}",
            "",
            "### 生存セル一覧",
            "",
            "| population | feature | bucket | n | months | EV_obs | p | mc_se | lift点推定 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for c in screen_summary["survivors"]:
            lines.append(
                f"| {c['population']} | {c['feature']} | {c['bucket']} | {c['n']} | {c['months']} | "
                f"{_fmt(c['ev_obs'], '.4%')} | {_fmt(c['p'], '.5f')} | {_fmt(c['mc_se'], '.5f')} | "
                f"{_fmt(c['lift_point'], '.2f')} |"
            )
        ref_cells = [c for c in cells if c["is_reference"]]
        lines += [
            "",
            "### 参考: reference_only セル（既検証・近傍の再現アンカー。選抜対象外）",
            "",
            "| population | feature | bucket | status | n | EV_obs | p | lift点推定 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for c in sorted(ref_cells, key=lambda c: (c["population"], c["feature"], c["bucket"])):
            lines.append(
                f"| {c['population']} | {c['feature']} | {c['bucket']} | {c['status']} | {c['n']} | "
                f"{_fmt(c.get('ev_obs'), '.4%')} | {_fmt(c.get('p'), '.5f')} | {_fmt(c.get('lift_point'), '.2f')} |"
            )
        lines.append("")

    if confirm is not None:
        lines += [
            "## confirmフェーズ（historical validation・独立確認ではない）",
            "",
            f"- 生存セル数 S={confirm['S']} / Bonferroni閾値 0.05/S={_fmt(confirm['bonferroni_alpha'], '.6f')}",
            "",
            "| population | feature | bucket | n_confirm | confirm_p | confirm_lift | 合否 | bull_EV | bear_EV | 2020除外EV |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in confirm["results"]:
            lines.append(
                f"| {r['population']} | {r['feature']} | {r['bucket']} | {r['n_confirm']} | "
                f"{_fmt(r['confirm_p'], '.6f')} | {_fmt(r['confirm_lift_point'], '.2f')} | "
                f"{'PASS' if r['confirm_pass'] else 'FAIL'} | {_fmt(r['bull_ev'], '.4%')} | "
                f"{_fmt(r['bear_ev'], '.4%')} | {_fmt(r['ev_excl2020'], '.4%')} |"
            )
        if trials_meta is not None:
            lines += [
                "",
                f"- trials.jsonl記録: N_before={trials_meta['n_before']} → N_after={trials_meta['n_after']}"
                f"（family-wise感度CI水準={trials_meta['ci_level']:.6%}・n_boot={SENSITIVITY_N_BOOT}）",
            ]
        lines.append("")

    lines += ["## 位置づけ", ""]
    if confirm is not None:
        pass_count = sum(1 for r in confirm["results"] if r["confirm_pass"])
        if pass_count > 0:
            lines += [
                f"検証段（historical validation）通過は {pass_count} セル。これらは「統計的に確認された発見」",
                "ではなく「前向き検証への優先順位付けを通過した候補」である（§6付記2項）。運用組み込みではない。",
            ]
        else:
            lines += [
                "**発見段の生存セルは検証段（historical validation）で全て棄却された。前向き検証へ送る",
                "新候補はなし**（§6付記の規律どおりの帰結）。発見段の数値（EV・lift・p）は選抜済みの",
                "見かけ値であり、単独で候補採用の根拠にしてはならない。",
            ]
    else:
        lines += [
            "本バッチは screen フェーズのみ実行済み。BH/BY 生存セルは「統計的に確認された発見」ではなく",
            "検証段（confirm）待ちの優先順位付け候補である（§6付記2項）。最終帰結は confirm 実行後に確定。",
        ]
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# --- メイン処理 ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="第22周: 候補ファクトリー v1 スクリーニング（カタログ§7-K）")
    parser.add_argument("--phase", required=True, choices=["screen", "confirm", "both"])
    parser.add_argument("--grid", default=str(DEFAULT_GRID_PATH))
    parser.add_argument("--grid-override", default=None, help="smoke test用: --gridより優先される代替グリッドJSON")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--screening-ledger", default=None, help="smoke test用: 既定はdata/kpi_trials/screening_batches.jsonl")
    parser.add_argument("--trials-path", default=str(DEFAULT_TRIALS_PATH))
    parser.add_argument("--no-trials-append", action="store_true", help="confirmフェーズのtrials.jsonlへの追記をスキップ（smoke用）")
    args = parser.parse_args()

    grid_path = Path(args.grid_override) if args.grid_override else Path(args.grid)
    grid = load_grid(grid_path)
    grid_sha = sha256_16(grid_path)
    batch_id = grid["batch_id"]

    # Codexレビュー⑬M1: 凍結ハッシュ照合と smoke 経路の本番台帳保護
    if args.grid_override:
        if not args.no_trials_append or not args.screening_ledger:
            raise SystemExit(
                "FATAL: --grid-override は --no-trials-append と --screening-ledger の併用が必須"
                "（本番台帳の保護・Codexレビュー⑬M1）"
            )
    else:
        expected_sha = FROZEN_GRID_HASHES.get(batch_id)
        if expected_sha is None:
            raise SystemExit(
                f"FATAL: batch_id={batch_id} は FROZEN_GRID_HASHES に未登録"
                "（新バッチは事前登録の一部として登録簿への追記が必要）"
            )
        if grid_sha != expected_sha:
            raise SystemExit(
                f"FATAL: グリッドJSONのsha256_16が凍結値と不一致: actual={grid_sha} expected={expected_sha}"
                "（実行後のグリッド変更は禁止・§6付記6項）"
            )
        verify_grid_prose_constants(grid)
    screening_ledger_path = Path(args.screening_ledger) if args.screening_ledger else DEFAULT_SCREENING_LEDGER
    trials_path = Path(args.trials_path)

    pops = included_populations(grid)
    verify_population_integrity(pops)
    pops_by_name = {p["kpi_name"]: p for p in pops}
    feature_defs = grid["features"]
    reference_set = {(c["population"], c["feature"]) for c in grid["reference_only_cells"]["cells"]}

    print(f"batch_id={batch_id} grid_sha256_16={grid_sha} 対象母集団={len(pops)}本 特徴量={len(feature_defs)}種", file=sys.stderr)

    if args.phase in ("screen", "both"):
        check_batch_replay_fatal(screening_ledger_path, batch_id)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_df = pd.read_csv(
        BASE_RATE_DIR / f"universes_w{UNIVERSE_WINDOW}.csv.gz", dtype={"month": str, "code": str}
    )

    screen_summary = None
    if args.phase in ("screen", "both"):
        print("screenフェーズ実行中...", file=sys.stderr)
        screen_summary = run_screen_phase(
            grid, pops, feature_defs, reference_set, bday_index, all_bdays, base_rate_by_month, universes_df
        )
        screen_summary["cell_count"] = len(screen_summary["cells"])
        for cell in screen_summary["cells"]:
            kpi_event_study.append_trial(
                cell_to_ledger_record(cell, batch_id, grid_sha, screen_summary["n_boot_used"]),
                trials_path=screening_ledger_path,
            )
        print(
            f"screen完了: cells={screen_summary['cell_count']} eligible={screen_summary['eligible_count']} "
            f"reference={screen_summary['reference_count']} family={screen_summary['family_size']} "
            f"bh_pass={screen_summary['bh_pass_count']} by_pass={screen_summary['by_pass_count']} "
            f"survivors={len(screen_summary['survivors'])} n_boot_used={screen_summary['n_boot_used']} "
            f"escalated={screen_summary['escalated']}",
            file=sys.stderr,
        )

    confirm_result = None
    trials_meta = None
    if args.phase in ("confirm", "both"):
        if args.phase == "both":
            survivors = [
                {"population": c["population"], "feature": c["feature"], "bucket": c["bucket"], "cond": c["cond"]}
                for c in screen_summary["survivors"]
            ]
            screen_stats_for_trials = screen_summary
        else:
            screen_stats_for_trials = load_screen_summary_from_ledger(screening_ledger_path, batch_id)
            expected_cells = len(pops) * sum(len(f["buckets"]) for f in feature_defs)
            if screen_stats_for_trials["cell_count"] != expected_cells:
                raise SystemExit(
                    f"FATAL: screening台帳のセル数不整合: {screen_stats_for_trials['cell_count']} != "
                    f"期待 {expected_cells}（screen中断・不完全台帳の疑い・Codexレビュー⑬）"
                )
            survivors = [
                {"population": c["population"], "feature": c["feature"], "bucket": c["bucket"], "cond": c["cond"]}
                for c in screen_stats_for_trials["survivors"]
            ]

        # Codexレビュー⑬: trials.jsonl への二重append防止（メタ行の存在で判定）
        if not args.no_trials_append and trials_path.exists():
            meta_name = meta_kpi_name(batch_id)
            with open(trials_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and json.loads(line).get("kpi_name") == meta_name:
                        raise SystemExit(
                            f"FATAL: trials.jsonl に {meta_name} のメタ行が既に存在（confirm の二重append防止。"
                            "出版前差し替えが必要な場合は該当行を手動で除去してから再実行すること）"
                        )

        print(f"confirmフェーズ実行中... 生存セル数={len(survivors)}", file=sys.stderr)
        confirm_result = run_confirm_phase(
            grid, survivors, pops_by_name, bday_index, all_bdays, base_rate_by_month, universes_df
        )

        # sensitivity CI計算にcell_dfを使うため、run_confirm_phase内で捨てたDataFrameを
        # ここで再構築せず、直接resultsに埋め込む形にするための補助再計算
        needed_pop_names = sorted({s["population"] for s in survivors})
        frames = [load_population_period_df(pops_by_name[n], confirm_result["period"]) for n in needed_pop_names]
        combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        combined = attach_features(combined, bday_index, all_bdays, universes_df)
        for r in confirm_result["results"]:
            colname = feature_colname(r["feature"])
            pop_df = combined[combined["population"] == r["population"]]
            valid_df = pop_df[pop_df[colname].notna()] if len(pop_df) else pop_df
            if len(valid_df):
                mask = eval(r["cond"], {"__builtins__": {}}, {r["feature"]: valid_df[colname]})  # noqa: S307
                r["_cell_df"] = valid_df[mask]
            else:
                r["_cell_df"] = valid_df

        cell_records, trials_meta = build_confirm_trial_records(
            grid, grid_sha, confirm_result, screen_stats_for_trials, trials_path
        )
        for r in confirm_result["results"]:
            r.pop("_cell_df", None)

        if not args.no_trials_append:
            for rec in cell_records:
                kpi_event_study.append_trial(rec, trials_path=trials_path)
            kpi_event_study.append_trial(trials_meta["meta_record"], trials_path=trials_path)
            print(f"trials.jsonl追記完了: N_before={trials_meta['n_before']} → N_after={trials_meta['n_after']}", file=sys.stderr)
        else:
            print(
                f"--no-trials-append: trials.jsonl追記をスキップ "
                f"(would-be N_before={trials_meta['n_before']} → N_after={trials_meta['n_after']})",
                file=sys.stderr,
            )
        pass_count = sum(1 for r in confirm_result["results"] if r["confirm_pass"])
        print(f"confirm完了: S={confirm_result['S']} PASS={pass_count} FAIL={confirm_result['S'] - pass_count}", file=sys.stderr)

    report_path = Path(args.output_dir) / batch_id / "report.md"
    write_report(report_path, grid, grid_sha, screen_summary, confirm_result, trials_meta)
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
