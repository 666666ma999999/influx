"""Gold Set 候補サンプラー（中立性ルール準拠）。

plan.md M1 T1.6: F1 計測用の Gold Set を作成するためのサンプリングスクリプト。

中立性ルール:
1. LLM 出力を見せない: 出力 `candidates.jsonl` から `categories`/`llm_categories`/`category_count` などの
   LLM 由来フィールドを除外し、テキストのみ提示する。
2. 時期層化: 直近 6 ヶ月を月次バケットに分割してサンプリング（LLM の季節的偏向を回避）。
3. LLM 推測カテゴリで層化: LLM が各カテゴリに入れたツイートから均等にサンプリングする（カバレッジ確保）。
4. LLM 推測は別ファイル `answer_key.jsonl` に news_id 単位で保存。ラベリング後の F1 計測で再突合する
   ためにのみ使う（ラベラーは見ない）。

Usage:
    python scripts/sample_gold_set_candidates.py --per-category 5
    # -> data/gold_set/candidates.jsonl と data/gold_set/answer_key.jsonl を生成
    # 以降、人手で candidates.jsonl を読みながら data/gold_set/gold_set.jsonl にラベル付与
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

JST = timezone(timedelta(hours=9))

ALLOWED_CATEGORIES = {
    "recommended_assets", "purchased_assets", "ipo", "market_trend",
    "bullish_assets", "bearish_assets", "warning_signals",
}

CANDIDATE_FIELDS = ["news_id", "tweet_url", "username", "display_name", "posted_at", "text"]


def _month_bucket(posted_at: str) -> str:
    if not posted_at:
        return "unknown"
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return dt.strftime("%Y-%m")


def _news_id_from(tweet: Dict[str, Any]) -> str:
    url = tweet.get("url") or tweet.get("tweet_url") or ""
    if tweet.get("news_id"):
        return str(tweet["news_id"])
    # ツイート URL の末尾 ID を news_id 代替に
    if "/status/" in url:
        return url.rstrip("/").split("/status/")[-1].split("?")[0]
    import hashlib
    return hashlib.sha256((url or tweet.get("text", "")).encode()).hexdigest()[:16]


def _load_all_tweets(root: Path) -> List[Dict[str, Any]]:
    files = sorted(root.glob("*/classified_llm*.json")) + sorted(root.glob("*/tweets.json"))
    seen_ids: set = set()
    tweets: List[Dict[str, Any]] = []
    for fp in files:
        try:
            data = json.load(open(fp))
        except Exception:
            continue
        arr = data["tweets"] if isinstance(data, dict) and "tweets" in data else data
        if not isinstance(arr, list):
            continue
        for t in arr:
            if not isinstance(t, dict):
                continue
            nid = _news_id_from(t)
            if nid in seen_ids:
                continue
            seen_ids.add(nid)
            t["_news_id"] = nid
            tweets.append(t)
    return tweets


def _stratified_sample(
    tweets: List[Dict[str, Any]],
    per_category: int,
    months_cap: int,
    seed: int,
) -> List[Dict[str, Any]]:
    """カテゴリ × 月次バケットで層化サンプリング。"""
    random.seed(seed)

    # カテゴリ → 月バケット → tweet リスト
    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for t in tweets:
        cats = t.get("llm_categories") or t.get("categories") or []
        month = _month_bucket(t.get("posted_at", ""))
        for c in cats:
            if c in ALLOWED_CATEGORIES:
                buckets[c][month].append(t)

    selected: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for category in sorted(ALLOWED_CATEGORIES):
        month_dict = buckets.get(category, {})
        months = sorted(month_dict.keys(), reverse=True)[:months_cap]
        if not months:
            print(f"  [WARN] {category}: 該当ツイートなし", file=sys.stderr)
            continue
        # 月あたりの割当: per_category を月数で等分（端数は先頭月に）
        base = per_category // len(months)
        extra = per_category % len(months)
        picked = 0
        for i, m in enumerate(months):
            quota = base + (1 if i < extra else 0)
            pool = [t for t in month_dict[m] if t["_news_id"] not in seen_ids]
            if not pool:
                continue
            sample = random.sample(pool, min(quota, len(pool)))
            for t in sample:
                seen_ids.add(t["_news_id"])
                selected.append({"_sampled_category": category, "tweet": t})
                picked += 1
                if picked >= per_category:
                    break
            if picked >= per_category:
                break
        print(f"  {category}: {picked}/{per_category} 件（月次層化 {len(months)} ヶ月）")

    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Gold Set 候補サンプラー (中立性準拠)")
    parser.add_argument("--source-dir", default="output", help="分類済みツイートのルート（デフォルト: output）")
    parser.add_argument("--output-dir", default="data/gold_set", help="出力ディレクトリ")
    parser.add_argument("--per-category", type=int, default=5, help="カテゴリあたりサンプル数")
    parser.add_argument("--months-cap", type=int, default=6, help="層化対象の月数上限（直近N ヶ月）")
    parser.add_argument("--seed", type=int, default=42, help="再現性のためのシード")
    args = parser.parse_args()

    root = Path(args.source_dir).resolve()
    if not root.exists():
        print(f"ERROR: ソースディレクトリが存在しません: {root}", file=sys.stderr)
        return 1

    print(f"ツイート読み込み: {root}")
    tweets = _load_all_tweets(root)
    print(f"重複排除後ツイート: {len(tweets)} 件")

    print(f"\n層化サンプリング (per_category={args.per_category}, months_cap={args.months_cap}):")
    sampled = _stratified_sample(tweets, args.per_category, args.months_cap, args.seed)

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = out_dir / "candidates.jsonl"
    answer_key_path = out_dir / "answer_key.jsonl"

    # candidates: 中立性準拠（LLM 出力フィールドを除外）
    with open(candidates_path, "w", encoding="utf-8") as f_cand, \
         open(answer_key_path, "w", encoding="utf-8") as f_key:
        for item in sampled:
            t = item["tweet"]
            nid = t["_news_id"]
            cand = {"news_id": nid}
            for field in CANDIDATE_FIELDS[1:]:
                if field == "tweet_url":
                    cand["tweet_url"] = t.get("url") or t.get("tweet_url", "")
                else:
                    cand[field] = t.get(field, "")
            f_cand.write(json.dumps(cand, ensure_ascii=False) + "\n")
            key = {
                "news_id": nid,
                "llm_categories": t.get("llm_categories") or t.get("categories") or [],
                "sampled_from_category": item["_sampled_category"],
            }
            f_key.write(json.dumps(key, ensure_ascii=False) + "\n")

    print(f"\n候補 (中立): {candidates_path} ({len(sampled)} 件)")
    print(f"解答鍵 (LLM推測): {answer_key_path}")
    print(f"\n次のステップ:")
    print(f"  1. {candidates_path} をラベラーに渡し、{out_dir / 'gold_set.jsonl'} に正解ラベルを付与")
    print(f"  2. ラベリング完了後: F1 計測は candidates + gold_set + answer_key を使って実行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
