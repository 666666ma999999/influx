"""
X投稿収集システム設定ファイル
"""
from dataclasses import dataclass


@dataclass
class CollectTask:
    """収集タスク1件を表すデータクラス"""
    search_url: str
    group_key: str
    group_name: str
    is_contrarian: bool = False
    url_type: str = "normal"  # "normal" or "quote"
    url_index: int = 0
    retries: int = 0
    status: str = "pending"   # "pending", "completed", "failed", "blocked"
    error_message: str = ""


# インフルエンサーグループ定義（アカウントごとにmin_favesを個別設定）
INFLUENCER_GROUPS = {
    "group1": {
        "name": "超大型インフルエンサー",
        "accounts": [
            {"username": "goto_finance", "min_faves": 400},
            {"username": "cissan_9984", "min_faves": 400},
            {"username": "tesuta001", "min_faves": 300},
            {"username": "yurumazu", "min_faves": 300},
        ]
    },
    "group2": {
        "name": "大型インフルエンサー",
        "accounts": [
            {"username": "2okutameo", "min_faves": 150},
            {"username": "tapazou29", "min_faves": 150},
            {"username": "kanpo_blog", "min_faves": 80},
            {"username": "haru_tachibana8", "min_faves": 80},
            {"username": "utbuffett", "min_faves": 80},
            {"username": "heihachiro888", "min_faves": 80},
        ]
    },
    "group3": {
        "name": "中型インフルエンサー",
        "accounts": [
            {"username": "uehara_sato4", "min_faves": 30},
            {"username": "miku919191", "min_faves": 30},
            {"username": "yuki75868813751", "min_faves": 30},
            {"username": "shikiho_10", "min_faves": 30},
            {"username": "kakatothecat", "min_faves": 30},
            {"username": "yukimamax", "min_faves": 30},
            {"username": "tomoyaasakura", "min_faves": 30},
            {"username": "_teeeeest", "min_faves": 30},
            {"username": "paurooteri", "min_faves": 30},
        ]
    },
    "group4": {
        "name": "小型インフルエンサー",
        "accounts": [
            {"username": "momoblog0214", "min_faves": 20},
            {"username": "pay_cashless", "min_faves": 20},
            {"username": "nobutaro_mane", "min_faves": 20},
            {"username": "w_coast_0330", "min_faves": 20},
        ]
    },
    "group5": {
        "name": "極小型インフルエンサー",
        "accounts": [
            {"username": "YasLovesTech", "min_faves": 10},
            {"username": "Toushi_kensh", "min_faves": 10},
            {"username": "Kosukeitou", "min_faves": 10},
            {"username": "piya00piya", "min_faves": 10},
            {"username": "yys87495867", "min_faves": 10},
            {"username": "Adscience12000", "min_faves": 10},
            {"username": "hd_qu8", "min_faves": 10},
        ]
    },
    "group6": {
        "name": "逆指標インフルエンサー",
        "accounts": [
            {"username": "gihuboy", "min_faves": 50},
        ],
        "is_contrarian": True  # 逆指標フラグ
    }
}

# X検索のfrom:オペレータ数の安全上限（X検索の推奨範囲: 5-8アカウント）
MAX_FROM_ACCOUNTS = 5

# min_faves引き下げ係数（Xインデックスの概算値ずれを吸収）
MIN_FAVES_SEARCH_RATIO = 0.33
MIN_FAVES_FLOOR = 3  # 検索用min_favesの最低値


