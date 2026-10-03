# Task: X 発見器の「初出レーン」（候補語の初出時刻を保存し受益銘柄と株の初動を突合できる形にする）

Phase: 商品価格レーン（`influx-stock-algo-architecture.md` 発見器行）／P-INF-25（オーナー裁定 a 2026-10-03）

## 答える問い
- 「SNS の方が株より情報早いですよ」＝ X の投稿の**真の初出**は受益株の初動より先か（追記 9 の「注視日の最も早い投稿」では測れなかった分）

## 対象と数え方
- 範囲: 発見器 `scripts/price_watch_discover.py` の候補語に〈初出 posted_at・今回が初出か〉を保存する／候補語→受益銘柄の対応を `configs/x_shortage_map.json` の subject に `discover_tokens` で付けられるようにする／採点の事前登録を docs に書く。**非ゴール**: 採点の実行（事象が 30 件溜まるまで走らせない）・固定 55 式の変更・plist の時刻変更（週 1 回 `--days 7` の窓修理は 2026-10-03 便で済み）
- **母集団の定義**: 発見器が毎便出す候補語（CANDIDATE_CAP 20／日）。除外= 既知語彙・STOPLIST・投稿者 1 人の語（既存の抽出条件のまま）
- 終了時に照合する実測項目: 保存行の候補 1 件ごとに `first_posted_at`（ISO）と `first_seen`（bool）が入る／過去 9 便の候補語が `first_seen=false` の判定に使われる（件数= 85 語）／unittest 全件 OK
- 数え方: `jq '[.candidates[]|has("first_posted_at")]|all' data/x_price_watch/discovery_queue.jsonl`（新行）・`python -m unittest discover -s tests`

## Metadata
| 項目 | 値 |
|---|---|
| Status | done（採点は事象 30 件到達後に別途・追記9c） |
| 開始日時 | 2026-10-03 18:20 |
| 最終更新 | 2026-10-03 18:55 |
| 担当 | Claude Fable 5.1（統括）＋Codex gpt-6-astra（実装後レビュー） |

## Goal
候補語ごとの初出時刻がデータとして残り、人が銘柄を付けた語について「初出 → 株の初動」を追記 9 と同じ定義で採点できる状態にする。

## 成功基準
- [x] `x_search_collect_twittora.collect_query` の行に `posted_at_full`（`<time datetime>` の全文）が入る（既存 `posted_at` は日付 10 文字のまま＝互換）
- [x] `price_watch_discover.extract_candidates` が候補ごとに `first_posted_at`（その語を含む投稿の最小 posted_at_full）を出す
- [x] `run_day` が候補ごとに `first_seen`（過去の candidate_batch に同じ token が無い）を付ける
- [x] `x_shortage_map.subjects_for_token(m, token)` が subject の `discover_tokens` を引く（validate は `discover_tokens` を許容）
- [x] 新規テスト 3 本を含め Docker の unittest 全件 OK
- [x] 実データで 1 日分を実走し、保存行の候補に `first_posted_at`・`first_seen` が入る
- [x] 採点の事前登録を `docs/price-watch-universe.md` 追記 9c に記載（n≥30 事象で 1 回だけ採点）

## Current Agreed Scope
### Must
- [x] 上の成功基準 7 点
### Descoped
- 採点スクリプトの本実装（事象 0 件の今は検証できない＝追記 9 の `_tmp_x_leadlag_now.py` を候補語版に流用する設計だけ記す）
- plist の日次化（窓修理は `--days 7` で済み・初出 posted_at は取得の遅れに影響されない）

## Session Handoff
- 2026-10-03 18:20 着手。可否の実測（追記 9b）は済・設計は vault 報告 §9。
- 2026-10-03 18:55 完了。commit 6f2e3e5（実装＋テスト）・7e69dad（docs・機能マップ）。実走 `--date 2026-10-02 --force`: 投稿 147 件→候補 20／20 件に first_posted_at・first_seen あり・うち first_seen 17 件（初回便は既出語の母数が 85 語しか無いため初出が多く出る＝参考扱い・追記9c 偏り③）。Codex gpt-6-astra 指摘なし・unittest 452／452 OK。残= 週 1 回の人手の銘柄付け（discover_tokens）と事象 30 件での採点。
