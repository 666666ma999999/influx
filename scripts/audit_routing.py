"""誤振り分け率の月次サンプリング監査（plan.md M6 T6.5）。

対象: `output/posting/drafts.jsonl` の全ドラフト。
監査方法: 各ドラフトの `source_items` (ツイート URL) を分類済みデータから突合し、
ツイートカテゴリ → CATEGORY_TEMPLATE_MAP の主テンプレートと draft.template_type を比較する。

Usage:
    python scripts/audit_routing.py                       # 全件集計
    python scripts/audit_routing.py --sample 30           # 30 件サンプリング
    python scripts/audit_routing.py --since 2026-04-01    # 期間絞り込み
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector.config import CATEGORY_TEMPLATE_MAP  # noqa: E402

JST = timezone(timedelta(hours=9))

NON_CATEGORY_TEMPLATES = {"win_rate_ranking", "weekly_report", "earnings_flash", "manual", "make_article"}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


def _build_tweet_category_index(output_dir: Path) -> Dict[str, List[str]]:
    """output/*/classified_llm*.json から url → [categories] マップを構築。"""
    idx: Dict[str, List[str]] = {}
    for fp in sorted(output_dir.glob("*/classified_llm*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        tweets = data.get("tweets") if isinstance(data, dict) and "tweets" in data else data
        if not isinstance(tweets, list):
            continue
        for t in tweets:
            if not isinstance(t, dict):
                continue
            url = t.get("url") or t.get("tweet_url") or ""
            if not url:
                continue
            cats = t.get("llm_categories") or t.get("categories") or []
            idx[url] = list(cats)
    return idx


def _expected_templates(categories: List[str]) -> List[str]:
    """plan.md CATEGORY_TEMPLATE_MAP から期待テンプレート集合（主のみ）を返す。"""
    return sorted({CATEGORY_TEMPLATE_MAP[c] for c in categories if c in CATEGORY_TEMPLATE_MAP})


def audit(drafts: List[Dict[str, Any]], tweet_cats: Dict[str, List[str]]) -> Dict[str, Any]:
    results = {
        "total_drafts": len(drafts),
        "evaluated": 0,
        "skipped_non_category": 0,
        "skipped_no_source": 0,
        "skipped_unknown_source": 0,
        "matches": 0,
        "mismatches": 0,
        "per_template": Counter(),
        "per_template_mismatch": Counter(),
        "samples_mismatch": [],
    }
    for d in drafts:
        tmpl = d.get("template_type", "")
        if tmpl in NON_CATEGORY_TEMPLATES or not tmpl:
            results["skipped_non_category"] += 1
            continue
        sources = d.get("source_items") or []
        if not sources:
            results["skipped_no_source"] += 1
            continue
        src_cats: List[str] = []
        missing = 0
        for url in sources:
            if url in tweet_cats:
                src_cats.extend(tweet_cats[url])
            else:
                missing += 1
        if not src_cats:
            results["skipped_unknown_source"] += 1
            continue
        expected = _expected_templates(src_cats)
        evaluated = True
        results["evaluated"] += 1
        results["per_template"][tmpl] += 1
        if tmpl in expected:
            results["matches"] += 1
        else:
            results["mismatches"] += 1
            results["per_template_mismatch"][tmpl] += 1
            if len(results["samples_mismatch"]) < 10:
                results["samples_mismatch"].append({
                    "news_id": d.get("news_id"),
                    "actual_template": tmpl,
                    "expected_templates": expected,
                    "source_categories": sorted(set(src_cats)),
                    "missing_source_urls": missing,
                })

    total = results["evaluated"]
    results["mismatch_rate"] = (results["mismatches"] / total) if total else 0.0
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="plan.md M6 T6.5: 誤振り分け率監査")
    parser.add_argument("--drafts", default="output/posting/drafts.jsonl")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD 以降のドラフトのみ")
    parser.add_argument("--sample", type=int, default=None, help="評価対象をランダムサンプリング")
    parser.add_argument("--threshold", type=float, default=0.05, help="誤振り分け率警告閾値（デフォルト 5%）")
    parser.add_argument("--report", default="output/routing_audit.json")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    drafts = _load_jsonl(Path(args.drafts))
    if args.since:
        drafts = [d for d in drafts if d.get("scheduled_at", "") >= args.since]
    if args.sample:
        random.seed(args.seed)
        drafts = random.sample(drafts, min(args.sample, len(drafts)))

    tweet_cats = _build_tweet_category_index(Path(args.output_dir).resolve())
    print(f"ドラフト: {len(drafts)} 件、ツイート分類インデックス: {len(tweet_cats)} URL")

    result = audit(drafts, tweet_cats)
    print(f"\n=== 監査結果 ===")
    print(f"  評価対象: {result['evaluated']} 件（非カテゴリ駆動スキップ {result['skipped_non_category']} / source不在 {result['skipped_no_source']} / source分類未詳 {result['skipped_unknown_source']}）")
    print(f"  一致: {result['matches']} / 不一致: {result['mismatches']}")
    print(f"  誤振り分け率: {result['mismatch_rate']:.2%} (閾値 {args.threshold:.0%})")

    print(f"\n=== テンプレート別 ===")
    for t, n in result["per_template"].most_common():
        bad = result["per_template_mismatch"].get(t, 0)
        print(f"  {t:20s}  n={n}  mismatch={bad} ({bad*100//n if n else 0}%)")

    if result["samples_mismatch"]:
        print(f"\n=== 不一致サンプル (最大 10 件) ===")
        for s in result["samples_mismatch"]:
            print(f"  news_id={s['news_id']} actual={s['actual_template']} expected={s['expected_templates']}")
            print(f"    source_cats={s['source_categories']}")

    gate_pass = result["mismatch_rate"] < args.threshold
    print(f"\n判定: {'✓ PASS' if gate_pass else '✗ FAIL'} (mismatch_rate < {args.threshold:.0%})")

    # 結果保存（per_template Counter は dict に変換）
    out = {
        "measured_at": datetime.now(JST).isoformat(),
        "threshold": args.threshold,
        "gate": "pass" if gate_pass else "fail",
        **result,
        "per_template": dict(result["per_template"]),
        "per_template_mismatch": dict(result["per_template_mismatch"]),
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果保存: {args.report}")
    return 0 if gate_pass else 2


if __name__ == "__main__":
    sys.exit(main())
