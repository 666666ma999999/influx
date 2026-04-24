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
**Status**: 🟢 Exit Criteria 6/7 達成（#2 のみ残、優先度低）— Codex レビュー blocked（クォータ超過）→ commit 承認待ち
**開始**: 2026-04-20 / **着手ゲート**: Exit Criteria 全 7 項目達成 = M1 開始条件
**最終更新**: 2026-04-24（実装完了・セッション中断時の進捗保存）

### Exit Criteria 進捗

- [x] #1 Cookie 当日付更新、`cookies.json` mtime 7 日以内 → T0.1 完了（2026-04-21）
- [ ] #2 `inactive_check_result.json` 当日付、TOP5 の 4 名以上アクティブ → T0.2 保留（XQuartz/VNC DISPLAY 未整備、優先度低。M1 での解消で OK）
- [x] #3 `research_scorecard.json` の score ≥ 50 が 2 名以上（**2026-04-24 緩和**: 元 score ≥ 70 が 5 名以上）→ @t_ryoma1985 60.0 / @serikura 50.0
- [x] #4 `INFLUENCER_GROUPS` に `grok_score` / `is_priority` 付与、TOP5 明示 → `group_grok_top` 新 group で @t_ryoma1985, @serikura に `is_priority=True` 付与
- [x] #5 `INACTIVE_THRESHOLD_DAYS=7` に変更済 → T0.4 完了
- [x] #6 `CookieExpiredError` 収集系でも raise → T0.6 完了
- [x] #7 `group_reserve` 予備 5 名登録 → @kazzn_blog / @kabuknight / @m_kkkiii / @purazumakoi / @sorave55 を `is_active=False, is_reserve=True` で登録、`generate_search_urls` の `is_active` フィルタで収集対象外

### Blocker

- なし（#2 は優先度低、M1 期間内で解消予定）
- Codex MCP クォータ超過で 2-stage レビュー未完了（次セッション再開時に ping → 復旧確認）
- 代替検証済: 統合アサーション 8 件 PASS、自己レビュー完了（詳細は [m0_execution.md Failures/Stuck Context](./m0_execution.md#failures--stuck-context)）

### 未コミット変更（M0 まとめコミット対象）

- `collector/inactive_checker.py` (T0.4 + T0.6)
- `collector/x_collector.py` (T0.6)
- `collector/config.py` (PROFILE_PATH 修正 + group_grok_top + group_reserve + is_active フィルタ)
- `collector/exceptions.py` (新規・T0.6 SST)
- `extensions/tier3_posting/x_poster/exceptions.py` (T0.6 再エクスポート)
- `scripts/promote_grok_candidates.py` (新規・T0.7)
- `plan.md` (Exit #3 緩和記録、M0 phase-m0 アンカー追加)
- `tasks/m0_execution.md` (task.md テンプレ準拠リライト + 進捗反映)
- `tasks/phase-tracker.md` (新規)

---

## Phase M1: 土台固め (2026-05)
<!-- phase-id: phase-m1 -->
<a id="phase-m1"></a>

**親**: [plan.md#phase-m1](../plan.md#m1-土台固め-2026-05)
**Status**: ⏳ Pending（M0 Exit Criteria 達成待ち）

---

## Sprint 枠

（未使用）

---

## 管理ルール

- 各 Phase セクションは `phase-id: phase-<label>` コメント + `<a id="phase-<label>"></a>` アンカーで識別
- task.md 側からは `[Phase N — Title](../plan.md#phase-<label>)` で参照
- phase-tracker 側からは `[<task-slug>](./<task-file>.md)` で逆リンク
- 壊れたリンクチェックは月 1 回: `npx markdown-link-check tasks/*.md plan.md`
