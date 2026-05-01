> **グローバルルール準拠**: ~/.claude/CLAUDE.md および ~/.claude/rules/ のルールに従うこと。

# influx - X(Twitter)株式インフルエンサー ツイート収集・LLM分類システム

## プロジェクト概要

X(Twitter)上の株式投資インフルエンサーのツイートをPlaywrightで自動収集し、キーワードベース分類とClaude APIによるLLM分類の2段階で7カテゴリに分類するシステム。収集データはHTMLビューア（`output/viewer.html`）で閲覧可能。

## アーキテクチャ

### モジュール構成

```
influx/
├── collector/                  # コアモジュール
│   ├── config.py              # インフルエンサー定義（個別min_faves）・分類ルール・収集設定・LLM設定
│   ├── x_collector.py         # SafeXCollector: Playwright+Cookie認証によるツイート収集
│   ├── classifier.py          # TweetClassifier: キーワード/正規表現ベース分類
│   └── llm_classifier.py      # LLMClassifier: Claude API(urllib)によるバッチ分類
# 2026-05-01 Phase 3: extensions/tier3_posting/ は ~/Desktop/biz/tier3_posting/ に物理分離
# 投稿管理（管理画面・register_external/mark_posted/get_correction CLI 等）は
# 別リポ tier3_posting で稼働。詳細はそちらの CLAUDE.md 参照。
├── scripts/                    # 収集・分類スクリプト
│   ├── collect_tweets.py      # ツイート収集 + キーワード分類 + JSON/CSV保存
│   ├── classify_tweets.py     # LLM分類実行 + viewer.html更新
│   ├── check_inactive_accounts.py    # アカウント状態・最終投稿日確認
│   └── import_chrome_cookies.py  # ホスト macOS Chrome から X Cookie 抽出（唯一の確実経路、2026-04-21）
├── data/
│   └── few_shot_examples.json # LLM分類のFew-shot例（24例）
├── output/                    # 生成ファイル（.gitignoreでJSON/CSVは除外）
│   ├── viewer.html            # ツイート閲覧用HTMLビューア（EMBEDDED_DATAにJSON埋込）
│   ├── tweets_*.json          # 収集済みツイート
│   ├── classified_llm_*.json  # LLM分類済みツイート
│   └── inactive_check_result.json  # アカウント状態チェック結果
├── x_profile/                 # ブラウザプロファイル（cookies.json等、.gitignore対象）
├── Dockerfile                 # 標準ビルド（playwright/python:v1.57.0-jammy + 日本語フォント）
├── Dockerfile.vnc             # VNCビルド（Xvfb + x11vnc + fluxbox + noVNC）
├── docker-compose.yml         # 標準構成（X11転送、network_mode: host）
├── docker-compose.mac.yml     # macOS XQuartz用（DISPLAY=host.docker.internal:0）
├── docker-compose.vnc.yml     # VNC構成（noVNC port 6080）
├── requirements.txt           # playwright==1.57.0, python-dateutil>=2.8.2
└── supervisord.conf           # VNC版のプロセス管理設定
```

### データフロー

```
1. Cookie 取得: import_chrome_cookies.py（ホスト Chrome から抽出）→ x_profiles/<account>/cookies.json（詳細は refresh-x-cookies スキル）
2. 収集: collect_tweets.py → SafeXCollector（Cookie認証+人間らしい操作）→ output/tweets_*.json
3. キーワード分類: TweetClassifier（キーワード+正規表現）→ categories フィールド追加
4. LLM分類: classify_tweets.py → LLMClassifier（Claude API）→ llm_categories フィールド追加 → viewer.html更新
```

## Docker実行方法

### ビルド

```bash
docker compose build
```

### 3つの実行モード

| モード | compose ファイル | 用途 |
|--------|------------------|------|
| 標準 X11 | `docker-compose.yml` | Linux（X11転送） |
| macOS XQuartz | `docker-compose.mac.yml` | macOS（XQuartz経由GUI表示） |
| VNC | `docker-compose.vnc.yml` | リモート/ヘッドレス環境（ブラウザで `http://localhost:6080` にアクセス） |

### 主要コマンド

