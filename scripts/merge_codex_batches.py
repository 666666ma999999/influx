#!/usr/bin/env python3
"""Codex MCPバッチ分類結果をマージし、classified_llm.jsonとviewer.htmlを更新する。"""

import json
import re
import sys
from pathlib import Path
from collections import Counter

# パス定義
BATCH_DIR = Path("/tmp")
BATCH_FILES = [BATCH_DIR / f"codex_batch_{i}.json" for i in range(5)]
TWEETS_PATH = Path("/Users/masaaki_nagasawa/Desktop/prm/influx/output/2026-02-19/tweets.json")
OUTPUT_PATH = Path("/Users/masaaki_nagasawa/Desktop/prm/influx/output/2026-02-19/classified_llm.json")
VIEWER_PATH = Path("/Users/masaaki_nagasawa/Desktop/prm/influx/output/viewer.html")


def load_batch_results():
    """5つのバッチファイルを読み込み、index -> 分類結果のマッピングを作成する。"""
    index_map = {}
    total_loaded = 0
    for batch_file in BATCH_FILES:
        if not batch_file.exists():
            print(f"[ERROR] バッチファイルが見つかりません: {batch_file}")
            sys.exit(1)
        with open(batch_file, "r", encoding="utf-8") as f:
            batch_data = json.load(f)
        print(f"  {batch_file.name}: {len(batch_data)}件")
        for item in batch_data:
            idx = item["index"]
            index_map[idx] = {
                "llm_categories": item.get("llm_categories", []),
                "llm_reasoning": item.get("llm_reasoning", ""),
                "llm_confidence": item.get("llm_confidence", 0.5),
            }
        total_loaded += len(batch_data)
    print(f"  合計: {total_loaded}件のバッチ結果を読み込み")
    return index_map


def merge_results(tweets, index_map):
    """バッチ結果をツイートデータにマージする。"""
    matched = 0
    unmatched = 0
    for i, tweet in enumerate(tweets):
        if i in index_map:
            tweet["llm_categories"] = index_map[i]["llm_categories"]
            tweet["llm_reasoning"] = index_map[i]["llm_reasoning"]
            tweet["llm_confidence"] = index_map[i]["llm_confidence"]
            matched += 1
        else:
            tweet["llm_categories"] = []
            tweet["llm_reasoning"] = "分類対象外"
            tweet["llm_confidence"] = 0.5
            unmatched += 1
    print(f"  マッチ: {matched}件, 未マッチ(デフォルト値): {unmatched}件")
    return tweets


def print_summary(tweets):
    """分類サマリーを表示する。"""
    category_counter = Counter()
    classified_count = 0
    unclassified_count = 0
    for tweet in tweets:
        cats = tweet.get("llm_categories", [])
        if cats:
            classified_count += 1
            for cat in cats:
                category_counter[cat] += 1
        else:
            unclassified_count += 1

    print(f"\n{'='*50}")
    print(f"分類サマリー")
    print(f"{'='*50}")
    print(f"総ツイート数: {len(tweets)}")
    print(f"分類済み: {classified_count}件")
    print(f"未分類(カテゴリなし): {unclassified_count}件")
    print(f"\nカテゴリ別件数:")
    print(f"{'-'*40}")
    for cat, count in category_counter.most_common():
        print(f"  {cat}: {count}件")
    print(f"{'-'*40}")
    print(f"  合計(延べ): {sum(category_counter.values())}件")


def update_viewer_html(tweets):
    """viewer.htmlのEMBEDDED_DATAを更新する。"""
    with open(VIEWER_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()

    # EMBEDDED_DATAのJSON文字列を生成
    json_str = json.dumps(tweets, ensure_ascii=False, indent=2)

    # パターンでEMBEDDED_DATAを置換（re.subのlambdaで生改行変換を防止）
    pattern = r"const EMBEDDED_DATA = \[.*?\];"
    replacement = f"const EMBEDDED_DATA = {json_str};"

    new_html = re.sub(pattern, lambda m: replacement, html_content, count=1, flags=re.DOTALL)

    if new_html == html_content:
        print("[ERROR] EMBEDDED_DATAの置換に失敗しました")
        sys.exit(1)

    with open(VIEWER_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"  viewer.html更新完了: {VIEWER_PATH}")


def verify_viewer_html():
    """viewer.htmlのEMBEDDED_DATAが有効なJSONか確認する。"""
    with open(VIEWER_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()

    match = re.search(r"const EMBEDDED_DATA = (\[.*?\]);", html_content, re.DOTALL)
    if not match:
        print("[ERROR] EMBEDDED_DATAが見つかりません")
        sys.exit(1)

    json_str = match.group(1)
    try:
        data = json.loads(json_str)
        print(f"  JSON検証OK: {len(data)}件のツイートデータ")
        # llm_categoriesフィールドの存在を確認
        has_llm = sum(1 for t in data if "llm_categories" in t)
        print(f"  llm_categories付き: {has_llm}件 / {len(data)}件")
        return True
    except json.JSONDecodeError as e:
        print(f"[ERROR] EMBEDDED_DATAのJSON検証失敗: {e}")
        # エラー位置の前後を表示
        pos = e.pos if hasattr(e, 'pos') else 0
        start = max(0, pos - 100)
        end = min(len(json_str), pos + 100)
        print(f"  エラー位置前後: ...{json_str[start:end]}...")
        sys.exit(1)


def main():
    print("=" * 50)
    print("Codex MCPバッチ結果マージ")
    print("=" * 50)

    # 1. バッチ結果読み込み
    print("\n[1/6] バッチファイル読み込み...")
    index_map = load_batch_results()

    # 2. 元ツイート読み込み
    print(f"\n[2/6] 元ツイートデータ読み込み...")
    with open(TWEETS_PATH, "r", encoding="utf-8") as f:
        tweets = json.load(f)
    print(f"  {len(tweets)}件のツイート")

    # 3. マージ
    print(f"\n[3/6] バッチ結果をマージ...")
    tweets = merge_results(tweets, index_map)

    # 4. 保存
    print(f"\n[4/6] classified_llm.json保存...")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)
    print(f"  保存完了: {OUTPUT_PATH}")

    # 5. サマリー表示
    print(f"\n[5/6] 分類サマリー...")
    print_summary(tweets)

    # 6. viewer.html更新
    print(f"\n[6/6] viewer.html更新...")
    update_viewer_html(tweets)

    # 7. 検証
    print(f"\n[検証] EMBEDDED_DATA JSON検証...")
    verify_viewer_html()

    print(f"\n{'='*50}")
    print("完了!")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
