# Task: 稼働中レーンの株式分割の穴を塞ぐ（反発系の合図・価格レーンの採点）

Phase: 株アルゴ研究（`influx-stock-algo-architecture.md` 反発系=sell_reg_trigger_rebound 行・価格レーン=price_watch_forward 行）／オーナー裁定 2026-10-04

## 答える問い
- 「反発系と価格レーンを今直す（推奨）」

## 対象と数え方
- 範囲:
  1. 反発系の合図の生成器 `scripts/kpi_round23_signals.py` `generate_sell_reg_trigger_signals`（分割の権利落ち日の偽合図）
  2. ペーパー台帳 `data/paper_trades/ledger.jsonl` の反発系の既存の偽合図行（印を付けて成績から外す・価格は書き換えない）
  3. 価格レーンの採点 `scripts/price_watch_forward.py` `evaluate`（評価日の価格が分割で縮む）
  4. ニュースレーンの採点 `scripts/news_shock_eval.py`（同型・小さく直せる場合）
- **非ゴール**: 2016〜2025 のバックテスト履歴の再計算（取得が 2026-07 に一括＝実測でほぼ無傷）／α台帳・ウォッチリストの変更／`pair_forward_ledger.jsonl` の P4/P5 の結果の付け替え（別裁定）
- **母集団の定義**:
  - 反発系: 台帳の `kpi_name=sell_reg_trigger_rebound` 行 408 行（2026-10-04 時点・影の台帳の実測）。偽合図の定義= 未調整値で判定し直すと急落でない行＝ `L(D) <= C(D-1) × AdjFactor(D) × 0.90` が不成立
  - 価格レーン: `forward_log.jsonl` の evaluation 行 3 行（変わるのは 80350・2026-08-02・w8 の 1 行）
  - ニュースレーン: 評価行 1618 行（変わるのは 5706・entry 2026-09-18・w5 の 2 行）
- 終了時に照合する実測項目: 印を付けた行数（期待 24＝7/30:1・8/28:2・9/29:21／open 21・closed 3）／成績表の除外件数表示／80350 の補正値 ret +13.08・超過 +9.13／5706 の補正値 stock +5.43・超過 +5.00／一括ゲート Ran/OK
- 数え方: `jq -c 'select(.split_false_signal==true)' data/paper_trades/ledger.jsonl | wc -l`・`grep evaluation_correction data/price_watch/forward_log.jsonl`・Docker unittest

## 切替日（cutover）
- **`SPLIT_SAFE_CUTOVER = "20261005"`**（2026-10-04 の翌営業日・合図日 signal_date 基準）。この日以降の合図日は未調整判定で出す。それより前の合図日は従来どおり（バックテスト履歴を動かさない）。
- 切替日より前に残る偽合図は、台帳側の印付け（paper_eval の関数・毎回走る）で拾う。

## Metadata
| 項目 | 値 |
|---|---|
| Status | done（2026-10-04・Codex 5回目 commit 可・commit 2116648/3bc169c/5549b2b/609aa30・データ適用済み） |
| 開始日時 | 2026-10-04 |
| 担当 | Claude Opus 5.5（実装）＋Codex gpt-6-astra（実装後レビュー） |

