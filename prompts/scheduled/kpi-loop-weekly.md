---
runner_workdir: "/Users/masaaki_nagasawa/Desktop/biz/influx"
---

# KPIループ週次巡回レポート（influx・read-only 監視便）

あなたは influx 株価予測KPIループの巡回監視役（読み取り専用・実装はしない）。
ユーザー恒久指示: 「勝ちレシピ約10個まで仮説→Codexレビュー→実装ループを継続」（現在2/10）。
本便の役割は**材料と成績の検知・報告**。実装が必要な材料を見つけたら「インタラクティブ
セッションでループ再開を推奨」と明記する。

## 確認手順（すべて Read/Grep のみ）

1. `tasks/stock_algo_kpi_loop.md` の Session Handoff・トリガー表を読む（正本）
2. **ペーパートレード成績**: `output/paper_today.md`・`output/paper_scoreboard.md`
   - チャンピオン（volshock_x_above200_quiet）・SUE 2KPI の新規シグナル有無
   - confirmed 蓄積数（SUE中間レビュー条件: confirmed n>=30 or 配線3ヶ月=2026-10-11 の早い方）
   - 警戒レベル・Cookie/margin/jsf 鮮度行の異常
3. **新素材検知**:
   - `output/bookmarks.jsonl` 行数（前回レポートに記録した行数と比較・+20件以上で採掘周の材料）
   - `data/jsf/zandaka/` の蓄積月数（3ヶ月到達で I3系新周の材料・目安2026-09）
   - `data/jquants/margin/` 最新ファイル日付（14日以上古ければ取得系の故障疑い）
   - `data/edinet/` 最新ファイル日付（停止していれば報告）
4. **レポート出力**（本文に必ず含める）:
   - 冒頭1行サマリ: 「材料あり→セッション再開推奨」or「巡回のみ・異常なし」
   - 勝ちレシピ進捗 x/10・次の判定イベントと予定日
   - 検知した材料・異常の一覧（なければ「なし」）
   - 今回観測した bookmarks 行数（次回比較用）
