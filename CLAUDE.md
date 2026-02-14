# influx - X(Twitter)株式インフルエンサー ツイート収集・LLM分類システム

## プロジェクト概要

X(Twitter)上の株式投資インフルエンサーのツイートをPlaywrightで自動収集し、キーワードベース分類とClaude APIによるLLM分類の2段階で7カテゴリに分類するシステム。収集データはHTMLビューア（`output/viewer.html`）で閲覧可能。

## アーキテクチャ

### モジュール構成

```
influx/
├── collector/                  # コアモジュール
│   ├── config.py              # インフルエンサー定義・分類ルール・収集設定・LLM設定
│   ├── x_collector.py         # SafeXCollector: Playwright+Cookie認証によるツイート収集
│   ├── classifier.py          # TweetClassifier: キーワード/正規表現ベース分類
│   └── llm_classifier.py      # LLMClassifier: Claude API(urllib)によるバッチ分類
├── scripts/                    # 実行スクリプト
│   ├── collect_tweets.py      # ツイート収集 + キーワード分類 + JSON/CSV保存
│   ├── classify_tweets.py     # LLM分類実行 + viewer.html更新
│   ├── merge_llm_classifications.py  # LLM分類結果のマージ + viewer再生成
│   ├── check_inactive_accounts.py    # アカウント状態・最終投稿日確認
│   ├── setup_profile.py       # 初回セットアップ（persistent context）
│   ├── setup_profile_vnc.py   # VNC版セットアップ（対話入力なし）
│   ├── setup_from_chrome.py   # 既存Chrome Cookieコピーによるセットアップ
│   └── setup_with_remote_chrome.py   # CDP接続（port 9222）によるCookie取得
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
1. セットアップ: setup_profile.py → x_profile/cookies.json（Xログイン状態保存）
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
# 初回セットアップ（Xへの手動ログイン）
docker compose --profile setup up setup

# macOS XQuartzの場合
docker compose -f docker-compose.mac.yml --profile setup up setup

# VNCの場合
docker compose -f docker-compose.vnc.yml up -d
docker exec xstock-vnc python scripts/setup_profile_vnc.py

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
```

### collect_tweets.py オプション

| オプション | デフォルト | 説明 |
|-----------|-----------|------|
| `--groups` / `-g` | `all` | 収集グループ（group1, group2, group3, all） |
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

`collector/config.py` で3グループを定義。

| グループ | 名称 | min_faves | アカウント数 | 特記 |
|----------|------|-----------|-------------|------|
| group1 | 主要インフルエンサー | 502 | 7 | - |
| group2 | 追加インフルエンサー | 72 | 21 | - |
| group3 | 逆指標インフルエンサー | 50 | 1 | `is_contrarian: True`（強気発言 = 警戒シグナル） |

## 分類カテゴリ

7カテゴリの定義（`collector/config.py` の `CLASSIFICATION_RULES`）。

| カテゴリキー | 名称 | 概要 |
|-------------|------|------|
| `recommended_assets` | オススメしている資産・セクター | 「割安」「おすすめ」「一択」等の推奨表現 |
| `purchased_assets` | 個人で購入・保有している資産 | 「買った」「購入」「イン」「エントリー」等の購入報告 |
| `ipo` | 申し込んだIPO | IPO関連（新規公開、抽選、当選） |
| `market_trend` | 市況トレンドに関する見解 | 相場、地合い、利上げ/利下げ、円安/円高 |
| `bullish_assets` | 高騰している資産 | 爆上げ、急騰、ストップ高 |
| `bearish_assets` | 下落している資産 | 暴落、急落、ストップ安 |
| `warning_signals` | 警戒すべき動き・逆指標シグナル | 逆指標アカウントの強気発言、バブルサイン、信用買い残 |

### LLM分類の特別ルール

- `is_contrarian=true` のアカウントの強気発言 → `bullish_assets` ではなく `warning_signals` に分類
- 各ツイートは複数カテゴリに該当可能
- 信頼度（confidence）: 0.0-1.0 のスコアを付与
- Few-shot例は `data/few_shot_examples.json` に24例定義

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
