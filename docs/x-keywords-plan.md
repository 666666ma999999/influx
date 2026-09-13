> 移設: 2026-09-13 に `~/.claude/docs/x-keywords-plan.md` から本 repo `docs/` へ移設（influx の計画＝置き場整理・オーナー裁定）。本文中の `~/.claude/docs/x-keywords-plan.md` 表記は旧パス。
# Plan: x-keywords — X検索キーワード群の生成・自己改善パイプライン（v2 統合版）

**Status: 実装完了（2026-07-12）** — 残: ユーザーによる launchctl load のみ（✅ 実施済み・`launchctl list` に `com.masa.x-keywords-weekly` 登録を 2026-09-11 実測）。Codex コードレビュー 3R で GO-WITH-CHANGES・条件充足済み。追加成果: Web ページ版（`influx/output/bookmarks/x_keywords.html`・閲覧コピー用）+ `precedent-check` グローバルスキル。実行追跡と仕様差分は `tasks/archive/p-x-keywords.md`。
**Strategy宣言: Delivery** — 成功基準: §成功基準 v2（本書末尾）参照。
**レビュー経緯**: Fable5 設計 v1 → Codex 敵対的レビュー R1（P0×6）→ v2 改訂 → Codex R2（統合を条件に GO-WITH-CHANGES）→ 本書は R2 条件を織り込んだ統合版。旧 v1 仕様（hit 自動強化/退役・LLM への派生事実申告）は**全廃**。

## Context

MASA が X 検索で「自分がブックマークしたくなる記事」を見つけやすくするため、自身の X ブックマーク履歴（influx raw 245 件・97.5% 日本語・text 空 19.6%）から「強いキーワード群＝コピペ可能な X 検索クエリ集」を生成し、ブックマークが増えると自動で進化させる。進化の駆動力は 2 本: **(a) 新規ブックマークの自動反映**（量トリガー）と **(b) ノート上の人間の✅評価**（真の検索フィードバック）。「本文×キーワード一致」は検索成功の証明にならない（Codex P0-1）ため、**関心の継続シグナル（統計）としてのみ記録し、自動の強化/退役はしない**。

ユーザー確定事項: 週次自動 + 手動 `/x-keywords` / Vault ノート + influx repo 台帳 / v1 はキーワード群まで（候補記事自動収集は v2 非ゴール）。

## 非ゴール

候補記事リストの自動収集 / X 検索の実行そのもの / normalized.jsonl（文体教師データ）系の変更 / text 空 48 件の backfill（v2。件数はノートに明記）/ ブックマーク解除の検出（v1 は累積履歴分析と明記）/ クラスタの split・merge（v1 禁止）

## 設計 v2（正・これ以外の仕様は無効）

### A. パイプライン

```
launchd com.masa.x-keywords-weekly（週次・masa-2 固定）
  └─ /bin/bash -lc "~/.claude/bin/obs-x-keywords --fetch"
     0. hostname ガード（masa-2 以外は書込系を拒否。他 Mac は show のみ）
     1. fetch: fetch_bookmarks.py（--append・消失バグ修正後）→ SUCCESS/DEGRADED/FAILED に分類
        FAILED（自動実行）→ fetch_run イベント記録・health 赤・非ゼロ exit で停止
     2. worklist（bookmarks_keyword_worklist.py）:
        a. ノートから評価マークを収穫（読取時 hash 保存・厳格 parse・未知値があれば UPDATE 中止+通知）
           → evaluation_batch イベント（(query_id, mark, note_revision) で冪等）
        b. delta = URL 集合差分（正規キー = status ID）。基準点 = last_accepted_generation
           ※週次 snapshot 更新は基準点を進めない（reject/DEGRADED/revision も進めない）
        c. 関心継続シグナル = delta 本文（正規化）× 各クラスタ keywords の照合統計（判断はしない）
        d. 再生成トリガー判定: fetch==SUCCESS かつ
           [eligible_nonempty_delta >= 10] or [elapsed >= 28d かつ eligible_nonempty_delta > 0] or [--force(理由必須・観測窓を進めない)]
           非該当 → fetch_run イベント記録のみで NO_CHANGE 終了（ノートは触らない）
        → keyword_worklist.json（前世代・評価履歴・継続統計・delta標本・統計候補語・空きslot数）
     3. LLM: vault-prompt-runner.sh "00_General/prompts/scheduled/x-keywords-weekly.md"
        runner_model: sonnet / runner_tools: Read Grep Glob（Write/Bash なし）/ runner 単体通知は抑制
     4. ingest（bookmarks_keyword_ingest.py・柵）: 検証 → generation イベント append（flock+flush+fsync）
        → latest.json 再導出 → ノート render（上書き直前に hash 再確認・変化してたら中止し再収穫）
        reject → rejection イベント+理由を構造化保存 → 1 回だけ再生成 → 連続 2 回で REJECTED 終了+health 赤
     5. wrapper が最終状態 UPDATED/NO_CHANGE/DEGRADED/FAILED/REJECTED を 1 回だけ通知
```

