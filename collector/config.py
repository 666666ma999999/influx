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
    },
    # plan.md M0 T0.5: Grok 20BD 再評価で score ≥ 50 を満たした TOP
    "group_grok_top": {
        "name": "Grok 20BD 評価 TOP（is_priority）",
        "accounts": [
            {"username": "t_ryoma1985", "min_faves": 30,
             "grok_score": 60.0, "is_priority": True},
            {"username": "serikura", "min_faves": 30,
             "grok_score": 50.0, "is_priority": True},
        ]
    },
    # plan.md M0 T0.8: 予備候補（collect/evaluate 対象外、is_active=False）
    # is_active を参照する side ロジックは M1 で実装（現状フィールド定義のみ）
    "group_reserve": {
        "name": "Grok 予備候補",
        "accounts": [
            {"username": "kazzn_blog", "min_faves": 30,
             "grok_score": 45.5, "is_active": False, "is_reserve": True},
            {"username": "kabuknight", "min_faves": 30,
             "grok_score": 31.2, "is_active": False, "is_reserve": True},
            {"username": "m_kkkiii", "min_faves": 30,
             "grok_score": 20.0, "is_active": False, "is_reserve": True},
            {"username": "purazumakoi", "min_faves": 30,
             "grok_score": 20.0, "is_active": False, "is_reserve": True},
            {"username": "sorave55", "min_faves": 30,
             "grok_score": 20.0, "is_active": False, "is_reserve": True},
        ]
    }
}

# X検索のfrom:オペレータ数の安全上限（X検索の推奨範囲: 5-8アカウント）
MAX_FROM_ACCOUNTS = 5

# min_faves引き下げ係数（Xインデックスの概算値ずれを吸収）
MIN_FAVES_SEARCH_RATIO = 0.33
MIN_FAVES_FLOOR = 3  # 検索用min_favesの最低値


def iter_active_accounts(groups=None):
    """INFLUENCER_GROUPS から is_active=True のアカウントのみを列挙する。

    plan.md M0 T0.8 で追加した is_active フィールドを参照する Canonical Owner。
    収集系・非活動チェック系など「本日の処理対象アカウント集合」を
    決める箇所は全てこの helper 経由で取得する。

    Args:
        groups: 対象グループキーのリスト（None で全グループ）

    Yields:
        (group_key, account_dict) のタプル
    """
    target_keys = list(INFLUENCER_GROUPS.keys()) if groups is None else list(groups)
    for group_key in target_keys:
        group = INFLUENCER_GROUPS.get(group_key)
        if not group:
            continue
        for acc in group.get("accounts", []):
            if acc.get("is_active", True):
                yield group_key, acc


def get_all_active_usernames(groups=None):
    """is_active=True のユーザー名をフラットリストで返す。

    `iter_active_accounts` の薄い便宜ラッパー。順序は INFLUENCER_GROUPS の
    宣言順を保持する（collect/inactive_check のログ並びと一致させるため）。
    """
    return [acc["username"] for _, acc in iter_active_accounts(groups)]


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
    # plan.md M0 T0.8: is_active=False は収集対象外（group_reserve 等の予備アカウント）
    accounts = [acc for acc in accounts if acc.get("is_active", True)]
    if not accounts:
        return []
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
# is_active=False のみで構成されるグループは空 URL リストとなり、ここで除外する
SEARCH_URLS = {
    group_key: urls
    for group_key in INFLUENCER_GROUPS
    for urls in [generate_search_urls(group_key)]
    if urls
}


