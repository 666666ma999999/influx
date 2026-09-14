# Task-light: 無料3データで「大口」を追う（空売り残高・大量保有報告の本文・X言及）— 2026-09-14 オーナー裁定 A

## Goal
「入る日」を出来高・材料で当てる路線は3計測で空振り（tasks/bigholder_footprint.md・busy_with_reason.md・jsea_backtest）。手元にある未使用データ2本＋収集中1本で、大口の「本体」に近い情報を使って測り直す。

## 成功基準
- [ ] S1 ①空売り残高: 凍結定義の事象（ショートカバー転換／大口空売りの新規出現）について 2016-08〜2026-09 の 20/60営業日 超過リターンを対照と比べた表（`output/shortsale_backtest.md`）
- [ ] S2 ②報告本文: 2021-07〜2026-09 の新規大量保有報告（formCode=010000）の本文CSV（EDINET type=5）を取得し、報告義務発生日・保有割合・取得資金・60日取得状況（日付・単価）を抽出（取得数／失敗数を記録）→ 「提出日の株価 ÷ 大口の平均取得単価」で層別した 20/60営業日の超過リターン表（`output/bigholder_body.md`）
- [ ] S3 ③X言及: mention_alerts.jsonl の評価（w5/w20）の集計を本 task に記載（既存データの要約・作業なし）
- [ ] S4 3本の結論を tasks の Decision Log に1行ずつ

## 凍結定義
- ①事象A「ショートカバー転換」= scripts/kpi_shortcover_signals.py の条件そのまま（銘柄別の継続報告者 ShrtPosToSO 合計 ≥3% かつ最新報告で前回比減少・消失レコードは減少判定に加算しない・シグナル確定日= DiscDate の翌営業日）。事象B「大口空売りの新規出現」= ある報告者の ShrtPosToSO が初めて ≥1.0% で報告された DiscDate。entry= 確定日の翌営業日 AdjO、exit= 20/60日目 AdjC、超過= 銘柄−TOPIX。対照= 同銘柄の無作為日（各事象3日・seed=7・事象±120日を避ける）。層= 全体／前後半（2016-08〜2021-06／2021-07〜2026-09）／静的時価総額2層。
- ②取得: data/edinet/*.json.gz の ordinanceCode=060・formCode=010000・csvFlag=1（2021-07-01〜2026-09-10）。`/api/v2/documents/{docID}?type=5` を 1 req/秒・data/edinet/bodies/{docID}.zip に保存（既存はスキップ＝再開可能）。抽出: DateWhenFilingRequirementAroseCoverPage（義務発生日）・HoldingRatioOfShareCertificatesEtc（FilingDateInstant の総計）・TotalAmountOfFundingForAcquisition・保有株券等の数（要素名は実CSVで確認）・60日取得状況 TextBlock（年月日・数量・単価の表を正規表現で）。平均取得単価= 取得資金合計 ÷ 保有株数（TextBlock から単価が取れればそちらを優先し両方記録）。
- ②判定: T= 提出日。層= 提出日終値 ÷ 平均取得単価 が ≤0.95／0.95〜1.05／>1.05 の3層 と 到達日→提出日の営業日数（≤5／>5）。entry= T+1 AdjO・exit 20/60日目・超過= 銘柄−TOPIX。対照= 全営業日×全銘柄の無作為（seed=7）。
- ③: mention_alerts.jsonl の type=evaluation を window 別に n・goal_confirmed 数・超過中央値・勝率で集計（2026-08-04〜）。

## 変更禁止
repo 既存ファイル（読むだけ）。書き込みは output/ の2ファイル・data/edinet/bodies/（本文キャッシュ）・本 task のみ。

## Decision Log
| 日時 | 決定 | 理由 |
|---|---|---|
| 2026-09-14 | 起票・裁定 A | 「あと何を取得すれば」→ 無料3本を先に使い切る |
| 2026-09-14 | ③X言及の集計（作業なし）: w5 n=51・goal 22（43%）・超過中央値 +0.4pt・勝率 51%／w20 n=22・goal 7（32%）・超過中央値 −4.0pt・勝率 32% | 2026-08-04〜の警報 67 件・評価 73 行。標本小・前向きのみ |