- **DEGRADED**（部分取得）: raw 保存・統計更新まで。世代生成は禁止（次回 SUCCESS で再評価）
- `--no-fetch` は手動専用。fetch_status は `SKIPPED_MANUAL` となり、**手動モード + `--force --reason "…"` の場合のみ**既存 raw の整合検証（全行 JSON 妥当・件数が前回 snapshot 以上）を条件に再生成を許可（自動実行の再生成は fetch==SUCCESS のみ、の原則は不変）
- `--force` は `--reason "<理由>"` 必須・観測窓/トリガー時計を進めない

### B. LLM の入出力契約（P0-5 対応・最小権限）

LLM が出力してよいのは**表示名・why・候補キーワード・候補クエリ・evidence URL の選択・既存クラスタへの操作指定**のみ。世代番号・snapshot・delta・統計・status・count・日時は全て ingest が worklist/前世代/raw から再計算し、LLM 値は受理しない。

操作は 4 種限定（split/merge/delete は v1 禁止）:
- `keep(existing_id)` / `revise(existing_id)` — 既存 active の維持/改訂。**既存 active id は出力内にちょうど 1 回必須**（欠落・重複・未知 id は reject）
- `reactivate(existing_id)` — dormant の再活性化（dormant id のみ可）
- `propose_new`（id なし）— 新クラスタ提案。**id 採番は受理後に ingest が行う**。空き slot 数は worklist が提示し、超過分は proposals に隔離（active に入らない）

評価の反映規則（優先順位: ①人間の明示評価 > ②schema/安全制約 > ③継続シグナル > ④LLM 提案）:
- ✅使えた → そのクエリは**逐語で保持**（revise でも変更禁止）
- 🔁多すぎ → 絞る方向に改訂 / 🔍少なすぎ → 広げる方向に改訂
- ❌使わない → そのクエリの置換候補を出す（クラスタ削除ではない）
- ✅を持つクラスタは dormant 候補として提案禁止

### C. クラスタ状態と提案（自動降格なし）

- 状態は `active` / `dormant` の 2 つ。**遷移は全て人間の✅承認経由**（自動 dormant なし — Codex R2 P0-2 対応）
- システムは「休眠候補」「再活性化候補」「新クラスタ提案（最大 3）」をノートの**提案セクション**に✅セル付きで描画し、人間が✅を付けたものだけ次回 regen で反映（wiki-ingest-queue と同じ✅ゲート）
- 提案は継続シグナル観測窓に含めない。dormant はクラスタ上限（active 12）を消費しない
- 新提案は dormant とも類似照合し、既出テーマの再出現は new でなく reactivate 候補として出す
- **proposal_id**: 提案ごとに ingest 採番の不変 `proposal_id` + ノート隠しマーカー `<!-- pid:… -->` を付与。`proposal_decision` イベントはこの ID に結び付け、reactivate・dormant 遷移・new 昇格は**対応する承認イベントがある場合のみ** ingest が許可する

### D. 台帳 = 唯一の正本（event-sourced・P0-6 対応）

