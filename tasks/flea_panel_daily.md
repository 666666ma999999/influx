# Task: Yahoo!フリマ 固定パネルの日次追跡（P-INF-19）

## 答える問い
- 「ebayやフリマ等で需要と供給を調べたいです。例えばAという商品の ①値段が上がった ②売れない商品が増えた ③売買成立がすごく増えた」（オーナー原文 2026-09-22）— 指数の無い消費財について、この 3 つを毎日測れる状態にする。

## 対象と数え方
- 範囲: Yahoo!フリマ 1 板・`configs/flea_panel.json` のクエリ（消費財 6 本・2026-09-22 時点）。非ゴール= 銘柄付与（消費財 5 群は受益 0・7/28 分類）／メルカリ・ヤフオク・eBay（eBay は 9/22 も 403）／指数のある商品（price_universe が担う）。
- **母集団の定義**: クエリごとに〈出品中の先頭 100 件〉＋〈売却済（`?sold=1`）の先頭 100 件〉。除外= itemStatus が OPEN/SOLD 以外の行。ページ送りはしない（1 面 100 件が上限＝1 日の成約が 100 件を超える商品では sold_new が頭打ちになる・既知）。
- 終了時に照合する実測項目: `--- flea_panel <date>: queries_planned=N processed=M` の N/M が一致すること（不一致= 未完・runner が通知）。
- 数え方: `tail -1 ~/.claude/state/flea-panel.out.log`／`jq -c 'select(.date=="<date>")' data/flea_panel/daily.jsonl`。

## Metadata
| 項目 | 値 |
|---|---|
| Status | active（初回実走済み・launchd 登録待ち） |
| 開始日時 | 2026-09-22 21:50 |
| 最終更新 | 2026-09-22 22:40 |
| 担当 | Claude（統括 fable-5-1） |
| 裁定 | vault decisions 2026-09-22 P-INF-19 a／報告 [[influx-flea-demand-supply-review-2026-09-22]] |

## 成功基準
- 日次で `daily.jsonl` に 1 クエリ 1 行（status=ok）が追記され、7 日後に `stale_n`（売れ残り）と `sold_new`（成約）が 0 以外の実値で埋まる。
- 取得の失敗（遮断・構造変化・両面 0 件）が「該当なし」に化けず error/exit 2 で止まり Mac 通知が出る（テスト 2 件で固定）。

## Progress
- [x] 2026-09-22 `scripts/flea_panel_track.py`・`configs/flea_panel.json`・`tests/test_flea_panel_track.py`（7 件 OK・一括 364 件 OK）
- [x] 2026-09-22 初回実走（Docker）: 6/6 クエリ・出品 ID 1,186 件・snapshot 1,186 行。同日再実行で置換を確認（daily 6 行のまま）
- [x] 2026-09-22 `scripts/flea_panel_run.sh`・`config/launchd/com.influx.flea-panel.plist`（bash -n / plutil OK）・故障注入（遮断ページ→exit 2）
- [x] 2026-09-22 Codex（gpt-6-astra）実装後レビュー 7 件を全件修正: 件数を state の日付から導出（同日再実行で 0 に化けない）／OPEN・SOLD に絞った後の 0 件ガード／再出品（SOLD→OPEN）で sold_seen 解除＋relisted／stale_n は今回の OPEN 集合だけ／一時ファイル→置換の原子的書き込み／runner は件数が数値でなければ exit 3／読み方の限界（先頭100件の頭打ち= sold_new_capped）を docstring に。回帰テスト 4 件追加（計 10 件・一括 367 件 OK）
- [x] 2026-09-22 配管図・機能マップ・gitignore（snapshots 非追跡）・vault decisions／提案履歴／pokeca INBOX
- [ ] **launchd 登録（オーナー手番・AI の launchctl は権限拒否）**: `cp config/launchd/com.influx.flea-panel.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.influx.flea-panel.plist`
- [ ] 2026-09-29 以降: 7 日分たまったら stale_n／sold_new の実値を見て閾値（何倍で「急増」と言うか）を決める

## Stuck / 既知の穴
- 商品名同定は検索語まかせ（例: 「RTX 5090」の売却済中央値 684,000 円＝PC 本体の混入）。同定ルールは 7 日分の実値を見てから足す。
- 初日は全出品が new_open／全売却が sold_new になる（基準日）。比較は 2 日目以降。
- 指数側の週次チェッカー（price_universe）は 9/21 に TE 不達で FATAL・未修理（別件・レビュー A1）。

## Session Handoff
- 次にやること: オーナーが launchd を登録 → 翌日 21:40 の初回自動実行を `~/.claude/state/flea-panel.out.log` で確認。
