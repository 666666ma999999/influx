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

---

## 2026-07-26 打ち止め再検証（X検索精度・最小実行版）— 確定知見

> 出所: plan `~/.claude/plans/kind-questing-scone.md`（敵対レビュー2系統で縮小承認）。
> スクリプト: `scripts/_tmp_c0_censoring.py` / `_tmp_a_lite.py` / `_tmp_c_lite.py`（使い捨て・untracked・
> 全数値は Codex 独立再計算で照合済み、onset 窓の暦日バグ1件を検出→修正済み）。
> 標本注記: uncovered=188投稿/150アカ（新規144人）は急騰銘柄の事前窓収集＝条件付き標本。
> 記述的トリアージであり PASS 認定には使わない。corpus=57アカ/1,538投稿（20251001〜20260717）。

### 結論: 打ち止め維持を支持（偽陽性説を確定・見逃し説は起点近傍で不支持）

1. **偽陽性説の確定（A-lite）**: 散弾（1投稿3銘柄以上）除外＋スパム指紋（テンプレ語彙6アカ・
   数学装飾文字4アカ）適用後、uncovered 150アカの pass_episodes（独立episode≥2）遷移は
   PASS→FAIL 10 / PASS→PASS 3 / FAIL→FAIL 137。**散弾群の before PASS 10アカは after 0 に全滅**。
   生存3アカは手読みで全て非予測（製錬所訪問回想/優待報告/利確事後報告）＝
   **クリーン後の実質的な事前コール者はゼロ**。「当てた」ように見えた主体は散弾水増しで説明できる。
2. **見逃し説は起点近傍で不支持（C-lite・完全観測 episode 限定 primary）**: 真の起点 t0
   （120営業日最安値翌日・episode_start より中央値117日前）直前の窓では corpus 言及がほぼ無い:
   **origin_10bd = 0/208 episode、origin_21bd = 2/190**（current 窓は 28/307）。
   起点で仕込みを語る発信者は手元コーパスに存在しない。
3. **onset 窓（t0〜episode_start 前日）は 37/199 episode・新規8銘柄**を拾うが、手読みでは
   大半がニュース転載・結果報告・列挙スレ・目標株価テンプレ（nikkeisignal 等）で、真の事前テーゼは
   drdebuneko のエンプラス（6941）ピア・キャッチアップ等ごく少数。窓長 exposure 差の注記あり。
4. **副産物（次プラン候補・恒久実装するなら）**:
   - スレッド型散弾（1投稿1-2銘柄×同日多数）が per-post 指標をすり抜ける: 同日 distinct codes≥5 が
     uncovered 29アカ / corpus 20アカ。per-day 指標の併用が必要
   - NFKC バグ実在確認: 数学装飾数字（𝟒𝟓𝟖𝟔等）が direct_codes を素通り（uncovered に4アカ）。
     採点器の恒久修正は「NFKC 前 styled 判定→フラグ」方式が敵対レビュー推奨
   - corpus の codes/post は direct のみ最大2だが社名解決込み最大4（散弾閾値の前提訂正）
5. **recall は本検証の対象外**（手元データは発見済み投稿のみ。未発見アカ・未取得投稿は測れない）。
   検索網の拡張（57アカ制約）を再検討する場合は盲検 recall 監査を別途事前登録すること。

### 次アクション裁定待ち

- 推奨: **打ち止め維持**（発掘レーンの供給は FDR / 急騰前兆モデル優先を継続）
- 代替1: 採点器の防御的恒久修正のみ実施（NFKC styled フラグ・per-day 散弾指標・遷移表方式。
  ゲート定義は変えない小玉）
- 代替2: onset 帯の標的新規収集（起点〜急騰間の発信者を狙う。ただし本検証で発信自体が僅少と
  実測されたため期待値低）