def build_collect_tasks(groups: list = None, interleave: bool = True,
                        exclude_accounts: set = None,
                        since: str = None, until: str = None,
                        split_per_account: bool = False) -> list:
    """
    全グループのURLをCollectTaskリストに変換

    Args:
        groups: 対象グループキーのリスト（Noneで全グループ）
        interleave: Trueでグループ横断ラウンドロビン
        exclude_accounts: 除外するアカウントのセット
        since: 検索開始日 (YYYY-MM-DD形式)
        until: 検索終了日 (YYYY-MM-DD形式、排他的)
        split_per_account: Trueでアカウントごとに個別検索URL生成（引用ツイートの取りこぼし防止）

    Returns:
        CollectTaskのリスト
    """
    if groups is None:
        groups = list(SEARCH_URLS.keys())

    # exclude_accounts or split_per_account指定時は動的にURL生成
    if exclude_accounts or split_per_account:
        url_source = {
            group_key: generate_search_urls(group_key, exclude_accounts=exclude_accounts,
                                            since=since, until=until,
                                            split_per_account=split_per_account)
            for group_key in groups
            if group_key in INFLUENCER_GROUPS
        }
    elif since or until:
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
            "ゴールド", "ビットコイン", "BTC", "ETF", "投資信託",
            "最強", "有望", "狙い目", "妙味", "出遅れ"
        ],
        "patterns": [
            r"[ぁ-んァ-ン一-龥]+がいい",
            r"[ぁ-んァ-ン一-龥]+を推す",
            r"[ぁ-んァ-ン一-龥]+一択",
            r"金ETF|金価格|金地金|純金|金相場|金先物|金スポット",
            r"(おすすめ|推奨|注目).*?(銘柄|セクター|ETF)"
        ]
    },
    "purchased_assets": {
        "name": "個人で売買している資産",
        "keywords": [
            # 購入系
            "買った", "購入", "追加", "ナンピン", "買い増し",
            "エントリー", "仕込んだ", "仕込み",
            "突っ込んだ", "突っ込み",            # aggressive buying (2026-04-19 拡充)
            "現物", "田中貴金属", "地金", "金貨",
            "イーサリアム", "ETH", "暗号資産", "仮想通貨",
            "インデックス", "積立",
            "ポートフォリオ", "保有", "含み益", "含み損",
            "約定", "指値", "成行", "NISA",
            # ポジション系（FX/信用/デリバ含む、2026-04-19 拡充）
            "ポジる", "ポジった", "ポジション構築",
            "ガチホ",
            "レバレッジ",  # "レバ" 単独は "レバ焼き" 等で誤発火するため除外、patterns で金融文脈に限定
            # 以下は全て多義語のため bare keyword から除外、patterns で金融文脈マッチ:
            # "ロング", "ショート", "スワップ", "塩漬け", "レバ", "ホールド"
            # 売却系（旧 sold_assets を統合）
            "売った", "売却", "利確", "利食い", "損切り", "ロスカット",
            "手放した", "全売り", "一部売却", "ポジション解消",
            "エグジット", "出金", "引き揚げ",
            # 損益報告系（旧 winning_trades を統合）
            "爆益", "勝ち", "勝った", "大勝ち", "プラス転換",
            "利益確定", "プラ転", "勝率",
            "リターン", "パフォーマンス", "運用益", "配当金",
            "儲かった", "儲けた", "黒字"
        ],
        "patterns": [
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+をイン",
            r"(全力|株|銘柄|ドル|ゴールド|ビットコイン|BTC|ETH).*イン(?!フ|サ|タ|デ|ス|パ|ド|ナ)",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+エントリー",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+仕込(んだ|み)",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+(に|を)突っ込",  # 2026-04-19 拡充
            r"(売り|売却).*?(完了|済み|した)",
            r"(利確|利食い|損切り).*?(した|済み|完了)",
            r"(ポジション|持ち株).*?(解消|整理|縮小|構築)",
            # 多義語は金融文脈必須（2026-04-19 FP 対策）
            r"(株|銘柄|円|ドル|日経|TOPIX|FX|CFD|BTC|ETH|XRP|コイン|仮想通貨|暗号資産|先物|金融)\w*(ロング|ショート)",
            r"(ロング|ショート)\w*(持|乗|エントリー|仕込|利確|損切|撤退|爆益)",
            r"(ポジション|持ち株|銘柄|株|FX|ドル|円|BTC|ETH)\w*塩漬け",
            r"塩漬け\w*(ポジ|株|銘柄)",
            r"(株|銘柄|FX|BTC|コイン|ポジション|ガチ|長期)\w*ホールド",
            r"ホールド\w*(株|銘柄|ポジ|BTC|ETH|コイン|投資)",
            r"(FX|ドル|通貨|金利|為替|ポジション)\w*スワップ",
            r"スワップ\w*(収益|金|狙い|取|付|乗|ポイント)",
            r"レバ\w*(倍|\d|エントリー|ポジ|ポジション|効か|掛け)",
            r"(\d|証拠金|CFD|信用|先物)\w*レバ",
            r"[+＋][0-9].*?(万|%|円|pips)",
            r"(利益|収益|リターン).*?[0-9]+(万|%|円)",
            r"(勝率|的中率).*?[0-9]"
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
            "利上げ", "利下げ", "インフレ", "円安", "円高",
            "GDP", "景気", "金利", "国債", "為替",
            "関税", "VIX", "決算", "業績",
            "マクロ", "リセッション", "スタグフレーション"
        ],
        "patterns": [
            r"(政策|関税|規制).*?(変更|引き上げ|発動|撤廃)",
            r"(資金|マネー).*?(シフト|流入|流出)",
            r"(GDP|景気|経済).*?(成長|減速|後退|回復)"
        ]
    },
    "bullish_assets": {
        "name": "高騰している資産",
        "keywords": [
            "爆上げ", "急騰", "ストップ高", "S高", "上昇",
            "が強い", "絶好調",
            "好調", "上方修正", "増益", "増収", "最高値",
            "年初来高値", "高値更新", "続伸", "反発"
        ],
        "patterns": [
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+が強い",
            r"[ぁ-んァ-ン一-龥A-Za-z0-9]+絶好調",
            r"[0-9]+倍",
            r"(決算|業績).*?(好調|上方|増益|増収)",
            r"前[日年期]比.*?[+＋][0-9]"
        ]
    },
    "bearish_assets": {
        "name": "下落している資産",
        "keywords": [
            "暴落", "急落", "ストップ安", "S安", "下落",
            "が弱い", "終わった",
            "割高", "続落", "反落", "安値更新", "年初来安値",
            "減益", "減収", "下方修正"
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
            "天井", "底打ち", "暴落フラグ",
            "過熱", "危機", "警戒"
        ],
        "contrarian_accounts": ["gihuboy"],
        "patterns": [
            r"(流動性|信用|融資).*?(不足|懸念|危機|収縮)",
            r"(機関投資家|ヘッジファンド).*?(売り|ショート)"
        ]
    }
}


