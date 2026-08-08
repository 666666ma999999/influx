#!/usr/bin/env python3
"""pending 試行の終端整理ツール（resolutions.jsonl・台帳外・α非消費）。

正本設計: tasks/pending_verdict_flow.md v0.2（catalog §「pending解消ルール」372-381 の具体化）。

不変条件（必須ゲート）:
- trials.jsonl には一切書かない。--apply / --resolve は実行前後で行数+sha256 を照合し、
  変化していれば FATAL（resolutions も書かずに中断）
- resolution は判決（verdict）ではない: 統計的地位ゼロの運用整理。α・Bonferroni分母に無関係
  （分母の正本は kpi_bonferroni_check.effective_trial_count = trials.jsonl 行数のみ）
- resolutions.jsonl も append-only。取り消し・更新は同 run_id への新行 append で表現し、
  同一 run_id の**ファイル内最終行が現在状態**（reduction 規則）

使い方:
    python3 scripts/kpi_pending_resolutions.py --audit    # 未整理一覧+SLA検査（超過あれば exit 1）
    python3 scripts/kpi_pending_resolutions.py --summary  # 稼働状況向け1行（daily_screen が呼ぶ）
    python3 scripts/kpi_pending_resolutions.py --apply    # 機械規則 R0-R4 で未整理分を仕分け
    python3 scripts/kpi_pending_resolutions.py --resolve RUN_ID --path closed_no_action \
        --reason "..." [--evidence "..."]                 # ユーザー裁定1件を記帳（rule=USER）
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRIALS_PATH = REPO / "data" / "kpi_trials" / "trials.jsonl"
RESOLUTIONS_PATH = REPO / "data" / "kpi_trials" / "resolutions.jsonl"
WATCHLIST_PATH = REPO / "config" / "paper_watchlist.json"

SLA_DAYS = 90  # pending 行は append から90日以内に resolution 必須（行単位SLA）
JST = timezone(timedelta(hours=9))

RESOLUTION_PATHS = {
    "superseded_rejected",   # 同一KPIに後続の棄却側判決行あり
    "rejected_by_evidence",  # holdout開封記録等の棄却証拠が系譜に直接該当
    "awaiting_forward",      # watchlist observation 掲載＝判決は前向き評価から（非終端）
    "structurally_capped_n", # in-sample 全域走査済みで n<100＝判決不能の明示
    "closed_no_action",      # 運用開始ライン3条件未達等＝追試を予定しない旨の運用整理（判決ではない）
}
REJECT_VERDICTS = {"fail", "hoos_rejected", "confirm_fail", "rejected", "invalidated"}
# 運用開始ライン（catalog §0 枠F・3条件。頻度は n÷期間月数の近似値で判定し理由に明記）
LINE_CI_LOW = 1.2
LINE_EV = 0.01
LINE_MONTHLY = 5.0
HOLDOUT_START = "2023"  # period.start がこれ以降なら holdout 期の行
# holdout 1回開封（2026-07-06/07 PEAD棄却・catalog:277）の当事者系譜。R0 で棄却証拠に紐付けてよいのはこれだけ
HOLDOUT_REJECTED_LINEAGE = {"pead_gap8_vol3", "pead_gap8_vol3_defer3"}


def sha256_of(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), sum(1 for b in data.splitlines() if b.strip())


def load_trials() -> list[dict]:
    rows = []
    for line in TRIALS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_resolutions() -> dict[str, dict]:
    """reduction 規則: 同一 run_id の最終行が現在状態。"""
    current: dict[str, dict] = {}
    if not RESOLUTIONS_PATH.exists():
        return current
    for line in RESOLUTIONS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            current[rec["run_id"]] = rec
    return current


def months_spanned(period: dict) -> float | None:
    try:
        s, e = str(period["start"]), str(period["end"])
        sy, sm = int(s[:4]), int(s[5:7])
        ey, em = int(e[:4]), int(e[5:7])
        return (ey - sy) * 12 + (em - sm) + 1
    except (KeyError, ValueError, TypeError):
        return None


def watchlist_observation_names() -> set[str]:
    wl = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))["watchlist"]
    return {e["kpi_name"] for e in wl if e.get("status") == "observation"}


def classify(idx: int, row: dict, trials: list[dict], wl_obs: set[str]) -> tuple[str, str, str, str]:
    """機械仕分け R0-R4。返り値 (rule, resolution_path, reason, evidence)。該当なしは rule=''。"""
    k = row["kpi_name"]
    n = row.get("n")
    ci = row.get("ci_low")
    ev = row.get("ev")
    m = months_spanned(row.get("period", {}))
    freq = (n / m) if (n and m) else None

    start = str(row.get("period", {}).get("start", ""))
    end = str(row.get("period", {}).get("end", ""))
    valid_period = len(start) >= 4 and start[:4].isdigit() and m is not None and m > 0
    # in-sample 全域走査（2016-11〜2022-11系）を実測で確認できる行だけが R3/R4 の対象
    in_sample_full = valid_period and start <= "2017-01" and end >= "2022"

    if valid_period and start >= HOLDOUT_START:
        # HOLDOUT変種名（*_HOLDOUT / *_HOLDOUT_obs）は基底系統名で照合する
        base = k.removesuffix("_obs").removesuffix("_HOLDOUT")
        if base in wl_obs or k in wl_obs:
            return ("R0", "awaiting_forward",
                    "holdout期の観測記録。系統は前向き観察中＝判決は前向きが下す。本行のholdout数値は表示・推奨に使用禁止",
                    f"watchlist:{base if base in wl_obs else k}")
        if base in HOLDOUT_REJECTED_LINEAGE:
            return ("R0", "rejected_by_evidence",
                    "本行自体が holdout 1回開封の記録で、開封時の合格3条件を満たさなかった棄却記録",
                    "catalog:277（2026-07-06/07 PEAD holdout棄却）")
        # 未知の holdout 行は機械では閉じない（rejected_by_evidence は直接該当する証拠が必須）
        return ("", "", "", "")
    later_rejects = [t for j, t in enumerate(trials)
                     if j > idx and t.get("kpi_name") == k and t.get("verdict") in REJECT_VERDICTS]
    if later_rejects:
        return ("R1", "superseded_rejected",
                f"後続の棄却側判決行あり（{later_rejects[0].get('verdict')}）",
                f"run_id:{later_rejects[0].get('run_id')}")
    if k in wl_obs:
        return ("R2", "awaiting_forward", "paper_watchlist で status=observation＝前向き観察経路に接続済み",
                f"watchlist:{k}")
    if n is not None and n < 100:
        if in_sample_full:
            return ("R3", "structurally_capped_n",
                    f"in-sample 全域走査済み（{start}〜{end}）で n={n}<100・増える見込みなし＝判決不能", "")
        return ("", "", "", "")  # 部分走査の小標本は「構造的上限」と断定できない＝機械では閉じない
    # R4: 欠測・期間不正は「未達」とみなさない（実在する値の閾値未達だけを数える）
    misses = []
    if ci is not None and ci <= LINE_CI_LOW:
        misses.append(f"CI下限={round(ci, 2)}≤{LINE_CI_LOW}")
    if ev is not None and ev < LINE_EV:
        misses.append(f"EV={round(ev, 4)}<+1%/月")
    if freq is not None and freq > 0 and freq < LINE_MONTHLY:
        misses.append(f"月次頻度≈{round(freq, 1)}<{LINE_MONTHLY:g}件/月(n÷期間月数の近似)")
    if misses and in_sample_full:
        return ("R4", "closed_no_action",
                "運用開始ライン3条件未達（" + "・".join(misses) + "）・in-sample全域走査済みのため追試を予定しない",
                "")
    return ("", "", "", "")


def pending_rows(trials: list[dict]) -> list[tuple[int, dict]]:
    return [(i, r) for i, r in enumerate(trials) if r.get("verdict") == "pending"]


def age_days(row: dict) -> int:
    ts = datetime.fromisoformat(row["ts"])
    return (datetime.now(JST) - ts).days


def counts(trials: list[dict], resolutions: dict[str, dict]) -> dict:
    pend = pending_rows(trials)
    resolved = [r for _, r in pend if r["run_id"] in resolutions]
    forward = [r for r in resolved if resolutions[r["run_id"]]["resolution_path"] == "awaiting_forward"]
    unresolved = [r for _, r in pend if r["run_id"] not in resolutions]
    overdue = [r for r in unresolved if age_days(r) > SLA_DAYS]
    return {
        "total_trials": len(trials), "pending": len(pend), "resolved": len(resolved),
        "forward": len(forward), "unresolved": len(unresolved), "overdue": len(overdue),
        "unresolved_rows": unresolved,
    }


def validate_current(trials: list[dict], current: dict[str, dict]) -> list[str]:
    """reduction 後の現在状態を全件検証する（手編集・競合書込み・実装バグの混入検知）。"""
    trials_by_id = {t["run_id"]: t for t in trials if t.get("run_id")}
    problems = []
    for run_id, rec in current.items():
        t = trials_by_id.get(run_id)
        if t is None:
            problems.append(f"run_id {run_id[:8]}: trials.jsonl に存在しない")
            continue
        if t.get("verdict") != "pending":
            problems.append(f"{rec.get('kpi_name')}: 対象行の verdict が {t.get('verdict')}（pending のみ整理対象）")
        if t.get("kpi_name") != rec.get("kpi_name"):
            problems.append(f"run_id {run_id[:8]}: kpi_name 不一致（台帳={t.get('kpi_name')} / 整理={rec.get('kpi_name')}）")
        if rec.get("resolution_path") not in RESOLUTION_PATHS:
            problems.append(f"{rec.get('kpi_name')}: 未知の resolution_path={rec.get('resolution_path')}")
        if rec.get("resolution_path") == "rejected_by_evidence" and not rec.get("evidence"):
            problems.append(f"{rec.get('kpi_name')}: rejected_by_evidence に evidence が無い")
    return problems


def summary_line(c: dict) -> str:
    return (f"α消費 {c['total_trials']}試行 / pending {c['pending']}: "
            f"整理済み{c['resolved']}（うち前向き接続{c['forward']}）・未整理{c['unresolved']}"
            f"・{SLA_DAYS}日SLA超過{c['overdue']}")


def guarded_append(records: list[dict], trials_fingerprint: tuple[str, int],
                   skip_already_resolved: bool = False) -> list[dict]:
    """G1/G2 検査つき append。trials.jsonl の照合は3点: 読込時（呼び出し元）→lock取得後（書込み直前）→書込み完了後。

    - 書込み前に不一致 → resolutions を1行も書かずに FATAL（exit 2）
    - 書込み後に不一致 → 書いた行の run_id を列挙して FATAL（exit 2）。オペレーターは reduction 規則
      （最終行勝ち）に従い取り消し行を append して無効化する
    - skip_already_resolved=True（--apply 用）: lock 取得後に resolutions を再読込し、
      既に整理済みの run_id を除外する（並行 --apply の二重記帳防止）
    実際に書いた行のリストを返す。
    """
    RESOLUTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESOLUTIONS_PATH.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            mid = sha256_of(TRIALS_PATH)
            if mid != trials_fingerprint:
                print(f"FATAL: trials.jsonl が読込時から変化（{trials_fingerprint} → {mid}）。"
                      "resolutions は書き込まず中断", file=sys.stderr)
                sys.exit(2)
            if skip_already_resolved:
                fresh = load_resolutions()
                records = [r for r in records if r["run_id"] not in fresh]
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    after = sha256_of(TRIALS_PATH)
    if after != trials_fingerprint:
        print(f"FATAL: 書込み中に trials.jsonl が変化（{trials_fingerprint} → {after}）。"
              f"今回書いた {len(records)}行（run_id: {[r['run_id'][:8] for r in records]}）は"
              "取り消し行の append で無効化して原因を調査すること", file=sys.stderr)
        sys.exit(2)
    return records


def validate_target(run_id: str, trials: list[dict]) -> dict:
    matches = [r for r in trials if r.get("run_id") == run_id]
    if not matches:
        print(f"FATAL: run_id {run_id} は trials.jsonl に存在しない", file=sys.stderr)
        sys.exit(2)
    if matches[0].get("verdict") != "pending":
        print(f"FATAL: run_id {run_id} の verdict は {matches[0].get('verdict')}（pending のみ整理対象）",
              file=sys.stderr)
        sys.exit(2)
    return matches[0]


def make_record(row: dict, rule: str, path: str, reason: str, evidence: str) -> dict:
    return {
        "run_id": row["run_id"], "kpi_name": row["kpi_name"], "resolution_path": path,
        "rule": rule, "reason": reason, "evidence": evidence,
        "review_batch": datetime.now(JST).strftime("%Y-%m"),
        "ts": datetime.now(JST).isoformat(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--summary", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--resolve", metavar="RUN_ID")
    ap.add_argument("--path", choices=sorted(RESOLUTION_PATHS))
    ap.add_argument("--reason")
    ap.add_argument("--evidence", default="")
    args = ap.parse_args()

    fingerprint = sha256_of(TRIALS_PATH)
    trials = load_trials()
    resolutions = load_resolutions()
    c = counts(trials, resolutions)

    if args.summary:
        print(summary_line(c))
        return 0

    if args.audit:
        problems = validate_current(trials, resolutions)
        print(summary_line(c) + (f"・整理行の不正{len(problems)}" if problems else ""))
        for r in c["unresolved_rows"]:
            print(f"  未整理: {r['kpi_name']}  run_id={r['run_id'][:8]}  経過{age_days(r)}日"
                  f"  n={r.get('n')} ci_low={r.get('ci_low')} ev={r.get('ev')}")
        for p in problems:
            print(f"  不正: {p}")
        if c["overdue"] or problems:
            print(f"NG: {SLA_DAYS}日SLA超過 {c['overdue']}件 / 整理行の不正 {len(problems)}件", file=sys.stderr)
            return 1
        return 0

    if args.resolve:
        if not (args.path and args.reason):
            ap.error("--resolve には --path と --reason が必須")
        row = validate_target(args.resolve, trials)
        if args.resolve in resolutions:
            print(f"注意: run_id {args.resolve} は整理済み（{resolutions[args.resolve]['resolution_path']}）。"
                  "新行 append で上書きする（reduction 規則=最終行勝ち）")
        guarded_append([make_record(row, "USER", args.path, args.reason, args.evidence)], fingerprint)
        print(f"記帳: {row['kpi_name']} → {args.path}（rule=USER）")
        return 0

    # --apply
    wl_obs = watchlist_observation_names()
    new_records = []
    skipped = []
    for idx, row in pending_rows(trials):
        if row["run_id"] in resolutions:
            continue
        rule, path, reason, evidence = classify(idx, row, trials, wl_obs)
        if not rule:
            skipped.append(row["kpi_name"])
            continue
        new_records.append(make_record(row, rule, path, reason, evidence))
    written = guarded_append(new_records, fingerprint, skip_already_resolved=True)
    by_rule: dict[str, int] = {}
    for rec in written:
        by_rule[rec["rule"]] = by_rule.get(rec["rule"], 0) + 1
    print(f"仕分け記帳 {len(written)}件: " + " ".join(f"{k}={v}" for k, v in sorted(by_rule.items())))
    print(f"機械規則で決まらず未整理のまま（ユーザー裁定待ち）: {len(skipped)}件 {skipped}")
    print(summary_line(counts(trials, load_resolutions())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
