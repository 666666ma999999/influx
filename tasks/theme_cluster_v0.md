# Task: テーマ初動 v0（受益銘柄のクラスタ初動を記録だけの前向き試行として登録・X 裏取り欄を同梱）

Phase: 株アルゴ研究 ペーパートレード第 N 陣（`influx-stock-algo-architecture.md` daily_screen 行）／P-INF-28（オーナー裁定 A 2026-10-04・叩き台= vault `02_Ai/influx/influx-x-lane-rebuild-draft-2026-10-03.md`）

## 答える問い
- 「推奨できる銘柄出てこないと意味ないでしょ」＝ 株側で「テーマがまとまって動いた日」を当日夜に検知し、翌朝に受益銘柄を X の裏取りつきで出せば、20 営業日で TOPIX に勝つ候補になるか（過去 2026-01〜10-01 の疑似前向き= +20 日 +2.82pt・勝率 48／83）

## 目的
`configs/x_shortage_map.json` の subject ごとに、受益銘柄（tier≠rejected）のうち同日に対 TOPIX 超過 +3pt 以上が **3 社以上かつ 3 割以上** 出た日を「テーマ初動日」とし、翌営業日に受益銘柄全部を既存のペーパートレード台帳に記録だけする（判断に使わない）。株レコメンド表の「🟡 観察のみ」行に X の裏取り（当日件数／前 14 日平均・初出語・代表投稿 1 本）を添える。

## 対象と数え方
- 母集団: `configs/x_shortage_map.json` の subject のうち受益銘柄（tier≠rejected・code あり）が 3 社以上の物。銘柄は TOP1000 台帳の code（5 桁化= code+"0"）。
- 除外: 分割日（AdjFactor≠1）の銘柄はその日の判定から外す／同一 subject の再発火は直近発火から 5 営業日以内なら出さない／bars・TOPIX が欠ける日は判定しない（黙って 0 にしない・diag に件数）。
- 数え方: 事象= subject×初動日。採点は成熟 60 日かつ事象 30 件で 1 回（short_forward と同じ出口）。確認= `jq -c 'select(.kpi_name=="theme_cluster_v0")' data/paper_trades/ledger.jsonl | wc -l`。

## 事前に固定する判定（記録開始前に凍結・v0）
- 閾値: 日次対 TOPIX 超過 ≥ +3.0pt（AdjC/前日 AdjC − TOPIX C/前日 C）・3 社以上・受益社数の 30% 以上・再発火抑止 5 営業日。
- エントリー: 既存枠組みどおり（defer_entry=true・翌営業日始値）。exit は既存の exit_variants に従う。v0 の主採点は台帳から「+20 営業日の対 TOPIX 超過（終値）」を別途計算（叩き台 §0 と同じ定義）。
- FROZEN_START: 実装翌営業日（2026-10-05 以前の signal_date は台帳に入れない）。

## 成功基準
- [x] `scripts/kpi_theme_cluster_signals.py` の `generate_theme_cluster_signals(start_bd, end_bd, all_bdays, bday_index) -> (df, diag)` が columns `signal_date, code, subject, n_hits, n_ben` を返し、`_tmp_ab_backtest2.py` と同じ定義で 2026-09-18 dram（8/12）・2026-10-01 dram（11/12）を再現する
- [x] `scripts/daily_screen.py` に `THEME_CLUSTER_KPI_NAME = "theme_cluster_v0"` と `generate_kpi_signals` の分岐が足され、`config/paper_watchlist.json` に observation エントリ（changelog 追記つき）が入る。既存 19 系統の判定・`CHAMPION_KPI_NAME` は不変
- [x] `scripts/build_daily_reco.py` の「🟡 観察のみ」表に **theme_cluster_v0 の行だけ** `X 裏取り` 列が入る（他系統は空欄）。中身= subject label／対応クエリの signal_date（JST）件数と前 14 日平均（texts の posted_at を数え直す・ledger count は使わない）／`discovery_queue.jsonl` の first_seen 語で `subjects_for_token` に当たる物／代表投稿 1 本 40 字
- [x] 裏取りの計算は `scripts/x_shortage_map.py` の新関数 `x_backing(m, subject_id, day_jst, texts_dir, queue_path) -> dict` に 1 箇所化（build_daily_reco から import・再実装なし）
- [x] 新規テスト（生成器の合成 bars での発火／3 社未満・3 割未満・分割日・再発火 5 日で非発火／x_backing の件数と初出語）を含め Docker の unittest 全件 OK
- [x] daily_screen を手動 1 周実走（`python3 scripts/daily_screen.py` 相当・launchd と同じ経路）し、台帳に theme_cluster_v0 の行が **FROZEN_START 前は 0 行**・`output/daily_reco.md` が生成されることをログで確認
- [x] Codex gpt-6-astra の実装後レビューで指摘ゼロまたは全件反映