## 成功基準
- [ ] 未調整値の判定が1か所（`kpi_round23_signals.raw_drop_hit` / `raw_recheck`）にあり、生成器と `short_forward_runner` の両方がそれを使う（runner の挙動・既存テストは不変）
- [ ] 生成器: 合図日 >= 切替日は未調整判定（未調整値が欠ける銘柄は従来の調整後判定）。切替日前は従来どおり
- [ ] `paper_eval.flag_split_false_signals` が反発系の偽合図行に `split_false_signal: true`・判定方法・印付け日を付ける（冪等・価格は不変）。`run_update` から毎回走る
- [ ] `paper_eval.compute_scoreboard` が印付き行を成績・件数から外し、除外件数を出す。`output/paper_scoreboard.md` に除外件数が出る
- [ ] `build_daily_reco.py` が印付き行を候補から外し、除外件数を1行出す（ホスト実行＝標準ライブラリのみを維持）
- [ ] `price_watch_forward.evaluate`: 評価日の価格を `jq_fetch.split_scale` で発火時の尺度へ揃える。既存の評価行は上書きせず `evaluation_correction` 行を追記（冪等）。`status` は補正行を優先
- [ ] `news_shock_eval.evaluate`: 同様（`evaluation_correction` 追記・冪等）
- [ ] 単体テスト: 分割日の偽合図が落ちる／分割日の本物の急落は残る／調整済み前日足の反例（34940 型）が残る／価格レーンの分割補正リターン／印付き行が成績表から外れる
- [ ] 一括ゲート OK・Codex 敵対レビューで「commit してよい」
- [ ] 実データで印 24 行・80350 と 5706 の補正行を確認

## 影響範囲
- `scripts/kpi_round23_signals.py`（共有ヘルパー追加・生成器に切替日分岐）
- `scripts/short_forward_runner.py`（`_raw_recheck` を共有ヘルパーへ委譲・生成器は従来判定で呼ぶ）
- `scripts/paper_eval.py`（印付け関数・成績表の除外）
- `scripts/build_daily_reco.py`（印付き行の除外と件数表示・最小）
- `scripts/price_watch_forward.py`（評価の分割補正・補正行）
- `scripts/news_shock_eval.py`（評価の分割補正・補正行）
- `tests/test_split_fix_live_lanes.py`（新規）
- データ: `data/paper_trades/ledger.jsonl`（印の追加のみ）・`data/price_watch/forward_log.jsonl` / `data/news_shock/news_log.jsonl`（補正行の追記のみ）
- 文書: `influx-stock-algo-architecture.md`（レーンの挙動の記述が変わる場合のみ・自分の差分だけ stage）

## 変更禁止ファイル
- data/kpi_trials/trials.jsonl
- config/paper_watchlist.json
- config/recipe_shelf_meta.json
- scripts/kpi_run_evidence.py

## 全体図
| 軸 | 担い手 | 止まる線 | 既知の穴 |
|---|---|---|---|
| 合図を出す（反発系） | `kpi_round23_signals.generate_sell_reg_trigger_signals`＋共有ヘルパー `raw_drop_hit` | 合図日 >= 20261005 は未調整判定 | 切替日前の合図日は従来判定のまま（台帳の印で補う）／未調整 L・C が欠ける銘柄は調整後判定に戻る |
| 台帳の偽合図を外す | `paper_eval.flag_split_false_signals`（`run_update` 内） | 印付き行は成績表・本日レコメンドから除外し件数を出す | `pair_forward_ledger` の P4/P5 結果・vault の勝敗ビュー（`kpi_clock_sla`）は印を見ない（別裁定） |
| 価格レーンの採点 | `price_watch_forward.evaluate`＋`jq_fetch.split_scale` | 評価日の価格を発火時の尺度に揃える／既存行は補正行で訂正 | 発火日の前に取り直した足の扱いは split_scale の前提（取得日記録）に依存 |
| ニュースレーンの採点 | `news_shock_eval.evaluate`＋`jq_fetch.split_scale` | 同上 | 同上 |