`influx/output/bookmarks/keywords_ledger.jsonl` — append-only の型付きイベントログ:
`fetch_run` / `evaluation_batch` / `generation`（受理世代・rev 付き）/ `rejection` / `proposal_decision`
- 各イベントに host / run_id / prompt_version / schema_version / generation_reason(delta|manual_regen) を記録
- 同一 snapshot の手動 regen は同一 generation の **revision**（delta・観測窓・トリガー時計を進めない）
- `latest.json` とノートは**常に台帳 replay から再導出可能**。起動時に 3 成果物の整合を検査し、不一致なら LLM を呼ばず再 render
- append は flock 下で flush+fsync。起動時に全行 JSON 検証、末尾破損は黙殺せず backup 回復 or REJECTED
- `.gitignore` 否定 + `git add -f` で強制追跡（再生成不能な正本・influx の research 3 例外と同じ扱い）

### E. マッチング（「正規化フレーズ照合」・形態素解析ではない）

NFKC + casefold + 空白正規化・URL/mention/絵文字除去。ASCII 語は英数字境界で一致、日本語は正規化部分一致（1 文字禁止・2 文字は要注意リスト）。keyword ごとに match_mode を持つ。陽性/陰性 fixture ≥50 件。共通実装は `bookmarks_keyword_common.py` に一元化（worklist と ingest で共用）。

### F. evidence とクエリの検証（ingest が強制）

- evidence: URL 実在 + 本文非空 + キーワード対応を Python 再検証。count は配列長から算出。新クラスタは独立 evidence ≥3。クラスタ間の URL 使い回し上限あり。ノートの根拠抜粋は render が raw から決定的生成（LLM 出力に本文長文を含めない）
- クエリ: ≤120 字・OR≤3・引用句≤2・除外≤2・改行/URL 禁止。**演算子つき `q` と演算子控えめ `q_simple` の 2 版必須**。min_faves 等の演算子は実装中に X 実 UI で受入試験
- 各クエリに ingest 採番の不変 `query_id`。ノート行に `<!-- qid:… -->` 隠しマーカー

### G. Vault ノート（render が決定的生成）

`02_Ai/x-buzz/influx-x/influx_x_search_keywords.md`（`<project>_` prefix・自動生成ミラー型・旧 `02_Ai/influx/` から移動）
- バナー: 「🤖 自動生成（**評価列のみ編集可**・それ以外は次回更新で消えます）」+ frontmatter 6 必須項目
- BLUF: 使い方 3 行 → データ鮮度（**最終 fetch 成功日時**・総件数・text空除外数・最終世代 gen/rev/日付/理由）
- クラスタ節: name・why・クエリ表（コピー用 q / 簡易 q_simple / 意図 / **評価セル** / 前回評価と日付は表示継続=「消えた」誤解防止）・根拠（件数 + 期間窓 30/90/365/全期間 + 抜粋 2-3。created_at は投稿日時であって bookmark 日時でない旨の注記）
- 提案セクション（✅セル付き・最大 3+休眠/再活性化候補）→ 休眠クラスタ節 → 世代履歴（直近 5 行）
- 評価セルの許可値は単一トークン ✅/🔁/🔍/❌ のみ。未知の非空値が 1 つでもあれば regen 中止・ノート非上書き・通知

### H. 変更ファイル一覧