## 影響範囲
- 新規: `scripts/kpi_theme_cluster_signals.py`・`tests/test_theme_cluster.py`
- 変更: `scripts/daily_screen.py`（定数＋分岐＋import）・`config/paper_watchlist.json`（1 エントリ＋changelog）・`scripts/build_daily_reco.py`（列追加）・`scripts/x_shortage_map.py`（x_backing）・`influx-stock-algo-architecture.md`（daily_screen 行に 1 句）

## 変更禁止ファイル
- `configs/x_price_watch.json`・`scripts/price_watch_alert.py`・`scripts/price_watch_collect.py`（X 本番の判定・収集は触らない）
- `scripts/kpi_volshock_signals.py` ほか既存生成器・`scripts/short_forward_runner.py`・`config/launchd/*.plist`

## 全体図
- 検知軸: 担い手= `kpi_theme_cluster_signals.py`（bars＋TOPIX＋shortage_map）／止まる線= bars 欠落日・TOPIX 欠落日は判定せず diag に記録／既知の穴= 受益台帳は 2026-07-28 以降に人が選んだ＝後知恵の混入（前向きで解消）
- 記録軸: 担い手= `daily_screen.py` → `ledger.jsonl`（既存の冪等 append）／止まる線= FROZEN_START 前は 0 行／既知の穴= 同一銘柄が複数 subject に属す（5713 等）＝同日重複は (kpi,code,signal_date) キーで 1 行
- 表示軸: 担い手= `build_daily_reco.py` ← `x_shortage_map.x_backing`／止まる線= texts が無い日は「X 記録なし」と書く（空欄にしない）／既知の穴= texts は 08-02 以降のみ

## タスク
1. 生成器 `scripts/kpi_theme_cluster_signals.py`（検証= Docker で `python -c` 2026-09-18／10-01 の dram 再現）
2. `daily_screen.py` 定数・分岐・watchlist エントリ（検証= `python3 -c "import json;json.load(open('config/paper_watchlist.json'))"`＋unittest）
3. `x_shortage_map.x_backing` と `build_daily_reco.py` の列（検証= unittest＋`daily_reco.md` 生成）
4. テスト・実走・Codex レビュー・architecture 1 句・commit

## Session Handoff
- 2026-10-04 着手（叩き台 v0 検収= オーナー「a」= 案 A）。
- 2026-10-04 00:50 完了（実装= SubAgent general-purpose・親が差分実読と unittest を再実行）。成功基準 7/7。unittest 507/507 OK（新規 12 件）。実走= launchd と同じ経路で 1 周・theme_cluster_v0 の台帳行 0（FROZEN_START 20261005 前）・他系統の新規 11 件は日次ジョブと同じ。Codex gpt-6-astra 3 周（高 1・中 3 → 全反映・3 周目指摘なし）。
- 仕様から外れた点（既定で採る・各 1 行で戻せる）: ①TOP500 ユニバース絞りを本系統だけ免除（`daily_screen.UNIVERSE_EXEMPT_KPI_NAMES`・受益銘柄全部を記録するため・Codex 高）②再発火抑止の連鎖起点を `REFIRE_CHAIN_ANCHOR_BD=20260105` に固定（start_bd の 5 営業日前からだと 10-01 単日走査で dram 等が 0 件になる実測）③`recipe_shelf_meta.json` には載せない（α管理の棚は maturity 行必須で build_recipe_shelf が落ちた）＝`build_daily_reco.WATCH_ONLY_KPIS` で観察のみ表に出す。
- 残= 2026-10-05（月）07:30 便で初回の本番実走を確認／採点は成熟 60 日かつ事象 30 件で 1 回。
