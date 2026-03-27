#!/usr/bin/env python3
"""投稿ドラフト生成スクリプト。

収集済みツイートデータから7パターンのX投稿ドラフトを生成し、
PostStoreに保存する。テンプレートベース(3種)とLLMベース(4種)の
2段階で構成される。
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..x_poster.post_store import PostStore
from ..services.style_prompt_builder import build_style_aware_prompt
from ..account_routing import resolve_account


# Lazy import for image generation
def _get_image_generators():
    from ..image_generator.chart_generator import ChartGenerator
    from ..image_generator.ogp_generator import OGPGenerator
    return ChartGenerator(), OGPGenerator()


def generate_images_for_draft(draft: dict, chart_gen, ogp_gen) -> list[dict]:
    """ドラフトのテンプレートタイプに応じた画像を生成

    Returns:
        list[dict]: [{"path": str, "type": str, "description": str}]
    """
    template_type = draft.get("template_type", "")
    images = []

    try:
        if template_type == "win_rate_ranking":
            # Extract data for bar chart from draft metadata or body
            source_items = draft.get("source_items", [])
            if source_items:
                labels = [item.get("username", "?") for item in source_items[:5]]
                values = [item.get("win_rate", 0) for item in source_items[:5]]
                if any(v > 0 for v in values):
                    path = chart_gen.generate_bar_chart({
                        "labels": labels,
                        "values": values,
                        "title": "勝率TOP5 インフルエンサー",
                        "ylabel": "勝率 (%)",
                        "filename": f"win_rate_{draft.get('news_id', 'unknown')}"
                    })
                    images.append({"path": path, "type": "bar_chart", "description": "勝率ランキング"})

        elif template_type == "weekly_report":
            # Pie chart for category distribution
            metadata = draft.get("metadata", {})
            cat_counts = metadata.get("category_counts", {})
            if cat_counts:
                path = chart_gen.generate_pie_chart({
                    "labels": list(cat_counts.keys()),
                    "values": list(cat_counts.values()),
                    "title": "カテゴリ別ツイート分布",
                    "filename": f"category_dist_{draft.get('news_id', 'unknown')}"
                })
                images.append({"path": path, "type": "pie_chart", "description": "カテゴリ分布"})

        elif template_type in ("contrarian_signal", "market_summary", "hot_picks",
                                "trade_activity", "earnings_flash"):
            # OGP card for text-based templates
            category = draft.get("metadata", {}).get("category", "market_trend")
            path = ogp_gen.generate({
                "title": draft.get("title", "市場レポート"),
                "summary": draft.get("body", "")[:100],
                "category": category,
                "badge_text": draft.get("metadata", {}).get("corner_name", ""),
                "filename": f"ogp_{draft.get('news_id', 'unknown')}"
            })
            images.append({"path": path, "type": "ogp_card", "description": "OGPカード"})
    except Exception as e:
        print(f"  ⚠️ 画像生成エラー: {e}")

    return images


# --- Constants ---

BULLISH_KEYWORDS = [
    "強気", "上がる", "買い", "ロング", "底打ち",
    "反転", "チャンス", "爆上げ", "急騰",
]

EARNINGS_KEYWORDS = [
    "決算", "業績", "増収", "減収", "増益",
    "減益", "上方修正", "下方修正", "営業利益", "経常利益",
]

CATEGORY_LABELS = {
    "recommended_assets": "推奨銘柄",
    "purchased_assets": "購入報告",
    "ipo": "IPO情報",
    "market_trend": "市況トレンド",
    "bullish_assets": "高騰銘柄",
    "bearish_assets": "下落銘柄",
    "warning_signals": "警戒シグナル",
}


# --- Helpers ---

def make_news_id(seed: str) -> str:
    """seed文字列からsha256の先頭16文字を返す。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def extract_date(path: str) -> str:
    """ファイルパスから8桁日付を抽出、なければ今日の日付。"""
    match = re.search(r"(\d{8})", path)
    return match.group(1) if match else datetime.now().strftime("%Y%m%d")


