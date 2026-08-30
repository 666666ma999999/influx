# Task: 素材不足・フリマ高騰物品の探索拡張（一次統計主入口＋発見器補助・P-INF-10 裁定 a）

## Metadata

| 項目 | 値 |
|------|-----|
| Status | done（裁定3件も同日実装済み・残なし）|
| 開始日時 | 2026-08-30 19:00 |
| 最終更新 | 2026-08-30 20:00 |
| 担当 | Claude（統括）＋ SubAgent 2本 |
| 出所 | 敵対レビュー wf_c81fa484-32d 割れ→オーナー裁定 a（2026-08-30）。報告= vault `02_Ai/influx/notes/influx-shortage-goods-mcap500-2026-08-30.md` §5 |

## Goal

時価総額500億超の受益銘柄に効く「まだ監視していない素材・商品」を、無料一次統計（日銀 CGPI/SPPI 品目・TDnet 開示）を主入口に列挙し、既存の X 発見器 `price_watch_discover.py` を補助として再稼働する。

## 成功基準

- [x] A-1 `python3 scripts/driver_discover_boj.py` が日銀 CGPI/SPPI の**全品目**を読み、`configs/price_universe_sources.json` で未監視の品目を前年同月比の大きい順に出力する（実行ログに「品目数 N／未監視 M／上位 K 件」が出る）
- [x] A-2 同スクリプトが TDnet 直近90日の開示タイトルから〈価格改定・値上げ・増産・受注停止・出荷停止・供給〉語を含む件を列挙し、会社コードを `data/center_pin/center_pin.jsonl`（TOP1000）と照合して in/out を付ける
- [x] A-3 上位候補（品目・イベント）に center_pin の pin/note をキーワード照合した**候補会社**を付け、`output/driver_discover.md` に表で出す（tier は付けない＝帰属は別工程）
- [x] B-1 `docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_discover.py` を1回実走し `data/x_price_watch/discovery_queue.jsonl` の行数が 2 から増える（増えない場合は理由をログで示す）
- [x] B-2 launchd `com.influx.price-discover`（週1）を `config/launchd/` に追加・登録し `launchctl list | grep price-discover` に出る
- [x] B-3 `influx-architecture.md`（該当なら `docs/pipeline-map.md`）に定期ジョブ1本の増加を反映し vault 鏡を再同期
- [x] 共通 `python3 -m unittest discover -s tests` が全件 PASS

## Current Agreed Scope

### Must
- [x] A 一次統計主入口（新規 `scripts/driver_discover_boj.py`＝役割が既存と違うため新設・オーナー裁定 a の範囲内）
- [x] B 発見器の再稼働＋launchd 週1

### Descoped
- `x_shortage_map.json` の tier 同期（メルカリ/TEL/SUMCO）・CCBJH 格下げ＝未裁定（別カード）
- 受益の tier 判定（帰属プロトコル v2 は別工程 `beneficiary-attribution`）
- 有料データ

## Progress Snapshot

### Done
- [x] 敵対レビュー・裁定 a（2026-08-30）

### In Progress
- [x] A / B 実装完了（2026-08-30 19:25・B commit cfcce83）

## Session Handoff

- 2026-08-30 完了: A 0f0aa69/f58e61b・B cfcce83・vault §5/§6。追加裁定①5系列 87e1524（64→69本）②③ tier 同期＋関門 18c0e5b（329 tests OK）
- 逸脱申告: 関門(a)は price/secondary 型のみ適用（capex 型の 4062 イビデンを誤検出したため統括判断で絞った・オーナー未裁定）
- 残: なし（push はオーナーの `!`）。次回の観測= 日曜 10:40 発見器・月曜 11:00 週次系列（新5本の初回発火は 2026-07 分で記録済み・次の公表月から）