# カテゴリ → テンプレート対応表（plan.md M1 T1.0 で正規化）
# tier3_posting/cli/compose.py から参照され、ドラフト生成時の振り分けを決定する
CATEGORY_TEMPLATE_MAP = {
    "recommended_assets": "hot_picks",
    "purchased_assets":   "trade_activity",
    "ipo":                "hot_picks",          # IPO サブテンプレート扱い
    "market_trend":       "market_summary",
    "bullish_assets":     "hot_picks",
    "bearish_assets":     "market_summary",     # 従来未割り当てだったため明示化
    "warning_signals":    "contrarian_signal",
}

# 旧 9 カテゴリ → 新 7 カテゴリ マッピング（過去データ移行用）
LEGACY_CATEGORY_MAP = {
    "sold_assets":     "purchased_assets",   # 売買活動として統合
    "winning_trades":  "purchased_assets",   # 個人の損益報告として統合
}


# 逆指標アカウントの発言が warning_signals 扱いされるトリガーカテゴリ
# Single Source of Truth (plan.md M1 残タスク + ユーザー指示 2026-04-19)
#
# ユーザー方針: gihuboy は「逆神」として投資関連カテゴリが 1 つでも付いたら警戒シグナル化する。
# 日常投稿（カテゴリ空のツイート）のみ除外。これにより 6/7 カテゴリが警戒トリガーになる。
# （warning_signals 自身は除外 — 自己重複を避けるため）
CONTRARIAN_TRIGGER_CATEGORIES = {
    "recommended_assets",
    "purchased_assets",
    "ipo",
    "market_trend",
    "bullish_assets",
    "bearish_assets",
}