def find_latest_input() -> Optional[str]:
    """output/tweets_*.json の最新ファイルを返す。"""
    # プロジェクトルート: extensions/tier3_posting/cli/compose.py → 4階層上
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    pattern = os.path.join(project_root, "output", "tweets_*.json")
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def load_tweets(path: str) -> List[Dict[str, Any]]:
    """JSONファイルからツイートリストを読み込む。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return []


def get_categories(tweet: Dict[str, Any]) -> List[str]:
    """ツイートからカテゴリリストを取得（llm_categories優先）。"""
    cats = tweet.get("llm_categories") or tweet.get("categories") or []
    if isinstance(cats, list):
        return cats
    return []


def call_llm(prompt: str) -> str:
    """Claude APIを呼び出してテキストを返す。

    APIキー未設定時は空文字を返す。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set, skipping LLM drafts")
        return ""

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = json.dumps({
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["content"][0]["text"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as e:
        print(f"WARNING: LLM API call failed: {e}")
        return ""


# --- Template-based generators ---

def generate_win_rate_ranking(date_str: str) -> List[Dict[str, Any]]:
    """勝率ランキングTOP5ドラフトを生成。"""
    # プロジェクトルート: extensions/tier3_posting/cli/compose.py → 4階層上
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    scorecards_path = os.path.join(
        project_root, "output", "performance", "scorecards.json"
    )
    if not os.path.exists(scorecards_path):
        print("  scorecards.json が見つかりません、win_rate_rankingをスキップ")
        return []

    try:
        with open(scorecards_path, "r", encoding="utf-8") as f:
            scorecards = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  scorecards.json の読み込みに失敗: {e}")
        return []

    # 各インフルエンサーの勝率を抽出
    influencers = scorecards.get("influencers", scorecards)
    rankings = []
    for username, card in influencers.items():
        if not isinstance(card, dict):
            continue
        by_period = card.get("by_period", {})
        all_period = by_period.get("all", {})
        win_rate = all_period.get("win_rate")
        total = all_period.get("total", 0)
        wins = all_period.get("wins", 0)
        display_name = card.get("display_name", username)
        if win_rate is not None and total > 0:
            rankings.append({
                "username": username,
                "display_name": display_name,
                "win_rate": win_rate,
                "total": total,
                "wins": wins,
            })

    if not rankings:
        return []

    # 勝率降順でソート、TOP5
    rankings.sort(key=lambda x: x["win_rate"], reverse=True)
    top5 = rankings[:5]

    lines = []
    for i, r in enumerate(top5, 1):
        lines.append(
            f"{i}位 {r['display_name']} {r['win_rate']}%"
            f"（{r['total']}件中{r['wins']}件的中）"
        )

    body = "📈勝率ランキングTOP5\n\n" + "\n".join(lines) + "\n\n#投資 #株式 #インフルエンサー"

    return [{
        "news_id": make_news_id(f"win_rate_ranking:{date_str}:top5"),
        "source_items": [],
        "title": f"勝率ランキングTOP5 ({date_str})",
        "body": body,
        "format": "x_post",
        "scheduled_at": datetime.now().isoformat(),
        "hashtags": ["#投資", "#株式", "#インフルエンサー"],
        "status": "draft",
        "template_type": "win_rate_ranking",
    }]


def generate_contrarian_signals(
    tweets: List[Dict[str, Any]], date_str: str
) -> List[Dict[str, Any]]:
    """逆指標シグナルドラフトを生成。"""
    drafts = []
    for tweet in tweets:
        if not tweet.get("is_contrarian", False):
            continue
        text = tweet.get("text", "")
        if not any(kw in text for kw in BULLISH_KEYWORDS):
            continue

        display_name = tweet.get("display_name", tweet.get("username", ""))
        username = tweet.get("username", "")
        tweet_url = tweet.get("url", "")
        truncated = text[:100]

        body = (
            f"⚠️逆指標シグナル検知\n\n"
            f"{display_name}(@{username})が強気発言:\n"
            f"「{truncated}」\n\n"
            f"逆指標アカウントの強気発言は警戒シグナルとして注目\n\n"
            f"#逆指標 #投資 #株式"
        )

        drafts.append({
            "news_id": make_news_id(f"contrarian_signal:{date_str}:{tweet_url}"),
            "source_items": [tweet_url] if tweet_url else [],
            "title": f"逆指標シグナル: {display_name} ({date_str})",
            "body": body,
            "format": "x_post",
            "scheduled_at": datetime.now().isoformat(),
            "hashtags": ["#逆指標", "#投資", "#株式"],
            "status": "draft",
            "template_type": "contrarian_signal",
        })

    return drafts


def generate_weekly_report(
    tweets: List[Dict[str, Any]], date_str: str
) -> List[Dict[str, Any]]:
    """週間レポートドラフトを生成。"""
    if not tweets:
        return []

    category_counts: Dict[str, int] = {key: 0 for key in CATEGORY_LABELS}
    usernames = set()

    for tweet in tweets:
        usernames.add(tweet.get("username", ""))
        for cat in get_categories(tweet):
            if cat in category_counts:
                category_counts[cat] += 1

    lines = []
    for key, label in CATEGORY_LABELS.items():
        lines.append(f"・{label}: {category_counts[key]}件")

    body = (
        f"📊今週の投資インフルエンサー動向\n\n"
        + "\n".join(lines)
        + f"\nアクティブ: {len(usernames)}名\n\n"
        f"#週間レポート #投資 #株式"
    )

    return [{
        "news_id": make_news_id(f"weekly_report:{date_str}:summary"),
        "source_items": [],
        "title": f"週間レポート ({date_str})",
        "body": body,
        "format": "x_post",
        "scheduled_at": datetime.now().isoformat(),
        "hashtags": ["#週間レポート", "#投資", "#株式"],
        "status": "draft",
        "template_type": "weekly_report",
    }]


# --- LLM-based generators ---

def _collect_tweets_text(tweets: List[Dict[str, Any]]) -> str:
    """ツイートリストをLLMプロンプト用テキストに変換。"""
    parts = []
    for t in tweets:
        name = t.get("display_name", t.get("username", ""))
        parts.append(f"- {name}: {t.get('text', '')}")
    return "\n".join(parts)


def generate_market_summary(
    tweets: List[Dict[str, Any]], date_str: str
) -> List[Dict[str, Any]]:
    """市況サマリードラフトを生成（LLM使用）。"""
    market_tweets = [
        t for t in tweets if "market_trend" in get_categories(t)
    ]
    if not market_tweets:
        return []

    tweets_text = _collect_tweets_text(market_tweets)
    prompt = build_style_aware_prompt(
        task="market_summary",
        source_data=tweets_text,
        target_account="kabuki666999",
        target_style="explainer",
        topic="investing",
    )
    body = call_llm(prompt)
    if not body:
        return []

    source_urls = [t.get("url", "") for t in market_tweets if t.get("url")]
    return [{
        "news_id": make_news_id(f"market_summary:{date_str}:trend"),
        "source_items": source_urls,
        "title": f"市況サマリー ({date_str})",
        "body": body,
        "format": "x_post",
        "scheduled_at": datetime.now().isoformat(),
        "hashtags": ["#市況", "#投資", "#株式"],
        "status": "draft",
        "template_type": "market_summary",
    }]


def generate_hot_picks(
    tweets: List[Dict[str, Any]], date_str: str
) -> List[Dict[str, Any]]:
    """注目銘柄ドラフトを生成（LLM使用）。"""
    pick_tweets = [
        t for t in tweets
        if set(get_categories(t)) & {"recommended_assets", "purchased_assets"}
    ]
    if not pick_tweets:
        return []

    tweets_text = _collect_tweets_text(pick_tweets)
    prompt = build_style_aware_prompt(
        task="hot_picks",
        source_data=tweets_text,
        target_account="kabuki666999",
        target_style="listicle",
        topic="investing",
    )
    body = call_llm(prompt)
    if not body:
        return []

    source_urls = [t.get("url", "") for t in pick_tweets if t.get("url")]
    return [{
        "news_id": make_news_id(f"hot_picks:{date_str}:picks"),
        "source_items": source_urls,
        "title": f"注目銘柄 ({date_str})",
        "body": body,
        "format": "x_post",
        "scheduled_at": datetime.now().isoformat(),
        "hashtags": ["#注目銘柄", "#投資", "#株式"],
        "status": "draft",
        "template_type": "hot_picks",
    }]


def generate_trade_activity(
    tweets: List[Dict[str, Any]], date_str: str
) -> List[Dict[str, Any]]:
    """売買動向ドラフトを生成（LLM使用）。"""
    trade_tweets = [
        t for t in tweets if "purchased_assets" in get_categories(t)
    ]
    if not trade_tweets:
        return []

    tweets_text = _collect_tweets_text(trade_tweets)
    prompt = build_style_aware_prompt(
        task="trade_activity",
        source_data=tweets_text,
        target_account="kabuki666999",
        target_style="breaking",
        topic="investing",
    )
    body = call_llm(prompt)
    if not body:
        return []

    source_urls = [t.get("url", "") for t in trade_tweets if t.get("url")]
    return [{
        "news_id": make_news_id(f"trade_activity:{date_str}:trades"),
        "source_items": source_urls,
        "title": f"売買動向 ({date_str})",
        "body": body,
        "format": "x_post",
        "scheduled_at": datetime.now().isoformat(),
        "hashtags": ["#売買動向", "#投資", "#株式"],
        "status": "draft",
        "template_type": "trade_activity",
    }]


def generate_earnings_flash(
    tweets: List[Dict[str, Any]], date_str: str
) -> List[Dict[str, Any]]:
    """決算フラッシュドラフトを生成（LLM使用）。"""
    earnings_tweets = [
        t for t in tweets
        if any(kw in t.get("text", "") for kw in EARNINGS_KEYWORDS)
    ]
    if not earnings_tweets:
        return []

    tweets_text = _collect_tweets_text(earnings_tweets)
    prompt = (
        "以下のツイートから決算関連の情報を280文字以内で要約してください。\n\n"
        + tweets_text
    )
    body = call_llm(prompt)
    if not body:
        return []

    source_urls = [t.get("url", "") for t in earnings_tweets if t.get("url")]
    return [{
        "news_id": make_news_id(f"earnings_flash:{date_str}:earnings"),
        "source_items": source_urls,
        "title": f"決算フラッシュ ({date_str})",
        "body": body,
        "format": "x_post",
        "scheduled_at": datetime.now().isoformat(),
        "hashtags": ["#決算", "#投資", "#株式"],
        "status": "draft",
        "template_type": "earnings_flash",
    }]


# --- Main ---

def parse_args() -> argparse.Namespace:
    # プロジェクトルート: extensions/tier3_posting/cli/compose.py → 4階層上
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    default_output_dir = os.path.join(project_root, "output", "posting")

    parser = argparse.ArgumentParser(
        description="収集済みツイートからX投稿ドラフトを生成"
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="入力JSONファイルパス (省略時は最新のtweets_*.jsonを使用)"
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="テンプレートのみモード（LLM呼び出しをスキップ）"
    )
    parser.add_argument(
        "--output-dir", type=str, default=default_output_dir,
        help=f"出力ディレクトリ (default: {default_output_dir})"
    )
    parser.add_argument(
        "--with-images", action="store_true",
        help="ドラフトに合わせた画像を自動生成"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # 画像生成の初期化
    if args.with_images:
        chart_gen, ogp_gen = _get_image_generators()
        print("🖼️ 画像生成モード: ON")
    else:
        chart_gen = ogp_gen = None

    # 入力ファイル決定
    input_path = args.input
    if not input_path:
        input_path = find_latest_input()
        if not input_path:
            print("ERROR: 入力ファイルが見つかりません。--input で指定してください。")
            sys.exit(1)
        print(f"最新ファイルを使用: {input_path}")

    if not os.path.exists(input_path):
        print(f"ERROR: ファイルが存在しません: {input_path}")
        sys.exit(1)

    tweets = load_tweets(input_path)
    print(f"ツイート読み込み: {len(tweets)}件")

    date_str = extract_date(input_path)
    store = PostStore(base_dir=args.output_dir)

    # --- ドラフト生成 ---
    drafts: List[Dict[str, Any]] = []

    # テンプレートベース（常に実行）
    print("\n--- テンプレートベース ---")
    print("  win_rate_ranking ...")
    drafts.extend(generate_win_rate_ranking(date_str))
    print("  contrarian_signal ...")
    drafts.extend(generate_contrarian_signals(tweets, date_str))
    print("  weekly_report ...")
    drafts.extend(generate_weekly_report(tweets, date_str))

    # LLMベース（--no-llm でスキップ）
    if not args.no_llm:
        print("\n--- LLMベース ---")
        print("  market_summary ...")
        drafts.extend(generate_market_summary(tweets, date_str))
        print("  hot_picks ...")
        drafts.extend(generate_hot_picks(tweets, date_str))
        print("  trade_activity ...")
        drafts.extend(generate_trade_activity(tweets, date_str))
        print("  earnings_flash ...")
        drafts.extend(generate_earnings_flash(tweets, date_str))
    else:
        print("\n--- LLMベースはスキップ (--no-llm) ---")

    # --- ストアに保存 ---
    print(f"\n--- 保存 ({len(drafts)}件) ---")
    added = 0
    skipped = 0
    for draft in drafts:
        if args.with_images:
            images = generate_images_for_draft(draft, chart_gen, ogp_gen)
            if images:
                draft["images"] = images
                for img in images:
                    print(f"  🖼️ 生成: {img['description']} -> {img['path']}")
        if store.add_draft(draft):
            print(f"  追加: {draft['title']}")
            added += 1
        else:
            print(f"  スキップ（重複）: {draft['title']}")
            skipped += 1

    print(f"\n完了: {added}件追加, {skipped}件スキップ")


if __name__ == "__main__":
    main()
