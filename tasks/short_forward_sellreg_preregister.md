Phase: 株アルゴ研究（influx-stock-algo）／前向き記録だけの試行
親: vault 02_Ai/influx/influx-winrate-4model-20261003.md（P-INF-23）

## 答える問い
「では、現状の設計モデルからどう変えれば勝率が上がるかを、4モデルで議論して 不明点は、私に聞いて」→ 裁定3（2026-10-03）「空売り版を記録だけの試行として登録」

## 目的
反発系の合図（sell_reg_trigger_rebound＝安値が前日終値×0.90 以下）を「空売りの合図」として、登録日以降の前向き成績だけで合否を決められる記録を毎日自動で残す。実際のお金は入れない。

## 対象と数え方
- 母集団: 各営業日 D に `kpi_round23_signals.generate_sell_reg_trigger_signals` が出した発火のうち、本番と同じ上位500社（`daily_screen.UniverseCache`＝前月末の売買代金21日合計 上位500）に入る銘柄。
- 日の関門: その日 D の上位500社内の発火数が **1〜9件の日だけ**記録する（10件以上の日は `skipped_day_count` として件数だけ残す）。閾値は 2016-11〜2022-11 の遡り検証だけで決めて固定（10件以上の日 −0.03%／2,028件・95日）。
- 入口: D+2 営業日の寄り（AdjO）で空売り（D と D+1 は空売り価格規制の期間＝証券会社の解説で確認・二次）。D+2 に寄り値が無ければ `unfilled`（先送りしない）。
- 出口: 入口から20営業日後（D+22）の引け（AdjC）で買い戻す。上場廃止等で欠けたら `censored`（最後の価格とその価格日を保存・主集計外＝下の「記録の状態」）。
- 損益（2本とも記録）:
  - 空売り単体 `ret_short = -(exit/entry - 1) - 0.003 - 0.0115 × 暦日数/365`
  - TOPIX 組 `ret_pair = ret_short + (TOPIX引け[出口]/TOPIX寄り[入口] - 1)`
- 分割: `jq_fetch.split_scale` で入口日の尺度に揃える。
- 主指標: 1日1票（同じ D の発火を平均してから日で平均）の `ret_pair` の勝ち日率と平均。副: 1発火1票。
- 判定枠: **既存の試行一覧（data/kpi_trials/trials.jsonl）には足さない**（行数がほかの試行の Bonferroni 分母になるため）＝台帳外・α非消費・既存と別枠（オーナー既定値宣言 2026-10-03）。
- スコアの意味（Codex R5）: v1 は「片脚の想定元本あたりの価格差スコア」。配当の受払い・TOPIX側の費用・逆日歩・貸株の在庫は入れない＝**実売買への採用判定にはそのまま使わない**（使う時は v2 で両脚の費用・配当・資金を定義）。

## 事前に固定する判定（Codex R1・R4 反映・記録開始前に凍結）
- 開始日 FROZEN_START = 記録を始める最初の合図日 D（実装 commit 日の翌営業日以降）。それ以前の日は記録しない。
- 判定時点: 成熟した日コホートが **60日以上** かつ開始から **12か月以上** の最初の月末。遅くとも **2027-12-30** で打ち切り判定。途中は数字を表示するだけで、合否・停止は出さない。
- 合格の線（全部満たす）: ①1日1票の `ret_pair` 平均 > 0 で、月ブロック・ブートストラップ（2000回・seed 42）の片側95%下限 > 0 ②勝ち日率 ≥ 55% ③1発火1票の `ret_pair` 平均 > 0。
- 日コホートの分母: その日 D の約定済み（filled）発火が**すべて成熟**した日だけを数える。unfilled は分母から外し件数を併記。打ち切り（censored）を含む日は主集計から外し、別集計で件数と値を出す。
- 同一銘柄の再発火: 遡り検証どおり保有中の再発火も記録する（`overlap_open=true` の印を付ける）。推測統計は月ブロックで重なりを扱う。

## 記録の状態と来歴（Codex R2・R3・R6・R7 反映）
- 営業日の正本: `measure_base_rate.all_business_days`（取引所カレンダー）。bars ファイルの有無で営業日を作らない。D+2／D+22 はこの暦で数え、該当日の bars が未取得なら `pending_data` で待つ。
- 貸株料の日数: 入口日〜出口日の**実暦日数**（遡りの固定28日とは違う＝テストで両方の値を固定）。
- 日ごとの確定: 各営業日 D について `day` 事象を1行（上位500社内の発火数・銘柄一覧・名簿月・bars の取得時刻・コードの sha・初検出時刻）。発火0件の日も残す。D の bars が欠けている（銘柄数が通常の 90% 未満）なら `incomplete` として記録せず再試行。
- 遅れて見つけた合図: 初検出が入口日 D+2 の寄り以降なら `late_detected` として主集計から外す。
- 状態: `pending_data`（データ待ち）／`unfilled`（D+2 の bars はあるが寄り値なし＝本当に寄らなかった）／`filled`／`exit_pending`／`matured`／`censored`（D+22 前に上場廃止等。最後の価格と**その価格日**を保存・主集計外）。
- 再実行: ランナーは毎回「FROZEN_START から今日まで」の未処理日を全部走査する冪等処理（処理済みは事象の有無で判定）。daily_screen の末尾フックに加え、**本線が「新規営業日なし」で早く終わる経路でも呼ぶ**。

