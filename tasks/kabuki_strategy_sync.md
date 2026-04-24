# Task: kabuki666999 運用戦略同期（bio改修 + 週2投稿運用）

**Phase:** [M4 日次 1 投稿パイプライン自動化](../plan.md#m4-日次-1-投稿パイプライン自動化-2026-08) / [アカウント戦略 SSoT](../../account_strategy.md)
**Tracker:** [phase-tracker](./phase-tracker.md)

## Metadata
- Status: active
- 優先度: 高
- 開始日: 2026-04-24

## Execution Strategy
Delivery — 成功基準が観測可能（kabuki 30日で月10,000imp）

## 成功基準
- `@kabuki666999` が週2投稿（火木 20:00）で安定運用
- 30日で月10,000imp到達
- 詳細な数値・KPI定義は SSoT `~/Desktop/biz/account_strategy.md` に従う（本ファイルには重複記載しない）

## M4 との関係
- influx `plan.md` M4 は「日次1投稿パイプライン」を定義するが、本タスクは **週2（火木）の頻度スロット** を SSoT に合わせて設定する
- `impression_tracker` へ kabuki 分を登録し、M4 稼働時は SSoT の頻度指定に従って投稿キューを絞る

## Current Agreed Scope

### Must
- [ ] **過去投稿クリーンアップ（bio改修の前提）** — §2.4 SSoT 参照
  - [ ] 最優先削除: post `2031876737525752273`（成人ジョーク・実名波及時のコンプラ重大リスク）
  - [ ] 高優先削除: 日常雑談系（電車混雑予想等、bio と不整合）
  - [ ] 中優先削除: 漫画感想系（チェンソーマン等、bio と不整合）
  - [ ] 残留: IHI・JX金属等の投資投稿、ポケカ投稿
  - [ ] 完了後、SSoT §2.4 テーブルに照合して漏れがないか確認
- [ ] bio 改修案1 を `@kabuki666999` プロファイルに適用（クリーンアップ完了後に実行。内容は SSoT 参照）
- [ ] 週2投稿カレンダー設定（火木 20:00）を `account_routing` / 予約投稿に反映
- [ ] `impression_tracker` に `@kabuki666999` 分を登録

### Nice-to-have
- [ ] 週次 imp レポートの自動通知（M4 連動）

### Descoped
- 日次パイプライン全体の改修（→ M4 本体タスク）
- make_article 側の記事生成（→ `make_article/tasks/account_strategy_kickoff.md`）

## Progress Snapshot
- **Blocked**: なし
- **Next**: SSoT で bio 改修案1 の確定文面を確認 → プロファイル更新手順ドラフト

## Failures / Stuck Context
（未記録）

## Session Handoff
- **Start Here**: X UI で `@kabuki666999` の過去投稿を開き、post `2031876737525752273` を削除。その後 SSoT §2.4 の優先度テーブルに沿って残り9件をレビュー
- **Avoid Repeating**:
  - SSoT の数値（週2・月10,000imp）は本タスクに複写しない。変更は SSoT のみで行う
  - **bio 改修はクリーンアップ完了後**（順序逆転するとプロフ遡及時の違和感＋コンプラ波及リスク）
- **Key Evidence**:
  - `x_profiles/kabuki666999/`、`extensions/tier3_posting/`、`impression_tracker/`
  - 2026-04-24 grok 実測（`from:kabuki666999` 10件取得）で判明した要削除候補:
    - `2031876737525752273` — 成人ジョーク（最優先）
    - `2034216722933444694` — 総武線/大江戸線混雑予想（日常）
    - `2036798959595970670` — チェンソーマン感想（漫画）

## Decision Log
- 2026-04-24: task.md 起票。M4 の日次頻度を SSoT の週2に上書き（アカウント別頻度は SSoT が決定）
- 2026-04-24: grok 実測で過去投稿と bio 改修案1 の乖離が判明。Must に過去投稿クリーンアップを追加、順序は「クリーンアップ → bio改修 → 週2運用」に確定
