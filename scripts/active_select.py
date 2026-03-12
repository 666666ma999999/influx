#!/usr/bin/env python3
"""効率的な追加アノテーション対象を自動選定するスクリプト

3分類器（KW / LLM / ML）の出力を比較し、不一致度や不確実性に
基づいて優先的にアノテーションすべきツイートを選定する。

使い方:
    docker compose run --rm xstock python3 scripts/active_select.py \
        --pool output/merged_all.json \
        --gold output/annotations.json \
        --k 50 \
        --strategy mixed \
        --output output/active_selection.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone

CATEGORIES = [
    "recommended_assets", "purchased_assets", "sold_assets", "winning_trades", "ipo",
    "market_trend", "bullish_assets", "bearish_assets", "warning_signals"
]


def load_pool(pool_path: str) -> list:
    """プールツイートJSONを読み込む。

    Args:
        pool_path: ツイートJSONファイルパス

    Returns:
        ツイートのリスト
    """
    with open(pool_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_gold_urls(gold_path: str) -> set:
    """アノテーション済みURLの集合を取得する。

    Args:
        gold_path: アノテーションJSONファイルパス

    Returns:
        アノテーション済みURLのセット
    """
    if gold_path is None:
        return set()
    try:
        with open(gold_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {a["url"] for a in data.get("annotations", []) if "url" in a}
    except FileNotFoundError:
        print(f"WARN: goldファイルが見つかりません: {gold_path}", file=sys.stderr)
        return set()


def get_classifier_flags(tweet: dict, category: str) -> dict:
    """各分類器がそのカテゴリを判定しているかを取得する。

    Args:
        tweet: ツイート辞書
        category: カテゴリ名

    Returns:
        {"kw": 0|1, "llm": 0|1, "ml": 0|1} の辞書
    """
    kw_cats = tweet.get("categories", []) or []
    llm_cats = tweet.get("llm_categories", []) or []
    ml_cats = tweet.get("ml_categories", []) or []

    return {
        "kw": 1 if category in kw_cats else 0,
        "llm": 1 if category in llm_cats else 0,
        "ml": 1 if category in ml_cats else 0,
    }


def score_disagreement(tweet: dict) -> float:
    """3分類器の不一致度をスコアリングする。

    各カテゴリについて3分類器の判定(0/1)を確認し、
    全一致しない（3者一致でない）カテゴリの数をスコアとする。

    Args:
        tweet: ツイート辞書

    Returns:
        不一致カテゴリ数（0 ~ len(CATEGORIES)）
    """
    disagreement_count = 0
    for cat in CATEGORIES:
        flags = get_classifier_flags(tweet, cat)
        values = list(flags.values())
        # 全一致でなければ不一致
        if not (values[0] == values[1] == values[2]):
            disagreement_count += 1
    return float(disagreement_count)


def score_uncertainty(tweet: dict) -> float:
    """ML分類器の不確実性をスコアリングする。

    ml_confidence が 0.3〜0.7 の範囲にある場合にスコア加算。
    ml_confidence フィールドがなければ 0 を返す。

    Args:
        tweet: ツイート辞書

    Returns:
        不確実性スコア（0.0 or 1.0）
    """
    ml_conf = tweet.get("ml_confidence")
    if ml_conf is None:
        return 0.0
    if 0.3 <= ml_conf <= 0.7:
        return 1.0
    return 0.0


def score_tweet(tweet: dict, strategy: str) -> float:
    """指定された戦略に基づいてツイートをスコアリングする。

    Args:
        tweet: ツイート辞書
        strategy: "disagreement" / "uncertainty" / "mixed"

    Returns:
        優先度スコア
    """
    if strategy == "disagreement":
        return score_disagreement(tweet)
    elif strategy == "uncertainty":
        return score_uncertainty(tweet)
    else:  # mixed
        return score_disagreement(tweet) + score_uncertainty(tweet)


def select_top_k(tweets: list, k: int, strategy: str) -> list:
    """スコア上位k件のツイートを選定する。

    Args:
        tweets: 未ラベルツイートのリスト
        k: 選定数
        strategy: スコアリング戦略

    Returns:
        (tweet, score) のタプルリスト（スコア降順）
    """
    scored = []
    for tw in tweets:
        s = score_tweet(tw, strategy)
        scored.append((tw, s))

    # スコア降順でソート（同スコアの場合はURL順で安定ソート）
    scored.sort(key=lambda x: (-x[1], x[0].get("url", "")))
    return scored[:k]


def build_output(selected: list) -> dict:
    """annotator.htmlインポート互換の出力JSONを構築する。

    Args:
        selected: (tweet, score) のタプルリスト

    Returns:
        出力JSON辞書
    """
    annotations = []
    for tw, score in selected:
        annotations.append({
            "url": tw.get("url", ""),
            "human_categories": [],
            "priority_score": round(score, 4),
        })

    return {
        "annotator": "active_learning",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
        "total": len(annotations),
        "annotations": annotations,
    }


def main():
    parser = argparse.ArgumentParser(
        description="アクティブラーニング: 追加アノテーション対象を自動選定"
    )
    parser.add_argument("--pool", required=True, help="全ツイートJSONパス")
    parser.add_argument("--gold", default=None, help="既存アノテーションJSONパス")
    parser.add_argument("--k", type=int, default=50, help="選定数 (default: 50)")
    parser.add_argument(
        "--strategy", default="mixed",
        choices=["disagreement", "uncertainty", "mixed"],
        help="選定戦略 (default: mixed)"
    )
    parser.add_argument("--output", default=None, help="出力JSONパス（省略時はstdout）")
    args = parser.parse_args()

    # 1. プールツイート読み込み
    pool = load_pool(args.pool)
    print(f"プールツイート数: {len(pool)}", file=sys.stderr)

    # 2. アノテーション済みURL除外
    gold_urls = load_gold_urls(args.gold)
    if gold_urls:
        print(f"アノテーション済み: {len(gold_urls)}件", file=sys.stderr)

    unlabeled = [tw for tw in pool if tw.get("url") not in gold_urls]
    print(f"未ラベルツイート数: {len(unlabeled)}", file=sys.stderr)

    if not unlabeled:
        print("WARN: 未ラベルツイートがありません", file=sys.stderr)
        result = build_output([])
    else:
        # 3-4. スコアリング & 上位k件選定
        selected = select_top_k(unlabeled, args.k, args.strategy)
        print(
            f"選定数: {len(selected)} (戦略: {args.strategy})",
            file=sys.stderr
        )

        if selected:
            scores = [s for _, s in selected]
            print(
                f"スコア範囲: {min(scores):.2f} ~ {max(scores):.2f}",
                file=sys.stderr
            )

        result = build_output(selected)

    # 5. 出力
    output_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_text)
        print(f"出力: {args.output}", file=sys.stderr)
    else:
        print(output_text)


if __name__ == "__main__":
    main()
