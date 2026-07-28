> **グローバルルール準拠**: ~/.claude/CLAUDE.md および ~/.claude/rules/ のルールに従うこと。

# influx - X(Twitter)株式インフルエンサー ツイート収集・LLM分類システム

## プロジェクト概要

X(Twitter)上の株式投資インフルエンサーのツイートを Playwright で自動収集し、キーワードベース分類と Claude API による LLM 分類の 2 段階で 7 カテゴリに分類するシステム。収集データは HTML ビューア（`output/viewer.html`）で閲覧可能。

主要モジュール: `collector/`（config, x_collector, classifier, llm_classifier）+ `scripts/`（collect_tweets, classify_tweets 等）。投稿管理は **2026-05-01 Phase 3** で `~/Desktop/biz/autopost/`（旧 tier3_posting）に物理分離済み。

**詳細リファレンス**（モジュール構成・データフロー・カテゴリ定義・データスキーマ・各種設定値）→ `.claude/docs/architecture.md`

## Docker 実行モード

| モード | compose ファイル | 用途 |
|--------|------------------|------|
| 標準 X11 | `docker-compose.yml` | Linux（X11転送） |
| macOS XQuartz | `docker-compose.mac.yml` | macOS（XQuartz経由GUI表示） |
| VNC | `docker-compose.vnc.yml` | リモート/ヘッドレス（`http://localhost:6080`） |

## 主要コマンド

```bash
# Cookie 取得（ホスト Chrome から抽出、X bot 検知を回避する唯一の確実経路）
# 詳細: refresh-x-cookies スキル
python3 scripts/import_chrome_cookies.py --chrome-profile "Profile 2" --account kabuki666999
python3 scripts/import_chrome_cookies.py --chrome-profile "Default"   --account maaaki

# ツイート収集（全グループ、スクロール 10 回。オプション一覧は architecture.md 参照）
docker compose run xstock python scripts/collect_tweets.py
docker compose run xstock python scripts/collect_tweets.py --groups group1 group2 --scrolls 5

# LLM 分類 + viewer.html 更新
docker compose run xstock python scripts/classify_tweets.py
docker compose run xstock python scripts/classify_tweets.py --input output/tweets_20260214.json

# アカウント状態確認
docker compose run xstock python scripts/check_inactive_accounts.py

# X値上がり検出（日次・launchd com.influx.price-watch 22:10。手動は runner 経由）
bash scripts/xprice_watch_run.sh   # 収集36クエリ→zスコア判定→検知時Mac通知。台帳 data/x_price_watch/

# B2B価格チェッカー（33系列・launchd com.influx.price-universe 毎週月8:30。手動は runner 経由）
bash scripts/price_universe_run.sh   # Docker待機→33系列取得→発火/取得低下をMac通知
docker compose run --rm xstock python scripts/price_universe_check.py   # 素の実行（通知なし）
docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_discover.py  # 新商品名の候補キュー生成
docker compose run --rm xstock python scripts/price_watch_forward.py --eval          # 発火の前向き記録を評価（8/15週後）
# 銘柄→センターピン（利益を動かす中心の価格）の全977社台帳: data/center_pin/center_pin.jsonl
# 帰属ルール（誤帰属の防ぎ方）と系列の罠は docs/price-watch-universe.md §0a/§0b が正本

# Grok リサーチパイプライン（.envrc 自動読み込み + docker exec ラッパー）
scripts/run_research.sh --phase evaluate
scripts/run_research.sh --phase report

# === 投稿管理は autopost リポへ移管（2026-05-01 Phase 3） ===
# cd ~/Desktop/biz/autopost && python3 -m tier3_posting.cli.server --port 8080
```

## 環境変数（必須のみ）

- `ANTHROPIC_API_KEY` — LLM 分類時に Claude API で使用
- `XAI_API_KEY` — `scripts/run_research.sh` が `.envrc` から読み込む
- 完全な一覧（DISPLAY, TZ 等）→ `.claude/docs/architecture.md`

## 外部利用 I/F 契約

投稿管理側の I/F 契約（register_external / get_correction / mark_posted の 3 CLI、画像配置パス、x_profiles レイアウト等）は `~/Desktop/biz/autopost/CLAUDE.md` に記載。**make_article 等の外部プロジェクトはそちらを参照すること**。

### influx 残存契約（X Cookie 管理）

`x_profiles/` Cookie SST は依然 influx 側に残置。autopost リポからは symlink 経由で参照される。

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

## リサーチ運用（worktree / データ格納 / ファイル数）

探索的分析・計測（few-shot 実験 / `measure_*` / `btc_*_analysis` 等）を「main を汚さず・データを
散らかさず・ファイルを増やさず」回す規約は **[`docs/research-workflow.md`](docs/research-workflow.md)** が SSoT。
思想・Why は global skill `research-isolation`。influx 固有の要点:

- 試行錯誤は `analysis/<topic>` worktree（`../influx-<topic>`）で隔離 → 確定だけ main へ昇格
- 一度きりの探索スクリプトは `_tmp_<name>.py`（gitignore で追跡外）。`measure_*` 等の定常計測は無印 tracked
- 実験出力は `output/`（gitignore 済）/ 学習データ `data/few_shot_examples.json` 等は tracked 正本

## 実装完了ごとの同セッション commit（ユーザー恒久指示 2026-07-21）

実装・文書の意味ある変更が終わったら、**同セッション内で意味単位の個別 add で commit まで行う**。
この指示自体が恒久承認＝コミットの都度のユーザー確認は不要（rules/10 の ~/.claude 方式の influx 版）。
push は従来どおりユーザーの `!` 実行または明示依頼時のみ。`git add -A`/`.` 禁止・無関係変更の混入禁止は
従来どおり（git-safety-reference）。機械催促: Stop hook `~/.claude/hooks/stop-influx-loop-closing.sh`
（未コミットの実装編集があると停止時に1回ブロックで催促。教訓クロージング3点自問も同 hook が担う）。