```bash
# Cookie 取得（ホスト Chrome から抽出、X bot 検知を回避する唯一の確実経路）
# 詳細: refresh-x-cookies スキル参照
python3 scripts/import_chrome_cookies.py --chrome-profile "Profile 2" --account kabuki666999
python3 scripts/import_chrome_cookies.py --chrome-profile "Default"   --account maaaki

# ツイート収集（全グループ、スクロール10回）
docker compose run xstock python scripts/collect_tweets.py

# 特定グループのみ、スクロール回数指定
docker compose run xstock python scripts/collect_tweets.py --groups group1 group2 --scrolls 5

# LLM分類 + viewer.html更新
docker compose run xstock python scripts/classify_tweets.py

# 入力ファイル指定
docker compose run xstock python scripts/classify_tweets.py --input output/tweets_20260214.json

# アカウント状態確認
docker compose run xstock python scripts/check_inactive_accounts.py

# === 投稿管理は tier3_posting リポへ移管（2026-05-01 Phase 3）===
# cd ~/Desktop/biz/tier3_posting
# python3 -m tier3_posting.cli.server --port 8080
# 詳細はそちらの CLAUDE.md 参照
```

### collect_tweets.py オプション

| オプション | デフォルト | 説明 |
|-----------|-----------|------|
| `--groups` / `-g` | `all` | 収集グループ（group1, group2, group3, group4, group5, group6, all） |
| `--scrolls` / `-s` | `10` | スクロール回数 |
| `--no-csv` | false | CSV出力スキップ |
| `--no-classify` | false | キーワード分類スキップ |
| `--profile` / `-p` | `./x_profile` | ブラウザプロファイルパス |
| `--output` / `-o` | `./output` | 出力ディレクトリ |

## 環境変数

| 変数 | 必須 | 用途 |
|------|------|------|
| `ANTHROPIC_API_KEY` | LLM分類時 | Claude API キー（LLMClassifier が参照） |
| `DISPLAY` | GUI表示時 | X11ディスプレイ（標準: `:0`、macOS: `host.docker.internal:0`、VNC: `:99`） |
| `TZ` | 自動設定 | タイムゾーン（`Asia/Tokyo`） |

## インフルエンサーグループ定義

`collector/config.py` で6グループを定義。各アカウントに個別の `min_faves` を設定。

| グループ | 名称 | アカウント数 | min_faves範囲 | 特記 |
|----------|------|-------------|--------------|------|
| group1 | 超大型インフルエンサー | 4 | 300-400 | tesuta001, goto_finance, yurumazu, cissan_9984 |
| group2 | 大型インフルエンサー | 6 | 80-150 | 2okutameo, tapazou29, kanpo_blog 等 |
| group3 | 中型インフルエンサー | 8 | 30-50 | uehara_sato4, miku919191, yuki75868813751 等 |
| group4 | 小型インフルエンサー | 4 | 20 | momoblog0214, pay_cashless 等 |
| group5 | 極小型インフルエンサー | 7 | 10 | YasLovesTech, Adscience12000, hd_qu8 等 |
| group6 | 逆指標インフルエンサー | 1 | 50 | `is_contrarian: True`（強気発言 = 警戒シグナル） |

## 分類カテゴリ

7カテゴリの定義（`collector/config.py` の `CLASSIFICATION_RULES`）。

| カテゴリキー | 名称 | 概要 |
|-------------|------|------|
| `recommended_assets` | オススメしている資産・セクター | 「割安」「おすすめ」「一択」等の推奨表現 |
| `purchased_assets` | 個人で売買している資産 | 購入・売却・損益報告（旧 `sold_assets`/`winning_trades` を統合） |
| `ipo` | 申し込んだIPO | IPO関連（新規公開、抽選、当選） |
| `market_trend` | 市況トレンドに関する見解 | 相場、地合い、利上げ/利下げ、円安/円高 |
| `bullish_assets` | 高騰している資産 | 爆上げ、急騰、ストップ高 |
| `bearish_assets` | 下落している資産 | 暴落、急落、ストップ安 |
| `warning_signals` | 警戒すべき動き・逆指標シグナル | 逆指標アカウントの強気発言、バブルサイン、信用買い残 |

### LLM分類の特別ルール

- `is_contrarian=true` のアカウントの強気発言 → `bullish_assets` ではなく `warning_signals` に分類
- 各ツイートは複数カテゴリに該当可能
- 信頼度（confidence）: 0.0-1.0 のスコアを付与
- Few-shot例は `data/few_shot_examples.json` に46例定義（7カテゴリ各3件以上）

### カテゴリ → テンプレート対応表（plan.md M1 T1.0 で正規化）

`collector/config.py` の `CATEGORY_TEMPLATE_MAP` を Single Source of Truth とする。