| # | パス | 内容 | 担当 |
|---|---|---|---|
| 1 | `influx/scripts/fetch_bookmarks.py` | **--append 消失バグ修正**（URL→record map・全件 tempfile+os.replace・件数不減少ガード）+ 回帰テスト | Sonnet builder + validator |
| 2 | `influx/scripts/bookmarks_keyword_common.py` | 正規化・照合・URL 正規キー・台帳 IO の共通庫 | Sonnet builder |
| 3 | `influx/scripts/bookmarks_keyword_worklist.py` | 収穫・delta・継続統計・トリガー判定 → worklist.json | Sonnet builder |
| 4 | `influx/scripts/bookmarks_keyword_ingest.py` | 柵（検証）・台帳・latest 導出・ノート render | Sonnet builder |
| 5 | `influx/docs/prompts/bookmarks_keyword_extraction_v2.md` | LLM プロンプト正本（B 契約・評価反映規則。v1 は superseded 印済み 2026-08-29・張替え= K-133/I-14） | **Main (Fable5)** |
| 6 | vault `00_General/prompts/scheduled/x-keywords-weekly.md` | runner frontmatter 付き実行プロンプト（#5 参照・薄い） | Main |
| 7 | `~/.claude/bin/obs-x-keywords` | wrapper（A のフロー・hostname ガード・状態通知） | Sonnet builder |
| 8 | `~/.claude/launchd/com.masa.x-keywords-weekly.plist` | 週次・masa-2 固定コメント・cp+load 手順 | Sonnet builder |
| 9 | `~/.claude/commands/x-keywords.md` | /x-keywords（update/regen/show） | Sonnet builder |
| 10 | `~/.claude/scripts/update_claudeenv.py` | JOB_ARTIFACTS に 1 タプル | Sonnet builder |
| 11 | vault `02_Ai/x-buzz/influx-x/influx_x_search_keywords.md` | 成果物ノート（render 生成） | 自動 |
| 12 | vault `02_Ai/influx/influx_ope.md` + `03_ClaudeEnv/collector-health.md` | MOC 1 行 + health (a)(b) 行 | Main |
| 13 | `~/.claude/data/bookmarks*.jsonl` | symlink 2 本を biz 実体へ（/bookmarks 復旧） | Sonnet builder |
| 14 | `influx/.gitignore` | 台帳の否定パターン + git add -f | Sonnet builder |
| 15 | `~/.claude/docs/x-keywords-plan.md` / `tasks/archive/p-x-keywords.md` | 本書 v2 へ同期（B0） | Main |

※ `obs-x-bookmarks` 本体は不変更（fetcher 修正で足りる。wrapper から --append 付きで呼ぶ）

### I. バッチと検証（3-Fix Limit・高リスクは 1/バッチ）

- **B0**: 文書同期（#15）
- **B1（高リスク・単独）**: fetcher 修正（#1）。fast_verify: 「245件→新規3件+補完1件成功」シミュレーションで件数不減少・既存本文保持。実 fetch 前に `bookmarks.jsonl` バックアップ
- **B2**: 共通庫+worklist（#2,3）。fast_verify: 実データで worklist.json 妥当 / 照合 fixture 50 件 PASS / 評価 parse（正常・未知値・列外編集・TOCTOU hash 不一致）が仕様通り
- **B3**: ingest（#4）。fast_verify: 不正 fixture 群（fence 2 個・未知 id・既存 id 欠落/重複・架空 URL・text空 URL・slot 超過・クエリ 121 字・台帳末尾破損・同一 snapshot regen）が全て正しく reject/隔離/revision 化。正常 fixture → generation イベント+ノート render
- **B4**: プロンプト（#5,6・Main）+ **v1 世代 1 生成は Opus SubAgent**（245 件全量・generation 1 契約: 全件 baseline・delta_count=0・全クラスタ new・評価/継続は null）+ X 演算子の実 UI 受入試験 + MOC/health 記帳（#12）。fast_verify: 世代 1 受理・ノート実在・≥5 クラスタ×各≥2 クエリ・evidence 全 URL 実在
- **B5**: wiring（#7,8,9,10,13,14）。fast_verify: `obs-x-keywords --no-fetch --force --reason "wiring-test"` exit 0 → 直後の**force なし通常実行**が NO_CHANGE / plutil -lint PASS / cp+load 後 launchctl list に登録 / symlink 2 本 `wc -l` 成功
- **B6**: ループ実証 + 監査。fixture delta ≥10 + ✅/❌ 評価マーク → 再実行 → 評価が台帳イベント化・✅クエリが世代 2 で逐語保持・❌クエリ置換・継続統計記録。**Codex コードレビュー**（実装後）+ **Sonnet validator が fresh-context で成功基準を全再検証** + implementation-checklist

### 成功基準 v2

