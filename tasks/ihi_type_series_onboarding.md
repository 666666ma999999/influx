# Task: IHI型スクリーニング候補のセンターピン系列を price_universe に登録する

## Metadata

| 項目 | 値 |
|------|-----|
| Status | active |
| 開始日時 | 2026-09-13 16:20 |
| 最終更新 | 2026-09-13 17:10 |
| 担当 | Claude（統括）＋ SubAgent（実装） |
| 関連 plan | `plan.md`（既存範囲内の Slice・price-watch 系列追加）／設計正本 `docs/price-watch-universe.md` |

## Goal

「半年30%下落 × 時価総額1000億以上 × 国が潰せない」で抽出した候補（`output/ihi_type_screen.md`）のうち、無料の一次ソースが実証できた銘柄のセンターピン系列を週次の値上がり検出網（`configs/price_universe_sources.json`）に登録し、自動で追える状態にする。

## オーダー原文（2026-09-13 オーナー裁定 A）

「この5銘柄を追える形にする」→ 実現可否の実測後に「A（推奨）取れる3本を登録＋2本は調査続行」を選択。

## 成功基準

- [x] S1 `configs/price_universe_sources.json` に `iata-rpk`（IHI 7013）と `jsea-export-ships`（三井E&S 7003）の2系列が受益カード付き（btype/sign/tier/evidence/verified）で追加され、`jq '.series|length'` が 69→71
- [x] S2 `python3 scripts/monthly_sources.py --selftest` が新規パーサ（iata / jsea）の固定テキスト検査を含めて全 ok（NG 0）
- [x] S3 `docker compose run --rm xstock python scripts/price_universe_check.py` を実行し、`data/price_watch/universe_weekly.jsonl` に 2 系列の `status: ok` 行が入る（value が IATA 7月分= yoy +0.2 近傍・JSEA 7月分= CGT 403,390）
- [x] S4 `python3 -m unittest discover -s tests` が全件 PASS（件数が実行前と同じか増加）
- [x] S5 `docs/price-watch-universe.md` に2系列の出所・落とし穴・閾値根拠の節が追加され、`output/ihi_type_screen.md` の実現可否表が「登録済み」に更新
- [ ] S6 同セッションで意味単位 commit（config／scripts／docs）

## Current Agreed Scope

### Must
- [x] IHI 7013 ← IATA 月次プレスリリース「Air Passenger Demand …」の Total RPK 前年比（type=iata・数量型・value=yoy%）
- [x] 三井E&S 7003 ← 日本船舶輸出組合「輸出船契約実績」月次 PDF の当月 CGT と前年同月比（type=jsea・数量型）
- [x] 受益カードは `beneficiary-attribution` の5関門で審査（B: center_pin 台帳に 7013/7003 とも行あり＝通過。A: 数量型で sign=+。C: RPK→スペア部品需要／新造船契約→舶用エンジン受注は1ホップ。D: 式1本。証拠: 決算短信のセグメント利益を実読して E2 が取れれば confirmed・取れなければ provisional）

### Descoped（明示的に除外・理由つき）
- 千代田化工 6366 ← 天然ガス価格: **関門C（1ホップ制限）に抵触**（ガス価格→LNG投資判断→プラント受注の2ホップ）かつ center_pin 台帳に行なし（関門B）。裁定 A 時点では「登録」だったが、台帳ルールと矛盾するため**登録せず調査続行群へ回す**（逸脱の申告＝統括の最終報告で明記）
- 東亜建設 1885（国交省 受注動態統計）・伊勢化学 4107（貿易統計 ヨウ素輸出単価）: 一次ソース未実証のため次周
- スカパーJSAT 9412: 公開の月次系列なし

## 影響範囲

`configs/price_universe_sources.json`／`scripts/monthly_sources.py`（fetch_iata・parse_iata・fetch_jsea・parse_jsea・_selftest 追記）／`scripts/price_universe_check.py`（type allowlist 1行）／`docs/price-watch-universe.md`／`output/ihi_type_screen.md`／本 task

## 変更禁止ファイル

`data/price_watch/forward_log.jsonl`（過去の発火は書き換えない）／`configs/x_shortage_map.json`／`scripts/price_watch_forward.py`

## Current State

| 項目 | 値 |
|------|-----|
| Summary | 実装完了（SubAgent 2026-09-13）: 2系列登録・selftest all ok・check 実走で 70/71 ok（新2系列とも ok・parse_fail 0）・unittest 357 OK。残= S6 commit（統括） |
| Focus | S6 commit → オーナー裁定（IHI の tier・数量型の関門D/btype 解釈） |
| Confidence | medium（PDF の列順は実測で固定・IATA の URL 命名は月ごとに変わる） |

## Progress Snapshot

### Done
- [x] 実現可否の実測（`output/ihi_type_screen.md` §追える形にする）
- [x] 一次ソースの実証: IATA `/en/pressroom/2026-releases/08-31-air-passenger-demand-grows-july/` 本文「Total demand, measured in revenue passenger kilometers (RPK), was up 0.2% compared to July 2025」／JSEA `https://www.jsea.or.jp/results/` の月次 PDF（7月分 CGT 行 `<4,026,569> <610,420> <297,478> <312,948> <646,862> <403,390> <1,660,678> <2,597,776>`＝列順は 前年度計・3月・4月・5月・6月・7月・4〜7月・1〜7月、前年同月比行 `(221.4)` が当月）

