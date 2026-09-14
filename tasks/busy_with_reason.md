# Task-light: 「賑わい始めた株」×「理由あり」の事前登録バックテスト（2026-09-14 オーナー裁定 a）

## Goal
大口の足跡計測（tasks/bigholder_footprint.md）で「売買代金の膨らみは大口の印だが、それだけで買っても超過なし」と出た。賑わいの**初日**に「理由」（会社開示）が伴う銘柄だけに絞ると成績が変わるかを、過去5年で先に測る。

## 成功基準
- [ ] S1 凍結定義どおりに 2021-07-01〜2026-09-10 の全上場普通株で「賑わい初日」イベントを毎営業日走査で抽出し、理由あり／理由なしに分けて 20/60営業日の超過リターン（銘柄−TOPIX）を表にする（中央値・平均・勝率・+10%以上・−10%以下・n）
- [ ] S2 前半（2021-07〜2024-06）／後半（2024-07〜2026-09）・時価総額2層（静的・最新 MktCap ≥1000億）で同じ表
- [ ] S3 「静→動」（賑わいの前20日が静か）の副群も同じ表
- [ ] S4 `output/busy_with_reason.md` に実測メモ・表・注意を書く

## 凍結定義（後から変えない）
- 賑わい指標 R(t) = 直近5営業日の売買代金(Va)平均 ÷ その前120営業日の Va 平均。**初日**= R(t) > 1.17 かつ R(t−1) ≤ 1.17（1.17 は bigholder_footprint の前半対照 p75 を流用）。履歴 125 営業日未満は対象外。
- 理由あり = その銘柄の TDnet 開示（data/tdnet/index/YYYY/*.json.gz の items[].Tdnet: company_code 5桁・pubdate・title）が **[t−3, t] 営業日**にある（15:00 以降の開示は翌営業日扱い＝ scripts/_tmp_material_forward.py の規約）。2 変種: (a) 開示が何でもあり (b) 凍結7型（earnings／upward_revision／forecast_revision_other／buyback／tob／alliance／dividend_up＝同スクリプトの TYPES/RULES を import か複製・NFKC+casefold・先勝ち）のみ。
- 理由なし = 同期間に開示ゼロ。
- entry = t+1 の AdjO（look-ahead なし）。exit = entry を1日目として 20日目／60日目の AdjC。超過 = 銘柄リターン − TOPIX 同期間リターン。
- 静→動 = 初日の直前20営業日の R が常に < 0.8。
- 分割補正: bars の AdjC/AdjO/AdjVo は各ファイルの取得時点までの分割のみ反映＝ fetch_log.jsonl の取得日より後の AdjFactor≠1 を累積して旧側に掛ける（Va は分割不変）。
- 対照は不要（理由あり vs 理由なし が比較）。参考に「全営業日×全銘柄」の同窓リターンも出す。

## 変更禁止
repo 内の既存ファイル（読むだけ）。出力は output/busy_with_reason.md と本 task のみ。

## Decision Log
| 日時 | 決定 | 理由 |
|---|---|---|
| 2026-09-14 | 起票・裁定 a | 「賑わいを捉えるには」→ 既存の出来高ショック検知に「理由」を付けて事前登録で測る |
