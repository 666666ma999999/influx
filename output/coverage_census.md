# 実効カバレッジ census（監視の広さ・重複除外）

- 生成: 2026-08-17T07:21:33+00:00（`python3 scripts/coverage_census.py`）
- 数え方の正本: tasks/xprice_reform_review.md §8（P-08a 裁定 2026-08-17）。
  **入力件数ではなく「独立ドライバー×稼働取得経路×関門通過カード」の重複除外集合**を数える。

## いまの実効カバレッジ

- **実効ドライバー数 = 22**（3倍の目標値 = **66**）
- 銘柄を出せる会社数 = 27 社（関門通過カードのみ・重複除外）
- 参考: 全ドライバー 80 / 稼働中 50（入力 117 件から重複 37 件を統合した後）
- ⚠️ 実効のうち 3 件は loose な対応表（市場全体を単一指標に縮約）に依存

## 詰まりの内訳（ここを開けないと網を広げても増えない）

- 🔇 カードはあるが上昇を鳴らす経路が無い: 1 件 — memory-asp-estat
- 🈳 取得は動くがカード無し: 27 件
- ⏳ 裏取り待ちのみ（confirmed 0・provisional あり）: 12 ドライバー / 22 社
  （参考: 全ドライバーの provisional を合算すると 65 社）

## レーン別の稼働

| レーン | 登録数 | 稼働 |
|---|---|---|
| B2B価格 | 55 系列 | 直近取得OK 50 系列 |
| news_shock | 20 クエリ | 稼働（最終 run=2026-08-16T22:20・成功20） |
| X品薄 | 38 subject | 発火不能（直近1件が n_wave_prev=0（前28日窓が未充填＝発火不能）） |

## 実効ドライバー一覧

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
- methanol
- mtool-orders
- nand_spot
- nickel
- platinum
- pvc
- scfi
- scrap
- semicon-equip
- silver
- toreca-sar
- uss-used-car


## ⚠️ 入力の警告（0件と破損の区別）

- sign 未記入のカード 58 件（受益と主張できないため計上しない）