# pair_forward_v1 — KPI×KPIペアの前向き観察（凍結ドラフト 2026-07-29・Codex最終レビュー前）

> **位置づけ**: 2026-07-29 の覗き見（27ペアの全in-sampleプールEV/到達率の記述測定）により、
> ペア仮説は**過去データでの検定が永久に不可**（Codex R1 BLOCKER-1）。本登録は
> **前向き観察のみ**（お金を張らない記録・成績によるGO判定なし・**trials.jsonl と
> screening_batches.jsonl には不算入**＝Codex R2 MINOR-1対応。専用台帳のみに記録）。
> 正式検定に載せる場合は、前向きデータが12ヶ月貯まった後に §7-X 単発事前登録＋Codex GO を別途経る。

## 1. 候補（機械規則で確定・追加変更禁止）
選定規則＝「2026-07-29 の記述測定27ペア（n≥80）のうち、プール到達率>9.5% かつ プールEV>0 の全ペア」。
**選定が覗き見由来である事実を開示**（前向き測定の妥当性は選定バイアスに依存しない＝測るだけ）。

| # | 主KPI（成績はこちら基準） | 先行相方（−3〜0営業日） |
|---|---|---|
| P1 | three_up_ignition | turnover_rank_surge |
| P2 | turnover_rank_surge | gap_hold_close_strong |
| P3 | turnover_rank_surge | volshock_5x |
| P4 | sell_reg_trigger_rebound | engulf_reversal_day |
| P5 | engulf_reversal_day | sell_reg_trigger_rebound |
| P6 | volshock_5x | turnover_rank_surge |
| P7 | high52_breakout | gap_hold_close_strong |
| P8 | high52_breakout | volshock_5x |

## 2. 判定・執行仕様（凍結）
- **ペア成立**: 同一銘柄で、相方KPIのシグナル日が主KPIシグナル日の**−3〜0営業日**（営業日インデックス差・
  両シグナルとも各Canonical生成器の出力・判定時点は主シグナル日の引け後＝主KPIのT+1寄付エントリー前に
  観察可能＝PIT安全・Codex MAJOR-5の5条件を採用）。窓の事後調整禁止（MINOR-1）。
- **成績**: 主KPIの**凍結済みCanonical規則をそのまま適用**（entry/defer/dedup/exit/stopとも不変・
  Codex R2 MAJOR-2対応）: sell_reg_trigger_rebound=fixed_t1、他の主KPI（three_up/engulf/gap_hold/
  turnover/volshock_5x/high52_breakout）=defer_max3bd（max_defer_bdays=3）。20営業日後終値・
  往復0.3%・-8%stop並走記録。相方は判定条件のみで一切変更しない。
- **相方シグナルの正本（Codex R2 MAJOR-3対応・実装前に固定）**: 相方＝**generator直接出力**
  （returns生成前・保有期間dedup前・ユニバース判定前）。主KPI＝通常のCanonical prefilter・
  ユニバース・dedupを適用した取引可能イベント。一意キー=(kpi, code, signal_date)。

| KPI | モジュール | generator関数 | 相方として使う段階 |
|---|---|---|---|
| three_up_ignition | kpi_round30_signals | generate_three_up_ignition_signals | generator直接出力 |
| engulf_reversal_day | kpi_round30_signals | generate_engulf_reversal_signals | 同上 |
| gap_hold_close_strong | kpi_round29_signals | generate_gap_hold_close_strong_signals | 同上 |
| sell_reg_trigger_rebound | kpi_round23_signals | generate_sell_reg_trigger_signals | 同上 |
| turnover_rank_surge | kpi_round23_signals | generate_turnover_rank_surge_signals | 同上 |
| volshock_5x | kpi_volshock_signals | generate_volshock_signals | 同上 |
| high52_breakout | kpi_high52_signals | generate_high52_signals | 同上 |

パラメータ正本＝各§7登録時の凍結値（config/paper_watchlist.json の params と各生成器既定値）。
実装コードの content hash は稼働前の実装レビュー時に凍結する。
- **開始日**: 2026-07-30 以降の新規シグナルのみ（覗き見済み期間と完全分離）。
- **記録**: `data/paper_trades/pair_forward_ledger.jsonl`（追記専用・(pair_id, code, signal_date) 去重・
  全行に `tainted_origin: "peek_20260729"` を付与）。既存ペーパー台帳とは分離（本線の前向き完全性を守る）。
  **記録時刻規律（Codex R2 MINOR-2対応）**: 各行に detected_at / signal_date / entry_mode /
  generator_sha を必須記録。**初回シグナル行と成績確定は同一行の後日上書きにせず、成熟時に
  別イベント行（event=matured・outcome_matured_at付き）で追記**する。
- **評価**: 各ペア n≥30 到達時または12ヶ月経過時に記述評価（EV・到達率・対単体差）。
  **成績によるGO/NO-GO判定はしない**。正式化は§7-X経由のみ。
- **停止**: bot等の外部要因なし（bars のみで完結）。データ欠損日は欠測として記録し補完しない。

## 3. 実装（凍結後に着手）
週次または日次バッチ `scripts/pair_forward_scan.py`（新設・読み取り→専用台帳append のみ・
本線 daily_screen/ledger には一切書かない）。実装後に回帰テスト＋Codex実装レビューを経て稼働。

## 4. 経緯
2026-07-29: ユーザー発案「複数KPIの掛け合わせで勝率見てますか？」→初回記述測定（27ペア）→
第4バッチ申請→Codex R1で ペア=BH家族からNO-GO・前向きなら可 → ユーザー裁定「やる」→本凍結。