## 成功基準
1. `scripts/short_forward_runner.py` を手で実行すると、`data/paper_trades/short_forward_ledger.jsonl` に signal → filled/unfilled → matured の事象が追記される（同じ日を2回走らせても行が増えない）。
2. 損益式のテストが独立に手計算した期待値で通る（28日以外の暦日・分割・出口欠損＝censored・寄り値なし＝unfilled・データ待ち＝pending_data・関門9/10件・二重実行で行が増えない）。
3. `daily_screen.py` の最後に独立サブプロセスとして呼ばれ、失敗しても本線の台帳・exit code に影響しない。
4. 一括ゲート `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest discover -s tests` が全件通る（新テスト含む）。
5. 本線の `data/paper_trades/ledger.jsonl`・`config/paper_watchlist.json`・`output/paper_scoreboard.md`・daily_reco に空売りの行が一切出ない。

## 影響範囲
- 新規: `scripts/short_forward_runner.py`／`scripts/short_forward_v1_spec.py`（凍結定数・損益式）／`tests/test_short_forward.py`／`data/paper_trades/short_forward_ledger.jsonl`（実行で生成）
- 変更: `scripts/daily_screen.py`（main 末尾の pair_forward フックの隣と、「新規営業日なし」の早期 return の直前の2か所に、同じ非致死サブプロセス呼び出しを足す）
- 文書: `influx-stock-algo-architecture.md`（機能マップに1行）・`docs/pipeline-map.md`（daily_screen の後段に1行）

## 変更禁止ファイル
- scripts/paper_eval.py
- config/paper_watchlist.json
- config/recipe_shelf_meta.json
- scripts/kpi_run_evidence.py
- data/kpi_trials/trials.jsonl
- data/paper_trades/ledger.jsonl
- scripts/build_daily_reco.py
- scripts/build_recipe_shelf.py

## 全体図
| 軸 | 担い手 | 止まる線 | 既知の穴 |
|---|---|---|---|
| 合図 | `kpi_round23_signals.generate_sell_reg_trigger_signals`（既存・再利用） | 生成器の失敗はランナーだけ WARN 終了 | 本番の約9件/日と遡りの4.5〜5.6件/日の差の残りは未解明 |
| 母集団 | `daily_screen.UniverseCache`（既存・再利用） | 名簿が作れない月は記録しない（理由を事象で残す） | — |
| 約定 | 本ランナー（D+2 寄り・D+22 引け） | 寄り値欠けは unfilled | 逆日歩・貸株の在庫切れは計算外 |
| 損益 | `short_forward_v1_spec`（凍結） | 式の変更は v2 として別登録 | 1件 −391% の尾＝損失上限なし |
| 記録 | `short_forward_ledger.jsonl`（追記のみ・ロック＋fsync） | 二重起動はロックで弾く | — |

## タスク
- [ ] T1 凍結モジュール `scripts/short_forward_v1_spec.py`（定数・`ret_short`・`ret_pair`・日の関門）＋テスト
- [ ] T2 ランナー `scripts/short_forward_runner.py`（tob_forward_runner を手本・pair_forward_scan のロック/fsync/FROZEN_START）＋テスト
- [ ] T3 `daily_screen.py` 末尾にサブプロセス呼び出し
- [ ] T4 機能マップ・配管図に1行
- [ ] T5 一括ゲート・手動実行・Codex 敵対レビュー → commit

## Session Handoff
- 2026-10-03: 計画作成 → Codex gpt-6-astra 敵対レビュー7件を全件反映（R1 判定の事前固定／R2 日次確定と来歴／R3 欠測状態／R4 分母／R5 スコアの意味／R6 暦の正本／R7 再実行経路）。実装は SubAgent に委譲。
- 2026-10-03（実装 SubAgent・Codex 3回目反映）: (a) 日コホートは全終端事象（matured・unfilled・censored）の recorded_at が判定日 23:59:59 JST 以内の時だけ判定へ（unfilled にも recorded_at）。(b) 銘柄の行が無いだけでは unfilled(no_bar)/censored を確定しない＝初回は pending_data(absence_unconfirmed・first_absent_fetched_at) を残し、同じ日付ファイルを厳密に後の fetched_at で取り直してもなお無い時に2回の走査で確定（no_open・行あり AdjC 欠けは即時）。(c) judgment_due は judge_as_of と同じ cohort_available 規則で60日を数える。Docker 一括ゲート Ran 442 / OK。未コミット。注意: bars を取り直さない運用だと no_bar・censored は pending のまま残る。
- 2026-10-03（Codex 4回目）: 3回目の3件は解消。新規1件=行はあるが寄り値/引け値が空の時、取得時刻を見ず即 unfilled/censored に確定（runner:422,484・test:222）。3-Fix Limit 到達でオーナー確認待ち。テスト Ran 442 / OK。未コミット。
- 2026-10-03（Codex 4回目反映）: 行はあるが価格が欠けている（入口 AdjO 欠け/<=0・出口 AdjC 欠け）時は、その日の bars 取得時刻（JST）がその日の翌営業日以降の時だけ unfilled(no_open)/censored に確定し null_confirm_fetched_at を残す。取得時刻が不明・それより早い時は pending_data(price_null_unconfirmed・stage ごとに重複排除) で待ち、後の回で価格が入れば filled/matured へ進む。未コミット。
