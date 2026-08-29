# influx

**株で勝つ情報を収集する**ための基盤（目的の正本= [`plan.md`](plan.md) の「## 目的」）。
X の投稿・商品価格の系列・企業の開示を集め、投資判断の材料にする。

> 🪦 **2026-08-29 に旧2段階分類パイプラインを退役**（`collect_tweets.py` → `classify_tweets.py` →
> 7カテゴリ → `viewer.html`）。定期実行は登録されておらず、最後の精度計測が不合格だった
> （macro F1 0.3905／合格線 0.80・`output/f1_baseline.json` 2026-04-24）。
> 本 README にあった収集コマンドと7カテゴリの説明はその退役に伴い削除した。
> 人手の正解データ（`data/gold_set/`）と例文（`data/few_shot_examples.json`）は資産として残置。

## どこを見るか

| 知りたいこと | 見る場所 |
|---|---|
| 目的・非ゴール | `plan.md`「## 目的」 |
| いま何が動いているか（X収集基盤） | `influx-architecture.md` |
| いま何が動いているか（株アルゴ研究） | `influx-stock-algo-architecture.md` |
| 何がいつ動くか（定期ジョブ） | `docs/pipeline-map.md`（機械生成） |
| 日々のコマンド・Docker の使い分け | `CLAUDE.md` |
| X Cookie の取り直し | `.claude/skills/refresh-x-cookies/SKILL.md` |

## 注意事項

- **自動ログインは永久禁止**: ログイン済み Cookie でのブラウザ閲覧のみ。X API は使わない
- **高頻度アクセスを避ける**: 収集は定期ジョブの頻度に従う（ブロックリスク）
- **`x_profiles/` は秘密**: Cookie を含むため共有しない・値を転記しない
