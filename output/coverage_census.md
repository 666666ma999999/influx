# 実効カバレッジ census（監視の広さ・重複除外）

- 生成: 2026-08-19T05:01:08+00:00（`python3 scripts/coverage_census.py`）
- 数え方の正本: tasks/xprice_reform_review.md §8（P-08a 裁定 2026-08-17）。
  **入力件数ではなく「独立ドライバー×稼働取得経路×関門通過カード」の重複除外集合**を数える。

## いまの実効カバレッジ

- **実効ドライバー数 = 33**（3倍の目標値 = **99**）
- 銘柄を出せる会社数 = 40 社（関門通過カードのみ・重複除外）
- 参考: 全ドライバー 86 / 稼働中 58（入力 123 件から重複 37 件を統合した後）
- ⚠️ 実効のうち 2 件は loose な対応表（市場全体を単一指標に縮約）に依存

## 詰まりの内訳（ここを開けないと網を広げても増えない）

- 🔇 カードはあるが上昇を鳴らす経路が無い: 1 件 — memory-asp-estat
- 🈳 取得は動くがカード無し: 24 件
- ⏳ 裏取り待ちのみ（confirmed 0・provisional あり）: 19 ドライバー / 29 社
  （参考: 全ドライバーの provisional を合算すると 78 社）

- 🔓 **止まっているレーンが開いた場合の実効**: 37 ドライバー / 46 社（増える分: memory-asp-estat, x:cpu-logic, x:secondary-resale, x:shokuhin-neage）

## レーン別の稼働

| レーン | 登録数 | 稼働 |
|---|---|---|
| B2B価格 | 61 系列 | 直近取得OK 61 系列 |
| news_shock | 20 クエリ | 稼働（最終 run=2026-08-18T22:20・成功20） |
| X品薄 | 38 subject | ウォームアップ中（直近2件が n_wave_prev=0＝**ウォームアップ中**（故障ではない）。見込み開通 **2026-08-31**（最短は gen2-kounyu-seigen（前窓 9/直近窓 28）・クエリ単位で計算・毎日収集が続く前提の最短値）。⚠️ configs/x_price_watch.json のクエリ文言か min_faves を編集すると query_sha が変わり、そのクエリの前窓履歴が無効化されて28日以上巻き戻る） |

## 実効ドライバー一覧

- aluminum
- auto-production
- bdi
- cgpi-carbon-graphite
- cgpi-cement
- cgpi-si-wafer
- coal
- coking-coal
- copper
- crude-oil
- fmbi-iridium
- fmbi-ruthenium
- gold
- iron-ore
- jepx
- lithium
- methanol
- mtool-orders
- nand_spot
- nickel
- palm-oil
- platinum
- pvc
- salmon
- scfi
- scrap
- semicon-equip
- silver
- uranium
- urea
- uss-used-car
- visitors-jnto
- zinc


## ⚠️ 入力の警告（0件と破損の区別）

- sign 未記入のカード 58 件（受益と主張できないため計上しない）