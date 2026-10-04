# Task: 品薄の取り口を増やす（鉱工業 在庫率指数の業種別を B2B 系列へ・P-INF-29 案 4）

Phase: 株アルゴ研究 情報収集の拡充（`influx-stock-algo-architecture.md` B2B価格チェッカー行）／P-INF-29 案 4（オーナー「進めて」2026-10-04・叩き台= vault `02_Ai/influx/influx-shortage-sources-proposal-2026-10-04.md`）
Status: **done 2026-10-04**（未コミット＝commit は親セッション）

## 答える問い
- 「品薄で取れる情報をもっと増やしてください」＝ 価格（B2B 72 系列）だけでなく、業界の数量側（在庫率の低下＝品薄）を月次の系列として同じ網に載せられるか

## 目的
経産省 鉱工業指数（IIP・2020 年基準）の **業種別 在庫率指数（季節調整済）** を B2B 価格チェッカー（`scripts/price_universe_check.py`）の月次系列として 5〜7 本足し、**在庫率の前年同月比が閾値以下へ新たに落ちた月**を「品薄の環境指標」として前向き記録する（受益銘柄は付けない・成績の主張禁止）。工作機械受注の内訳（内需・外需）は別系列化できるか判断する。

## 対象と数え方
- 取得元: e-Stat の鉱工業指数ファイル（政府統計コード 00550300・「2020年基準時系列データ（2018年１月～）」の「業種別／月次／季節調整済指数」＝ `b2020_gsm1j.xlsx`）。METI 本体（meti.go.jp/statistics/tyo/iip/）は 2026-10-04 実測で AWS WAF の JS チャレンジ（202 空応答／403）＝機械取得不可のため e-Stat の同一ファイルを使う。
- 母集団: `在庫率` シートの業種行のうち config に載せた業種コード（`iip_code`）。追加本数= `jq '[.series[]|select(.type=="iip_inventory_ratio")]|length' configs/price_universe_sources.json`。
- 発火の数え方（過去検証）: 2019-01〜最新月の各月で前年同月比を計算し、**閾値以下へ新たに跨いだ月**（直前月は閾値より上＝`monthly_triggers` のエピソード規則と同じ）を 1 回と数える。母集団= 前年同月比が計算できた月数。
- 確認コマンド: `docker compose run --rm xstock python scripts/price_universe_check.py --only <新系列id,...>`（ただし台帳に追記される＝初回投入の 1 回のみ）／取得関数だけなら `docker compose run --rm xstock python scripts/monthly_sources.py iip_inventory_ratio <code>`。

## 成功基準
- [x] `scripts/monthly_sources.py` に取得器 1 つ（業種コード引数・`parse_*`/`fetch_*` 分離・fail-closed）＋ checker の type ディスパッチ登録
- [x] 下落側で発火できる最小拡張（`alert.yoy_pct_down` 等）を `monthly_triggers` に追加し、既存の上昇判定の挙動は不変
- [x] 系列 5〜7 本を `configs/price_universe_sources.json` に追加（`beneficiaries: []`・note に算出根拠と「環境指標・前向き記録のみ・成績の主張禁止」）
- [x] 閾値は過去データ（2019〜）で発火回数を数えてから決める（月 1 回未満〜年数回）
- [x] `tests/` に parse と下落判定の単体テスト・Docker 一括ゲート OK
- [x] 新系列だけの実走で値が取れることを確認
- [x] `docs/price-watch-universe.md` 追記・architecture の B2B 行に 1 句

## 実測（2026-10-04）
- 系列 72→81（在庫率 7・工作機械の内需/外需 2）。閾値と過去の発火回数は `docs/price-watch-universe.md` §16o-3 追記11 の表（在庫率= 2019-01〜2026-08 の 92 ヶ月で 3〜7 回／内需 +15%= 91 ヶ月で 3 回・外需 +15%= 5 回）。
- 新系列だけの実走（台帳・前向き記録は一時ファイルとスタブへ逃がした本物の `main(only=…)`）: 9/9 ok・最新月 2026-08（在庫率は速報 p）。初回で鳴る見込み= 内需 +61.6%・外需 +65.8%（速報に前月の前年比が無い＝局面の途中でも 1 回鳴る）・窯業・土石 −5.7%（前月 −3.1%＝本物の新規跨ぎ）。電気・情報通信機械 −15.5% は前月 −10.3% で局面継続＝鳴らない。
- 実台帳 `data/price_watch/universe_weekly.jsonl`・`forward_log.jsonl` の md5 は実走の前後で不変（触っていない）。
- Docker 一括ゲート Ran 552 OK（新規 `tests/test_iip_inventory_ratio.py` 25 件）。Codex（gpt-6-astra）3 周: 空ヘッダで値が 1 列ずれる／下落判定が取得漏れ月を飛ばす／比較相手不明で鳴る／内訳を行位置で選ぶ、の 4 件を修正。最新月をヘッダで決める案は不採用（ヘッダ末尾に空の月が来うる・陳腐化ガードで代替）

## Session Handoff
- 済: 取得器 `fetch_iip_inventory_ratio`（e-Stat 経由・業種コード引数）・`fetch_jmtba(part)`・`monthly_triggers` の下落側キー・config 9 系列・テスト・docs 追記11・architecture の B2B 行
- 保留: commit は親セッション判断
- 既知の穴: ①過去検証は改定後の現行値 ②2021 年の跨ぎの一部はコロナ反動 ③工作機械内訳は初回に 1 回鳴る ④在庫発見器 `inventory_discover_iip.py` の取得 URL（経産省本体）は WAF で現在取れない（`--offline` は可・本 task の範囲外）⑤`price_universe_run.sh` の健全性下限 MIN_OK=42 は 72 系列時の値のまま（81 系列で約 52%）
- 次の自動便: 2026-10-05（月）11:00 の weekly 便で新 9 系列の初回記録
