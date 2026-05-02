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
