"""
X投稿収集システム設定ファイル
"""

# インフルエンサーグループ定義
INFLUENCER_GROUPS = {
    "group1": {
        "name": "主要インフルエンサー",
        "min_faves": 502,
        "accounts": [
            "tesuta001",
            "goto_finance",
            "yurumazu",
            "cissan_9984",
            "tomoyaasakura",
            "2okutameo",
            "yuki75868813751"
        ]
    },
    "group2": {
        "name": "追加インフルエンサー",
        "min_faves": 72,
        "accounts": [
            "utbuffett",
            "miku919191",
            "YasLovesTech",
            "heihachiro888",
            "tapazou29",
            "_teeeeest",
            "nobutaro_mane",
            "kakatothecat",
            "Toushi_kensh",
            "Kosukeitou",
            "uehara_sato4",
            "pay_cashless",
            "haru_tachibana8",
            "piya00piya",
            "yys87495867",
            "kanpo_blog",
            "w_coast_0330",
            "shikiho_10",
            "Adscience12000",
            "yukimamax",
            "momoblog0214",
            "hd_qu8"
        ]
    },
    "group3": {
        "name": "逆指標インフルエンサー",
        "min_faves": 50,
        "accounts": [
            "gihuboy"
        ],
        "is_contrarian": True  # 逆指標フラグ
    }
}

# X検索URL生成
def generate_search_url(group_key: str, since: str = None, until: str = None) -> str:
    """
    グループに対応する検索URLを生成

    Args:
        group_key: "group1", "group2", "group3"
        since: 開始日 (YYYY-MM-DD形式)
        until: 終了日 (YYYY-MM-DD形式)
    """
    group = INFLUENCER_GROUPS[group_key]
    min_faves = group["min_faves"]
    accounts = group["accounts"]

    # アカウントをOR条件で連結
    if group_key == "group3":
        # group3は1名なのでfrom:を使用
        account_query = f"from:{accounts[0]}"
    else:
        # 複数アカウントは｜で連結
        account_query = " ｜ ".join(accounts)

    # クエリ構築
    query_parts = [f"min_faves:{min_faves}", account_query]

    if since:
        query_parts.append(f"since:{since}")
    if until:
        query_parts.append(f"until:{until}")

    query = " ".join(query_parts)

    # URLエンコード
    import urllib.parse
    encoded_query = urllib.parse.quote(query)

    return f"https://twitter.com/search?q={encoded_query}&f=live&vertical=default"


# 事前定義された検索URL
SEARCH_URLS = {
    "group1": "https://twitter.com/search?q=min_faves%3A502%20%20tesuta001%20%EF%BD%9C%20goto_finance%20%EF%BD%9C%20yurumazu%20%EF%BD%9C%20cissan_9984%20%EF%BD%9C%20tomoyaasakura%20%EF%BD%9C%202okutameo%20%EF%BD%9C%20yuki75868813751&f=live&vertical=default",
    "group2": "https://twitter.com/search?q=min_faves%3A72%20%20utbuffett%20%EF%BD%9C%20miku919191%20%EF%BD%9C%20YasLovesTech%20%EF%BD%9C%20heihachiro888%20%EF%BD%9C%20tapazou29%20%EF%BD%9C%20_teeeeest%20%EF%BD%9C%20nobutaro_mane%20%EF%BD%9C%20kakatothecat%20%EF%BD%9C%20Toushi_kensh%20%EF%BD%9C%20Kosukeitou%20%EF%BD%9C%20uehara_sato4%20%EF%BD%9C%20pay_cashless%20%EF%BD%9C%20haru_tachibana8%20%EF%BD%9C%20piya00piya%20%EF%BD%9C%20yys87495867%20%EF%BD%9C%20kanpo_blog%20%EF%BD%9C%20w_coast_0330%20%EF%BD%9C%20shikiho_10%20%EF%BD%9C%20Adscience12000%20%EF%BD%9C%20yukimamax%20%EF%BD%9C%20momoblog0214%20%EF%BD%9C%20hd_qu8&f=live&vertical=default",
    "group3": "https://twitter.com/search?q=min_faves%3A50%20from%3Agihuboy&f=live&vertical=default"
}


# 分類ルール
CLASSIFICATION_RULES = {
    "recommended_assets": {
        "name": "オススメしている資産・セクター",
        "keywords": [
            "割安", "おすすめ", "ベスト", "PERが落ちている",
            "買い", "チャンス", "激安", "注目", "仕込み",
            "がいい", "を推す", "一択",
            "金", "ゴールド", "ビットコイン", "BTC", "ETF", "投資信託"
        ],
        "patterns": [
            r"[ぁ-んァ-ン一-龥]+がいい",
            r"[ぁ-んァ-ン一-龥]+を推す",
            r"[ぁ-んァ-ン一-龥]+一択"
        ]
    },
    "purchased_assets": {
        "name": "個人で購入・保有している資産",
        "keywords": [
            "買った", "購入", "追加", "ナンピン", "買い増し",
            "イン", "エントリー", "仕込んだ",
            "現物", "田中貴金属", "地金", "金貨",
            "イーサリアム", "ETH", "暗号資産", "仮想通貨",
            "インデックス", "積立",
            "ポートフォリオ", "保有", "含み益", "含み損"
        ],
        "patterns": [
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+をイン",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+エントリー",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+仕込んだ"
        ]
    },
    "ipo": {
        "name": "申し込んだIPO",
        "keywords": [
            "IPO", "新規公開", "抽選", "当選", "落選", "申込"
        ],
        "patterns": []
    },
    "market_trend": {
        "name": "市況トレンドに関する見解",
        "keywords": [
            "相場", "地合い", "トレンド", "センチメント",
            "利上げ", "利下げ", "インフレ", "円安", "円高"
        ],
        "patterns": []
    },
    "bullish_assets": {
        "name": "高騰している資産",
        "keywords": [
            "爆上げ", "急騰", "ストップ高", "S高", "上昇",
            "が強い", "絶好調"
        ],
        "patterns": [
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+が強い",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+絶好調"
        ]
    },
    "bearish_assets": {
        "name": "下落している資産",
        "keywords": [
            "暴落", "急落", "ストップ安", "S安", "下落",
            "が弱い", "終わった"
        ],
        "patterns": [
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+が弱い",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+終わった"
        ]
    },
    "warning_signals": {
        "name": "警戒すべき動き・逆指標シグナル",
        "keywords": [
            "岐阜", "ぎふ", "岐阜暴威",
            "ジム・ロジャーズ", "Jim Rogers",
            "信用買い残", "過去最高", "バブル",
            "天井", "底打ち", "暴落フラグ"
        ],
        "contrarian_accounts": ["gihuboy"],
        "patterns": []
    }
}


# 収集設定
COLLECTION_SETTINGS = {
    "max_scrolls": 20,           # 1セッションあたりの最大スクロール回数（動的終了あり）
    "min_wait_sec": 3,           # 最小待機時間（秒）
    "max_wait_sec": 8,           # 最大待機時間（秒）
    "reading_probability": 0.2,  # 読み込み動作を入れる確率
    "reading_min_sec": 5,        # 読み込み動作の最小時間
    "reading_max_sec": 12,       # 読み込み動作の最大時間
    "scroll_min": 400,           # スクロール量の最小値
    "scroll_max": 700,           # スクロール量の最大値
    "stop_after_empty": 3,       # 新規0件が連続N回で終了
}


# プロファイルパス
PROFILE_PATH = "./x_profile"

# 出力ディレクトリ
OUTPUT_DIR = "./output"
DATA_DIR = "./data"
