# Task: 品薄情報の取り口を増やす（TDnet 開示表題の供給側レーン＋発見器 16 語・週 2 便）

Phase: 株アルゴ研究 情報収集の拡充（`influx-stock-algo-architecture.md` 毎朝スクリーン行・発見器行）／P-INF-29（オーナー裁定 A 2026-10-04・叩き台= vault `02_Ai/influx/influx-shortage-sources-proposal-2026-10-04.md`）
Status: **done 2026-10-04**（commit 8e8470e 案 2・3／a1f822b 案 1）

## 答える問い
- 「品薄で取れる情報をもっと増やしてください」＝ X の固定 55 式を凍結したまま、企業発（TDnet 表題）と X の発見器の 2 口で品薄の材料を増やせるか

## 目的
①TDnet 開示表題から企業が自分で言う供給側の品薄（増産・生産能力増強・新工場・大口受注・価格改定）を銘柄コード付きで台帳に記録だけする（判断に使わない）。②X 発見器の入口語を 7→16 語にし、日曜＋水曜の週 2 便にする。

## 対象と数え方
- TDnet: `data/tdnet/index/<YYYY>/*.json.gz` を Tdnet.id で重複除去し pubdate で絞る（週次・日次チャンクが重なり同じ開示が最大 3 回出る）。母集団= 重複除去後に期間内へ絞った表題数。確認= `python3 scripts/tdnet_supply_lane.py --days 30 --dry-run`（出力行に 候補／走査／periodic／TOP1000内）。
- 採点対象の事象= `periodic=false` かつ `in_top1000=true`。確認= `jq -c 'select(.periodic==false and .in_top1000==true)' data/tdnet/supply_lane.jsonl | wc -l`。
- 発見器: `configs`ではなく `scripts/price_watch_discover.py` の `DISCOVER_QUERIES`（16 語）。便ごとの件数= `data/x_price_watch/discovery_queue.jsonl` の `n_posts/n_cand/first_seen`。

## 成功基準
- [x] `scripts/tdnet_supply_lane.py`（stdlib のみ・ホスト /usr/bin/python3 で動く）が id で冪等 append し、index 欠損・窓内 0 件は exit 1
- [x] index 読込・id 重複除去・5桁→4桁は `scripts/tdnet_index_lib.py` に 1 箇所化し `driver_discover_boj.py` も同じ部品を使う（`--selftest` 0 NG）
- [x] `daily_screen.py` の表示ビルダー loop 先頭で非致死実行・失敗は表示ビルド状況欄に ⚠️
- [x] `build_daily_reco.py` に 🏭 節（直近 5 営業日・判断に使わない）
- [x] 採点の事前登録= `docs/price-watch-universe.md` §16o-3 追記10
- [x] 発見器 16 語・STOPLIST に 3 文字以上の入口語・plist に Weekday 3・`~/Library/LaunchAgents` 再登録
- [x] Docker unittest 522 OK（新規 `tests/test_tdnet_supply_lane.py` 9 件）

## 実測（2026-10-04）
- TDnet `--days 30 --dry-run`: 範囲 2026-09-04〜10-04／候補 16（走査 4,178）／periodic 2／TOP1000 内 3（タマホーム 1419 受注速報＝定期・日東工 6651 価格改定・メック 4971 生産能力増強）＝採点対象 2
- 故障注入: index ディレクトリ無し→ exit 1／窓内 0 件（2020-01-01〜02）→ exit 1
- 発見器 10-03 便: posts=255・候補 20・初出 9・割当／生産振替 0 件（参考・次便で再測。0 が続けば調達難と同じく除外候補）

## Session Handoff
- 済: 上記すべて・vault 台帳 P-INF-29 ✅・decisions.md 2026-10-04 エントリ
- 保留: 案 4（在庫率・受注残統計）／5（フリマ品目）／6（Terapeak）は別起案
- 既知の穴: 連休で窓が全休場の朝（年末年始・GW）は tdnet_supply_lane が exit 1 で ⚠️ が出る（docs 追記10 に明記）／30 日の近傍こぼれ= 設備投資（単独）7・「大型案件の受注」5 は v0 では拾わない
- 次の自動便: 発見器 水曜 10-07 10:40／TDnet レーンは 10-05 月 07:30 の daily_screen で本番初回
