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
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

JST = timezone(timedelta(hours=9))

ALLOWED_CATEGORIES = {
    "recommended_assets", "purchased_assets", "ipo", "market_trend",
    "bullish_assets", "bearish_assets", "warning_signals",
}

CANDIDATE_FIELDS = ["news_id", "tweet_url", "username", "display_name", "posted_at", "text"]

# `_STATUS_ID` と `_TWITTER_HOSTS` は apply_human_annotations.py と同一定義を lockstep で維持する
# （candidates と human_annotations の突合は両側で同じ正規化をしないと overlap が取れない）。
# anchored 形式により `/settings/.../status/1`・`/foo/with_replies/status/1` 等の誤マッチを避ける。
_STATUS_ID = re.compile(
    r"^/(?:i/web|@?[^/]+)/status/(\d+)(?:/(?:photo|video)/\d+)?/?$"
)
_TWITTER_HOSTS = {
    "twitter.com", "x.com",
    "mobile.twitter.com", "mobile.x.com",
    "www.twitter.com", "www.x.com",
}


def _extract_tweet_id(url: Any) -> str:
    """ツイート URL から tweet ID を抽出。非文字列・非 Twitter ホスト・変則パスは空文字。"""
    if not isinstance(url, str) or not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if (parsed.netloc or "").lower() not in _TWITTER_HOSTS:
        return ""
    m = _STATUS_ID.search(parsed.path or "")
    return m.group(1) if m else ""


