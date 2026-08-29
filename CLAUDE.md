> **グローバルルール準拠**: ~/.claude/CLAUDE.md および ~/.claude/rules/ のルールに従うこと。

# influx - X(Twitter)株式インフルエンサー ツイート収集・LLM分類システム

**task 3役の宣言（rules/05・2026-08-29 P-ENV-71）**: `tasks/NOW.md`・`tasks/phase-tracker.md` は**未設置＝短期の優先順位の正本なし**（`tasks/*.md` 39件は単発追跡のみ）。優先順位が要る作業はセッション冒頭でオーナーに確認する（設置は別裁定）。

## プロジェクト概要

**株で勝つ情報を集める**ための収集基盤（目的の正本= `plan.md`「## 目的」）。X の投稿を Playwright で収集し、商品価格の系列・企業の開示と合わせて投資判断の材料にする。

主要モジュール: `collector/`（Cookie 復号・例外・`SafeXCollector`＝現行の収集器）+ `scripts/`（ブックマーク・トレーサー・週次バズ・価格系列・前向き検証）。投稿管理は **2026-05-01 Phase 3** で `~/Desktop/biz/autopost/`（旧 tier3_posting）に物理分離済み。

> 🪦 **旧2段階分類パイプライン（`collect_tweets.py`→`classify_tweets.py`→7カテゴリ→`output/viewer.html`）は 2026-08-29 に退役判断**。定期実行は未登録・最後の精度計測は不合格（macro F1 0.3905／合格線 0.80・`output/f1_baseline.json` 2026-04-24）。人手の正解データ（`data/gold_set/`）と例文（`data/few_shot_examples.json`）は資産として残置。

**いま何が動いているか（機能マップ）** → `influx-architecture.md`（X収集基盤）／`influx-stock-algo-architecture.md`（株アルゴ研究）／**何がいつ動くか（配管図）** → `docs/pipeline-map.md`。⚠️ **自動で読み込まれるのは `influx-architecture.md` の1枚だけ**（注入は最初に見つかった1枚で止まる仕様・2026-08-29 実測）。**株アルゴ側・配管図は着手時に自分で Read する**。
**詳細リファレンス**（カテゴリ定義・データスキーマ）→ `.claude/docs/architecture.md`（⚠️ 2026-08-29 退役進行中＝現況は上の機能マップが正本。**環境変数の一覧は `influx-architecture.md` §5.2**・グループ定義とオプションは実装と不一致で読まない）

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

# アカウント状態確認
docker compose run xstock python scripts/check_inactive_accounts.py

# X値上がり検出（日次・launchd com.influx.price-watch 22:10。手動は runner 経由）
bash scripts/xprice_watch_run.sh   # 収集→zスコア判定→検知時Mac通知。台帳 data/x_price_watch/
# クエリ本数は jq '.queries|length' configs/x_price_watch.json で引く（本数をここに書かない＝腐るため）
# 発火時の受益銘柄は configs/x_shortage_map.json 経由（TOP1000台帳内に限定・一部のクエリのみ銘柄が出る）
python3 scripts/x_shortage_map.py  # 対応表の自己検証（関門B・符号・網羅。NGなら銘柄付与は自動停止）
# 品薄の7分類と「なぜ転売プレ値では銘柄を出さないか」は docs/price-watch-universe.md §16a が正本

# B2B価格チェッカー（launchd com.influx.price-universe 毎週月11:00。手動は runner 経由）
# 系列数は jq '.series|length' configs/price_universe_sources.json で引く
bash scripts/price_universe_run.sh   # Docker待機→全系列取得→発火/取得低下をMac通知
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

## 設定ファイルの置き場（`config/` と `configs/` の使い分け）

**1字違いの2ディレクトリが両方現役**（統合すると読み手 34ファイルの張替えになるため分けたまま運用する・監査 I-2/I-39 2026-08-29 裁定）。新しい設定を足す時はこの基準で選ぶ:

| 置き場 | 何を置くか | 実例 |
|---|---|---|
| `config/` | **株アルゴ研究のチューニング値・銘柄リスト・定期実行の定義** | `paper_watchlist.json`・`screening_grid_v*.json`・`recipe_shelf_meta.json`・`us_universe_seed.json`・`launchd/`（plist） |
| `configs/` | **アプリ基盤と収集系の設定**（アプリの起動・収集レーンごとの定義） | `app.yaml`・`extensions.enabled.yaml`・`x_price_watch.json`・`price_universe_sources.json`・`news_shock.json`・`sedori_trend.json`・`x_watchlist.json`・`profiles/` |

迷ったら「**株アルゴの検証パラメータか**（→ `config/`）／**収集・アプリの動かし方か**（→ `configs/`）」で判定する。

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

## 完了前に回す検証（入口はこの1つ）

```bash
python3 -m unittest discover -s tests    # 324件・数秒。pytest は入っていない（設定ファイルも無い）
```

これが**唯一の一括ゲート**。個別スクリプトの `--selftest`（`coverage_census` `foreign_forward`
`news_shock_eval` `news_shock_collect` `price_watch_alert` `x_mention_extract`
`news_shock_clock_report` の7本）は、そのスクリプトを触った時だけ追加で回す＝一括ゲートの代わりには
ならない。`x_watchlist_tracer.py` は実行の末尾に `--- self-check:` 行を出すが、これは実行時の自己申告で
テストではない。（2026-08-29 集約: 入口が3系統あり「完了前に何を回すか」が人にも AI にも決まっていなかった）

## 実装完了ごとの同セッション commit（ユーザー恒久指示 2026-07-21）

実装・文書の意味ある変更が終わったら、**同セッション内で意味単位の個別 add で commit まで行う**。
この指示自体が恒久承認＝コミットの都度のユーザー確認は不要（rules/10 の ~/.claude 方式の influx 版）。
push は従来どおりユーザーの `!` 実行または明示依頼時のみ。`git add -A`/`.` 禁止・無関係変更の混入禁止は
従来どおり（git-safety-reference）。機械催促: Stop hook `~/.claude/hooks/stop-influx-loop-closing.sh`
（未コミットの実装編集があると停止時に1回ブロックで催促。教訓クロージング3点自問も同 hook が担う）。
