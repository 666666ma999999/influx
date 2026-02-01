# X投稿収集システム (xstock)

株インフルエンサーのX投稿を収集し、7カテゴリに分類してNEWS配信用データを生成するシステム。

## 特徴

- **安全な収集方式**: 手動ログイン済みプロファイルを使用し、人間らしいアクセスパターンで収集
- **ブロックリスク軽減**: ヘッドレスモード不使用、低頻度アクセス、ランダム待機時間
- **自動分類**: 7カテゴリへの自動分類機能
- **Docker対応**: コンテナ環境で実行可能

## カテゴリ

1. **オススメしている資産・セクター** - 買い推奨、注目銘柄
2. **個人で購入・保有している資産** - 実際の売買報告
3. **申し込んだIPO** - IPO関連
4. **市況トレンドに関する見解** - 相場観、マクロ分析
5. **高騰している資産** - 急騰銘柄
6. **下落している資産** - 急落銘柄
7. **警戒すべき動き・逆指標シグナル** - 警戒情報、逆指標

---

## Docker環境でのセットアップ（推奨）

### 前提条件

- Docker / Docker Compose
- XQuartz（macOSの場合）

### macOSの場合: XQuartz設定

GUIブラウザを表示するためにXQuartzが必要です。

```bash
# XQuartzをインストール
brew install --cask xquartz

# XQuartzを起動し、以下を設定:
# 環境設定 → セキュリティ → 「ネットワーク・クライアントからの接続を許可」にチェック

# 一度ログアウト/ログインしてから以下を実行
xhost +localhost
```

### Step 1: Dockerイメージをビルド

```bash
./scripts/start.sh build
```

### Step 2: 初回セットアップ（Xにログイン）

```bash
./scripts/start.sh setup
```

ブラウザが開くので、手動でXにログインしてください（2FA含む）。
ログイン完了後、ターミナルでEnterを押してください。

### Step 3: ツイート収集

```bash
./scripts/start.sh run
```

---

## ローカル環境でのセットアップ

### インストール

```bash
# 依存パッケージをインストール
pip install -r requirements.txt

# Playwrightブラウザをインストール
playwright install chromium
```

### 初回セットアップ

```bash
python scripts/setup_profile.py
```

### ツイート収集

```bash
# 全グループを収集
python scripts/collect_tweets.py

# 特定グループのみ
python scripts/collect_tweets.py --groups group1

# スクロール回数を変更
python scripts/collect_tweets.py --scrolls 5
```

---

## コマンドオプション

| オプション | 説明 |
|-----------|------|
| `--groups`, `-g` | 収集グループ (group1, group2, group3, all) |
| `--scrolls`, `-s` | スクロール回数（デフォルト: 10） |
| `--no-csv` | CSV出力をスキップ |
| `--no-classify` | 分類処理をスキップ |
| `--profile`, `-p` | プロファイルパス |
| `--output`, `-o` | 出力ディレクトリ |

---

## 出力ファイル

収集後、`output/` ディレクトリに以下のファイルが生成されます:

| ファイル | 内容 |
|----------|------|
| `tweets_YYYYMMDD_HHMMSS.json` | 全ツイートデータ |
| `tweets_YYYYMMDD_HHMMSS.csv` | CSV形式（Excel対応） |
| `news_YYYYMMDD_HHMMSS.json` | ニュース配信用データ |

---

## 収集対象インフルエンサー

### グループ1（主要インフルエンサー 7名、502いいね以上）
tesuta001, goto_finance, yurumazu, cissan_9984, tomoyaasakura, 2okutameo, yuki75868813751

### グループ2（追加インフルエンサー 22名、72いいね以上）
utbuffett, miku919191, YasLovesTech, heihachiro888, tapazou29, _teeeeest, nobutaro_mane, kakatothecat, Toushi_kensh, Kosukeitou, uehara_sato4, pay_cashless, haru_tachibana8, piya00piya, yys87495867, kanpo_blog, w_coast_0330, shikiho_10, Adscience12000, yukimamax, momoblog0214, hd_qu8

### グループ3（逆指標インフルエンサー 1名、50いいね以上）
gihuboy

---

## 推奨運用スケジュール

ブロックリスクを最小化するため、週1-2回の使用を推奨:

| 曜日 | 対象 | 備考 |
|------|------|------|
| 月曜 | グループ1 | 主要7名 |
| 水曜 | グループ2 | 追加22名 |
| 金曜 | グループ3 | 逆指標1名 |

---

## ディレクトリ構造

```
xstock/
├── collector/
│   ├── __init__.py
│   ├── config.py          # 設定ファイル
│   ├── x_collector.py     # 収集クラス
│   └── classifier.py      # 分類クラス
├── scripts/
│   ├── setup_profile.py   # 初回セットアップ
│   ├── collect_tweets.py  # 収集スクリプト
│   └── start.sh           # 起動スクリプト
├── data/                  # データ保存用
├── output/                # 出力ファイル
├── x_profile/             # ブラウザプロファイル
├── Dockerfile
├── docker-compose.yml
├── docker-compose.mac.yml # macOS用
├── requirements.txt
└── README.md
```

---

## 注意事項

- **週1-2回程度の使用を推奨**: 高頻度のアクセスはブロックリスクを高めます
- **手動ログインが必要**: 自動ログイン機能は意図的に実装していません
- **X利用規約**: 本ツールは個人利用を想定しています
- **プロファイル保護**: `x_profile/` ディレクトリにはログイン情報が含まれるため、共有しないでください