1. Vault ノートが存在し ≥5 クラスタ × 各 ≥2 のコピペ可能クエリ（q/q_simple 2 版）+ 根拠 + 評価列を含む
2. 台帳に generation 1 イベントが受理記録され、latest.json とノートが台帳から再導出可能
3. `obs-x-keywords --no-fetch --force --reason "acceptance"` が exit 0 で完走し、直後の force なし通常実行が NO_CHANGE になる
4. `launchctl list` に `com.masa.x-keywords-weekly` 登録済み（plutil -lint PASS）
5. **ループ実証**: fixture delta+評価マーク → 世代 2 に評価収穫・✅逐語保持・❌置換・継続統計が観測できる
6. **fetcher 回帰**: append+補完シミュレーションで件数不減少（消失バグの再発防止テスト PASS）
7. Codex 敵対的レビュー（設計 R1/R2 済）+ 実装後コードレビューの P0/P1 対応済み
8. symlink 2 本が biz 実体を指し `/bookmarks` が動く

## 影響範囲

- **influx repo**: fetch_bookmarks.py（append 経路のみ・fetch 本体ロジック不変）+ 新規 3 スクリプト + docs/prompts 1 枚 + .gitignore 2 行 + output/bookmarks/ 新設。既存 research/winrate パイプラインへの影響なし
- **~/.claude**: bin 1 本・launchd plist 1 枚・commands 1 枚・update_claudeenv.py の JOB_ARTIFACTS 1 タプル・data/ symlink 2 本張替・docs/tasks 文書
- **vault**: 02_Ai/x-buzz/influx-x/ にノート 1 枚新設（起案時は 02_Ai/influx/・後に移動）+ 02_Ai/influx/influx_ope.md へ 1 行 + collector-health.md へ 2 行 + prompts/scheduled/ 1 枚
- **他 Mac (MASA.local)**: plist は load しない（host 固定）。vault 同期でノートと health は見える（read-only）

## 変更禁止ファイル

- `~/.claude/scripts/vault-prompt-runner.sh` / `wiki_ingest_apply.py`（参照・呼出のみ。改修しない）
- `~/.claude/bin/obs-x-bookmarks`（本体不変更。fetcher 修正で吸収）
- `influx/output/bookmarks.jsonl`（スクリプト経由以外の直接編集禁止。B1 前にバックアップ必須）
- `autopost/` 配下全部・`influx/data/writing_style/` （normalized 教師データ系は読むだけ）
- `wiki/meta/decisions.md` 等の wiki 正本（/save 経由以外の直接編集禁止）

## 実装体制

Main (Fable5): プロンプト正本・schema 最終責任・各バッチ監査・vault 記帳 / Sonnet builder ×2-3 並列 + validator（fresh-context）/ Opus: 世代 1 品質パス / Codex: 実装後コードレビュー


---

# Plan: x-keywords v2 — AI 検索代行ダイジェスト（候補記事の自動収集）

**Strategy宣言: Delivery** — 成功基準: 週次で「候補記事ダイジェスト（≥10件・既読/既ブックマーク除外・発見元クエリ付き）」が Web ページ上部に自動生成され、ブックマークされた候補が次回 worklist でクエリ的中として観測できる。
**敵対レビュー: 軽**（Codex 1R。v1 で重 6R 済み・v2 は確立済みの柵/台帳/render パターンの踏襲のため）

## Context（v2 ダイジェスト）

v1（完了・2026-07-12）: ブックマーク→キーワード群生成→週次自動進化＋Web完結評価。ユーザーの新しい痛み: **28 クエリを人が検索して読み切れない**。ユーザー決定: AI が検索まで代行し、週次で候補記事ダイジェストを出す（v1 で非ゴールとして設計余地を残した機能）。副次効果: **ブックマークされた候補 = クエリの真の的中**として測定可能になり、Codex R1 P0-1 で「観測不能」とされた検索有効性シグナルが初めて実測になる。

## 骨組み（2段提示の1段目・承認対象）（✅ 実施済み 2026-07-12・以下は承認済みの起案原文）

失効印 2026-09-03 K-149: 承認済み（2026-07）・詳細= §詳細設計 v2・収集器の現行= `tasks/archive/p-x-keywords.md` §v2 C5/C6（ブラウザ収集器が既定・xAI は `=xai` で温存）。

## 詳細設計 v2

### 確定事実（explore-grok・grok_collect_twittora.py 238 行実読）

