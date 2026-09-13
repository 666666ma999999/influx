# 材料の型 → 20営業日フォワードリターン（記述測定・P-INF-15 裁定 a・2026-09-13）

**Phase**: plan.md 肝ツリー 大B「上昇サインだけを検知する」B1 の枝（参考観測・catalog §6 の正式判定ではない）
**裁定の正本**: vault `wiki/meta/decisions.md` 2026-09-13 P-INF-15／レビュー報告= vault `02_Ai/influx/reports/influx-bigholder-material-adversarial-review-2026-09-13.md`
**状態**: 実装中（Codex gpt-6-astra・task km70rvjc6・2026-09-13 11:30〜）

## 目的
「大口が何を見て買ったか」を、届出でなく**材料の公表日を起点**に測り直す。TDnet 表題18年分で材料7型ごとの「公表後20営業日の値動き」を出し、一般報告の大口が同時期に入ったか（2021-07〜）をタグで重ねる。出口= IHI 型スクリーニング MD の材料条件（セッション目標）。

## 成功基準
| # | 基準 | 測定 |
|---|---|---|
| S1 | 型×層別（全体／時価総額1000億以上／大口タグ）の表が `output/material_forward_2026-09.md` に出る | 実読 |
| S2 | 調整済み株価（AdjO/AdjC）のみ使用・TOPIX 超過も併記 | スクリプト実読＋selftest |
| S3 | lift と excess20 の 95% CI が月次ブロック bootstrap で出る（catalog §6-5 準拠） | json の CI フィールド |
| S4 | 凍結定義（下）からの逸脱なし。逸脱したい点は「変更提案」として別記 | MD 末尾の一行 |
| S5 | selftest 4点 PASS・本番1回の所要時間が記録される | 実行出力 |

## 凍結定義（データを見てから変えない）
- 型: earnings「決算短信」／upward_revision「上方修正」／forecast_revision_other「業績予想の修正」(上方修正を含まない)／buyback「自己株式の取得|立会外買付|自己株式取得」／tob「公開買付」／alliance「資本業務提携|業務提携|資本提携」／dividend_up「増配|配当予想の修正」(減配・無配を含まない)。先に一致した1型・同一銘柄同日同型は1件。
- T: pubdate < 15:00 JST の営業日なら当日、以後・非営業日は翌営業日。entry= T+1 の AdjO・exit= entry から20営業日目の AdjC。ret20・excess20（対TOPIX）・hit20（≥+20%）。
- 大口タグ: formCode 010000 の新規大量保有（documents_all・issuerEdinetCode→code master）の提出日が [T−10, T+30] 営業日にあれば 1（2021-07-07 以降のみ）。
- 対照: 同ユニバース×同期間の無作為 銘柄日 100,000 件（seed 7）。lift= hit20 率の比。CI= 月ブロック bootstrap 1,000 回。
- 層別: 全体／MktCap ≥ 1,000億円（T−1 の bars 値）／大口 0/1。

## 影響範囲
- 新規: `scripts/_tmp_material_forward.py`（gitignore・研究隔離）・`output/material_forward_2026-09.{json,md}`（output は非追跡）
- 変更なし: 既存 scripts・data・catalog

## 変更禁止ファイル
- `docs/stock-algo-kpi-catalog.md`（結果を KPI として書かない・参考観測）
- `data/**`（read-only）

## 全体図
influx-architecture.md §2 が代替（K-308）。流れ: TDnet index → 型分類 → T 確定 → bars(Adj) で ret20/excess20 → EDINET 一般新規報告でタグ → 対照・CI → MD/JSON。

## 既知の限界（レビュー一致より）
- 大口タグは EDINET 5年窓ゆえ 2021-07〜のみ＝参考観測。正式 KPI 化は E群既存定義（PEAD #8・上方修正 #9 等）の周回で行う。
- 外部価格型（センターピン高騰）は履歴が無く本測定の対象外（前向き記録）。
- 「国が潰せない」の機械定義は未定義（スクリーニング MD 側で決める）。

## Session Handoff
- 2026-09-13 11:30: 裁定 a を日報・decisions に記帳。Codex Astra へ凍結定義つきで発注（task km70rvjc6）。次= 返り値を実読→selftest/本番出力を統括が再実行して検証→結果を vault レビュー報告 §7 に接続→スクリーニング MD（セッション目標）へ。
- 2026-09-13 16:45 本番完走（Codex Astra・68秒・peak 483MiB・selftest PASS・統括が Adj 列使用と MD 末尾「凍結定義からの逸脱なし」を実読）。結果= vault レビュー報告 §7。**訂正**: 日足は 2016-07〜＝測れたのは約10年（18年は索引のみ）。**逸脱1件（記録）**: T−1 MktCap が全期間欠損（bars の MktCap 列は 2026 直近のみ）で「1000億円以上」層が測定不能→ 2026-09-10 時点の MktCap≥1000億の静的ユニバース層を追加依頼（codex-reply・task kn6314khj・将来情報を含む静的フィルタと明記）。S1 は静的層の追記待ち・S2〜S5 は達成。