def _load_annotated_ids(path: Path) -> Set[str]:
    """human_annotations.json から annotator='human' 確認後、tweet ID 集合を返す。

    annotator 非 human は fail-fast（中立性前提保護）。schema 逸脱（top-level/entry が dict でない）
    も ValueError に正規化する。読み込み失敗は OSError を raise。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(
            f"human_annotations.json の top-level が dict ではありません: type={type(data).__name__}"
        )
    annotator = data.get("annotator")
    if annotator != "human":
        raise ValueError(
            f"annotator='{annotator}' は受け付けません（'human' のみ許可）"
        )
    annotations = data.get("annotations", [])
    if not isinstance(annotations, list):
        raise ValueError(
            f"annotations は list である必要があります: type={type(annotations).__name__}"
        )
    ids: Set[str] = set()
    missing = 0
    for a in annotations:
        if not isinstance(a, dict):
            missing += 1
            continue
        tid = _extract_tweet_id(a.get("url", ""))
        if tid:
            ids.add(tid)
        else:
            missing += 1
    if missing:
        print(
            f"[WARN] human_annotations.json の {missing} 件で tweet ID を抽出できず除外",
            file=sys.stderr,
        )
    return ids


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
    seen_ids: Set[str] = set()
    tweets: List[Dict[str, Any]] = []
    skipped = 0
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[WARN] 読み込み失敗 {fp}: {e}", file=sys.stderr)
            skipped += 1
            continue
        arr = data["tweets"] if isinstance(data, dict) and "tweets" in data else data
        if not isinstance(arr, list):
            print(f"[WARN] 形式不正 {fp}: tweets 配列なし", file=sys.stderr)
            skipped += 1
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
    if skipped:
        print(f"[WARN] 合計 {skipped} ファイルをスキップ", file=sys.stderr)
    return tweets


def _stratified_sample(
    tweets: List[Dict[str, Any]],
    per_category: int,
    months_cap: int,
    seed: int,
    preferred_ids: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """カテゴリ × 月次バケットで層化サンプリング。

    `preferred_ids` 指定時:
    - バケット内順序: ID 一致ツイートを優先、不足分を残りから充当
    - 月順序: `months_cap` ウィンドウ内の月集合は不変だが、annotated 件数降順で並べ替え
      （同数は新しい月優先）。`extra = per_category % len(months)` 分の追加 quota は
      annotated 件数の多い月へ寄る。`months_cap` 範囲内での偏りに留まり季節的偏向は
      回避しつつ、overlap を最大化するための意図的なトレードオフ。
    """
    random.seed(seed)

    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for t in tweets:
        cats = t.get("llm_categories") or t.get("categories") or []
        month = _month_bucket(t.get("posted_at", ""))
        for c in cats:
            if c in ALLOWED_CATEGORIES:
                buckets[c][month].append(t)

    selected: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    preferred_hit = 0
    prefer_mode = preferred_ids is not None

    for category in sorted(ALLOWED_CATEGORIES):
        month_dict = buckets.get(category, {})
        months_window = sorted(month_dict.keys(), reverse=True)[:months_cap]
        if not months_window:
            print(f"  [WARN] {category}: 該当ツイートなし", file=sys.stderr)
            continue
        if prefer_mode:
            # 層化窓 (months_cap) は維持したまま、overlap 最大化のため
            # annotated 件数降順で月を並べ替える（同数は新しい月優先）。
            months = sorted(
                months_window,
                key=lambda m: (
                    -sum(1 for t in month_dict[m] if t["_news_id"] in preferred_ids),
                    months_window.index(m),
                ),
            )
        else:
            months = months_window
        base = per_category // len(months)
        extra = per_category % len(months)
        picked = 0
        pref_in_cat = 0
        for i, m in enumerate(months):
            quota = base + (1 if i < extra else 0)
            pool = [t for t in month_dict[m] if t["_news_id"] not in seen_ids]
            if not pool:
                continue
            if prefer_mode:
                pref = [t for t in pool if t["_news_id"] in preferred_ids]
                rest = [t for t in pool if t["_news_id"] not in preferred_ids]
                random.shuffle(pref)
                random.shuffle(rest)
                ordered = pref + rest
                sample = ordered[:min(quota, len(ordered))]
            else:
                sample = random.sample(pool, min(quota, len(pool)))
            for t in sample:
                seen_ids.add(t["_news_id"])
                if prefer_mode and t["_news_id"] in preferred_ids:
                    pref_in_cat += 1
                selected.append({"_sampled_category": category, "tweet": t})
                picked += 1
                if picked >= per_category:
                    break
            if picked >= per_category:
                break
        preferred_hit += pref_in_cat
        if prefer_mode:
            print(f"  {category}: {picked}/{per_category} 件（月次層化 {len(months)} ヶ月 / annotated {pref_in_cat}）")
        else:
            print(f"  {category}: {picked}/{per_category} 件（月次層化 {len(months)} ヶ月）")

    if prefer_mode:
        print(f"\n  annotated overlap 合計: {preferred_hit}/{len(selected)} 件")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Gold Set 候補サンプラー (中立性準拠)")
    parser.add_argument("--source-dir", default="output", help="分類済みツイートのルート（デフォルト: output）")
    parser.add_argument("--output-dir", default="data/gold_set", help="出力ディレクトリ")
    parser.add_argument("--per-category", type=int, default=5, help="カテゴリあたりサンプル数")
    parser.add_argument("--months-cap", type=int, default=6, help="層化対象の月数上限（直近N ヶ月）")
    parser.add_argument("--seed", type=int, default=42, help="再現性のためのシード")
    parser.add_argument("--prefer-annotated", action="store_true",
                        help="human_annotations.json で人手ラベル済みのツイートを優先（層化内順序のみ）")
    parser.add_argument("--annotations-path", default="output/human_annotations.json",
                        help="--prefer-annotated 指定時の annotator='human' ファイル")
    args = parser.parse_args()

    root = Path(args.source_dir).resolve()
    if not root.exists():
        print(f"ERROR: ソースディレクトリが存在しません: {root}", file=sys.stderr)
        return 1

    preferred_ids: Optional[Set[str]] = None
    if args.prefer_annotated:
        ann_path = Path(args.annotations_path).resolve()
        try:
            preferred_ids = _load_annotated_ids(ann_path)
        except (OSError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"annotated 優先 ON: {ann_path} から {len(preferred_ids)} 件の tweet ID を取得")

    print(f"ツイート読み込み: {root}")
    tweets = _load_all_tweets(root)
    print(f"重複排除後ツイート: {len(tweets)} 件")

    print(f"\n層化サンプリング (per_category={args.per_category}, months_cap={args.months_cap}):")
    sampled = _stratified_sample(
        tweets, args.per_category, args.months_cap, args.seed,
        preferred_ids=preferred_ids,
    )

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