- xai_sdk 必須（`Client` + `tools=[x_search(from_date, to_date)]` + `chat.parse(pydanticモデル)`）。**host に xai_sdk なし → 収集は Docker 実行必須**（Docker-Only 方針とも整合）
- 検索は **X 演算子でなく自然文プロンプト**（likes 下限・除外条件・言語はプロンプト指示 + コード側二重フィルタ）。日付範囲だけ API 引数
- レスポンスは pydantic 構造化（url/author/content/likes/impressions/posted_at 等）。リトライなし・逐次・クエリ単位 fail-soft の先例
- MODEL="grok-4-1-fast-non-reasoning"・XAI_API_KEY は環境変数

### アーキテクチャ（収集と確定の 2 段分離 = 既存の柵パターン踏襲）

```
週次 wrapper（fetch 成功後・regen とは独立に毎週）
  ├─ (D1) 収集: docker compose run --rm xstock python scripts/bookmarks_keyword_digest_collect.py
  │    ・keywords_latest.json の active クラスタごとに 1 プロンプト（= 週 8 API 呼び出し）
  │    ・プロンプト = クラスタ why + keywords_ja/en + 「いいね<クエリのmin_faves相当>以上・日本語・広告/bot除外・過去7日」
  │    ・x_search(from_date=7日前, to_date=今日) + chat.parse → 生候補を output/bookmarks/digest_raw.jsonl
  │      + digest_status.json {per_cluster_counts, errors}（**書くのはこの 2 ファイルのみ・台帳に触らない**）
  │    ・クラスタ単位 try/except fail-soft（先例踏襲）
  └─ (D2) 確定: host python3 scripts/bookmarks_keyword_digest_apply.py（stdlib のみ・柵）
       ・digest_raw を検証（url 形式・必須フィールド・件数上限 200・likes 下限のコード側二重フィルタ）
       ・除外: canonical URL で ①既ブックマーク（bookmarks.jsonl）②過去 digest 掲載済み（台帳 replay）
       ・選定: 決定的ランキング（クラスタ内 likes 降順・クラスタ ≤3 件・全体 ≤20 件・LLM なし）
       ・台帳 `digest` イベント append（pipeline_lock 下・{run_id, items:[{url(canonical), cluster_id, seed_query_id,
         excerpt(≤120字・md エスケープ), author, likes, posted_at}], errors}）→ render_html 再生成
       ・digest_raw 不在/空/全滅 → 何も書かず exit 0（DIGEST_SKIPPED ログ・ページは前回分+日付表示を維持）
```

### 的中測定（v1 の弱点を実測に変える）

- worklist に追加: delta（新規ブックマーク）∩ 過去 digest 掲載 URL → `continuity_stats[].digest_hits`（cluster_id / seed_query_id 別）+ generation イベントに記録
- これで「AI が提示 → 人がブックマーク」= クエリ/クラスタの**真の的中率**が台帳に蓄積（プロンプトは既存 v2 のまま。解釈強化は実測が貯まってから v3 として別途）

### 表示（render_html 拡張・Web 主線）

- ページ最上部に `## 📬 今週の候補記事`（掲載日付き）: 項目 = 抜粋(≤120字) / 著者 / likes / X リンク（scheme 制限は既存関数流用）/ 発見元クラスタ名。「良ければそのままブックマーク → 的中として学習されます」の 1 行説明
- ノート（Obsidian）は変更しない

### 変更ファイル

| # | パス | 内容 |
|---|---|---|
| 1 | `influx/scripts/bookmarks_keyword_digest_collect.py` | Docker・xai_sdk・収集のみ（新規） |
| 2 | `influx/scripts/bookmarks_keyword_digest_apply.py` | host・柵・台帳 digest イベント・render 呼出（新規） |
| 3 | `influx/scripts/bookmarks_keyword_render_html.py` | ダイジェストセクション追加 |
| 4 | `influx/scripts/bookmarks_keyword_worklist.py` | digest_hits 統計追加 |
| 5 | `~/.claude/bin/obs-x-keywords` | digest ステップ（D1→D2・fail-soft）+ `/x-keywords digest` |
| 6 | `~/.claude/commands/x-keywords.md` | digest サブコマンド追記 |
| 7 | `influx/tests/test_bookmarks_keyword_digest.py` ほか既存 2 本に追加 | apply の除外/選定/的中/fail-soft・render のセクション |

