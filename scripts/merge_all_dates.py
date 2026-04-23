#!/usr/bin/env python3
"""全日付ディレクトリの収集データを統合してviewer.htmlを更新するスクリプト。

手順:
1. output/ 配下の全日付ディレクトリを探索
2. classified_llm.json があればそちらを優先、なければ tweets.json を読み込む
3. URLをキーに重複排除（llm_categories がある方を優先）
4. マージ済みデータの統計を表示
5. merged_all.json に保存
6. viewer.html の EMBEDDED_DATA を更新
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


def load_tweets_from_directory(dir_path: Path) -> list:
    """日付ディレクトリからツイートを読み込む。

    classified_llm.json があればそちらを優先、なければ tweets.json を読み込む。
    """
    classified_path = dir_path / "classified_llm.json"
    tweets_path = dir_path / "tweets.json"

    if classified_path.exists():
        with open(classified_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            print(f"  {dir_path.name}: classified_llm.json から {len(data)} 件読み込み")
            return data
    elif tweets_path.exists():
        with open(tweets_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            print(f"  {dir_path.name}: tweets.json から {len(data)} 件読み込み")
            return data
    else:
        print(f"  {dir_path.name}: ツイートファイルなし（スキップ）")
        return []


def has_llm_categories(tweet: dict) -> bool:
    """ツイートにLLM分類結果があるかチェック。"""
    llm_cats = tweet.get("llm_categories")
    return llm_cats is not None and len(llm_cats) > 0


def merge_tweets(all_tweets_by_date: dict) -> list:
    """全日付のツイートをマージし、URLをキーに重複排除する。

    重複時はllm_categoriesがある方を優先する。
    """
    url_to_tweet = {}

    for date_dir, tweets in sorted(all_tweets_by_date.items()):
        for tweet in tweets:
            url = tweet.get("url", "")
            if not url:
                # URLがないツイートはそのまま追加（ユニークキーとしてテキスト+ユーザー名）
                key = f"{tweet.get('username', '')}_{tweet.get('text', '')[:50]}"
                if key not in url_to_tweet:
                    url_to_tweet[key] = tweet
                continue

            if url not in url_to_tweet:
                url_to_tweet[url] = tweet
            else:
                existing = url_to_tweet[url]
                # llm_categories がある方を優先
                if not has_llm_categories(existing) and has_llm_categories(tweet):
                    url_to_tweet[url] = tweet

    return list(url_to_tweet.values())


def print_statistics(all_tweets_by_date: dict, merged: list):
    """マージ済みデータの統計を表示する。"""
    print("\n" + "=" * 60)
    print("統計情報")
    print("=" * 60)

    # 各日付ディレクトリの元件数
    print("\n[各日付ディレクトリの元件数]")
    total_raw = 0
    for date_dir, tweets in sorted(all_tweets_by_date.items()):
        count = len(tweets)
        total_raw += count
        print(f"  {date_dir}: {count} 件")
    print(f"  合計（重複含む）: {total_raw} 件")

    # マージ後の合計
    print(f"\n[マージ後の合計（重複排除後）]: {len(merged)} 件")
    print(f"  重複排除数: {total_raw - len(merged)} 件")

    # グループ別件数
    group_counts = defaultdict(int)
    for tweet in merged:
        group = tweet.get("group", "unknown")
        group_name = tweet.get("group_name", "不明")
        group_counts[f"{group} ({group_name})"] += 1

    print("\n[グループ別件数]")
    for group, count in sorted(group_counts.items()):
        print(f"  {group}: {count} 件")

    # アカウント別件数
    account_counts = defaultdict(int)
    for tweet in merged:
        username = tweet.get("username", "unknown")
        account_counts[username] += 1

    print(f"\n[アカウント別件数] ({len(account_counts)} アカウント)")
    for username, count in sorted(account_counts.items(), key=lambda x: -x[1]):
        print(f"  @{username}: {count} 件")

    # LLM分類済みの件数
    llm_count = sum(1 for t in merged if has_llm_categories(t))
    llm_any = sum(1 for t in merged if t.get("llm_categories") is not None)
    print(f"\n[LLM分類状況]")
    print(f"  LLM分類フィールドあり: {llm_any} 件")
    print(f"  LLM分類カテゴリあり（1つ以上）: {llm_count} 件")
    print(f"  LLM未分類: {len(merged) - llm_any} 件")
    print("=" * 60)


def update_viewer_html(viewer_path: Path, merged: list):
    """viewer.htmlのEMBEDDED_DATAをマージ済みデータで更新する。"""
    with open(viewer_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    # EMBEDDED_DATAを置換
    json_str = json.dumps(merged, ensure_ascii=False, indent=2)
    replacement = f"const EMBEDDED_DATA = {json_str};"

    pattern = r'const EMBEDDED_DATA\s*=\s*\[.*?\]\s*;'
    new_html = re.sub(pattern, lambda m: replacement, html_content, flags=re.DOTALL)

    if new_html == html_content:
        print("\n[WARNING] EMBEDDED_DATA の置換パターンにマッチしませんでした！")
        return False

    with open(viewer_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"\nviewer.html を更新しました: {viewer_path}")
    return True


def validate_viewer_json(viewer_path: Path) -> bool:
    """viewer.htmlのEMBEDDED_DATA JSONが妥当か検証する。"""
    with open(viewer_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    match = re.search(r'const EMBEDDED_DATA\s*=\s*(\[.*?\])\s*;', html_content, re.DOTALL)
    if not match:
        print("[ERROR] EMBEDDED_DATA が見つかりません")
        return False

    json_str = match.group(1)
    try:
        data = json.loads(json_str)
        print(f"\n[JSON検証] OK - {len(data)} 件のツイートデータを確認")
        return True
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON パースエラー: {e}")
        return False


def main():
    output_dir = Path(__file__).resolve().parent.parent / "output"
    viewer_path = output_dir / "viewer.html"
    merged_path = output_dir / "merged_all.json"

    if not output_dir.exists():
        print(f"[ERROR] output ディレクトリが見つかりません: {output_dir}")
        sys.exit(1)

    if not viewer_path.exists():
        print(f"[ERROR] viewer.html が見つかりません: {viewer_path}")
        sys.exit(1)

    # 1. 日付ディレクトリを探索してツイートを読み込み
    print("=" * 60)
    print("全日付ディレクトリからツイートを読み込み")
    print("=" * 60)

    all_tweets_by_date = {}
    for entry in sorted(output_dir.iterdir()):
        if entry.is_dir() and re.match(r'\d{4}-\d{2}-\d{2}', entry.name):
            tweets = load_tweets_from_directory(entry)
            if tweets:
                all_tweets_by_date[entry.name] = tweets

    if not all_tweets_by_date:
        print("[ERROR] ツイートデータが見つかりませんでした")
        sys.exit(1)

    # 2. マージ（URL重複排除）
    merged = merge_tweets(all_tweets_by_date)

    # 投稿日時でソート（新しい順）
    merged.sort(key=lambda t: t.get("posted_at") or "", reverse=True)

    # 3. 統計表示
    print_statistics(all_tweets_by_date, merged)

    # 4. merged_all.json に保存
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(merged, ensure_ascii=False, indent=2, fp=f)
    print(f"\nマージ済みデータを保存しました: {merged_path}")

    # 5. viewer.html の EMBEDDED_DATA を更新
    if not update_viewer_html(viewer_path, merged):
        sys.exit(1)

    # 6. JSON妥当性検証
    if not validate_viewer_json(viewer_path):
        sys.exit(1)

    print("\n完了！")


if __name__ == "__main__":
    main()
