"""ツイートからティッカーシンボル・銘柄名を抽出するモジュール"""

import re
from typing import List, Dict, Any, Optional


# $記号付きティッカーパターン
TICKER_PATTERN = r'\$([A-Z]{1,5})\b'

# 日本語銘柄名→ティッカーマッピング
JAPANESE_TICKER_MAP = {
    "ブロードコム": "AVGO",
    "マイクロン": "MU",
    "メタプラネット": "3350.T",
    "ソフトバンクグループ": "9984.T",
    "ソフトバンクG": "9984.T",
    "トヨタ": "7203.T",
    "トヨタ自動車": "7203.T",
    "任天堂": "7974.T",
    "ソニー": "6758.T",
    "テスラ": "TSLA",
    "エヌビディア": "NVDA",
    "アップル": "AAPL",
    "マイクロソフト": "MSFT",
    "アマゾン": "AMZN",
    "メタ": "META",
    "グーグル": "GOOGL",
    "アルファベット": "GOOGL",
    "ビットコイン": "BTC-USD",
    "BTC": "BTC-USD",
    "イーサリアム": "ETH-USD",
    "ETH": "ETH-USD",
    "ゴールド": "GC=F",
    "金ETF": "GC=F",
    "金価格": "GC=F",
    "金地金": "GC=F",
    "日経平均": "^N225",
    "日経": "^N225",
    "S&P500": "^GSPC",
    "SP500": "^GSPC",
    "ダウ": "^DJI",
    "ダウ平均": "^DJI",
    "ナスダック100": "QQQ",
    "ナスダック": "QQQ",
    "ナス100": "QQQ",
}

# ETF・ファンド名→ティッカーマッピング
ETF_MAP = {
    "FANG+": "FNGS",
    "FANG＋": "FNGS",
    "M7": "MAGS",
    "マグニフィセント7": "MAGS",
    "マグニフィセントセブン": "MAGS",
    "韓国株ETF": "EWY",
    "韓国ETF": "EWY",
    "オルカン": "ACWI",
    "全世界株式": "ACWI",
    "全世界株": "ACWI",
}

# カテゴリのコンテキスト変換
CATEGORY_CONTEXT_MAP = {
    "recommended_assets": "推奨",
    "purchased_assets": "購入",
    "sold_assets": "売却",
    "bullish_assets": "高騰",
    "bearish_assets": "下落",
    "warning_signals": "警戒",
    "market_trend": "市況",
    "ipo": "IPO",
    "winning_trades": "勝ちトレード",
}


class TickerExtractor:
    """ツイートからティッカーシンボルを抽出するクラス"""

    def __init__(self):
        """初期化"""
        self.ticker_pattern = re.compile(TICKER_PATTERN)
        # 日本語マッピングは長い方から先にマッチさせる（部分マッチ防止）
        self.jp_map_sorted = sorted(
            JAPANESE_TICKER_MAP.items(), key=lambda x: len(x[0]), reverse=True
        )
        self.etf_map_sorted = sorted(
            ETF_MAP.items(), key=lambda x: len(x[0]), reverse=True
        )

    def extract(self, tweet: Dict[str, Any]) -> List[Dict[str, Any]]:
        """ツイートからティッカーを抽出する。

        Args:
            tweet: ツイートデータの辞書。以下のキーを使用:
                - text: ツイート本文
                - llm_categories / categories: 分類カテゴリ
                - llm_reasoning: LLM分類理由

        Returns:
            抽出結果のリスト。各要素は:
                - ticker: ティッカーシンボル (e.g., "AVGO")
                - source: 抽出方法 ("regex" / "jp_mapping" / "etf_mapping")
                - context: コンテキスト (e.g., "推奨", "購入")
                - matched_text: マッチしたテキスト
        """
        text = tweet.get("text", "")
        if not text:
            return []

        results = []
        seen_tickers = set()
        context = self._get_context(tweet)

        # 1. $記号付きティッカーを正規表現で抽出
        for match in self.ticker_pattern.finditer(text):
            ticker = match.group(1)
            if ticker not in seen_tickers:
                seen_tickers.add(ticker)
                results.append({
                    "ticker": ticker,
                    "source": "regex",
                    "context": context,
                    "matched_text": match.group(0),
                })

        # 2. 日本語銘柄名マッピング
        for jp_name, ticker in self.jp_map_sorted:
            if jp_name in text and ticker not in seen_tickers:
                seen_tickers.add(ticker)
                results.append({
                    "ticker": ticker,
                    "source": "jp_mapping",
                    "context": context,
                    "matched_text": jp_name,
                })

        # 3. ETF・ファンド名マッピング
        for etf_name, ticker in self.etf_map_sorted:
            if etf_name in text and ticker not in seen_tickers:
                seen_tickers.add(ticker)
                results.append({
                    "ticker": ticker,
                    "source": "etf_mapping",
                    "context": context,
                    "matched_text": etf_name,
                })

        return results

    def _get_context(self, tweet: Dict[str, Any]) -> str:
        """ツイートのカテゴリからコンテキスト文字列を生成する。

        Args:
            tweet: ツイートデータ

        Returns:
            コンテキスト文字列 (e.g., "推奨", "購入")
        """
        categories = tweet.get("llm_categories", tweet.get("categories", []))
        if not categories:
            return "不明"

        contexts = []
        for cat in categories:
            if cat in CATEGORY_CONTEXT_MAP:
                contexts.append(CATEGORY_CONTEXT_MAP[cat])

        return "/".join(contexts) if contexts else "不明"

    def extract_batch(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """複数ツイートからティッカーを一括抽出する。

        Args:
            tweets: ツイートデータのリスト

        Returns:
            ティッカー抽出結果付きのツイートリスト。各ツイートに extracted_tickers フィールドが追加される。
        """
        for tweet in tweets:
            tweet["extracted_tickers"] = self.extract(tweet)
        return tweets
