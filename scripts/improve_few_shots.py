#!/usr/bin/env python3
"""手動アノテーションからfew-shot事例を自動改善する"""

import argparse
import json
import shutil
from pathlib import Path
from collections import defaultdict

CATEGORIES = [
    "recommended_assets", "purchased_assets", "sold_assets", "winning_trades", "ipo",
    "market_trend", "bullish_assets", "bearish_assets", "warning_signals"
]


def load_annotations(gold_path, tweets_path):
    """アノテーションとツイートを結合"""
    with open(gold_path, "r", encoding="utf-8") as f:
        ann_data = json.load(f)
    with open(tweets_path, "r", encoding="utf-8") as f:
        tweets = json.load(f)

    url_to_tweet = {tw["url"]: tw for tw in tweets}

    annotated = []
    for a in ann_data.get("annotations", []):
        url = a["url"]
        cats = a.get("human_categories", [])
        if url in url_to_tweet and cats:
            tw = url_to_tweet[url]
            annotated.append({
                "url": url,
                "text": tw.get("text", ""),
                "is_contrarian": tw.get("is_contrarian", False),
                "human_categories": cats,
                "llm_categories": tw.get("llm_categories", []),
                "llm_reasoning": tw.get("llm_reasoning", ""),
                "kw_categories": tw.get("categories", [])
            })
    return annotated


def load_few_shots(path):
    """既存few-shot読み込み"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def select_candidates(annotated, existing_texts, max_per_cat=3):
    """高品質な事例を選定"""
    candidates = defaultdict(list)

    for item in annotated:
        text = item["text"]
        cats = item["human_categories"]

        # フィルタ: 単一カテゴリ、適切な長さ、重複なし
        if len(cats) != 1:
            continue
        if len(text) < 50 or len(text) > 200:
            continue
        if any(text[:40] in et for et in existing_texts):
            continue

        cat = cats[0]
        if cat not in CATEGORIES:
            continue

        llm_match = set(item["llm_categories"]) == set(cats)
        reasoning = item["llm_reasoning"] if item["llm_reasoning"] else "手動分類による教師データ"

        candidates[cat].append({
            "text": text,
            "is_contrarian": item["is_contrarian"],
            "categories": cats,
            "reasoning": reasoning,
            "llm_match": llm_match,
            "priority": 0 if not llm_match else 1  # LLM不一致を優先
        })

    # 各カテゴリでソート＆上位選定
    selected = {}
    for cat in CATEGORIES:
        cands = candidates.get(cat, [])
        cands.sort(key=lambda x: x["priority"])
        selected[cat] = cands[:max_per_cat]

    return selected


def main():
    parser = argparse.ArgumentParser(description="手動アノテーションからfew-shot事例を改善")
    parser.add_argument("--gold", required=True, help="手動アノテーションJSONパス")
    parser.add_argument("--tweets", default="output/merged_all.json", help="ツイートJSONパス")
    parser.add_argument("--few-shot", default="data/few_shot_examples.json", help="既存few-shotファイル")
    parser.add_argument("--output", default=None, help="出力ファイル（デフォルト: --few-shotと同じ）")
    parser.add_argument("--max-per-category", type=int, default=3, help="カテゴリあたり最大追加数")
    parser.add_argument("--dry-run", action="store_true", help="変更内容を表示のみ")
    args = parser.parse_args()

    output_path = args.output or args.few_shot

    print("=== Few-shot事例 自動改善 ===")

    # 読み込み
    annotated = load_annotations(args.gold, args.tweets)
    few_shot_data = load_few_shots(args.few_shot)
    existing_examples = few_shot_data.get("examples", [])
    existing_texts = [ex["text"] for ex in existing_examples]

    print(f"アノテーション件数: {len(annotated)}")
    print(f"既存few-shot件数: {len(existing_examples)}")

    # カテゴリ別の既存数
    existing_counts = defaultdict(int)
    for ex in existing_examples:
        for cat in ex.get("categories", []):
            existing_counts[cat] += 1
    print(f"既存カテゴリ別: {dict(existing_counts)}")

    # 候補選定
    selected = select_candidates(annotated, existing_texts, args.max_per_category)

    # 結果表示
    total_added = 0
    new_examples = list(existing_examples)

    for cat in CATEGORIES:
        cands = selected.get(cat, [])
        if not cands:
            print(f"\n  {cat}: 追加候補なし")
            continue

        print(f"\n  {cat} ({existing_counts[cat]}件 → +{len(cands)}件):")
        for c in cands:
            prefix = "★LLM不一致" if not c["llm_match"] else "　LLM一致"
            print(f"    {prefix}: {c['text'][:50]}...")
            if not args.dry_run:
                new_examples.append({
                    "text": c["text"],
                    "is_contrarian": c["is_contrarian"],
                    "categories": c["categories"],
                    "reasoning": c["reasoning"]
                })
                total_added += 1

    if args.dry_run:
        print(f"\n[DRY RUN] 追加予定: {sum(len(v) for v in selected.values())}件")
        return

    if total_added == 0:
        print("\n追加する事例がありません")
        return

    # バックアップ
    backup_path = args.few_shot.replace(".json", ".backup.json")
    shutil.copy2(args.few_shot, backup_path)
    print(f"\nバックアップ: {backup_path}")

    # 保存
    few_shot_data["examples"] = new_examples
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(few_shot_data, f, ensure_ascii=False, indent=2)

    print(f"更新完了: {len(existing_examples)}件 → {len(new_examples)}件 (+{total_added}件)")
    print(f"出力: {output_path}")


if __name__ == "__main__":
    main()
