#!/usr/bin/env python3
"""Grok APIインフルエンサー勝率リサーチ パイプライン。

Grok APIで候補発見 → Playwrightでツイート収集 → LLMシグナル抽出 →
株価突合 → 勝率計算のフルパイプラインを実行する。

Usage:
    # フルパイプライン
    python scripts/research_influencers.py --phase full

    # フェーズ別
    python scripts/research_influencers.py --phase discover --keywords "日本株 高配当" "グロース株"
    python scripts/research_influencers.py --phase collect --candidates output/research/discovery_*.json
    python scripts/research_influencers.py --phase evaluate
    python scripts/research_influencers.py --phase report
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESEARCH_DIR = "output/research"


def ensure_research_dir():
    """リサーチ出力ディレクトリを作成する。"""
    os.makedirs(RESEARCH_DIR, exist_ok=True)


# ============================================================
# Phase 1: Discover
# ============================================================
def phase_discover(keywords=None, max_candidates=50):
    """Grok APIで候補インフルエンサーを発見する。

    Args:
        keywords: 検索キーワードリスト（Noneの場合はRESEARCH_KEYWORDS使用）
        max_candidates: 最大候補数

    Returns:
        discovery結果JSONのパス
    """
    from collector.grok_client import GrokClient
    from collector.config import RESEARCH_KEYWORDS, INFLUENCER_GROUPS

    if keywords is None:
        keywords = RESEARCH_KEYWORDS

    # 既存アカウントを除外リストに追加
    excluded = set()
    for group in INFLUENCER_GROUPS.values():
        for acc in group.get("accounts", []):
            excluded.add(acc["username"])

    print(f"=== Phase 1: Discover ===")
    print(f"キーワード: {keywords}")
    print(f"除外アカウント数: {len(excluded)}")
    print(f"最大候補数: {max_candidates}")

    client = GrokClient()

    # キーワード検索
    result = client.discover_by_keywords(
        keywords=keywords,
        max_candidates=max_candidates,
        excluded_handles=list(excluded),
    )

    candidates = result.get("candidates", [])
    errors = result.get("errors", [])

    # ネットワーク検索（既存アカウントのネットワークから）
    existing_handles = list(excluded)[:20]  # 上位20アカウント
    network_result = client.discover_by_network(
        existing_handles=existing_handles,
        max_candidates=max_candidates,
        excluded_handles=list(excluded),
    )

    # マージ（重複排除）
    seen = {c.get("username", "").lower() for c in candidates}
    for nc in network_result.get("candidates", []):
        username = nc.get("username", "").lower()
        if username and username not in seen:
            seen.add(username)
            candidates.append(nc)
    errors.extend(network_result.get("errors", []))

    # 保存
    ensure_research_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(RESEARCH_DIR, f"discovery_{timestamp}.json")

    data = {
        "discovered_at": datetime.now().isoformat(),
        "keywords": keywords,
        "candidates_count": len(candidates),
        "candidates": candidates,
        "errors": errors,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"候補発見完了: {len(candidates)}件 → {output_path}")
    if errors:
        print(f"エラー: {len(errors)}件")

    return output_path


# ============================================================
# Phase 2: Collect
# ============================================================
def phase_collect(candidates_files=None, max_collect=10, scrolls=10):
    """候補インフルエンサーのツイートを収集する。

    Args:
        candidates_files: discovery JSONファイルパスのリスト（glob対応）
        max_collect: 収集する最大候補数
        scrolls: スクロール回数

    Returns:
        収集結果ファイルパスのリスト
    """
    from collector.x_collector import SafeXCollector
    from collector.config import COLLECTION_SETTINGS, PROFILE_PATH

    print(f"=== Phase 2: Collect ===")

    # 候補ファイル読み込み
    if candidates_files is None:
        pattern = os.path.join(RESEARCH_DIR, "discovery_*.json")
        candidates_files = sorted(glob.glob(pattern))

    if not candidates_files:
        print("エラー: 候補ファイルが見つかりません")
        return []

    # 全候補をマージ
    all_candidates = []
    for filepath in candidates_files:
        # glob パターン展開
        for actual_path in glob.glob(filepath):
            try:
                with open(actual_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                all_candidates.extend(data.get("candidates", []))
            except (json.JSONDecodeError, OSError) as e:
                print(f"警告: {actual_path} の読み込みに失敗: {e}")

    # 重複排除 & スコア順ソート
    seen = set()
    unique = []
    for c in all_candidates:
        username = c.get("username", "").lower()
        if username and username not in seen:
            seen.add(username)
            unique.append(c)

    unique.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    candidates_to_collect = unique[:max_collect]

    print(f"収集対象: {len(candidates_to_collect)}件 (全{len(unique)}件中)")

    # Playwright で収集
    import urllib.parse
    from collector.x_collector import CollectionResult

    collected_files = []
    shared_urls = set()

    for i, candidate in enumerate(candidates_to_collect):
        username = candidate.get("username", "")
        if not username:
            continue

        print(f"\n--- [{i+1}/{len(candidates_to_collect)}] @{username} ---")

        # 30日遡りの検索URL生成
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        until = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        query = f"from:{username} since:{since} until:{until}"
        encoded = urllib.parse.quote(query)
        search_url = f"https://x.com/search?q={encoded}&src=typed_query&f=live"

        try:
            collector = SafeXCollector(
                profile_path=PROFILE_PATH,
                shared_collected_urls=shared_urls,
            )
            result = collector.collect(
                search_url=search_url,
                max_scrolls=scrolls,
                group_name=f"research_{username}",
            )

            if result.status == "success" and result.tweets:
                tweets = result.tweets
                # ツイートにメタデータ追加
                for tweet in tweets:
                    tweet["research_candidate"] = True
                    tweet["candidate_score"] = candidate.get("score", 0)

                output_path = os.path.join(RESEARCH_DIR, f"tweets_{username}.json")
                with open(output_path, "w", encoding="utf-8") as f:
                    json.dump(tweets, f, ensure_ascii=False, indent=2)

                collected_files.append(output_path)
                print(f"  収集完了: {len(tweets)}件 → {output_path}")
            else:
                print(f"  ツイートなし (status={result.status})")

        except Exception as e:
            print(f"  収集エラー: {e}")

        # クールダウン
        if i < len(candidates_to_collect) - 1:
            wait = COLLECTION_SETTINGS.get("url_wait_min_sec", 60)
            print(f"  {wait}秒待機...")
            time.sleep(wait)

    print(f"\n収集完了: {len(collected_files)}ファイル")
    return collected_files


# ============================================================
# Phase 3: Evaluate
# ============================================================
def phase_evaluate(tweet_files=None):
    """シグナル抽出 → 株価突合 → 勝率計算を行う。

    Args:
        tweet_files: ツイートJSONファイルパスのリスト（Noneの場合はresearchディレクトリから検索）

    Returns:
        評価件数
    """
    from collector.signal_extractor import SignalExtractor
    from collector.price_fetcher import PriceFetcher
    from collector.business_days import add_business_days
    from extensions.tier1_collection.grok_discoverer.research_store import ResearchStore

    print(f"=== Phase 3: Evaluate ===")

    # ツイートファイル読み込み
    if tweet_files is None:
        pattern = os.path.join(RESEARCH_DIR, "tweets_*.json")
        tweet_files = sorted(glob.glob(pattern))

    if not tweet_files:
        print("エラー: ツイートファイルが見つかりません")
        return 0

    # 全ツイート読み込み
    all_tweets = []
    for filepath in tweet_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                tweets = json.load(f)
            all_tweets.extend(tweets)
        except (json.JSONDecodeError, OSError) as e:
            print(f"警告: {filepath} の読み込みに失敗: {e}")

    print(f"ツイート読み込み: {len(all_tweets)}件")

    if not all_tweets:
        return 0

    # シグナル抽出
    extractor = SignalExtractor()
    signals = extractor.extract_all(all_tweets)
    print(f"シグナル抽出: {len(signals)}件")

    # クロスバリデーション
    signals = extractor.cross_validate_with_extractor(signals)
    validated_count = sum(1 for s in signals if s.get("cross_validated"))
    print(f"クロスバリデーション: {validated_count}/{len(signals)}件が一致")

    # 株価突合 + 評価
    store = ResearchStore(base_dir=RESEARCH_DIR)
    fetcher = PriceFetcher(cache_file="output/price_cache.json")

    today = datetime.now().strftime("%Y-%m-%d")
    evaluation_count = 0

    for sig in signals:
        signal_id = store.get_signal_id(sig.get("tweet_url", ""), sig.get("ticker", ""))

        # シグナル保存
        sig["signal_id"] = signal_id
        store.add_signal(sig)

        # 投稿日の解析
        posted_at = sig.get("posted_at", "")
        if not posted_at:
            continue

        try:
            signal_date = posted_at[:10]  # YYYY-MM-DD
        except (IndexError, TypeError):
            continue

        # シグナル日の株価
        price_at_signal_data = fetcher.get_price_at_date(sig["ticker"], signal_date)
        price_at_signal = price_at_signal_data.get("close")

        if price_at_signal is None:
            continue

        # ホライズン評価
        evaluation = {
            "signal_id": signal_id,
            "username": sig.get("username", ""),
            "display_name": sig.get("display_name", ""),
            "ticker": sig["ticker"],
            "direction": sig["direction"],
            "signal_date": signal_date,
            "price_at_signal": price_at_signal,
            "evaluated_at": datetime.now().isoformat(),
        }

        # +5営業日
        target_5bd = add_business_days(signal_date, 5)
        if target_5bd <= today:
            price_5bd_data = fetcher.get_price_at_date(sig["ticker"], target_5bd)
            price_5bd = price_5bd_data.get("close")
            if price_5bd is not None and price_at_signal > 0:
                return_pct = round((price_5bd - price_at_signal) / price_at_signal * 100, 2)
                is_win = (return_pct > 0) if sig["direction"] == "LONG" else (return_pct < 0)
                evaluation["horizon_5bd"] = {
                    "target_date": target_5bd,
                    "price": price_5bd,
                    "return_pct": return_pct,
                    "is_win": is_win,
                }
            else:
                evaluation["horizon_5bd"] = {
                    "target_date": target_5bd,
                    "price": price_5bd,
                    "return_pct": None,
                    "is_win": None,
                }
        else:
            evaluation["horizon_5bd"] = {
                "target_date": target_5bd,
                "price": None,
                "return_pct": None,
                "is_win": None,
            }

        # +20営業日
        target_20bd = add_business_days(signal_date, 20)
        if target_20bd <= today:
            price_20bd_data = fetcher.get_price_at_date(sig["ticker"], target_20bd)
            price_20bd = price_20bd_data.get("close")
            if price_20bd is not None and price_at_signal > 0:
                return_pct = round((price_20bd - price_at_signal) / price_at_signal * 100, 2)
                is_win = (return_pct > 0) if sig["direction"] == "LONG" else (return_pct < 0)
                evaluation["horizon_20bd"] = {
                    "target_date": target_20bd,
                    "price": price_20bd,
                    "return_pct": return_pct,
                    "is_win": is_win,
                }
            else:
                evaluation["horizon_20bd"] = {
                    "target_date": target_20bd,
                    "price": price_20bd,
                    "return_pct": None,
                    "is_win": None,
                }
        else:
            evaluation["horizon_20bd"] = {
                "target_date": target_20bd,
                "price": None,
                "return_pct": None,
                "is_win": None,
            }

        store.add_evaluation(evaluation)
        evaluation_count += 1

    print(f"評価完了: {evaluation_count}件")
    return evaluation_count


# ============================================================
# Phase 4: Report
# ============================================================
def phase_report():
    """スコアカード + HTMLレポートを生成する。

    Returns:
        レポート出力パス
    """
    from extensions.tier1_collection.grok_discoverer.research_store import ResearchStore
    from extensions.tier1_collection.grok_discoverer.research_scorecard import ResearchScorecardBuilder

    print(f"=== Phase 4: Report ===")

    store = ResearchStore(base_dir=RESEARCH_DIR)
    evaluations = store.load_evaluations()

    if not evaluations:
        print("評価データがありません")
        return None

    print(f"評価データ読み込み: {len(evaluations)}件")

    builder = ResearchScorecardBuilder()
    scorecard = builder.build(evaluations)

    # JSON保存
    json_path = os.path.join(RESEARCH_DIR, "research_scorecard.json")
    builder.save(scorecard, json_path)
    print(f"スコアカード保存: {json_path}")

    # ランキング表示
    print("\n=== ランキング (20BD勝率) ===")
    ranking = builder.rank_influencers(scorecard, horizon="horizon_20bd")
    for i, entry in enumerate(ranking[:20]):
        print(
            f"  {i+1:2d}. @{entry['username']:<20s} "
            f"勝率: {entry['win_rate']:5.1f}% "
            f"({entry['winners']}/{entry['trackable']}) "
            f"平均リターン: {entry['avg_return_pct']:+.2f}%"
        )

    # HTMLレポート生成
    html_path = os.path.join(RESEARCH_DIR, "research_report.html")
    _generate_html_report(scorecard, ranking, html_path)
    print(f"\nHTMLレポート保存: {html_path}")

    return html_path


def _generate_html_report(scorecard, ranking, output_path):
    """HTMLレポートを生成する。"""
    generated_at = scorecard.get("generated_at", "")
    total_signals = scorecard.get("total_signals", 0)
    total_influencers = scorecard.get("total_influencers", 0)
    global_5bd = scorecard.get("global_summary", {}).get("horizon_5bd", {})
    global_20bd = scorecard.get("global_summary", {}).get("horizon_20bd", {})

    ranking_rows = ""
    for i, entry in enumerate(ranking):
        win_rate_class = "positive" if entry["win_rate"] >= 50 else "negative"
        return_class = "positive" if entry["avg_return_pct"] >= 0 else "negative"
        ranking_rows += f"""
        <tr>
            <td>{i+1}</td>
            <td>@{_escape_html(entry['username'])}</td>
            <td>{_escape_html(entry.get('display_name', ''))}</td>
            <td>{entry['total_signals']}</td>
            <td>{entry['trackable']}</td>
            <td>{entry['winners']}</td>
            <td class="{win_rate_class}">{entry['win_rate']:.1f}%</td>
            <td class="{return_class}">{entry['avg_return_pct']:+.2f}%</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>インフルエンサー勝率リサーチレポート</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', Tahoma, sans-serif; background: #f5f5f5; color: #333; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ font-size: 24px; margin-bottom: 20px; color: #1a1a2e; }}
        h2 {{ font-size: 18px; margin: 20px 0 10px; color: #16213e; }}
        .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }}
        .card {{ background: white; border-radius: 8px; padding: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .card .label {{ font-size: 12px; color: #888; margin-bottom: 5px; }}
        .card .value {{ font-size: 24px; font-weight: bold; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #1a1a2e; color: white; font-size: 13px; }}
        td {{ font-size: 13px; }}
        tr:hover {{ background: #f9f9f9; }}
        .positive {{ color: #27ae60; font-weight: bold; }}
        .negative {{ color: #e74c3c; font-weight: bold; }}
        .meta {{ font-size: 12px; color: #888; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>インフルエンサー勝率リサーチレポート</h1>

        <div class="summary">
            <div class="card">
                <div class="label">総シグナル数</div>
                <div class="value">{total_signals}</div>
            </div>
            <div class="card">
                <div class="label">対象インフルエンサー</div>
                <div class="value">{total_influencers}</div>
            </div>
            <div class="card">
                <div class="label">5BD全体勝率</div>
                <div class="value">{global_5bd.get('win_rate', 0):.1f}%</div>
            </div>
            <div class="card">
                <div class="label">20BD全体勝率</div>
                <div class="value">{global_20bd.get('win_rate', 0):.1f}%</div>
            </div>
            <div class="card">
                <div class="label">5BD平均リターン</div>
                <div class="value {'positive' if global_5bd.get('avg_return_pct', 0) >= 0 else 'negative'}">{global_5bd.get('avg_return_pct', 0):+.2f}%</div>
            </div>
            <div class="card">
                <div class="label">20BD平均リターン</div>
                <div class="value {'positive' if global_20bd.get('avg_return_pct', 0) >= 0 else 'negative'}">{global_20bd.get('avg_return_pct', 0):+.2f}%</div>
            </div>
        </div>

        <h2>ランキング (20BD勝率順)</h2>
        <table>
            <thead>
                <tr>
                    <th>#</th>
                    <th>ユーザー名</th>
                    <th>表示名</th>
                    <th>シグナル数</th>
                    <th>評価可能</th>
                    <th>勝ち</th>
                    <th>勝率</th>
                    <th>平均リターン</th>
                </tr>
            </thead>
            <tbody>
                {ranking_rows}
            </tbody>
        </table>

        <p class="meta">生成日時: {generated_at}</p>
    </div>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def _escape_html(text):
    """HTMLエスケープ。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Grok APIインフルエンサー勝率リサーチ パイプライン"
    )
    parser.add_argument(
        "--phase",
        choices=["discover", "collect", "evaluate", "report", "full"],
        default="full",
        help="実行フェーズ (default: full)",
    )
    parser.add_argument(
        "--keywords",
        nargs="*",
        help="Discovery用キーワード（--phase discover時）",
    )
    parser.add_argument(
        "--candidates",
        nargs="*",
        help="候補JSONファイルパス（--phase collect時、glob対応）",
    )
    parser.add_argument(
        "--max-collect",
        type=int,
        default=10,
        help="収集する最大候補数 (default: 10)",
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        default=10,
        help="スクロール回数 (default: 10)",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=50,
        help="Discovery最大候補数 (default: 50)",
    )

    args = parser.parse_args()

    ensure_research_dir()

    if args.phase == "discover" or args.phase == "full":
        discovery_path = phase_discover(
            keywords=args.keywords,
            max_candidates=args.max_candidates,
        )
        if args.phase == "discover":
            return

    if args.phase == "collect" or args.phase == "full":
        candidates_files = args.candidates if args.phase == "collect" else None
        collected = phase_collect(
            candidates_files=candidates_files,
            max_collect=args.max_collect,
            scrolls=args.scrolls,
        )
        if args.phase == "collect":
            return

    if args.phase == "evaluate" or args.phase == "full":
        phase_evaluate()
        if args.phase == "evaluate":
            return

    if args.phase == "report" or args.phase == "full":
        phase_report()


if __name__ == "__main__":
    main()
