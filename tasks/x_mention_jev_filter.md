# Task: 銘柄言及の数えの手前に判定器 Jev を入れ、明らかに関係ない投稿だけ外す（P-ENV-242 案 1 の③）

<!-- Phase: 株アルゴ研究（influx-stock-algo）／X 銘柄言及レーン（docs/price-watch-universe.md §16c）。phase-tracker の既存 Phase には紐付けない -->

## 答える問い
- 「めんどくさいのをjevで解決したいのですが、チェックせずに判定する方法がありますか？」→ 案 1 の③に「推奨で」（オーナー原文 2026-10-29＝明らかに関係ない投稿だけ外す・オーナーの確認は 0 件で使う）

## 対象と数え方
- 母集団= 毎晩の銘柄言及の数え（`x_mention_extract.extract_day`）で「数えられる」投稿（辞書に当たり・4 銘柄以上の羅列でなく・同じ投稿／同じ文面の重複を除いた後）。判定するのは 1 投稿 1 回
- 数え方（1 日分）: 〈判定にかけた投稿／点が返った／線 0.15 未満で外した／判定できず数えに残した（理由別: 伏せきれず・鍵なし・429・時間切れ・予算切れ）〉。渡した数＝返った＋残した の突合が合わなければ未完
- 急増の判定は、外した後の件数で **対象日と直前 14 日の両方** を数え直してから当てる（対象日だけ外すと基準が高いまま＝鳴りにくくなる偏り）

## 決め（変えない・オーナー裁定と統括の注文）
- 問い= 試しで合格した v2 の英語 2 問のまま（`~/.claude/state/wip/p-env-242/influx/trial_influx.py` の `QS_V2`）・点= 2 問の高い方・線 0.15（未満なら外す）
- 送るのは伏せた本文だけ（URL・@名前・メール・電話。伏せきれない物は送らずに数えに残す）
- 判定器が失敗・鍵なし・時間切れ・429 → 外さない（今までどおり数える）。理由は記録に残す
- 外した投稿は消さない。日ごとに `data/x_price_watch/jev_dropped/<日>.jsonl` へ〈id・点・理由〉
- スイッチ= `configs/x_mention_jev.json` の `enabled`（false で今と全く同じ動き）
- 判定済みの点は status_id ごとに貯める（`data/x_price_watch/jev_scores.jsonl`）。初回の貯め= 過去 3,503 件（`work/jev_pool.jsonl`）
- 通知の文面・送り先・launchd の設定は変えない

## 成功基準
1. スイッチ切: 最近の 1 日分で、言及の台帳（mentions.jsonl）と急増の判定（mention_alerts.jsonl の追記行）が変える前と全く同じ（run_at を除く）
2. スイッチ入: 最近の 1 日分（通知を送らない動かし方）で〈判定にかけた／外した／失敗で外さなかった〉と急増の判定の変化を出し、過去の当て直し（線 0.15・7/28〜10/26 で警報 138→70）と同じ向きか見る
3. 鍵なし: 1 回流して止まらず、外さずに今までどおり数える
4. `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest discover -s tests` が全部通る

## 影響範囲
- 新規: `scripts/x_mention_jev.py`（伏せ・点の貯め・Jev 呼び出し・外した記録）／`configs/x_mention_jev.json`（スイッチと線）／`tests/test_x_mention_jev.py`
- 改修: `scripts/x_mention_extract.py`（`extract_day` に外す口・`main` に判定器つきの数え直しと `--dry-run`）／`docs/price-watch-universe.md` §16c に追記
- データ（新規）: `data/x_price_watch/jev_scores.jsonl`・`jev_dropped/<日>.jsonl`・`mentions_jev.jsonl`・`jev_filter_log.jsonl`

## 変更禁止ファイル
- `config/launchd/com.influx.price-watch.plist`・`scripts/xprice_watch_run.sh`（通知の文面と送り先）・`scripts/x_mention_dict.py`・`scripts/price_watch_collect.py`
- `scripts/x_mention_extract.py` のうち未コミットの株式分割の補正（`tasks/split_fix_all_sites.md` の作業分）＝触らない・commit に混ぜない

## Session Handoff
- 2026-10-29 08:55 着手（統括から委託・SubAgent）
- 2026-10-29 09:40 実装・確かめ 1〜4 済み。Status= 本番に入れた（configs の enabled=true・次の 22:10 便から効く）
  - Jev の呼び出し= `~/.claude/hooks/lib/jev.py` をホストから読むだけ（本線がホストの /usr/bin/python3 で動くため compose 変更なし・鍵は env→~/.zshrc。launchd の bash -lc でも ~/.zshrc から見つかるのを確認）
  - 点の貯め= 過去 3,503 件を入れた（線未満 1,325＝38%）＋試しで新しく 93 件
  - 確かめ 1（切）: 10-27 分で画面出力・mentions.jsonl 5,278 行・mention_alerts.jsonl 349 行が変える前と一致（run_at を除く・別の写しで比較）
  - 確かめ 2（入・--dry-run）: 13 日を数え直し 判定 779／外した 309／判定できず 0。対象日 10-27= 判定 68／外した 27 → 急増 3 社→2 社（日東電工・さくらインターネットが消え、JX金属が出る）。10-26 は 6→2 社で過去の当て直しと同じ 2 社
  - 確かめ 3（鍵なし）: 判定 779 件すべて no_key で残し、外した 0・急増 3 社のまま・止まらず rc=0
  - 確かめ 4: Docker の一括試験 910 件 OK（新規 19 件を含む）・`--selftest` 全通過
  - Codex レビュー: 伏せの取りこぼし（大文字 URL・国番号つき・かっこ書きの電話）を直した。「直近 14 処理日」指摘は judge と同じ（暦の 14 日 ∩ 処理済み）＝不採用。「dry-run で点の貯めを書く」は意図（同じ投稿を二度送らない）＝不採用
- 残: 22:10 の初回の結果（`data/x_price_watch/jev_filter_log.jsonl` の最終行と `jev_dropped/2026-10-28.jsonl`）を翌朝に見る
