"""LLM 分類精度 F1 計測スクリプト（plan.md M2 T2.0）。

Gold Set (`data/gold_set/gold_set.jsonl`) に対し、LLM 出力 (`answer_key.jsonl`) を突合して
7 カテゴリ macro/micro F1 を算出する。M2 着手ゲート（macro F1 ≥ 0.80）の判定用。

Usage:
    python scripts/measure_f1.py
    python scripts/measure_f1.py --gold-dir data/gold_set --output output/f1_baseline.json
    python scripts/measure_f1.py --min-labels 5  # カテゴリあたり最低サンプル数
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

JST = timezone(timedelta(hours=9))

ALLOWED_CATEGORIES = [
    "recommended_assets", "purchased_assets", "ipo", "market_trend",
    "bullish_assets", "bearish_assets", "warning_signals",
]


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    if not path.exists():
        return recs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[WARN] JSON decode error in {path}", file=sys.stderr)
    return recs


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval (95% CI)。n=0 のときは (0, 1) を返す。"""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def compute_f1(
    gold: List[Dict[str, Any]], llm_by_id: Dict[str, List[str]]
) -> Dict[str, Any]:
    """macro/micro F1 + per-category precision/recall/F1 を算出する。"""
    tp: Dict[str, int] = defaultdict(int)
    fp: Dict[str, int] = defaultdict(int)
    fn: Dict[str, int] = defaultdict(int)
    support: Dict[str, int] = defaultdict(int)
    missing_llm = 0
    evaluated = 0

    for rec in gold:
        nid = rec.get("news_id")
        true_labels = set(rec.get("labels") or [])
        if nid not in llm_by_id:
            missing_llm += 1
            continue
        evaluated += 1
        pred_labels = set(llm_by_id[nid]) & set(ALLOWED_CATEGORIES)
        for cat in ALLOWED_CATEGORIES:
            if cat in true_labels:
                support[cat] += 1
            if cat in true_labels and cat in pred_labels:
                tp[cat] += 1
            elif cat in pred_labels:
                fp[cat] += 1
            elif cat in true_labels:
                fn[cat] += 1

    per_cat: Dict[str, Dict[str, Any]] = {}
    f1_values: List[float] = []
    for cat in ALLOWED_CATEGORIES:
        t, f_p, f_n = tp[cat], fp[cat], fn[cat]
        precision = t / (t + f_p) if (t + f_p) > 0 else 0.0
        recall = t / (t + f_n) if (t + f_n) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        # Wilson CI は recall（TP / (TP + FN)）の 95% 区間を表す（F1 自体の CI ではない点に注意）
        # 将来: F1 の正確な CI が必要になったらブートストラップに差し替え
        if t + f_n > 0:
            ci_lo, ci_hi = _wilson_ci(t, t + f_n)
        else:
            ci_lo, ci_hi = (0.0, 1.0)
        per_cat[cat] = {
            "tp": t, "fp": f_p, "fn": f_n,
            "support": support[cat],
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "recall_95ci": [round(ci_lo, 4), round(ci_hi, 4)],
        }
        f1_values.append(f1)

    total_tp = sum(tp.values())
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    micro_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    micro_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) > 0 else 0.0
    macro_f1 = sum(f1_values) / len(f1_values) if f1_values else 0.0

    return {
        "evaluated": evaluated,
        "missing_llm": missing_llm,
        "macro_f1": round(macro_f1, 4),
        "micro_f1": round(micro_f1, 4),
        "micro_precision": round(micro_p, 4),
        "micro_recall": round(micro_r, 4),
        "per_category": per_cat,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="plan.md M2 T2.0: Gold Set 基準 F1 計測")
    parser.add_argument("--gold-dir", default="data/gold_set", help="Gold Set ディレクトリ")
    parser.add_argument("--output", default="output/f1_baseline.json", help="結果 JSON 出力先")
    parser.add_argument("--min-labels", type=int, default=5, help="カテゴリあたり最低 support 件数（警告閾値）")
    parser.add_argument("--gate", type=float, default=0.80, help="M2 着手ゲート閾値 (macro F1)")
    args = parser.parse_args()

    gold_dir = Path(args.gold_dir).resolve()
    gold_path = gold_dir / "gold_set.jsonl"
    key_path = gold_dir / "answer_key.jsonl"

    gold = _load_jsonl(gold_path)
    if not gold:
        print(f"ERROR: gold_set.jsonl が空または不在: {gold_path}", file=sys.stderr)
        print("ラベリングを先に行ってください（data/gold_set/README.md 参照）", file=sys.stderr)
        return 1

    keys = _load_jsonl(key_path)
    llm_by_id = {r["news_id"]: r.get("llm_categories") or [] for r in keys if r.get("news_id")}

    print(f"Gold Set: {len(gold)} 件 / LLM 推測: {len(keys)} 件")
    result = compute_f1(gold, llm_by_id)

    print(f"\n=== F1 結果 ===")
    print(f"評価済み: {result['evaluated']} 件 / LLM推測欠落: {result['missing_llm']}")
    print(f"macro F1: {result['macro_f1']} | micro F1: {result['micro_f1']}")
    print(f"micro precision: {result['micro_precision']} | micro recall: {result['micro_recall']}")
    print(f"\n=== カテゴリ別 ===")
    print(f"  {'category':22s}  P      R      F1     R 95%CI         support")
    for cat, stats in result["per_category"].items():
        lo, hi = stats["recall_95ci"]
        sup = stats["support"]
        warn = " [少]" if sup < args.min_labels else ""
        print(
            f"  {cat:22s}  {stats['precision']:.3f}  {stats['recall']:.3f}  {stats['f1']:.3f}  "
            f"[{lo:.2f},{hi:.2f}]    {sup}{warn}"
        )

    # M2 着手ゲート判定
    macro = result["macro_f1"]
    print(f"\n=== M2 着手ゲート (macro F1 >= {args.gate}) ===")
    if macro >= args.gate:
        print(f"  ✓ PASS (macro F1 = {macro} >= {args.gate})")
        gate_status = "pass"
    else:
        print(f"  ✗ FAIL (macro F1 = {macro} < {args.gate})")
        print(f"  → plan.md M2 着手ゲートのフォールバック戦略を参照:")
        print(f"    1 回目未達: Few-shot 5-10 件追加 / Gold Set 50 件化")
        print(f"    2 回目未達: ルールベース強化 / prompt 改善")
        print(f"    3 回目未達: モデル変更 / Gold Set 100 件化")
        print(f"    4 回目未達: ゲート緩和判断")
        gate_status = "fail"

    # 書き込み
    out = {
        "measured_at": datetime.now(JST).isoformat(),
        "gold_size": len(gold),
        "gate_threshold": args.gate,
        "gate_status": gate_status,
        **result,
    }
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果保存: {out_path}")
    return 0 if gate_status == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
