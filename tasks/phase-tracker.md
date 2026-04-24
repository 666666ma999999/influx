# Phase Tracker

<!-- 軽量紐付け方式の継続条件（05-plan-task-md.md 準拠）:
     - tasks/*.md 数: < 30
     - 貢献者数: ≤ 2
     - Phase 数: ≤ 5 + Sprint 枠
     超過したら自動生成方式（frontmatter + build-phase-tracker.ts）へ段階移行 -->

plan.md の Phase 分解に対応する横串トラッカー。
各 Phase の進捗項目から `tasks/*.md` への逆リンクを貼る。

---

## Phase M0: 前提崩壊の緊急復旧
<!-- phase-id: phase-m0 -->
<a id="phase-m0"></a>

**親**: [plan.md#phase-m0](../plan.md#phase-m0)
**タスク**: [m0-execution](./m0_execution.md)
**Status**: ✅ M0 完了（commit b854c23, 2026-04-24）/ Exit Criteria 7/7 達成（#2 は 2026-04-24 headless 実行で解消）
**開始**: 2026-04-20 / **完了**: 2026-04-24
**最終更新**: 2026-04-24（Codex 2-stage PASS / /simplify 完了 / tests 24/24 PASS / commit b854c23）

### Exit Criteria 進捗

- [x] #1 Cookie 当日付更新、`cookies.json` mtime 7 日以内 → T0.1 完了（2026-04-21）
- [x] #2 `inactive_check_result.json` 当日付、group_grok_top (is_priority) 全員アクティブ → 2026-04-24 `docker compose run --rm -T xstock python3 scripts/check_inactive_accounts.py --headless --no-cache` で取得。32/32 巡回、@t_ryoma1985 / @serikura 共にアクティブ（2/2 = 100%）。原案「TOP5 の 4 名以上」は T0.3 緩和後は group_grok_top 2 名構成に再定義
- [x] #3 `research_scorecard.json` の score ≥ 50 が 2 名以上（**2026-04-24 緩和**: 元 score ≥ 70 が 5 名以上）→ @t_ryoma1985 60.0 / @serikura 50.0
- [x] #4 `INFLUENCER_GROUPS` に `grok_score` / `is_priority` 付与、TOP5 明示 → `group_grok_top` 新 group で @t_ryoma1985, @serikura に `is_priority=True` 付与
- [x] #5 `INACTIVE_THRESHOLD_DAYS=7` に変更済 → T0.4 完了
- [x] #6 `CookieExpiredError` 収集系でも raise → T0.6 完了
- [x] #7 `group_reserve` 予備 5 名登録 → @kazzn_blog / @kabuknight / @m_kkkiii / @purazumakoi / @sorave55 を `is_active=False, is_reserve=True` で登録、`generate_search_urls` の `is_active` フィルタで収集対象外

### Blocker

- なし（#2 は優先度低、M1 期間内で解消予定）

### Commit

- `b854c23` M0 complete: Cookie SST, INACTIVE 7 days, grok_score/is_priority, reserve group
  - 18 files changed / +1091 / -114
  - /simplify: `_load_cookies()` 5 箇所を `cookie_crypto.load_cookies_or_raise()` に集約
  - tests/ 追加: test_collector_exceptions, test_research_scorecard (24/24 PASS)

---

## Phase M1: 土台固め (2026-05)
<!-- phase-id: phase-m1 -->
<a id="phase-m1"></a>

**親**: [plan.md#phase-m1](../plan.md#m1-土台固め-2026-05)
**Status**: 🟢 進行中（T1.9 / T0.2 完了 2026-04-24 / 残 T1.8 のみ、M1 実質完了）

### 残タスク

- **T1.8** Grok 20BD 再評価（M0 T0.3 で先行対応済、追加評価があれば着手）
- [x] **T1.9** 収集→素材提供の時間計測基盤 → commit `265b2ac`（2026-04-24）
  - `pipeline_start` で `collect_start_at` 即時記録
  - `pipeline_metric` で `classify_done_at` / `collect_to_classify_sec` 永続化
  - `run_id` (uuid) で同日再実行汚染防止
  - 40 分超過時 stderr WARN（M4 ゲート監視）
  - `_append_log` / `_notify_pending` / `_summarize_log` の OSError は degraded warning で継続
- [x] **T0.2 解消** → XQuartz/VNC 整備は**不要**と判明。`--headless` + docker mount で解決（2026-04-24）
  - 実行: `docker compose run --rm -T xstock python3 scripts/check_inactive_accounts.py --headless --no-cache`
  - 結果: 32/32 アカウント巡回成功、group_grok_top 2/2 アクティブ

### Exit Criteria

1. `pipeline_log/{date}.jsonl` に時刻記録が出力される
2. `INFLUENCER_GROUPS` が Grok 再評価結果を反映済
3. M0 T0.1-T0.8 全て完了（#2 含む）

---

## Phase M2: 学習ループ + 品質フィルタ (2026-06)
<!-- phase-id: phase-m2 -->
<a id="phase-m2"></a>

**親**: [plan.md#m2-学習ループ--品質フィルタ-2026-06](../plan.md#m2-学習ループ--品質フィルタ-2026-06)
**Status**: 🟠 着手ゲート待ち（T2.0 人手ラベル付け 0/35 件）
**着手ゲート**: Gold Set macro F1 ≥ 0.80（未達時はフォールバック戦略あり、plan.md 参照）

### 着手ゲート進捗（T2.0）

- [x] `scripts/sample_gold_set_candidates.py` 実装済（M1 T1.6）
- [x] `scripts/measure_f1.py` 実装済（Wilson 95% CI 含む、2026-04-19）
- [x] `data/gold_set/candidates.jsonl` 生成済（35 件、中立性ルール準拠）
- [x] `data/gold_set/answer_key.jsonl` 生成済（35 件、LLM 推測保存）
- [ ] **`data/gold_set/gold_set.jsonl` 人手ラベル付け 0/35 件**（ユーザー作業、推定 30 分）
- [ ] `python scripts/measure_f1.py` 実行 → macro F1 算出
- [ ] macro F1 ≥ 0.80 判定 → M2 着手 or フォールバック戦略適用

### ラベル付け手順（ユーザー向け）

1. `data/gold_set/candidates.jsonl` を 1 行ずつ読む（35 件）
2. `text` を読んで 7 カテゴリのどれか（複数可）を判断
3. `data/gold_set/gold_set.jsonl` に `{news_id, tweet_url, username, posted_at, text, labels, labeler, labeled_at, notes}` で追記
4. 中立性ルール: ラベリング中 `answer_key.jsonl` を見ない（F1 計測の独立性担保のため）
5. 詳細スキーマ: `data/gold_set/README.md`

### 残タスク（着手ゲート通過後）

- **T2.2** `engagement_rate` 高位ドラフトの自動ブックマーク化
- **T2.3** ノイズ/重複除去フィルタ + 偽陽性ガード
- **T2.4** 有益度スコアリング
- **T2.5** A/B テスト基盤（`experiment_id`）

### Exit Criteria

1. F1 ≥ 0.80 を Gold Set で達成
2. ノイズ率 < 10%
3. 有益度スコア上位のみがドラフト素材
4. `experiment_id` が投稿〜インプレッションに一貫付与

---

## Sprint 枠

（未使用）

---

## 管理ルール

- 各 Phase セクションは `phase-id: phase-<label>` コメント + `<a id="phase-<label>"></a>` アンカーで識別
- task.md 側からは `[Phase N — Title](../plan.md#phase-<label>)` で参照
- phase-tracker 側からは `[<task-slug>](./<task-file>.md)` で逆リンク
- 壊れたリンクチェックは月 1 回: `npx markdown-link-check tasks/*.md plan.md`
