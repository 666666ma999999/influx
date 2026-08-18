# 実効カバレッジ census（監視の広さ・重複除外）

- 生成: 2026-08-18T12:36:29+00:00（`python3 scripts/coverage_census.py`）
- 数え方の正本: tasks/xprice_reform_review.md §8（P-08a 裁定 2026-08-17）。
  **入力件数ではなく「独立ドライバー×稼働取得経路×関門通過カード」の重複除外集合**を数える。

## いまの実効カバレッジ

- **実効ドライバー数 = 30**（3倍の目標値 = **90**）
- 銘柄を出せる会社数 = 37 社（関門通過カードのみ・重複除外）
- 参考: 全ドライバー 80 / 稼働中 52（入力 117 件から重複 37 件を統合した後）
- ⚠️ 実効のうち 2 件は loose な対応表（市場全体を単一指標に縮約）に依存

## 詰まりの内訳（ここを開けないと網を広げても増えない）

- 🔇 カードはあるが上昇を鳴らす経路が無い: 1 件 — memory-asp-estat
- 🈳 取得は動くがカード無し: 21 件
- ⏳ 裏取り待ちのみ（confirmed 0・provisional あり）: 10 ドライバー / 20 社
  （参考: 全ドライバーの provisional を合算すると 69 社）

## レーン別の稼働

| レーン | 登録数 | 稼働 |
|---|---|---|
| B2B価格 | 55 系列 | 直近取得OK 55 系列 |
| news_shock | 20 クエリ | 停止（最終 run=2026-08-18T10:01 だが成功クエリ 0） |
| X品薄 | 38 subject | 発火不能（直近3件が n_wave_prev=0（前28日窓が未充填＝発火不能）） |

## 実効ドライバー一覧

- aluminum
- auto-production
- bdi
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