| カテゴリ | テンプレート |
|---|---|
| `recommended_assets` | `hot_picks` |
| `purchased_assets` | `trade_activity` |
| `ipo` | `hot_picks`（IPO サブテンプレート） |
| `market_trend` | `market_summary` |
| `bullish_assets` | `hot_picks` |
| `bearish_assets` | `market_summary` |
| `warning_signals` | `contrarian_signal` |

## ツイートデータ構造

```python
{
    "username": str,          # @ユーザー名
    "display_name": str,      # 表示名
    "text": str,              # ツイート本文
    "url": str,               # ツイートURL (https://twitter.com/.../status/...)
    "posted_at": str | None,  # 投稿日時 (ISO 8601)
    "like_count": int | None, # いいね数
    "retweet_count": int | None,  # リツイート数
    "reply_count": int | None,    # リプライ数
    "collected_at": str,      # 収集日時 (ISO 8601)
    "group": str,             # グループキー (group1/group2/group3)
    "group_name": str,        # グループ名
    "is_contrarian": bool,    # 逆指標フラグ
    # キーワード分類後に追加
    "categories": list[str],      # キーワード分類カテゴリ
    "category_details": dict,     # 分類詳細（マッチしたキーワード等）
    "category_count": int,        # 該当カテゴリ数
    # LLM分類後に追加
    "llm_categories": list[str],  # LLM分類カテゴリ
    "llm_reasoning": str,         # LLM分類理由
    "llm_confidence": float       # LLM分類信頼度 (0.0-1.0)
}
```

## 外部利用 I/F 契約（2026-05-01 Phase 3 で tier3_posting リポへ移管）

投稿管理側の I/F 契約（register_external / get_correction / mark_posted の 3 CLI、
画像配置パス、x_profiles レイアウト等）は `~/Desktop/biz/tier3_posting/CLAUDE.md`
に記載。**make_article 等の外部プロジェクトはそちらを参照すること**。

### influx 残存契約（X Cookie 管理）

x_profiles/ Cookie SST は依然 influx 側に残置。tier3_posting からは symlink 経由で参照される:

- **配置先**: `x_profiles/<account>/cookies.json`
- **再取得経路**: `python3 scripts/import_chrome_cookies.py --chrome-profile "<profile>" --account <account>`

## コーディング規約

| 対象 | 規約 | 例 |
|------|------|-----|
| クラス名 | PascalCase | `SafeXCollector`, `TweetClassifier`, `LLMClassifier` |
| 関数・変数名 | snake_case | `collect_tweets`, `classify_all`, `few_shot_path` |
| 定数 | SCREAMING_SNAKE_CASE | `INFLUENCER_GROUPS`, `CLASSIFICATION_RULES`, `LLM_CONFIG` |
| docstring | Google スタイル | Args / Returns / Raises セクション |
| 型ヒント | 使用する | `List[Dict]`, `Optional[str]`, `Dict[str, Any]` |
| 文字列 | f-string 推奨 | `f"収集完了: {len(self.tweets)}件"` |
| エンコーディング | UTF-8 | JSON: `ensure_ascii=False`, CSV: `utf-8-sig` |

## LLM設定

`collector/config.py` の `LLM_CONFIG` で定義。

| 設定 | 値 | 説明 |
|------|-----|------|
| `model` | `claude-3-5-haiku-20241022` | 使用モデル |
| `batch_size` | `20` | 一度に処理するツイート数 |
| `max_tokens` | `4096` | 最大トークン数 |
| `few_shot_path` | `data/few_shot_examples.json` | Few-shot例ファイル |
| `max_retries` | `3` | API最大リトライ回数 |
| `retry_backoff_base` | `2.0` | 指数バックオフの基数 |

## 収集設定

`collector/config.py` の `COLLECTION_SETTINGS` で定義。

| 設定 | 値 | 説明 |
|------|-----|------|
| `max_scrolls` | `180` | 最大スクロール回数 |
| `min_wait_sec` / `max_wait_sec` | `3` / `8` | スクロール間待機時間 |
| `reading_probability` | `0.2` | 読み込み動作を入れる確率 |
| `reading_min_sec` / `reading_max_sec` | `5` / `12` | 読み込み動作時間 |
| `scroll_min` / `scroll_max` | `400` / `700` | スクロール量（px） |
| `stop_after_empty` | `3` | 新規0件が連続N回で動的終了 |
