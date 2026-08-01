#!/usr/bin/env python3
"""EV estimand v2 の一括算出と watchlist への凍結メタデータ書込み。

事前登録: tasks/ev_estimand_v2_preregister.md R4（2026-08-01 Codex GO・本文SHA凍結）。
- 計算正本は kpi_event_study.ev_v2_summary のみ（Dual-Path禁止）
- watchlist へは in_sample.estimand_v2 の単一ネストキーのみ追加（既存キー不変・構造/値の不変条件検査つき）
- 結果は tasks/ev_estimand_v2_results.md へ分離出力（prereg SHA・入力hash・生成時刻を記録）
- α非消費・trials.jsonl / 判定ロジックへの書込みなし
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from kpi_event_study import BOOTSTRAP_SEED, EV_V2_METHOD, EV_V2_N_BOOT, ev_v2_summary  # noqa: E402

WATCHLIST_PATH = REPO / "config/paper_watchlist.json"
PREREG_PATH = REPO / "tasks/ev_estimand_v2_preregister.md"
RESULTS_PATH = REPO / "tasks/ev_estimand_v2_results.md"
AMENDMENT_DATE = "2026-08-01"  # R4 凍結日
EXITS = {"none": ("ret", 0.003), "stop8": ("ret_stop8", 0.0)}  # R4 §3 コスト規約（stop8は控除済み）


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_entry(kpi_name: str) -> tuple[dict, dict]:
    """1系統の estimand_v2 dict と、比較表用の中間値を返す。"""
    base = {"amendment_date": AMENDMENT_DATE,
            "computed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    csv_path = REPO / "output/kpi" / kpi_name / "returns.csv"
    if not csv_path.exists():
        return {**base, "status": "not_computed", "reason": "no_returns_csv"}, {}
    df = pd.read_csv(csv_path)
    if "in_universe" not in df.columns:
        return {**base, "status": "not_computed", "reason": "missing_columns"}, {}
    in_uni = df[df["in_universe"].astype(str) == "True"]
    if len(in_uni) == 0:
        return {**base, "status": "not_computed", "reason": "empty_after_in_universe_filter"}, {}

    result = {**base, "status": "computed", "method": EV_V2_METHOD,
              "n_boot": EV_V2_N_BOOT, "seed": BOOTSTRAP_SEED}
    inputs = {"returns_sha256_16": sha256_file(csv_path)[:16]}
    for exit_name, (col, cost) in EXITS.items():
        summary = ev_v2_summary(in_uni, ev_column=col, cost=cost)
        if summary.get("status") != "computed":
            return {**base, "status": "not_computed", "reason": summary.get("reason", "missing_columns")}, {}
        result[f"n_used_{exit_name}"] = summary["n_used"]
        result[f"n_excluded_nonfinite_{exit_name}"] = summary["n_excluded_nonfinite"]
        result[f"months_spanned_{exit_name}"] = summary["months_spanned"]
        result[f"ev_{exit_name}_v2"] = summary["ev_v2"]
        result[f"ev_{exit_name}_ci1s_low"] = summary["ci1s_low"]
        result[f"ev_{exit_name}_ci95_low"] = summary["ci95_low"]
        result[f"ev_{exit_name}_ci95_high"] = summary["ci95_high"]
    return result, inputs


def strip_estimand_v2(obj: dict) -> dict:
    out = copy.deepcopy(obj)
    for entry in out.get("watchlist", []):
        if isinstance(entry.get("in_sample"), dict):
            entry["in_sample"].pop("estimand_v2", None)
    return out


def atomic_write_with_invariant(new_obj: dict, old_obj: dict) -> None:
    """R4 §6: estimand_v2 除去後の構造・値が旧と完全一致する場合のみ os.replace。"""
    if strip_estimand_v2(new_obj) != old_obj:
        raise RuntimeError("不変条件違反: estimand_v2 以外の構造/値が変化するため書込みを拒否")
    content = json.dumps(new_obj, ensure_ascii=False, indent=2) + "\n"
    reparsed = json.loads(content)
    if strip_estimand_v2(reparsed) != old_obj:
        raise RuntimeError("不変条件違反(再読込後): 書込みを拒否")
    fd, tmp = tempfile.mkstemp(prefix=".paper_watchlist.", dir=WATCHLIST_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, WATCHLIST_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def fmt(v) -> str:
    return f"{v * 100:+.2f}%" if isinstance(v, (int, float)) else "—"


def main() -> None:
    prereg_sha = sha256_file(PREREG_PATH)
    watchlist_sha_before = sha256_file(WATCHLIST_PATH)
    old_obj = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    new_obj = copy.deepcopy(old_obj)

    rows = []
    input_hashes = []
    computed = not_computed = 0
    for entry in new_obj["watchlist"]:
        kpi = entry["kpi_name"]
        ev2, inputs = compute_entry(kpi)
        entry.setdefault("in_sample", {})["estimand_v2"] = ev2
        if ev2["status"] == "computed":
            computed += 1
            input_hashes.append(f"{kpi}: {inputs['returns_sha256_16']}")
            ins = entry["in_sample"]
            rows.append(
                f"| {kpi} | {fmt(ins.get('ev_none'))} | {fmt(ev2['ev_none_v2'])} | {fmt(ev2['ev_none_ci1s_low'])} | "
                f"{fmt(ins.get('ev_stop8'))} | {fmt(ev2['ev_stop8_v2'])} | {fmt(ev2['ev_stop8_ci1s_low'])} | "
                f"{ev2['n_used_none']} | {ev2['months_spanned_none']} |"
            )
        else:
            not_computed += 1
            rows.append(f"| {kpi} | — | 未算出 ({ev2['reason']}) | — | — | 未算出 | — | — | — |")

    atomic_write_with_invariant(new_obj, old_obj)

    lines = [
        "# EV estimand v2 — v1→v2 比較表（生成物・判定不使用）",
        "",
        f"- 生成時刻: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 事前登録本文SHA256（算出前凍結）: `{prereg_sha}`",
        f"- watchlist 書込み前SHA256: `{watchlist_sha_before[:16]}…`",
        f"- 算出: {computed}系統 / 未算出: {not_computed}系統（理由コード付き）",
        f"- 入力hash（returns.csv sha256先頭16桁）: {'; '.join(input_hashes)}",
        "",
        "v1=凍結点推定（プール平均）/ v2=月等ウェイト two-stage / ci1s=片側95%下限（正）。コスト規約: none=0.003控除・stop8=控除済み0。",
        "",
        "| KPI | v1 EV(none) | v2 EV(none) | v2 片側95%下限(none) | v1 EV(stop8) | v2 EV(stop8) | v2 片側95%下限(stop8) | n | 月数 |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
    ]
    RESULTS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"computed={computed} not_computed={not_computed}")
    print(f"results -> {RESULTS_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
