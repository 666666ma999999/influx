# X 品薄系の検索語を増やす（P-INF-31・オーナー裁定 A＝物差しを直してから語を足す）

Phase: X 品薄レーン（P-INF-22 の続き）／叩き台= vault `02_Ai/influx/notes/influx-x-keyword-expansion-panel-2026-10-04.md`

## 答える問い
「品切れ関連で株に関わりそうなワードをもっと探したい」（オーナー原文・2026-10-03）

## 対象と数え方
- 物差しの校正: `data/x_price_watch/texts_vocab_eval/` の保存行 1,208 行（84 ファイル・6 語×14 日）から、投稿 ID と見出しの先頭 16 字で重複を除いた 40 投稿（判定器の当たり 20／外れ 20・seed 20261004）
- 人の正解= 「この投稿に供給・受注・値段の具体的な話が入っているか」の ○×？

## 成功基準
- 段 0: 銅・金の判定語が空でない（回帰テスト）／40 件すべてに人の ○×？ が付き、判定器の当たり・外れとの一致表が出る
- 段 2: 新しい語は発見器と同じ条件・別の 14 日で測ってから入口語に足す

## 段 0 の進み
- [x] 銅・金の判定語（1 文字 label を相場・地金の言い方に置換）`scripts/x_vocab_backfill_eval.py` SINGLE_CHAR_ALIASES
- [x] 40 投稿の抽出（`sample` モード・盲検＝判定器の結果はカードに出さない）→ `data/x_price_watch/vocab_label_sample.jsonl`
- [x] カード `output/x_vocab_label_cards.html`（git 対象外）・着地先 `data/x_price_watch/vocab_label_judgments.jsonl`
- [ ] オーナーの ○×（40 件）
- [ ] 一致表（判定器の当たり／外れ × 人の ○×）
- [ ] 事象の束ね（元記事・元開示の単位）・eval に期間と語の絞り込み

## Session Handoff
- 2026-10-04: 段 0 の前半まで。カードの保存サーバーは masa-2 で 127.0.0.1:59715（アイドル 24h で自動終了）。落ちていたら `python3 ~/.claude/scripts/card_verdict_server.py --html $PWD/output/x_vocab_label_cards.html --out $PWD/data/x_price_watch/vocab_label_judgments.jsonl` で立て直す（トークンは再利用されるので開いたページはそのまま使える）。
- 回帰: `docker compose run --rm -v "$PWD/tests:/app/tests" -v "$PWD/scripts:/app/scripts" xstock python -m unittest tests.test_x_vocab_backfill_eval_terms`（3 件）
