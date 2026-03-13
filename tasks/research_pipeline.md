# Grok API インフルエンサー勝率リサーチ パイプライン

## ステータス: 全フェーズ完了（ホライズン到来待ち）
**最終更新**: 2026-03-13

---

## 実装タスク

| # | タスク | ステータス | 備考 |
|---|--------|-----------|------|
| 0 | インフラ整備 (requirements, docker-compose, config) | ✅ 完了 | xai-sdk, pydantic追加、XAI_API_KEY環境変数追加 |
| 1 | Grok Discovery エクステンション | ✅ 完了 | extensions/tier1_collection/grok_discoverer/ |
| 2 | SignalExtractor モジュール | ✅ 完了 | collector/signal_extractor.py |
| 3 | 営業日計算ユーティリティ | ✅ 完了 | collector/business_days.py |
| 4 | ResearchStore & ResearchScorecardBuilder | ✅ 完了 | research_store.py, research_scorecard.py |
| 5 | オーケストレータスクリプト | ✅ 完了 | scripts/research_influencers.py |

## 検証タスク

| # | タスク | ステータス | 備考 |
|---|--------|-----------|------|
| V1 | Python構文チェック (全7ファイル) | ✅ 完了 | 全ファイル OK |
| V2 | JSON/YAML検証 | ✅ 完了 | config_schema.json, extension.yaml OK |
| V3 | Docker build | ✅ 完了 | xai-sdk 1.8.1, pydantic 2.12.5 インストール確認 |
| V4 | Phase 1 (discover) 実行 | ✅ 完了 | 18候補発見 |
| V5 | Phase 2 (collect) 実行 | ✅ 完了 | 3人分52ツイート収集 (purazumakoi:8, kabuknight:6, susakisiki:38) |
| V6 | Phase 3 (evaluate) 実行 | ✅ 完了 | xAI Grok API使用、4シグナル抽出 (6085.T x3, 7203.T x1) |
| V7 | Phase 4 (report) 実行 | ✅ 完了 | scorecard.json + report.html 生成 |
| V8 | HTMLレポート ブラウザ確認 | ✅ 完了 | 構造・データ整合性確認済み (2026-03-13) |

## 解決済みブロッカー

### xAI API クレジット未購入 → 解決済み
- クレジット購入完了。全Phase動作確認済み

### ANTHROPIC_API_KEY → 不要化
- SignalExtractor を xAI Grok API (grok-3-mini-fast) に変更
- XAI_API_KEY のみで全Phase動作可能

## 次のアクション

### ホライズン到来後の再評価
- **5BD**: 2026-03-18〜20（最短で3/18に最初の結果が出る）
- **20BD**: 2026-04-08〜10
- 再評価コマンド: `docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase evaluate`
- その後: `--phase report` でレポート再生成

## 再評価手順（ホライズン到来後）

```bash
# 5BD結果: 2026-03-18以降 / 20BD結果: 2026-04-08以降
source .envrc
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase evaluate
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase report
```

## フルパイプライン再実行手順（新規候補でやり直す場合）

```bash
source .envrc

# 1. インフルエンサー発見
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase discover --keywords "日本株 高配当" "グロース株 成長株"

# 2. ツイート収集（上位3人）
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase collect --max-collect 3

# 3. シグナル抽出+評価
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase evaluate

# 4. レポート生成
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase report
```

## 変更ファイル一覧

### 既存ファイル変更
- `requirements.txt` — xai-sdk>=0.4.0, pydantic>=2.0 追加
- `docker-compose.yml` — XAI_API_KEY (xstock, setup)
- `docker-compose.mac.yml` — XAI_API_KEY (xstock, setup)
- `docker-compose.vnc.yml` — XAI_API_KEY
- `collector/config.py` — DISCOVERY_CONFIG, RESEARCH_KEYWORDS
- `configs/extensions.enabled.yaml` — tier1.grok_discoverer

### 新規ファイル
- `collector/business_days.py`
- `collector/signal_extractor.py`
- `extensions/tier1_collection/grok_discoverer/__init__.py`
- `extensions/tier1_collection/grok_discoverer/extension.yaml`
- `extensions/tier1_collection/grok_discoverer/config_schema.json`
- `extensions/tier1_collection/grok_discoverer/extension.py`
- `extensions/tier1_collection/grok_discoverer/research_store.py`
- `extensions/tier1_collection/grok_discoverer/research_scorecard.py`
- `scripts/research_influencers.py`