### 変更禁止

v1 と同じ（vault-prompt-runner / wiki_ingest_apply / obs-x-bookmarks / 実データ直接編集）+ `bookmarks_keyword_ingest.py`（v2 では触らない）+ プロンプト v1/v2 ファイル（不変・v3 は将来）

### Codex 軽 1R（GO-WITH-CHANGES）の採用差分

1. **inbox 分離**: collect の入出力は `output/bookmarks/digest_inbox/` に限定（入力 = wrapper が置く latest.json のコピー・出力 = `digest_raw-<run_id>.jsonl` + status。**台帳・bookmarks.jsonl に触らない契約**をコードとテストで強制。compose のマウント絞りは実測後の任意強化）
2. **run 整合**: status に run_id / source generation・revision・urls_sha256 / complete フラグ / 生成時刻。temp→rename で原子公開。apply は一致・鮮度・complete を検証し、不一致は無書込
3. **URL 検証強化**: apply で https + host 完全一致（x.com/twitter.com）+ `/<handle>/status/<digits>` + URL/ID 整合 + **Snowflake ID→時刻が掲載窓と整合** + 型/長さ。不合格 item は drop（fatal にしない）。SDK が citation を返す場合は collect が保存し照合
4. **部分成功ガード**: 選定結果 <8 件なら digest イベントを書かず前回表示維持（DIGEST_DEGRADED 通知）
5. **replay 一元化**: common.replay() を digest 対応に拡張（掲載済み URL 集合・url→cluster/query/掲載時刻・最新 digest）。apply/worklist/render は全て replay 経由（変更ファイルに common.py を追加）
6. **wiring/キー前提**: digest ステップは fetch 分類・fetch_run 記録の後・**NO_CHANGE 早期 exit より前**。wrapper は `~/.envrc.shared` を source 後 XAI_API_KEY を preflight（空なら DIGEST_SKIPPED 通知）。C3 に launchd 実コンテキスト試験（launchctl start）を含める
7. **用語の誠実化**: digest_hits の定義は「digest 掲載後に初観測された同一 URL との相関」（真の的中率ではない・自動 reinforce/retire の単独根拠に使わない）
- replay 成長は現状問題なし（~1,040 URL/年）。計測して閾値超過時のみ compaction

### バッチと検証

- **C1**: collect + apply + テスト。fast_verify: fixture digest_raw → apply が除外/選定/台帳/render まで通る・再実行で掲載済み除外・likes 二重フィルタ・全滅 fail-soft
- **C2**: worklist digest_hits + render セクション + wrapper/コマンド。fast_verify: fixture（digest 掲載 URL を含む delta）→ continuity_stats に digest_hits / ページ最上部にセクション
- **C3**: **実 API 実走 1 回**（Docker・XAI_API_KEY）→ digest イベント ≥10 件・ページ表示確認 → Codex 軽 1R → validator fresh-context 検証

### 成功基準（v2 ダイジェスト）

1. 実 API 実走で digest イベントが記録され、ページ最上部に候補 ≥10 件（発見元クラスタ・X リンク付き）が表示される
2. 再実行で掲載済み URL が再掲されない（台帳 replay による除外を観測）
3. fixture: digest 掲載 URL をブックマーク delta に混ぜる → worklist の continuity_stats に digest_hits が計上される
4. collect 失敗/0 件時に fail-soft（台帳無書込・前回表示維持・DIGEST_SKIPPED ログ）
5. 追加テスト含む全スイート PASS + Codex 軽レビューで P0 なし

### コスト・運用

週 8 回の xAI API 呼び出し（grok-4-1-fast・x_search）。**注意（キー所在は未確認**・secret ファイルは読まない方針）: launchd の `-lc` は .zshrc を読まないため、wrapper は `~/.envrc.shared` があれば source → それでも XAI_API_KEY 空なら **DIGEST_SKIPPED（fail-soft・ログ+通知）**。手動 `/x-keywords digest`（ターミナル=zshrc 有効）は常に動く。週次で常にスキップされる場合の恒久化（plist EnvironmentVariables 等）は実測後に判断。plist 変更は不要（wrapper 内ステップ追加のみ）
