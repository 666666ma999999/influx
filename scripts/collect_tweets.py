#!/usr/bin/env python3
"""
ツイート収集スクリプト
インフルエンサーの投稿を収集し、分類してファイルに保存する
バッチ分割・インターリーブ・自動リトライによるブロック回避対応
"""

import sys
import re
import argparse
import json
import time
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# プロジェクトルートをパスに追加
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from collector.x_collector import SafeXCollector, CollectionResult
from collector.classifier import TweetClassifier, generate_news_data
from collector.config import (
    SEARCH_URLS,
    INFLUENCER_GROUPS,
    PROFILE_PATH,
    OUTPUT_DIR,
    DATA_DIR,
    COLLECTION_SETTINGS,
    BATCH_SETTINGS,
    BLOCK_ERROR_PATTERNS,
    CollectTask,
    build_collect_tasks,
)
from collector.inactive_checker import run_inactive_check, detect_inactive_accounts


def _emit_collection_metrics(
    all_tweets: list, output_dir: Path, date_str: str,
    min_tweets_per_day: int = 100, min_active_accounts: int = 20,
) -> dict:
    """日次収集メトリクスを output/collection_metrics.jsonl に追記する。

    plan.md M1 T1.7: 日次合計・アカウント別・カテゴリ別件数を記録し、
    閾値割れ時に stderr 警告を出して未達を運用者に可視化する。

    Args:
        all_tweets: 収集ツイート list
        output_dir: 出力ルート（output/）
        date_str: 集計日 YYYY-MM-DD
        min_tweets_per_day: 日次収集量の閾値（デフォルト 100）
        min_active_accounts: アクティブアカウント数の閾値（デフォルト 20）

    Returns:
        メトリクス dict（追記内容と同じ）
    """
    from collections import Counter

    per_account = Counter(t.get("username", "unknown") for t in all_tweets)
    per_category: Counter = Counter()
    for t in all_tweets:
        for cat in t.get("categories") or []:
            per_category[cat] += 1
        for cat in t.get("llm_categories") or []:
            per_category[cat] += 1

    warnings = []
    if len(all_tweets) < min_tweets_per_day:
        warnings.append(
            f"total_tweets={len(all_tweets)} < {min_tweets_per_day} (前提未達)"
        )
    if len(per_account) < min_active_accounts:
        warnings.append(
            f"active_accounts={len(per_account)} < {min_active_accounts} (前提未達)"
        )

    metrics = {
        "date": date_str,
        "collected_at": datetime.now(JST).isoformat(),
        "total_tweets": len(all_tweets),
        "active_accounts": len(per_account),
        "per_account": dict(per_account.most_common()),
        "per_category": dict(per_category),
        "thresholds": {
            "min_tweets_per_day": min_tweets_per_day,
            "min_active_accounts": min_active_accounts,
        },
        "warnings": warnings,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "collection_metrics.jsonl"
    with open(metrics_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(metrics, ensure_ascii=False) + "\n")

    if warnings:
        for w in warnings:
            print(f"[WARNING] 収集量モニタリング: {w}", file=sys.stderr)
        print(
            f"[INFO] 詳細: {metrics_path}（plan.md M1 T1.7 のベースライン確認手順を参照）",
            file=sys.stderr,
        )

    return metrics


def _execute_collect_task(task: CollectTask, profile_path: str, max_scrolls: int,
                          shared_collected_urls: set) -> CollectionResult:
    """単一タスクの収集実行"""
    collector = SafeXCollector(
        profile_path=profile_path,
        shared_collected_urls=shared_collected_urls
    )
    sub_name = f"{task.group_key} ({task.group_name}) [{task.url_type}]"
    result = collector.collect(
        search_url=task.search_url,
        max_scrolls=max_scrolls,
        group_name=sub_name
    )
    return result


def _append_tweets_with_group_info(all_tweets: list, result: CollectionResult, task: CollectTask):
    """グループ情報を付与してall_tweetsに追加"""
    for tweet in result.tweets:
        tweet['group'] = task.group_key
        tweet['group_name'] = task.group_name
        tweet['is_contrarian'] = task.is_contrarian
    all_tweets.extend(result.tweets)


def _get_retry_wait(task: CollectTask) -> int:
    """エラー種別に応じた待機秒数を取得"""
    # ブロック系は長めに待機
    if task.status == "blocked":
        return BATCH_SETTINGS["block_cooldown_sec"]
    # リトライ回数に応じた段階的バックオフ
    retry_waits = BATCH_SETTINGS["retry_wait_sec"]
    idx = min(task.retries, len(retry_waits) - 1)
    return retry_waits[idx]


def _countdown_wait(seconds: int, label: str = "待機中"):
    """カウントダウン表示付き待機"""
    for remaining in range(seconds, 0, -1):
        mins, secs = divmod(remaining, 60)
        sys.stdout.write(f"\r{label}: {mins:02d}:{secs:02d} 残り")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write(f"\r{label}: 完了{'':20}\n")
    sys.stdout.flush()


def _print_collection_summary(tasks: list):
    """結果サマリー表示"""
    completed = [t for t in tasks if t.status == "completed"]
    failed = [t for t in tasks if t.status == "failed"]
    blocked = [t for t in tasks if t.status == "blocked"]
    pending = [t for t in tasks if t.status == "pending"]

    print("\n" + "=" * 60)
    print("収集結果サマリー")
    print("=" * 60)
    print(f"  完了: {len(completed)}/{len(tasks)}")
    if failed:
        print(f"  失敗: {len(failed)}")
        for t in failed:
            print(f"    - {t.group_key} [{t.url_type}]: {t.error_message[:60]}")
    if blocked:
        print(f"  ブロック: {len(blocked)}")
        for t in blocked:
            print(f"    - {t.group_key} [{t.url_type}]: {t.error_message[:60]}")
    if pending:
        print(f"  未処理: {len(pending)}")
    print("=" * 60)


def parse_args():
    """コマンドライン引数をパース"""
    parser = argparse.ArgumentParser(
        description='X投稿収集ツール（バッチ分割・自動リトライ対応）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 全グループを収集
  python scripts/collect_tweets.py

  # 特定グループのみ収集
  python scripts/collect_tweets.py --groups group1

  # スクロール回数を指定
  python scripts/collect_tweets.py --scrolls 5

  # バッチサイズとクールダウンを指定
  python scripts/collect_tweets.py --batch-size 5 --cooldown 600

  # JSON出力のみ（CSV出力しない）
  python scripts/collect_tweets.py --no-csv
        """
    )

    parser.add_argument(
        '--groups', '-g',
        nargs='+',
        choices=['group1', 'group2', 'group3', 'group4', 'group5', 'group6', 'all'],
        default=['all'],
        help='収集するグループ (default: all)'
    )

    parser.add_argument(
        '--scrolls', '-s',
        type=int,
        default=20,
        help='スクロール回数 (default: 20)'
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

    parser.add_argument(
        '--batch-size', '-b',
        type=int,
        default=BATCH_SETTINGS["batch_size"],
        help=f'バッチあたりURL数 (default: {BATCH_SETTINGS["batch_size"]})'
    )

    parser.add_argument(
        '--cooldown',
        type=int,
        default=BATCH_SETTINGS["cooldown_sec"],
        help=f'バッチ間クールダウン秒 (default: {BATCH_SETTINGS["cooldown_sec"]})'
    )

    parser.add_argument(
        '--no-interleave',
        action='store_true',
        help='インターリーブ無効化（グループ順に処理）'
    )

    parser.add_argument(
        '--no-retry',
        action='store_true',
        help='自動リトライ無効化'
    )

    parser.add_argument(
        '--check-inactive',
        action='store_true',
        help='収集前に非活動アカウントを自動検出して除外'
    )

    parser.add_argument(
        '--inactive-days',
        type=int,
        default=30,
        help='非活動判定の閾値日数 (default: 30)'
    )

    parser.add_argument(
        '--no-inactive-cache',
        action='store_true',
        help='非活動チェックのキャッシュを使わず再チェック'
    )

    parser.add_argument(
        '--since',
        type=str,
        default=None,
        help='検索開始日 (YYYY-MM-DD形式)'
    )

    parser.add_argument(
        '--until',
        type=str,
        default=None,
        help='検索終了日 (YYYY-MM-DD形式、排他的)'
    )

    parser.add_argument(
        '--split-per-account',
        action='store_true',
        help='アカウントごとに個別検索URL生成（引用ツイートの取りこぼし防止、タスク数増加に注意）'
    )

    parser.add_argument(
        '--cleanup',
        action='store_true',
        help='収集前に古いデータを自動削除'
    )

    parser.add_argument(
        '--retention-days',
        type=int,
        default=90,
        help='データ保持日数 (デフォルト: 90)'
    )

    return parser.parse_args()


def main():
    """メイン処理"""
    args = parse_args()

    def _validate_date(date_str, param_name):
        """YYYY-MM-DD 形式の日付を検証"""
        if date_str and not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            print(f"[エラー] {param_name} は YYYY-MM-DD 形式で指定してください: {date_str}")
            sys.exit(1)

    if hasattr(args, 'since') and args.since:
        _validate_date(args.since, "--since")
    if hasattr(args, 'until') and args.until:
        _validate_date(args.until, "--until")

    # プロファイル存在確認
    profile_path = Path(args.profile).resolve()
    if not profile_path.exists():
        print("[エラー] ブラウザプロファイルが見つかりません。")
        print("先に以下のコマンドで Cookie を取得してください（Chrome に X ログイン済みが前提）:")
        print()
        print("  python3 scripts/import_chrome_cookies.py --chrome-profile \"Profile 2\" --account kabuki666999")
        print("  （詳細: refresh-x-cookies スキル参照）")
        print()
        sys.exit(1)

    # 収集対象グループを決定
    if 'all' in args.groups:
        groups = list(SEARCH_URLS.keys())
    else:
        groups = args.groups

    # データクリーンアップ
    if args.cleanup:
        import shutil
        from datetime import timedelta
        output_dir = Path(args.output)
        if output_dir.exists():
            cutoff = (datetime.now() - timedelta(days=args.retention_days)).strftime("%Y-%m-%d")
            for d in sorted(output_dir.iterdir()):
                if d.is_dir() and not d.name.startswith(".") and d.name < cutoff:
                    try:
                        datetime.strptime(d.name, "%Y-%m-%d")
                    except ValueError:
                        continue
                    print(f"クリーンアップ: {d}")
                    shutil.rmtree(d)

    # 非活動アカウント除外
    exclude_accounts = None
    if args.check_inactive:
        print("\n非活動アカウントチェック中...")
        results = run_inactive_check(
            profile_path=str(profile_path),
            headless=True,
            use_cache=not args.no_inactive_cache,
            output_dir=args.output
        )
        exclude_accounts = detect_inactive_accounts(results, threshold_days=args.inactive_days)
        if exclude_accounts:
            print(f"\n除外アカウント ({len(exclude_accounts)}件):")
            for username in sorted(exclude_accounts):
                # 理由を結果から取得
                for r in results:
                    if r["username"] == username:
                        status = r.get("status", "unknown")
                        last_post = r.get("last_post_date", "")[:10] if r.get("last_post_date") else "N/A"
                        print(f"  - @{username}: {status} (最終投稿: {last_post})")
                        break
            print()
        else:
            print("除外対象なし\n")

    # タスクキュー構築
    interleave = not args.no_interleave
    tasks = build_collect_tasks(groups, interleave=interleave, exclude_accounts=exclude_accounts,
                                since=args.since, until=args.until,
                                split_per_account=args.split_per_account)

    print()
    print("=" * 60)
    print("X投稿収集システム（バッチモード）")
    print("=" * 60)
    print(f"収集対象: {', '.join(groups)}")
    print(f"スクロール回数: {args.scrolls}")
    print(f"プロファイル: {profile_path}")
    print(f"タスク数: {len(tasks)} (インターリーブ: {'ON' if interleave else 'OFF'})")
    print(f"バッチサイズ: {args.batch_size}, クールダウン: {args.cooldown}秒")
    if exclude_accounts:
        print(f"除外アカウント: {len(exclude_accounts)}件")
    if args.since or args.until:
        print(f"検索期間: {args.since or '(デフォルト)'} ~ {args.until or '(デフォルト)'}")
    if args.split_per_account:
        print(f"アカウント分割: ON（個別URL生成）")
    print()

    # 共有重複排除セット
    shared_collected_urls = set()
    all_tweets = []
    retry_queue = []

    # バッチ分割ループ
    batch_num = 0
    i = 0
    while i < len(tasks):
        batch = tasks[i:i + args.batch_size]
        batch_num += 1
        print(f"\n{'='*60}")
        print(f"バッチ {batch_num} ({len(batch)}タスク)")
        print(f"{'='*60}")

        batch_blocked = False
        for task in batch:
            if task.status != "pending":
                continue

            # URL間待機（最初のタスク以外）
            if i > 0:
                wait_sec = random.uniform(
                    COLLECTION_SETTINGS["url_wait_min_sec"],
                    COLLECTION_SETTINGS["url_wait_max_sec"]
                )
                print(f"\n次のURL処理まで {wait_sec:.0f}秒 待機...")
                time.sleep(wait_sec)

            result = _execute_collect_task(
                task, str(profile_path), args.scrolls, shared_collected_urls
            )

            if result.status == "success":
                task.status = "completed"
                _append_tweets_with_group_info(all_tweets, result, task)
                print(f"  → {result.collected_count}件収集")
            elif result.status == "blocked":
                task.status = "blocked"
                task.error_message = result.error_message
                # 残りのバッチタスクもリトライキューに移動
                for remaining_task in batch[batch.index(task):]:
                    if remaining_task.status == "pending":
                        remaining_task.status = "blocked"
                        remaining_task.error_message = "バッチ内先行タスクのブロックにより未処理"
                if not args.no_retry:
                    retry_queue.extend(
                        [t for t in batch[batch.index(task):] if t.status == "blocked"]
                    )
                # 途中で収集できたツイートも保存
                if result.tweets:
                    _append_tweets_with_group_info(all_tweets, result, task)
                batch_blocked = True
                break
            elif result.status == "login_required":
                print("\n[エラー] ログインが必要です。")
                print("scripts/import_chrome_cookies.py で Chrome から Cookie を抽出してください（refresh-x-cookies スキル参照）")
                sys.exit(1)
            else:
                task.status = "failed"
                task.error_message = result.error_message
                if not args.no_retry and task.retries < BATCH_SETTINGS["max_retries"]:
                    retry_queue.append(task)

            i += 1

        if batch_blocked:
            # ブロック時: 残りタスクもスキップ分のインデックスを進める
            i += len(batch)  # 残りのバッチ分をスキップ
            # ブロッククールダウン
            wait_sec = BATCH_SETTINGS["block_cooldown_sec"]
            print(f"\n[ブロック検知] {wait_sec}秒 ({wait_sec//60}分) クールダウン...")
            _countdown_wait(wait_sec, "ブロッククールダウン")
            continue

        # バッチ間クールダウン（最後のバッチ以外）
        if i < len(tasks):
            print(f"\nバッチ間クールダウン: {args.cooldown}秒 ({args.cooldown//60}分)")
            _countdown_wait(args.cooldown, "クールダウン")

    # リトライフェーズ
    if retry_queue and not args.no_retry:
        print(f"\n{'='*60}")
        print(f"リトライフェーズ ({len(retry_queue)}タスク)")
        print(f"{'='*60}")

        for task in retry_queue:
            task.retries += 1
            if task.retries > BATCH_SETTINGS["max_retries"]:
                task.status = "failed"
                task.error_message = f"最大リトライ回数({BATCH_SETTINGS['max_retries']})超過"
                continue

            wait_sec = _get_retry_wait(task)
            print(f"\nリトライ {task.retries}/{BATCH_SETTINGS['max_retries']}: "
                  f"{task.group_key} [{task.url_type}]")
            _countdown_wait(wait_sec, f"リトライ待機")

            task.status = "pending"
            result = _execute_collect_task(
                task, str(profile_path), args.scrolls, shared_collected_urls
            )

            if result.status == "success":
                task.status = "completed"
                _append_tweets_with_group_info(all_tweets, result, task)
                print(f"  → リトライ成功: {result.collected_count}件収集")
            else:
                task.status = "failed"
                task.error_message = result.error_message
                print(f"  → リトライ失敗: {result.error_message[:60]}")

    # 結果サマリー
    _print_collection_summary(tasks + [t for t in retry_queue if t not in tasks])

    if not all_tweets:
        print("\n[警告] ツイートが収集できませんでした。")
        _print_collection_summary(tasks)
        sys.exit(1)

    # 分類処理
    if not args.no_classify:
        print("\n分類処理中...")
        classifier = TweetClassifier()
        all_tweets = classifier.classify_all(all_tweets)
        classifier.print_summary(all_tweets)

    # 出力ディレクトリ準備（日付フォルダ）
    output_dir = Path(args.output)
    date_str = datetime.now().strftime("%Y-%m-%d")
    date_dir = output_dir / date_str
    date_dir.mkdir(parents=True, exist_ok=True)

    # JSON保存
    json_path = date_dir / "tweets.json"

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_tweets, f, ensure_ascii=False, indent=2)
    print(f"\nJSON保存: {json_path}")

    # CSV保存
    if not args.no_csv:
        import csv

        csv_path = date_dir / "tweets.csv"

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
        news_path = date_dir / "news.json"

        with open(news_path, 'w', encoding='utf-8') as f:
            json.dump(news_data, f, ensure_ascii=False, indent=2)
        print(f"ニュースデータ保存: {news_path}")

    # plan.md M1 T1.7: 日次収集メトリクスを output/collection_metrics.jsonl に追記
    metrics = _emit_collection_metrics(all_tweets, output_dir, date_str)

    print()
    print("=" * 60)
    print("収集完了!")
    print("=" * 60)
    print(f"総ツイート数: {len(all_tweets)} (アクティブアカウント: {metrics['active_accounts']})")
    if metrics["warnings"]:
        print(f"[WARNING] 閾値未達: {', '.join(metrics['warnings'])}")
    print()


if __name__ == "__main__":
    main()