- [x] 実装（SubAgent・2026-09-13）: `scripts/monthly_sources.py` に fetch_iata/parse_iata/fetch_jsea/parse_jsea＋selftest 10件、checker allowlist に iata/jsea、config 69→71、docs §16z、screen 表を「登録済み」に
- [x] E2 実読: IHI Q1短信（TDnet 140120260805509194.pdf）航空・宇宙・防衛 29,199/連結 73,218＝39.9%→当初 confirmed・Codex 再レビューで関門D 未立証＋粒度を指摘され provisional に変更（オーナー裁定待ち）／三井E&S Q1短信（TDnet は 404・同社 IR の financial-results_2027-1q-results.pdf）舶用推進 3,828/連結 10,178＝37.6%→E2 は取れたが center_pin の pin（港湾クレーン）と別定義のため provisional
- [x] check 実走（2026-09-13 日曜・全系列）: iata-rpk 0.2(2026-07) ok／jsea-export-ships 403,390(2026-07・yoy+121.4) ok→初回発火・forward 記録 7003(仮)。副作用: 他11系列も同日発火（WTI 等）で forward_log に同日記録（月曜ジョブで記録されるはずだった分が1日早く入った）

### In Progress
- [ ] S6 commit（統括）

## Decision Log

| 日時 | 決定 | 理由 |
|---|---|---|
| 2026-09-13 | 千代田化工の天然ガス系列は登録しない | 関門C 2ホップ・関門B 台帳行なし。裁定 A からの逸脱として統括が報告 |
| 2026-09-14 | オーナー裁定 A: 材料先読みの起点は1ホップの値段・数量に限定・公開統計を起点にしない。7003 の JSEA カードを rejected（系列は通知のみ） | オーナー「船の注文を起点に考えるのがどうなんでしょうか」。vault decisions 2026-09-14 に記帳 |
| 2026-09-13 | 【interpreted】数量型カードの関門D は「セグメント営業利益構成比≥30%（E2）」で代替・btype は付けない | 連動比率 k が非開示で式単独では立証不能。波2（2026-08-02）の数量型カード（オークマ・三越伊勢丹）と同じ扱い。Codex REJECT 指摘4/5 への回答＝オーナー未裁定（本 task 完了報告で申告） |
| 2026-09-13 | 【deviation→修正】Codex REJECT 3件を修正: IATA 跳び検知を%ポイント差・IATA 対象年の誤記録・JSEA 前年比欠落を失敗扱い | 継続監視で通知が止まる/誤月で記録する欠陥。selftest に回帰4件追加 |
| 2026-09-13 | 【open-question】閾値 +8%（IATA）/+50%（JSEA）の年間発火頻度の目安 | AI 仮置き。初回4ヶ月の実測で決める |
| 2026-09-13 | 【deviation→修正】parse_jsea が7月分しか読めない欠陥（月列本数＝表題の月の仮定）を、月ラベル行から当月位置を決める8列固定に書き直し。2015年度〜136本で 136/136 | オーナー「過去のデータで確認できますよね」→ 遡り計測の副産物で発覚。来月（8月分・9/中旬公表）の週次取得から失敗するところだった |
| 2026-09-13 | 【open-question】JSEA 前年比 +50% は過去10年で 7003 の株価を予測していない（`output/jsea_backtest_7003.md`）。閾値の再定義（水準・手持工事量）は未裁定 | 群A n=36・超過中央値 +1.0/−0.9/−4.8%・対照と同等 |
| 2026-09-13 | 裁定A（水準で測り直し）実施→ 5定義（単月跳ね・3か月高水準・手持工事量増勢・12か月移動前年比・低水準）とも対照と差なし | `output/jsea_backtest_7003.md` §水準版。JSEA 系列の扱い（通知のみへ格下げ／船社発表日を entry に再測）は未裁定 |
| 2026-09-13 | 7003 は provisional（E2 37.6% は取得済み） | §0b 2026-08-31 裁定の confirmed 2条件のうち「center_pin の pin が同定義」を欠く（台帳 pin=港湾クレーンの受注台数）。台帳は影響範囲外＝pin 変更は別裁定 |
| 2026-09-13 | iata-rpk の value＝前年同月比%（オーダー通り）。跳び検知は %ポイント差 30 超に分離（修正済み） | 比率判定だと 0.2→2.0 で永久 suspect＝Codex REJECT 1 件目。「次の公表月で実測」の旧懸念は解消 |

## Session Handoff

- 次: 統括が S1〜S5 を実測（`jq '.series|length'`=71・`--selftest` all ok・`grep -E '"id": "(iata-rpk|jsea-export-ships)"' data/price_watch/universe_weekly.jsonl | tail -2`・unittest 357 OK）→ S6 意味単位 commit（configs／scripts／docs／tasks）
- 宿題: ①IHI 7013 の tier をオーナー裁定（関門D 未立証＋粒度で provisional に下げた。confirmed に戻すには「数量型は E2 セグメント利益構成比で桁チェック代替」の裁定が要る） ②三井E&S の center_pin pin を舶用に改めるかの裁定（改めれば 7003 を confirmed へ） ③IATA/JSEA 閾値（+8%/+50%）は AI 仮置き＝鳴りすぎ/沈黙が見えたら調整
- 使い捨て: `output/_tmp_jsea_2026_07.pdf`（統括が取得・selftest の固定行の出所）は残置。SubAgent の一時ファイルは削除済み
