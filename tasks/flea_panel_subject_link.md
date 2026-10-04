# Task: フリマ固定パネルの品目を X 品薄の受益 subject に揃える（P-INF-29 案 5）

Phase: 株アルゴ研究 情報収集の拡充（`influx-stock-algo-architecture.md` フリマ固定パネル行）／P-INF-29（オーナー「進めて」2026-10-04・案 1〜3 は `tasks/tdnet_supply_lane.md`）
Status: **実装済み・未コミット 2026-10-04**（統括が検収後に commit）

## 答える問い
- 「品薄で取れる情報をもっと増やしてください」＝ X の品薄対応表（`configs/x_shortage_map.json`）で受益銘柄が出る subject について、フリマで需給（出品・成約・価格）を毎日測れる品目を揃えられるか

## 目的
フリマ固定パネル（`configs/flea_panel.json`）の各クエリに、対応する受益 subject（`subject` 項目）を持たせ、`data/flea_panel/daily.jsonl` の行に subject を出す。受益銘柄は subject 経由で引けるが**判断・発火には使わない（記録だけ）**。

## 対象と数え方
- 母集団= `configs/x_shortage_map.json` の subject のうち `actionable=true` かつ sign=+ の受益銘柄（provisional 以上）を持つ消費財系 4 件: kaden-neage／aircon-reizouko／used-car／secondary-resale（game-console・toreka・figure-plamo は actionable=false＝決算裏取りで却下済み＝対象外）。
- 確認= `jq -r '.queries[] | select(.subject) | "\(.subject)\t\(.id)"' configs/flea_panel.json`（subject ごとの紐付けクエリ数）。
- 日次の確認= `jq -c 'select(.subject) | {date,query_id,subject,open_n,sold_d1,status}' data/flea_panel/daily.jsonl | tail`。

## 成功基準
- [x] 4 subject それぞれに〈採用クエリ〉か〈外す理由 1 行（実測つき）〉がある
- [x] 新クエリ候補は 1 回実走して確認（scratch の data-dir で実走・本番台帳は汚さない）＝結果として新規クエリは 0 本（下の判定表）
- [x] `flea_panel_track.py` が daily 行に `subject` を出す（無いクエリは null・error 行にも出る）
- [x] `tests/test_flea_panel_track.py` に subject のケース 3 件（行への出力・error 行・実 config の紐付けがレビュー済みの対応と完全一致し x_shortage_map に実在・actionable）
- [x] Docker 一括 unittest OK（Ran 527）
- [x] 機能マップのフリマ行に 1 句

## subject ごとの判定（2026-10-04・Codex 敵対レビュー 1 周目で kaden-neage／used-car を外した）
| subject | 受益（sign=+・provisional 以上） | フリマで測る品目 | 判定 |
|---|---|---|---|
| secondary-resale | 4385 メルカリ（仮）※ゲオ 2681 は rejected | 既存の resale 品目 7 本（Switch 2・PS5・リザードン PSA10・ポケカ 151・遊戯王・ワンピース P.O.P・ガンプラ） | **採用**（新規アクセスなし）。対応表の X クエリ（トレカ高騰・ポケカ価格・ゲーム機・フィギュアプレ値）と同じ品目。板は Yahoo!フリマ＝メルカリの競合で相場の代理として見るだけ |
| kaden-neage | 7965 象印マホービン | なし | 外す: subject は「メーカーの定価改定」＝フリマの中古価格は定価を測らない。冬の加湿器品薄は需要型で別の機序（「象印 加湿器」10-04 実走 open_total=425・sold_d1=2 は取れたが採用せず） |
| aircon-reizouko | 2730 エディオン／8282 ケーズ／7513 コジマ／8173 Joshin | なし | 外す: 受益は量販の新品販売でフリマ中古は代理にならない。「エアコン 未使用」10-04 実走は前日成約 17 件中、本体 1 件・残りは配管・洗浄スプレー等 |
| used-car | 7599 ＩＤＯＭ | なし（既存「中古車 車体」は計測を変えず残す） | 外す: 既存クエリの state 9 件はラジコン 2・人力車 1・膜厚計 6＝車体を測れていない（成約 31,980 円／36,200 円もラジコン） |

## 実測（2026-10-04・scratch の data-dir・本番台帳には書いていない）
- 「象印 加湿器」: open_total=425 new_open_d1=4 sold_d1=2 sold_d1_med=3300 open_med=13490 status=ok（不採用）
- 「エアコン 未使用」: open_total=36316 new_open_d1=1 sold_d1=17 sold_d1_med=2400 open_med=2695 status=ok（本体以外が大半・不採用）

## Session Handoff
- 済: secondary-resale の 7 本に subject・daily 行に subject 列・テスト 3 件・機能マップ 1 句
- 次の自動便: 10-04 21:40 の launchd から daily 行に subject が出る（確認= `jq -c 'select(.date=="2026-10-04" and .subject) | {query_id,subject,status}' data/flea_panel/daily.jsonl`）
- 保留: 案 4（在庫率・受注残統計）／6（Terapeak）は別起案
- 既知の穴: 既存クエリ「中古車 車体」（group 7599）は車体を測れていない（ラジコン・膜厚計が混入）＝見直しは別裁定／部分 config を本番 data-dir に流すと、同日の他クエリ行が消える（`run()` が同日行を全置換）＝部分実走は必ず scratch の `--data-dir` で
