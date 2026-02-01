#!/usr/bin/env python3
"""
ツイート収集スクリプト
インフルエンサーの投稿を収集し、分類してファイルに保存する
"""

import sys
import argparse
import json
from pathlib import Path
from datetime import datetime

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from collector.x_collector import SafeXCollector, collect_all_groups
from collector.classifier import TweetClassifier, generate_news_data
from collector.config import (
    SEARCH_URLS,
    INFLUENCER_GROUPS,
    PROFILE_PATH,
    OUTPUT_DIR,
    DATA_DIR
)


def parse_args():
    """コマンドライン引数をパース"""
    parser = argparse.ArgumentParser(
        description='X投稿収集ツール',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 全グループを収集
  python scripts/collect_tweets.py

  # 特定グループのみ収集
  python scripts/collect_tweets.py --groups group1

  # スクロール回数を指定
  python scripts/collect_tweets.py --scrolls 5

  # JSON出力のみ（CSV出力しない）
  python scripts/collect_tweets.py --no-csv
        """
    )

    parser.add_argument(
        '--groups', '-g',
        nargs='+',
        choices=['group1', 'group2', 'group3', 'all'],
        default=['all'],
        help='収集するグループ (default: all)'
    )

    parser.add_argument(
        '--scrolls', '-s',
        type=int,
        default=10,
        help='スクロール回数 (default: 10)'
    )

    parser.add_argument(
        '--no-csv',
        action='store_true',
        help='CSV出力をスキップ'
    )

    parser.add_argument(
        '--no-classify',
        action='store_true',
        help='分類処理をスキップ'
    )

    parser.add_argument(
        '--profile', '-p',
        type=str,
        default=PROFILE_PATH,
        help=f'ブラウザプロファイルのパス (default: {PROFILE_PATH})'
    )

    parser.add_argument(
        '--output', '-o',
        type=str,
        default=OUTPUT_DIR,
        help=f'出力ディレクトリ (default: {OUTPUT_DIR})'
    )

    return parser.parse_args()


def main():
    """メイン処理"""
    args = parse_args()

    # プロファイル存在確認
    profile_path = Path(args.profile).resolve()
    if not profile_path.exists():
        print("[エラー] ブラウザプロファイルが見つかりません。")
        print("先に以下のコマンドでセットアップしてください:")
        print()
        print("  python scripts/setup_profile.py")
        print()
        sys.exit(1)

    # 収集対象グループを決定
    if 'all' in args.groups:
        groups = list(SEARCH_URLS.keys())
    else:
        groups = args.groups

    print()
    print("=" * 60)
    print("X投稿収集システム")
    print("=" * 60)
    print(f"収集対象: {', '.join(groups)}")
    print(f"スクロール回数: {args.scrolls}")
    print(f"プロファイル: {profile_path}")
    print("=" * 60)
    print()

    # 収集実行
    all_tweets = []

    for group_key in groups:
        group_info = INFLUENCER_GROUPS.get(group_key, {})
        group_name = group_info.get('name', group_key)

        search_url = SEARCH_URLS[group_key]

        collector = SafeXCollector(profile_path=str(profile_path))

        tweets = collector.collect(
            search_url=search_url,
            max_scrolls=args.scrolls,
            group_name=f"{group_key} ({group_name})"
        )

        # グループ情報を追加
        for tweet in tweets:
            tweet['group'] = group_key
            tweet['group_name'] = group_name
            tweet['is_contrarian'] = group_info.get('is_contrarian', False)

        all_tweets.extend(tweets)

        # グループ間の待機（最後以外）
        if group_key != groups[-1]:
            import time
            import random
            wait_time = random.uniform(30, 60)
            print(f"\n次のグループまで {wait_time:.0f}秒 待機...")
            time.sleep(wait_time)

    if not all_tweets:
        print("\n[警告] ツイートが収集できませんでした。")
        print("ログイン状態を確認してください。")
        sys.exit(1)

    # 分類処理
    if not args.no_classify:
        print("\n分類処理中...")
        classifier = TweetClassifier()
        all_tweets = classifier.classify_all(all_tweets)
        classifier.print_summary(all_tweets)

    # 出力ディレクトリ準備
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON保存
    json_filename = f"tweets_{timestamp}.json"
    json_path = output_dir / json_filename

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_tweets, f, ensure_ascii=False, indent=2)
    print(f"\nJSON保存: {json_path}")

    # CSV保存
    if not args.no_csv:
        import csv

        csv_filename = f"tweets_{timestamp}.csv"
        csv_path = output_dir / csv_filename

        # CSVに適したフラット化
        csv_data = []
        for tweet in all_tweets:
            row = {
                'group': tweet.get('group', ''),
                'group_name': tweet.get('group_name', ''),
                'username': tweet.get('username', ''),
                'display_name': tweet.get('display_name', ''),
                'text': tweet.get('text', '').replace('\n', ' '),
                'url': tweet.get('url', ''),
                'posted_at': tweet.get('posted_at', ''),
                'like_count': tweet.get('like_count', ''),
                'retweet_count': tweet.get('retweet_count', ''),
                'categories': ','.join(tweet.get('categories', [])),
                'is_contrarian': tweet.get('is_contrarian', False),
                'collected_at': tweet.get('collected_at', '')
            }
            csv_data.append(row)

        if csv_data:
            fieldnames = list(csv_data[0].keys())
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(csv_data)
            print(f"CSV保存: {csv_path}")

    # ニュースデータ生成
    if not args.no_classify:
        news_data = generate_news_data(all_tweets)
        news_filename = f"news_{timestamp}.json"
        news_path = output_dir / news_filename

        with open(news_path, 'w', encoding='utf-8') as f:
            json.dump(news_data, f, ensure_ascii=False, indent=2)
        print(f"ニュースデータ保存: {news_path}")

    print()
    print("=" * 60)
    print("収集完了!")
    print("=" * 60)
    print(f"総ツイート数: {len(all_tweets)}")
    print()


if __name__ == "__main__":
    main()
