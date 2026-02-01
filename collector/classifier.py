"""
ツイート分類クラス
7カテゴリへの分類を行う
"""

import re
from typing import List, Dict, Set
from .config import CLASSIFICATION_RULES, INFLUENCER_GROUPS


class TweetClassifier:
    """
    ツイートを7カテゴリに分類するクラス

    カテゴリ:
    1. recommended_assets: オススメしている資産・セクター
    2. purchased_assets: 個人で購入・保有している資産
    3. ipo: 申し込んだIPO
    4. market_trend: 市況トレンドに関する見解
    5. bullish_assets: 高騰している資産
    6. bearish_assets: 下落している資産
    7. warning_signals: 警戒すべき動き・逆指標シグナル
    """

    def __init__(self, rules: Dict = None):
        """
        Args:
            rules: 分類ルール（省略時はデフォルトルールを使用）
        """
        self.rules = rules or CLASSIFICATION_RULES
        self._compile_patterns()

    def _compile_patterns(self):
        """正規表現パターンをコンパイル"""
        self.compiled_patterns = {}

        for category, rule in self.rules.items():
            patterns = rule.get('patterns', [])
            self.compiled_patterns[category] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

    def classify(self, tweet: Dict) -> Dict:
        """
        単一ツイートを分類

        Args:
            tweet: ツイートデータ

        Returns:
            分類結果を追加したツイートデータ
        """
        text = tweet.get('text', '')
        username = tweet.get('username', '')
        is_contrarian = tweet.get('is_contrarian', False)

        categories = []
        category_details = {}

        for category, rule in self.rules.items():
            matched_keywords = []
            matched_patterns = []

            # キーワードマッチング
            keywords = rule.get('keywords', [])
            for keyword in keywords:
                if keyword.lower() in text.lower():
                    matched_keywords.append(keyword)

            # パターンマッチング
            patterns = self.compiled_patterns.get(category, [])
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    matched_patterns.append(match.group())

            # 逆指標アカウントのチェック（warning_signalsカテゴリ）
            contrarian_accounts = rule.get('contrarian_accounts', [])
            is_from_contrarian = username.lower() in [a.lower() for a in contrarian_accounts]

            # 分類判定
            if matched_keywords or matched_patterns or is_from_contrarian:
                categories.append(category)
                category_details[category] = {
                    'name': rule.get('name', category),
                    'matched_keywords': matched_keywords,
                    'matched_patterns': matched_patterns,
                    'is_from_contrarian': is_from_contrarian
                }

        # 逆指標アカウントからの投稿は警告シグナルに追加
        if is_contrarian and 'warning_signals' not in categories:
            categories.append('warning_signals')
            category_details['warning_signals'] = {
                'name': self.rules['warning_signals'].get('name', '警戒すべき動き'),
                'matched_keywords': [],
                'matched_patterns': [],
                'is_from_contrarian': True,
                'note': '逆指標インフルエンサーからの投稿'
            }

        # 結果をツイートに追加
        tweet['categories'] = categories
        tweet['category_details'] = category_details
        tweet['category_count'] = len(categories)

        return tweet

    def classify_all(self, tweets: List[Dict]) -> List[Dict]:
        """
        複数ツイートを一括分類

        Args:
            tweets: ツイートデータのリスト

        Returns:
            分類結果を追加したツイートのリスト
        """
        return [self.classify(tweet) for tweet in tweets]

    def filter_by_category(
        self,
        tweets: List[Dict],
        category: str
    ) -> List[Dict]:
        """
        特定カテゴリのツイートをフィルタ

        Args:
            tweets: ツイートリスト
            category: カテゴリ名

        Returns:
            該当カテゴリを含むツイートのリスト
        """
        return [
            tweet for tweet in tweets
            if category in tweet.get('categories', [])
        ]

    def get_summary(self, tweets: List[Dict]) -> Dict:
        """
        分類結果のサマリーを取得

        Args:
            tweets: 分類済みツイートリスト

        Returns:
            サマリー情報
        """
        summary = {
            'total_tweets': len(tweets),
            'categories': {},
            'uncategorized': 0
        }

        for category in self.rules.keys():
            category_tweets = self.filter_by_category(tweets, category)
            summary['categories'][category] = {
                'name': self.rules[category].get('name', category),
                'count': len(category_tweets)
            }

        # 未分類のカウント
        summary['uncategorized'] = len([
            t for t in tweets if not t.get('categories')
        ])

        return summary

    def print_summary(self, tweets: List[Dict]):
        """
        分類結果のサマリーを表示

        Args:
            tweets: 分類済みツイートリスト
        """
        summary = self.get_summary(tweets)

        print("\n" + "=" * 60)
        print("分類結果サマリー")
        print("=" * 60)
        print(f"総ツイート数: {summary['total_tweets']}")
        print("-" * 60)

        for category, info in summary['categories'].items():
            print(f"  {info['name']}: {info['count']}件")

        print("-" * 60)
        print(f"未分類: {summary['uncategorized']}件")
        print("=" * 60)


def generate_news_data(classified_tweets: List[Dict]) -> Dict:
    """
    分類済みツイートからニュース配信用データを生成

    Args:
        classified_tweets: 分類済みツイートリスト

    Returns:
        ニュース配信用に整理されたデータ
    """
    classifier = TweetClassifier()

    news_data = {
        'generated_at': None,
        'sections': {}
    }

    from datetime import datetime
    news_data['generated_at'] = datetime.now().isoformat()

    # カテゴリごとにセクションを作成
    for category, rule in CLASSIFICATION_RULES.items():
        category_tweets = classifier.filter_by_category(classified_tweets, category)

        if not category_tweets:
            continue

        section = {
            'title': rule.get('name', category),
            'tweet_count': len(category_tweets),
            'tweets': []
        }

        for tweet in category_tweets:
            section['tweets'].append({
                'username': tweet.get('username'),
                'display_name': tweet.get('display_name'),
                'text': tweet.get('text'),
                'url': tweet.get('url'),
                'posted_at': tweet.get('posted_at'),
                'is_contrarian': tweet.get('is_contrarian', False),
                'matched_keywords': tweet.get('category_details', {}).get(category, {}).get('matched_keywords', [])
            })

        news_data['sections'][category] = section

    return news_data


def extract_assets(tweets: List[Dict]) -> Dict[str, Set[str]]:
    """
    ツイートから資産名（銘柄・暗号資産等）を抽出

    Args:
        tweets: ツイートリスト

    Returns:
        カテゴリごとの資産名セット
    """
    # 資産名のパターン（簡易版）
    patterns = {
        'stock_code': r'[0-9]{4}',  # 4桁の銘柄コード
        'ticker': r'\$[A-Z]{1,5}',  # $AAPL形式
        'crypto': r'(BTC|ETH|XRP|SOL|DOGE)',  # 主要暗号資産
    }

    extracted = {
        'stock_codes': set(),
        'tickers': set(),
        'cryptos': set(),
    }

    for tweet in tweets:
        text = tweet.get('text', '')

        # 銘柄コード
        codes = re.findall(patterns['stock_code'], text)
        extracted['stock_codes'].update(codes)

        # ティッカー
        tickers = re.findall(patterns['ticker'], text)
        extracted['tickers'].update(tickers)

        # 暗号資産
        cryptos = re.findall(patterns['crypto'], text, re.IGNORECASE)
        extracted['cryptos'].update([c.upper() for c in cryptos])

    return extracted
