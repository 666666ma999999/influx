# bookmarks_keyword_extraction_v1 — X検索キーワード群の世代提案プロンプト

バージョン: v1（2026-07-11 新設。**このファイルは直接書き換えず、変更時は `_v2.md` を新設**する — influx プロンプトバージョニング規約）
消費者: headless claude（週次・Sonnet）/ 有人セッションの SubAgent（baseline・Opus）
機械検証: `scripts/bookmarks_keyword_ingest.py` が出力を全件検証する。**このプロンプトのルールは ingest の reject 条件と 1:1 対応**しており、違反出力は自動却下される。

---

## あなたの役割

MASA（非エンジニア寄り・日本語話者）が X (Twitter) 検索で「自分がブックマークしたくなる記事」を見つけやすくするため、本人のブックマーク履歴から **X 検索クエリ集の次世代案**を作る。

## 入力（Read すること）

1. `output/bookmarks/keyword_worklist.json` — 必読。フィールド:
   - `previous_generation`: 既存クラスタ（`cluster_id`・キーワード・クエリ+`query_id`+評価履歴）。**空なら baseline モード**
   - `pending_evaluations`: 人間が付けた評価マーク（✅使えた/🔁多すぎ/🔍少なすぎ/❌使わない）
   - `continuity_stats`: 新規ブックマークと既存キーワードの照合統計（**参考シグナル。これだけを根拠にクラスタを消さない**）
   - `delta.samples`: 新規ブックマークの標本（最大40件）
   - `stats_candidates`: 頻出語統計（候補の種）
   - `free_slots`: 新クラスタ提案可能数
2. baseline モードのときは `output/bookmarks.jsonl` も Read し、全体像（テーマの分布・本人が何に反応するか）を把握する（text が空の行は無視）

## タスク

- **baseline**（previous_generation が空）: ブックマーク全体から **5〜8 個のクラスタ**を `proposals` として提案する。`clusters` は空配列 `[]` にする
- **通常**: `previous_generation` の **active クラスタ全てに `keep` か `revise` をちょうど 1 回ずつ**出す（1 つも省略しない・同じ id を 2 回使わない）。dormant の再活性化が妥当なら `reactivate`。新テーマは `proposals`（最大 3 件かつ free_slots まで）

## クラスタの切り方（品質指針）

- トピックだけでなく**ブックマークする意図**で切る（例:「実装ノウハウを保存」「事例・数字を保存」「ツール速報を保存」）
- 各クラスタの `why` は「MASA がなぜこれをブックマークするか」を 1 文で
- クエリは検索窓にコピペして**1 画面で当たりが出る具体性**。日本語主体・広すぎる 1 語クエリ禁止
- `min_faves:` は 50〜500 の範囲で、クエリの狭さに応じて調整（狭いクエリほど低く）

## 評価マークの反映（最優先ルール）

1. **✅ が付いたクエリ（q）は一字一句そのまま残す**（revise でも変更禁止）
2. **❌ が付いたクエリは逐語で再掲禁止**。代わりの置換案を出す
3. 🔁多すぎ → 絞る方向に改訂（語を足す・min_faves を上げる・期間を絞る）
4. 🔍少なすぎ → 広げる方向に改訂（語を減らす・OR を使う・min_faves を下げる）

## 出力形式（厳守・違反は機械 reject）

出力は次のフェンス**ちょうど 1 個**。フェンス外の文章は 3 行以内の要約のみ許可。

````
```json x-keywords-generation
{
  "clusters": [
    {
      "op": "keep",
      "id": "c-xxxxxxxx",
      "name_ja": "クラスタ表示名（60字以内）",
      "why": "MASAがこれをブックマークする理由1文（200字以内）",
      "keywords_ja": ["キーワード", "..."],
      "keywords_en": ["keyword", "..."],
      "queries": [
        {"q": "\"Claude Code\" 活用 min_faves:100 lang:ja -filter:replies",
         "q_simple": "\"Claude Code\" 活用事例",
         "intent": "このクエリで何を見つけたいか1文"}
      ],
      "evidence_urls": ["https://x.com/.../status/...", "..."]
    }
  ],
  "proposals": [
    { "op": "propose_new", "name_ja": "...", "why": "...",
      "keywords_ja": ["..."], "keywords_en": ["..."],
      "queries": [ ... ], "evidence_urls": ["...", "...", "..."] }
  ],
  "notes": "任意の補足（2000字以内）"
}
```
````

### 機械検証されるルール一覧

| 項目 | ルール |
|---|---|
| op | `keep` / `revise` / `reactivate` / `propose_new` の 4 種のみ。`clusters` に propose_new 不可・`proposals` は propose_new のみ |
| id | previous_generation にある id のみ使用。**propose_new に id を付けない**（システムが採番）。active id は全て・ちょうど 1 回ずつ |
| queries | 各クラスタ **2〜5 個**。`q`（演算子つき）と `q_simple`（min_faves/min_retweets なし）の 2 版必須 |
| クエリ構文 | 各 **120 字以内**・改行/URL(`://`) 禁止・`OR` は 3 個まで・引用句 2 個まで・`-`除外 2 個まで |
| 演算子 | 使用可能: `min_faves:` `min_retweets:` `lang:` `filter:` `-filter:` `since:` `until:` のみ。他の `xxx:` 形式は禁止 |
| keywords | ja+en 合計 2 個以上・各 2〜40 字・**日本語 1 文字キーワード禁止** |
| evidence_urls | **実在するブックマークの URL のみ**（worklist の samples か bookmarks.jsonl から選ぶ。捏造は即 reject）。本文が空のものは不可。そのクラスタの keyword が本文に含まれるものを選ぶ。propose_new は **3 件以上**・keep/revise/reactivate は 1 件以上。同じ URL を 3 つ以上のクラスタで使い回さない |
| メタ情報 | 世代番号・日付・件数・status・統計値を**書かない**（システムが計算する。書いても無視または reject） |

## 禁止事項

- ブックマーク本文の長い引用（evidence は URL のみ。抜粋はシステム側が生成する）
- APIキー・トークン等に見える文字列の出力（秘密スキャンで全体 reject される）
- continuity_stats が 0 だからという理由だけでクラスタを消す・dormant を提案する（休眠判断は人間の承認制）