# X検索URL生成
def generate_search_urls(group_key: str, since: str = None, until: str = None,
                         split_per_account: bool = False,
                         days_back: int = 3,
                         exclude_accounts: set = None) -> list:
    """
    グループに対応する検索URLリストを生成
    アカウントをmin_favesごとにサブグループ化し、MAX_FROM_ACCOUNTSでチャンク分割

    検索クエリには以下の最適化を適用:
    - min_favesをMIN_FAVES_SEARCH_RATIO倍に引き下げ（Xインデックスの概算値ずれ吸収）
    - since/until未指定時はdays_back日前〜翌日を自動計算
    - -filter:nativeretweetsでRTノイズを除去
    - filter:quoteで引用ツイートも別途検索

    Args:
        group_key: "group1" ~ "group6"
        since: 開始日 (YYYY-MM-DD形式)。Noneの場合はdays_back日前を自動設定
        until: 終了日 (YYYY-MM-DD形式、排他的)。Noneの場合は翌日を自動設定
        split_per_account: Trueの場合、アカウントごとに個別の検索URLを生成
            （1つのURLの失敗で複数アカウントが欠落するのを防ぐ）
        days_back: since未指定時に何日前から検索するか（デフォルト: 3日）

    Returns:
        検索URLのリスト
    """
    import urllib.parse
    from datetime import datetime, timedelta

    group = INFLUENCER_GROUPS[group_key]
    accounts = group["accounts"]
    if exclude_accounts:
        accounts = [acc for acc in accounts if acc["username"] not in exclude_accounts]
        if not accounts:
            return []

    # since/until 自動計算
    if since is None:
        since = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    if until is None:
        until = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    urls = []

    if split_per_account:
        # アカウントごとに個別URL生成
        for acc in accounts:
            search_min_faves = max(MIN_FAVES_FLOOR, int(acc["min_faves"] * MIN_FAVES_SEARCH_RATIO))
            query = (f"min_faves:{search_min_faves} from:{acc['username']}"
                     f" since:{since} until:{until} -filter:nativeretweets")

            encoded = urllib.parse.quote(query)
            url = f"https://x.com/search?q={encoded}&src=typed_query&f=live"
            urls.append(url)

        # 引用ツイート検索URL追加（min_favesなし、filter:quote付き）
        for acc in accounts:
            quote_query = f"from:{acc['username']} since:{since} until:{until} filter:quote"
            encoded = urllib.parse.quote(quote_query)
            url = f"https://x.com/search?q={encoded}&src=typed_query&f=live"
            urls.append(url)
    else:
        # min_favesごとにサブグループ化（既存動作）
        faves_groups = {}
        for acc in accounts:
            mf = acc["min_faves"]
            if mf not in faves_groups:
                faves_groups[mf] = []
            faves_groups[mf].append(acc["username"])

        for min_faves, usernames in sorted(faves_groups.items()):
            search_min_faves = max(MIN_FAVES_FLOOR, int(min_faves * MIN_FAVES_SEARCH_RATIO))

            # MAX_FROM_ACCOUNTSでチャンク分割
            for i in range(0, len(usernames), MAX_FROM_ACCOUNTS):
                chunk = usernames[i:i + MAX_FROM_ACCOUNTS]
                if len(chunk) == 1:
                    from_query = f"from:{chunk[0]}"
                else:
                    from_parts = " OR ".join(f"from:{u}" for u in chunk)
                    from_query = f"({from_parts})"

                query = (f"min_faves:{search_min_faves} {from_query}"
                         f" since:{since} until:{until} -filter:nativeretweets")

                encoded = urllib.parse.quote(query)
                url = f"https://x.com/search?q={encoded}&src=typed_query&f=live"
                urls.append(url)

        # 引用ツイート検索URL追加
        # 全アカウントをMAX_FROM_ACCOUNTSでチャンク分割（min_favesでのグループ化は不要）
        all_usernames = [acc["username"] for acc in accounts]
        for i in range(0, len(all_usernames), MAX_FROM_ACCOUNTS):
            chunk = all_usernames[i:i + MAX_FROM_ACCOUNTS]
            if len(chunk) == 1:
                from_query = f"from:{chunk[0]}"
            else:
                from_parts = " OR ".join(f"from:{u}" for u in chunk)
                from_query = f"({from_parts})"

            quote_query = f"{from_query} since:{since} until:{until} filter:quote"
            encoded = urllib.parse.quote(quote_query)
            url = f"https://x.com/search?q={encoded}&src=typed_query&f=live"
            urls.append(url)

    return urls


# 事前定義された検索URL（各グループはURLリスト、generate_search_urls()で自動生成）
SEARCH_URLS = {
    group_key: generate_search_urls(group_key)
    for group_key in INFLUENCER_GROUPS
}


