# Task: X 裏取りレーン（初出レーンの事象に①裏取りの列を足す・P-INF-26 実装便）

Phase: 株アルゴ研究 / X 収集基盤の予兆レーン（plan.md 既存範囲内の Slice・plan.md 変更なし）

## 答える問い
- これらをファクトチェックするのはどうしたら良いでしょう？（オーナー原文・2026-10-03）

## 対象と数え方
- 範囲: 初出レーン（P-INF-25・実装済 `6f2e3e5`）の「事象」＝`discovery_queue.jsonl` の候補で first_seen=true かつ first_posted_at あり かつ `subjects_for_token` が受益銘柄（sign +）を返す語。非ゴール= 生活系（受益表に主題がない語）の裏取り／因果判定／有料壁の突破／②の採点定義の変更（追記9c のまま）。
- **母集団の定義**: 候補キュー全便の candidate_batch の candidates（除外= first_seen=false・first_posted_at 空・受益銘柄が引けない語・台帳に既にある語）
- 終了時に照合する実測項目: 〈候補キューの事象数／台帳に下書きを書いた数〉・unittest の PASS 数・Codex レビューの指摘数
- 数え方: `python3 scripts/x_factcheck_draft.py list`（事象数と台帳の状態別件数を印字）／`docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_x_factcheck_draft`

## Metadata

| 項目 | 値 |
|------|-----|
| Status | active |
| 開始日時 | 2026-10-04 00:05 |
| 最終更新 | 2026-10-04 09:20 |
| 担当 | Claude（セッション a9ffac70） |

## Goal

初出レーンの事象ごとに〈投稿の取り直し・同一記事の束ね・元報道の公開時刻・遅れラベル〉を機械が下書きし、人が週 1 回〈一次ソース到達・範囲判定・出所ラベル〉を埋める台帳を作る。

## 成功基準

- [x] `scripts/x_factcheck_draft.py draft` が事象ごとに台帳 `data/x_price_watch/factcheck_ledger.jsonl` へ status=draft の 1 行を追記し、同じ語を二重に書かない（list で確認）
- [x] 下書き行に `refetched`（取り直した投稿・最古順・status_id/author/likes/posted_at_full/text/urls）・`bundles`（同一記事の束ね）・`source_published_at`（URL から取れた公開時刻・取れなければ空）・`delay_label`（速報 <6h／当日 ≤24h／週内 ≤7d／再流通 >7d／未確認）が入る
- [x] `fill --token 語 --primary ○|△|× --label 一次|二次|未確認 --scope 一致|範囲拡大|未確認 [--note]` で人の判定が同じ行に入り status=filled になる
- [x] unittest（束ね・遅れラベル・事象選別・二重書き防止・fill）が Docker で PASS
- [x] docs/price-watch-universe.md §16o-3 追記9d と機能マップ 2 枚に 1 行ずつ・vault 鏡同期
- [x] Codex gpt-6-astra レビュー（read-only）: 指摘 4 件（別件記事で遅れ判定／失敗行が再取得を止める／日付だけの時刻を補う／URL パスの小文字化）→ 全部修正・回帰 13 件 PASS

## Current Agreed Scope

### Must
- [x] scripts/x_factcheck_draft.py（draft / list / fill）
- [x] tests/test_x_factcheck_draft.py
- [x] docs 追記9d・機能マップ

### Descoped
- 生活系の裏取り（Q2 a で対象外）
- ②採点の変更（追記9c の `_tmp_x_leadlag_now.py` 流用のまま）
- 有料記事本文の取得

## Session Handoff
- 2026-10-04 00:05 着手。起案= vault `02_Ai/influx/notes/influx-x-factcheck-4model-review-2026-10-03.md` §実装便の起案（オーナー a）。
- 2026-10-04 09:20 実装・Docker 回帰 13 件 PASS・初回実走（事象 1 語= マイクロン・取り直し 13 件・covers_first_post=false・元報道 URL 無し→遅れ 未確認）・追記9d・機能マップ 2 枚＋鏡同期。残= commit／人の手番= `fill --token マイクロン …`（週 1 回）。
- 照合〈候補キューの事象数／台帳に下書きした数〉= 1／1。

- 2026-10-04 09:45 commit 0248346・3cc9b8d で実装完了。残= オーナー判断「マイクロン 1 行をいま fill するか（a）／事象が増えるまで待つか（b）」のみ。
- 2026-10-04 10:20 fill に --source-time/--source-url を追加（URL 無し事象の遅れラベルを人が付け直す経路）・回帰 14 件 PASS。daily_reco の「X 裏取り列」（349f639）は役割が別（株の初動日に X の投稿件数を添える）＝本台帳（主張の真偽）と統合不要と判定。
- 2026-10-04 10:30 オーナー「推奨」→ マイクロン fill 済（一次 △・二次・一致・遅れ 週内）。台帳 filled 1／事象 1。