## Session Handoff
- 2026-10-04: タスク作成。実装（作業ツリーのみ・**未 commit**）。一括ゲート Ran 504 / OK。
  - 実データのドライラン（書き込みなし）: 台帳の印 24 行（9/29:21・8/28:2・7/30:1／open 21・closed 3）・反発系 closed 361→358・open 43→22・nostop 確定 n 332→329・EV(nostop) +2.378%→+2.301%／価格レーン訂正 1 行（80350 −77.38→+13.08・超過 −81.33→+9.13）／ニュースレーン訂正 2 行（5706 w5 −89.46%→+5.43%）
  - **データへの書き込みはまだしていない**（台帳の印・訂正行とも、コードの commit 後に適用する）
  - Codex gpt-6-astra: 1回目 NG（分母調整済みの二重補正・途中欠損・部分訂正・重複）→対応／2回目 NG（評価日後の欠損・倍率1の既存誤評価）→対応／**3回目 NG（上限3回で停止）**:
    1. news_shock: bdays が bars の実在日なので、欠けた営業日の分割を検出できず不完全な訂正行を固定しうる → 取引所カレンダーで分割走査の完全性を検査して保留する必要（ホスト stdlib 実行のため measure_base_rate のカレンダーは使えない＝jq_fetch 側に stdlib のカレンダー読みが要る）
    2. price_watch: 走査上限が「評価日と最新の実在ファイル日」なので、最新ファイルより後の分割を反映した取り直し価格（評価日足を後日取り直した場合）を保留できない → 走査範囲を「入口・評価価格の足の取得日」まで確保し、そこまでのファイルが揃うまで保留する必要
  - 次: 上の2点を直して Codex 4回目（オーナー判断）。反発系の生成器・runner 共有化・台帳の印・成績表/レコメンドの除外は Codex 指摘なし（3回とも）
- 2026-10-04（続き・オーナー裁定「残り2点も直して4回目へ」）: 3回目の2点を実装（jq_fetch `offline_business_days()`／`split_scan_upper()`・news は取引所カレンダーで欠け検査・price は上限を評価日の足の取得日まで延長）。テスト 3 本追加＋既存調整。一括ゲート **Ran 510 / OK**・news selftest OK・ドライランは 3 回目と同じ（価格 1 行・ニュース 2 行）。
  - **Codex 4回目 NG（P2×2・指示どおり commit せず停止）**: どちらも「カレンダーの収録範囲が短い時」の穴（現行カレンダー 2016-07-05〜2027-12-30 では起きない）
    1. price_watch `_split_scan` 冒頭: 入口日・評価日がカレンダー外だと `return []`（分割なし扱い）→ −45% を `split_adjust` 付きで確定し訂正対象からも外れる → `None`（保留）にする
    2. news_shock `_split_scan`: カレンダーの末尾しか見ず開始側を検査しない → 入口日がカレンダー先頭より前なら保留にする
  - 次: 上の2点（各1〜2行）を直してテスト追加→ゲート→Codex 5回目か、ここで止めるかはオーナー判断。データ（印 24・訂正 3）は未適用のまま
- 2026-10-04（オーナー裁定 A「4回目の P2 2点を直して5回目へ」）: price `_split_scan` は入口日・評価日がカレンダー外なら None（保留）／news はカレンダー開始側も検査（入口日がカレンダー外・先頭より後なら保留）。テスト 2 本追加（Codex 4回目の再現を含む）。一括ゲート **Ran 512 / OK**・news selftest OK。実データの発火 111 件・news 評価行の入口日はすべてカレンダー内（実測＝永久保留は起きない）。
  - **Codex 5回目: VERDICT commit してよい**（指摘なし）→ 意味単位で 4 commit（jq_fetch 部品 2116648／反発系の切替 3bc169c／台帳の印と除外 5549b2b／価格・ニュースの補正 609aa30）。
  - データ適用: 価格レーン訂正 渡した 1／書かれた 1（forward_log 118→119 行）・ニュース訂正 渡した 2／書かれた 2（news_log 2872→2874 行）。再実行は両方 0 件（冪等）。両ファイルは他の定期ジョブの未 commit 分と同居のため commit しない。
  - 台帳の印 24 行は **10/4 09:13–09:16 の daily_screen 手動実走**（run_log run_id d8e8d1f1…・テーマ初動 v0 の実走＝tasks/theme_cluster_v0.md）が作業ツリーの未 commit コード経由（daily_screen → paper_eval.run_update → flag_split_false_signals）で既に書いていた。HEAD 版台帳での試走 24 行とキー（kpi・code・signal_date）が完全一致・再実行は新規 0／変化 0 行（冪等）＝再適用不要。ledger.jsonl は同じ便の 11 行追加と同居のため commit しない。