def build_collect_tasks(groups: list = None, interleave: bool = True,
                        exclude_accounts: set = None,
                        since: str = None, until: str = None) -> list:
    """
    全グループのURLをCollectTaskリストに変換

    Args:
        groups: 対象グループキーのリスト（Noneで全グループ）
        interleave: Trueでグループ横断ラウンドロビン
        exclude_accounts: 除外するアカウントのセット
        since: 検索開始日 (YYYY-MM-DD形式)
        until: 検索終了日 (YYYY-MM-DD形式、排他的)

    Returns:
        CollectTaskのリスト
    """
    if groups is None:
        groups = list(SEARCH_URLS.keys())

    # exclude_accounts指定時は動的にURL生成
    if exclude_accounts:
        url_source = {
            group_key: generate_search_urls(group_key, exclude_accounts=exclude_accounts,
                                            since=since, until=until)
            for group_key in groups
            if group_key in INFLUENCER_GROUPS
        }
    else:
        # since/until指定時も動的にURL生成
        if since or until:
            url_source = {
                group_key: generate_search_urls(group_key, since=since, until=until)
                for group_key in groups
                if group_key in INFLUENCER_GROUPS
            }
        else:
            url_source = SEARCH_URLS

    # グループごとに通常URLと引用URLを分離
    normal_by_group = {}
    quote_by_group = {}

    for group_key in groups:
        if group_key not in url_source:
            continue
        group_info = INFLUENCER_GROUPS.get(group_key, {})
        group_name = group_info.get('name', group_key)
        is_contrarian = group_info.get('is_contrarian', False)

        normal_by_group[group_key] = []
        quote_by_group[group_key] = []

        for idx, url in enumerate(url_source[group_key]):
            is_quote = 'filter%3Aquote' in url or 'filter:quote' in url
            task = CollectTask(
                search_url=url,
                group_key=group_key,
                group_name=group_name,
                is_contrarian=is_contrarian,
                url_type="quote" if is_quote else "normal",
                url_index=idx,
            )
            if is_quote:
                quote_by_group[group_key].append(task)
            else:
                normal_by_group[group_key].append(task)

    if not interleave:
        tasks = []
        for group_key in groups:
            tasks.extend(normal_by_group.get(group_key, []))
            tasks.extend(quote_by_group.get(group_key, []))
        return tasks

    # インターリーブ: ラウンドロビン
    tasks = []
    _interleave_round_robin(tasks, normal_by_group, groups)
    _interleave_round_robin(tasks, quote_by_group, groups)
    return tasks


def _interleave_round_robin(result: list, by_group: dict, groups: list):
    """グループ横断ラウンドロビンでresultに追加"""
    max_len = max((len(v) for v in by_group.values()), default=0)
    for i in range(max_len):
        for group_key in groups:
            urls = by_group.get(group_key, [])
            if i < len(urls):
                result.append(urls[i])


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
    "max_scrolls": 180,          # 1セッションあたりの最大スクロール回数（動的終了あり）
    "min_wait_sec": 5,           # 最小待機時間（秒）
    "max_wait_sec": 12,          # 最大待機時間（秒）
    "reading_probability": 0.3,  # 読み込み動作を入れる確率
    "reading_min_sec": 8,        # 読み込み動作の最小時間
    "reading_max_sec": 20,       # 読み込み動作の最大時間
    "scroll_min": 400,           # スクロール量の最小値
    "scroll_max": 700,           # スクロール量の最大値
    "stop_after_empty": 3,       # 新規0件が連続N回で終了
    "url_wait_min_sec": 60,      # URL間待機時間の最小値（秒）
    "url_wait_max_sec": 90,      # URL間待機時間の最大値（秒）
    "group_wait_min_sec": 120,   # グループ間待機時間の最小値（秒）
    "group_wait_max_sec": 180,   # グループ間待機時間の最大値（秒）
}

# バッチ実行設定
BATCH_SETTINGS = {
    "batch_size": 3,                          # バッチあたりURL数
    "cooldown_sec": 900,                      # バッチ間クールダウン（15分）
    "max_retries": 3,                         # 最大リトライ回数
    "retry_wait_sec": [900, 1800, 3600],      # リトライ待機時間（15分, 30分, 60分）
    "block_cooldown_sec": 3600,               # ブロック時クールダウン（60分）
}

# ブロック検知エラーパターン
BLOCK_ERROR_PATTERNS = [
    "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_TIMED_OUT",
    "net::ERR_", "NS_ERROR_", "429",
]


# プロファイルパス
PROFILE_PATH = "./x_profile"

# 出力ディレクトリ
OUTPUT_DIR = "./output"
DATA_DIR = "./data"


# LLM分類器設定
LLM_CONFIG = {
    "model": "claude-3-5-haiku-20241022",  # 使用するClaudeモデル
    "batch_size": 20,                      # 一度に処理するツイート数
    "max_tokens": 4096,                    # 最大トークン数
    "few_shot_path": "data/few_shot_examples.json",  # Few-shot例のJSONファイルパス
    "max_retries": 3,                     # API呼び出しの最大リトライ回数
    "retry_backoff_base": 2.0             # リトライ時の待機時間の基数（指数バックオフ）
}