def apply_contrarian_override(
    is_contrarian: bool, categories: list
) -> list:
    """逆指標アカウントの投資関連カテゴリ該当時に warning_signals を追加する。

    plan.md: classifier.py と llm_classifier.py の両経路で同一ロジックを強制するため、
    本モジュールを Single Source of Truth とする。

    ユーザー指示 (2026-04-19): gihuboy は「逆神」のため、投資関連カテゴリ (6/7) が
    1 つでも付いたら warning_signals を追加する。日常投稿（カテゴリ空）のみ除外。

    Args:
        is_contrarian: 逆指標アカウントかどうか
        categories: 分類結果カテゴリリスト（ミュータブル想定だが非破壊で返す）

    Returns:
        必要に応じて warning_signals を末尾追加した新リスト
    """
    if not is_contrarian:
        return list(categories)
    result = list(categories)
    if set(result) & CONTRARIAN_TRIGGER_CATEGORIES and "warning_signals" not in result:
        result.append("warning_signals")
    return result


# カテゴリ → 投稿アカウント Single Source of Truth (plan.md M5 T5.3 で正規化)
# tier3_posting/account_routing.py から参照され、TEMPLATE_ROUTING を自動導出する
# 全 7 カテゴリは現在 @kabuki666999 に振り分け（投資/仮想通貨アカウント）
# @maaaki は Claude/AI/経営系の手動投稿用に温存
CATEGORY_ACCOUNT_MAP = {
    "recommended_assets": "kabuki666999",
    "purchased_assets":   "kabuki666999",
    "ipo":                "kabuki666999",
    "market_trend":       "kabuki666999",
    "bullish_assets":     "kabuki666999",
    "bearish_assets":     "kabuki666999",
    "warning_signals":    "kabuki666999",
}

# カテゴリ駆動でないテンプレート（週次/勝率ランキング/決算）の明示マッピング
NON_CATEGORY_TEMPLATE_ACCOUNT_MAP = {
    "win_rate_ranking": "kabuki666999",
    "weekly_report":    "kabuki666999",
    "earnings_flash":   "kabuki666999",
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
# plan.md M0: マルチアカウント移行に伴い x_profiles/maaaki を既定とする
# （旧 x_profile/ パスは廃止、import_chrome_cookies.py が x_profiles/<account>/ に書き込む）
PROFILE_PATH = "./x_profiles/maaaki"

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


# Grok Discovery設定
DISCOVERY_CONFIG = {
    "model": "grok-4-1-fast-non-reasoning",
    "keyword_batch_size": 5,
    "network_batch_size": 8,
    "max_retries": 3,
    "retry_backoff_base": 2.0,
    "timeout_seconds": 120,
    "min_followers": 3000,
    "default_max_candidates": 50,
    "batch_result_limit": 15,
}

# スクリーニング設定
SCREENING_CONFIG = {
    "screen_batch_size": 10,       # allowed_x_handles上限
    "screen_result_limit": 5,      # 候補あたりの代表ツイート数
    "screen_min_score": 20,        # 最低relevanceスコア
    "screen_cooldown_sec": 2,      # バッチ間クールダウン
}

# リサーチ用キーワード
RESEARCH_KEYWORDS = [
    # --- batch 1 (軸A,B,C,D,E) ---
    "株クラ 日本株",              # A: コミュニティ
    "スイングトレード 日本株",     # B: 投資スタイル
    "利確 損切り",                # C: 売買行動
    "決算跨ぎ 決算プレー",        # D: 決算・還元
    "半導体 AI",                  # E: 旬テーマ
    # --- batch 2 ---
    "個別株 投資家",              # A
    "デイトレ 日本株",            # B
    "押し目買い ナンピン",         # C
    "決算 上方修正",              # D
    "防衛 宇宙",                  # E
    # --- batch 3 ---
    "億り人 FIRE",                # A
    "IPO セカンダリー",           # B
    "ストップ高 ストップ安",       # C
    "増配 自社株買い",             # D
    "データセンター AIインフラ",    # E
    # --- batch 4 ---
    "兼業投資家 日本株",           # A
    "小型株 成長株",              # B
    "含み益 含み損",              # C
    "連続増配 高配当株",           # D
    "再生医療 iPS細胞",           # E
    # --- batch 5 ---
    "投資家さんと繋がりたい",      # A
    "株主優待 優待株",            # B
    "エントリー 新規買い",         # C
    "決算短信 会社予想",           # D
    "量子コンピューター レアアース", # E
    # --- batch 6 ---
    "テクニカル分析 チャート",      # B
    "ファンダメンタル分析 財務",    # B
    "原子力 SMR",                 # E
    "サイバーセキュリティ 防衛",    # E
    "造船 海運",                  # E
]
