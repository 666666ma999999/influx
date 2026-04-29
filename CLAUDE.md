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
├── extensions/tier3_posting/   # 投稿管理エクステンション（独立モジュール）
│   ├── cli/                   # CLIエントリポイント（python -m で実行）
│   │   ├── server.py          # 管理画面APIサーバー
│   │   ├── manage.py          # ドラフト管理CLI（status/archive/compact等）
│   │   ├── run.py             # 予約投稿・即時投稿（--mode schedule|immediate）
│   │   ├── compose.py         # ドラフト自動生成（スタイル対応プロンプト）
│   │   ├── build_html.py      # 静的HTML生成（オフライン用）
│   │   ├── build_style_dataset.py  # ブックマーク教師データ構築
│   │   └── track.py           # インプレッション追跡
│   ├── services/              # ビジネスロジック（Single Source of Truth）
│   │   ├── draft_service.py   # ドラフト作成・news_id生成
│   │   ├── post_preparation.py # 文字数計算・日時正規化・ステータス定義
│   │   ├── view_model.py      # UI用データ事前計算
│   │   └── style_prompt_builder.py  # スタイル対応LLMプロンプト
│   ├── account_routing.py     # マルチアカウント自動振り分け
│   ├── ui/review.html         # 投稿進捗管理画面（カレンダー+リスト）
│   ├── x_poster/              # PostStore, XPoster
│   ├── scheduler/             # 投稿スケジューラー
│   ├── image_generator/       # チャート・OGP画像生成
│   ├── impression_tracker/    # インプレッションスクレイパー
│   ├── news_curator/          # ニュースキュレーター
│   └── post_composer/         # 投稿コンポーザー
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

# === 投稿管理（extensions/tier3_posting/cli/ 経由） ===

# 管理画面起動（http://localhost:8080）
python -m extensions.tier3_posting.cli.server --port 8080
# Docker: docker compose --profile review up review

# ドラフト管理（ステータス確認）
python -m extensions.tier3_posting.cli.manage status

# ドラフト自動生成
docker compose run xstock python -m extensions.tier3_posting.cli.compose

# 承認済みドラフトの予約投稿
docker compose run xstock python -m extensions.tier3_posting.cli.run --no-dry-run --limit 2

# 即時投稿
docker compose run xstock python -m extensions.tier3_posting.cli.run --mode immediate --no-dry-run
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

## 外部利用 I/F 契約（External Integration Contract）

> **重要**: 本セクションで定義する I/F は、外部プロジェクト（make_article 等）が依存する **公開契約**である。
> ここに記載の CLI 引数仕様・パス・コンテナ名を変更する場合は **breaking change** として扱い、
> 該当外部プロジェクトの SSoT（make_article では `plan.md`）に通知すること。

### 1. ドラフト登録 / 修正指示 / 投稿後ステータス CLI

外部プロジェクトは `extensions.tier3_posting.x_poster.post_store.PostStore` を**直接 import してはならない**。
代わりに以下 3 本の CLI を `subprocess.run` 経由で呼び出すこと（cwd = influx repo root, `python3 -m <module>`）。

| 用途 | モジュール | 入力 | 出力（stdout 1 行 JSON） |
|---|---|---|---|
| ドラフト登録（upsert） | `extensions.tier3_posting.cli.register_external` | `--json -` で stdin JSON or 個別フラグ | `{"news_id":"...","action":"added\|updated","ok":true}` |
| 修正指示取得 | `extensions.tier3_posting.cli.get_correction` | `--identifier <news_id\|make_article_id>` | `{"news_id":"...","correction_instructions":"..."}` または null |
| 投稿後ステータス更新 | `extensions.tier3_posting.cli.mark_posted` | `--news-id <hex16> [--status posted] [--posted-url ...] [--dry-run]` | `{"news_id":"...","ok":true,"actions":["history","status"]}` |

#### stdin JSON スキーマ（register_external 用）

```jsonc
{
  "news_id": "<sha256(title:promo_text)[:16]>",  // 必須
  "title": "...",                                  // 必須
  "promo_text": "...",                             // 必須（Xタイムライン本文）
  "article_body": "...",                           // 任意（記事全文）
  "image_paths": ["output/posting/images/a.png"],  // 任意（influx 相対パス）
  "format": "x_article",                           // 任意 既定: x_article
  "template_type": "make_article",                 // 任意 既定: make_article
  "metadata": {                                    // 任意（自由フィールド）
    "make_article_id": "art_013",
    "category": "tech_tips",
    "score": "8.5",
    "source_file": "output/drafts/...md"
  }
}
```

#### Exit codes（全 CLI 共通）

| code | 意味 |
|---|---|
| 0 | 成功 |
| 1 | 実行時エラー（PostStore 初期化失敗等） |
| 2 | 引数不足・JSON パース失敗 |

### 2. 画像配置パス契約

外部プロジェクトが `image_paths` で参照するファイルは事前に以下へコピーすること:

- **配置先**: `output/posting/images/<file>.png`
- **CLI 渡し時のパス**: `output/posting/images/<file>.png`（influx repo root 起点の相対パス）

### 3. x_profiles レイアウト

マルチアカウント Cookie の保管先と分離規約:

- **配置先**: `x_profiles/<account>/state.json`（例: `x_profiles/twittora_/state.json`）
- **再取得経路**: `python -m scripts.import_chrome_cookies`（VNC 方式は 2026-04-21 に廃止）
- 外部プロジェクトはこの配置を前提に `--profile <account>` を渡す

### 4. xstock-vnc コンテナ名

VNC 経由で稼働するコンテナ識別子（廃止済みだが互換性検証用に予約）:

- **コンテナ名**: `xstock-vnc`
- 現在は本番フローから外れているため、参照のみ。新規依存禁止

### 5. Breaking change 通知ルール

以下のいずれかを変更する場合は、make_article の `plan.md` に変更通知を起票し、両プロジェクトを同期更新する:
- CLI モジュール名・引数名・出力 JSON スキーマ
- 入出力 exit code 体系
- `output/posting/images/` の配置パス
- `x_profiles/<account>/` のディレクトリ構造
- `xstock-vnc` コンテナ名

外部プロジェクト側の修正なしに本契約を変更してはならない。

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
