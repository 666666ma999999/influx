# pending 判決到達フロー設計（v0.2・検収済み・1周目実行完了 2026-08-08）

## 1周目の実行結果（2026-08-08・ユーザー検収「それでいい」後に同セッション実行）

- `scripts/kpi_pending_resolutions.py` 新設（--audit/--summary/--apply/--resolve・G1/G2機械検査つき）
- --apply: 機械規則74件記帳（R0=2/R1=1/R2=17/R3=9/R4=45）・trials.jsonl 不変を機械照合済み
- ユーザー裁定4件（PEAD系譜・ask 2026-08-08）: 直系2件（pead_initial_gap8_vol3 4c915ee9 /
  pead_gap8_vol3_defer3 adb894af）= `rejected_by_evidence`・X複合変種2件（pead_x_max20_10 /
  pead_x_dev25_10）= `closed_no_action`
- **実装後Codexレビュー（1段統合）で訂正3行**（reduction規則=最終行勝ちで append 訂正・台帳は不変）:
  ①volshock_5x_HOLDOUT_obs: R0のwatchlist照合がHOLDOUTサフィックスを剥がさず棄却誤分類→`awaiting_forward`
  ②③pead_initial_gap8_vol3 のCI欠測行・部分走査行2件: 旧R4が機械クローズ→新規則では機械対象外のため
  ユーザー裁定（PEAD直系=棄却証拠）を系譜適用。あわせて classify() を修正（未知holdout行は機械で閉じない・
  欠測値を「未達」とみなさない・R3/R4は in-sample 全域走査の実測確認を必須化）、guarded_append を
  3点照合（読込時→lock内→書込み後）+並行apply二重記帳防止に強化、--audit に現在状態の全件検証を追加、
  SLA超過を macOS 通知にも配線
- 最終状態: **78/78 終端到達・未整理0・SLA超過0・機械規則との不一致0・全件検証OK**
  （`rejected_by_evidence` 6 / `closed_no_action` 44 / `awaiting_forward` 18 / `structurally_capped_n` 9 /
  `superseded_rejected` 1。resolutions.jsonl 82行=現在状態78+訂正4）
- 併せてユーザー裁定: `params.resolution_path` 記録方針は廃止→ resolutions.jsonl 一本化
  （catalog §pending解消ルール改定・草案 surge_precursor_model_preregister.md §8-4 追随済み）
- 表示配線: `daily_screen.py` 稼働状況に「試行整理状況」1行を常設（--audit 経由・SLA超過⚠️・失敗WARN縮退）
- 残: 翌朝ジョブ（com.influx.paper-screen 07:30）での実走確認

---

# （以下、検収済み設計本文 v0.2）

紐付け: 2026-07-31 敵対クロスレビュー**指摘8**「trials.jsonl の pending 78/114件が判決未到達で在庫化。
α予算を消費済みの試行が判決に到達しない。G3『αの配り方』再設計とは独立の別欠陥」
／ 正本 `docs/stock-algo-kpi-catalog.md:372-381`「pending解消ルール（新設・未実行・数値stale 53/61→現況78/114）」の具体化。
状態: **叩き台 v0.2**（ユーザー検収まで完成を宣言しない）
裁定済み（2026-08-08 冒頭ask）: 範囲=設計+1周目まで（検収後にその場で78件仕分け）

## 敵対レビュー1周の記録（2026-08-08）

- レビュアーA=同モデル別文脈（validator・read-only）14指摘 / レビュアーB=異モデル（Codex・read-only sandbox）8指摘。相互出力は未共有・割れなし（相互補完）
- **v0.1 は必須ゲート違反で全面改稿**: G3違反（汚染OOS「生存」を終端判決扱い＝両者一致・A1/B1。前向き観察中の主力14系統を在庫から消す＝両者一致・A2/B2）・G4違反（「78行は全て in-sample」は事実誤認。holdout期 2023-01〜2026-05 の行が2本ある＝A3・統括実測で確認）
- 統括の機械検証で確定した事実: ①holdout期 pending 2行実在 ②`data/kpi_trials/trial_fingerprints.json` が pending 75名を全被覆（122エントリ・11家族・consumer=`build_recipe_shelf.py:212-228`）→家族対応表の新設は不要（A6） ③`build_daily_reco.py:148` の pending は paper ledger の `pending_entry`（建玉状態）で trials の verdict=pending とは別概念（A7） ④`paper_today.md` の生成主体は `daily_screen.py`（B6）
- 主な反映: R2（前向き）を棄却系より先に判定・R1は棄却側verdictに限定・運用開始ライン3条件化（月5銘柄を追加=A4）・EV v2参照の明示（A5）・reduction規則の定義（B5/A8）・G1/G2の機械検査（A9）・採点表に分類正当性を追加（A10）・`rejected_by_evidence` 新設（A11）・α可視カウンタは分母常時表示（A12/B4）・SLAを行単位90日に（A13/B7）・表示接続を daily_screen.py に一本化（A7/B6）・params.resolution_path は catalog 修正提案として一本化（B8）

---

## 採点表（受け入れ基準・v0.2）

**必須ゲート（1つでも違反なら不合格）**:
- G1: `trials.jsonl` への書込みゼロ（--apply 実行前後で行数+sha256 を機械照合・不一致は FATAL）
- G2: Bonferroni 分母（=trials.jsonl 行数）の変更ゼロ（G1の行数照合と等価・事後圧縮禁止の恒久ルール遵守）
- G3: pending→**合格**への経路を作らない。具体的に:
  (a) 汚染OOSの「生存」を終端・判決・推奨の根拠にしない（棄却側のみ有効）
  (b) holdout期の数値を昇格材料として表示しない
  (c) resolution は**判決（verdict）ではない**＝統計的地位を一切持たない運用整理と明記
- G4: 事実誤認ゼロ（78件の内訳・正本引用の誤りが1つでもあれば全面差し戻し）

**評価項目（100点）**: ①完全到達性 20（78/78 が終端整理に到達する設計か）
②分類正当性 25（各ラベルが正本の凍結ルールと矛盾しないか・生存側を誤って閉じないか）
③再発防止 20（将来の pending が滞留しない仕組みの実効性・強制力）
④最小変更 15（新規ファイル・スクリプト数・既存 canonical の再利用）
⑤α可視性 20（分母を隠さず「消費114/整理済みN」の形で常設表示するか）

**合格ライン**: 合計75点以上 かつ ②≥20。
**修正ルール**: 1周で直すのは最大2項目・全文書き直し禁止（必須ゲート違反時を除く）・最大3周。

---

## 1. 問題の再定義（レビュー反映後）

- pending は「未処理キュー」ではなく **judge() の正常出力**（5基準の一部未達 かつ point_lift>1.0）。
- 78行の内訳（実測）: **in-sample（2016-11〜2022-11系）76行 + holdout期（2023-01〜2026-05）2行**
  （`pead_gap8_vol3_defer3_HOLDOUT` / `volshock_5x_HOLDOUT_obs`）。unique 75 KPI・n<100 は10件。
- in-sample の探索基盤は凍結済み・holdout 1回権は消費済み → **pending を in-sample の再判定で「判決」に変えることは制度上不可能**。
  できるのは次の4つだけ: (a) 既に出ている棄却証拠への紐付け (b) 唯一の正規判決経路＝前向き観察への接続の明示
  (c) 判決不能（構造的n不足）の明示 (d) 追試を予定しない旨の明示。
- したがって「判決到達」の定義を再設定する: **全78行が上記(a)〜(d)のどれかに到達し、その状態が機械監査可能であること**。
  「αを取り戻す」ことも「未解消0件に見せる」ことも目的にしない（分母114は常時可視のまま）。

## 2. 設計本体

### 2-1. 判決の器（新設・台帳外）

`data/kpi_trials/resolutions.jsonl`（append-only・**α非消費**=Bonferroni分母は trials.jsonl 行数のみを数える
`scripts/kpi_bonferroni_check.py:54-63` の実装のため、本ファイルは分母に影響しない）。1行スキーマ:

```json
{"run_id": "<trials.jsonl の対象行>", "kpi_name": "...", "resolution_path": "<下記5値>",
 "rule": "R0|R1|R2|R3|R4|USER", "reason": "1文", "evidence": "<棄却行run_id / watchlist名 / holdout記録>",
 "review_batch": "2026-08", "ts": "..."}
```

- **resolution は判決ではない**（統計的地位ゼロ・α無関係・judge() 序列の外側の運用整理）
- 過去行の verdict は書き換えない。取り消しは同 run_id への新行 append で表現
- **reduction 規則（現在状態の一意化・B5/A8対応）**: 同一 run_id に複数行がある場合、**ファイル内の最終行が現在状態**。
  読み手（audit・表示）は必ずこの規則で畳む。書き込み時検査: run_id が trials.jsonl に実在・kpi_name が一致・
  対象行の verdict が pending であること（違反は書き込み拒否）
- `escalate_user` は resolution_path の値では**ない**。resolution 行が無い pending 行＝「未整理」であり、
  未整理一覧がそのままエスカレーションキュー（別状態を発明しない・A8対応）

### 2-2. 終端分類（5値）

| resolution_path | 意味 | 統計的地位 |
|---|---|---|
| `superseded_rejected` | 同一KPIに後続の**棄却側**判決行（fail / hoos_rejected / confirm_fail / rejected / invalidated）がある | 棄却は有効（凍結ルールどおり） |
| `rejected_by_evidence` | holdout開封記録等の棄却証拠が当該行の系譜に直接該当（evidence に記録の所在を必須記載） | 棄却は有効 |
| `awaiting_forward` | `config/paper_watchlist.json` で status=observation の系統＝判決は前向き評価から来る。**非終端**（前向き判決が出たら後続行で更新） | 判決待ち（正規経路接続済み） |
| `structurally_capped_n` | in-sample 全域走査済みで n<100・増える見込みなし＝判決不能の明示 | 判決なし |
| `closed_no_action` | 運用開始ライン**3条件**（CI下限>1.2・EV≥+1%/月・月5銘柄以上）のいずれか未達 かつ in-sample 全域走査済み＝**追試を予定しない旨の運用整理**。判決ではなく、新規独立データでの将来の再検討を妨げない | 判決なし |

⚠️ `hoos_survived_tainted`（汚染OOS生存）は**どの分類の根拠にもならない**（生存に確証効力なし）。
生存系統は watchlist 掲載により R2 で `awaiting_forward` になる＝判決は前向きが下す。

### 2-3. 機械仕分け規則（R0→R4 の順・最初に該当した規則で確定）

- **R0 holdout行の明示処理**（2行）: period.start が 2023 以降の行。
  `pead_gap8_vol3_defer3_HOLDOUT` → `rejected_by_evidence`（2026-07-06/07 の holdout 1回開封の記録そのもの・ci_low=0.88/EV−0.25%・catalog:277）。
  `volshock_5x_HOLDOUT_obs` → `awaiting_forward`（volshock_5x は watchlist observation 稼働中。**本行の holdout 数値は以後いかなる表示・推奨にも使わない**）
- **R1 superseded_rejected**: 同一 `kpi_name` に後続の**棄却側** verdict 行がある（実測1件: sh_dip_reentry）
- **R2 awaiting_forward**: `config/paper_watchlist.json` で **status=observation** のエントリと同名（実測17件・チャンピオン volshock_x_above200_quiet や earnings_spillover を含む＝在庫からは消さず「前向き接続済み」区分へ）
- **R3 structurally_capped_n**: n<100 かつ in-sample 全域走査済み（実測9件）
- **R4 closed_no_action**: 運用開始ライン3条件のいずれか未達（月次頻度は n÷期間月数で近似・実測45件。理由フィールドに未達条件を機械記載）
- **残差=未整理**: どの規則にも該当しない行（実測4件・全てPEAD系譜: pead_initial_gap8_vol3×2有効行 / pead_x_max20_10 / pead_x_dev25_10 / pead_gap8_vol3_defer3）→ ユーザー裁定カードへ。
  カード記載: n・CI下限・EV（**v1点推定は意思決定材料にしない**。v2片側下限があれば併記・なければ「v2未算出」と明記=A5対応）・
  家族（`trial_fingerprints.json` の family を引用=A6対応）・holdout 棄却済み系譜との関係・推奨1文。
  **前向き枠への追加はこのフローの範囲外**（§6付記II の窓・α規律に従う別手続き。カードは「追加を検討するか」までしか言わない=A14対応）

ドライラン実測（2026-08-08・v0.2規則）: R0=2 / R1=1 / R2=17 / R3=9 / R4=45 / 未整理=4（計78・全数一致検算済み）。

### 2-4. 可視化（隠すのではなく分母ごと見せる）

- **paper_today「稼働状況」に常設1行**（生成主体 `scripts/daily_screen.py` に追記＝B6対応・表示専用・判定無関与）:
  `α消費 114試行 / pending 78: 整理済み74・前向き接続17・未整理4（90日SLA超過 0）` の形式。
  分母（累積試行数）を常時可視＝「使ったαを画面から消す」方向に働かせない（A12/B4対応）
- `build_recipe_shelf.py` / `build_daily_reco.py` は**変更しない**（現画面は trials の pending を表示しておらず、
  daily_reco の pending は別概念の `pending_entry`。誤配線リスクのみで益なし=A7対応・最小変更）

### 2-5. 再発防止

- **SLA**: pending 行は **append から90日以内**に resolution 行が付くこと（レビュー日基準でなく行単位=A13対応）
- **監査**: `scripts/kpi_pending_resolutions.py --audit`（読み取り専用）が未整理一覧と経過日数を出力し、
  **SLA超過があれば exit 1**。朝ジョブ（daily_screen 経由）で毎日実行され、超過は稼働状況⚠️と macOS 通知に載る（B7対応=強制力）
- **--apply の不変条件検査（G1/G2機械化=A9対応）**: 実行前後で trials.jsonl の行数と sha256 を照合・不一致は FATAL・resolutions を書かない
- **catalog 修正提案（検収後に反映・canonical-update-propagation 適用）**: catalog:372-381 を本設計で更新
  （stale数値 53/61→78/114 の訂正・5値語彙・90日SLA）。同時に「`params.resolution_path` を新規試行に記録」の既定方針は
  **resolutions.jsonl への一本化に置換することを提案**（params 内記録は生成時点と意味が整合せず二重正本化する=B8対応。
  この置換は catalog 既定方針の変更なのでユーザー裁定事項）

## 3. 非ゴール

- αの配り方の変更（G3再設計は 2026-07-22 起案棄却=現行FWER維持で決着済み・再訴条件に触れない）
- holdout の再開封・pending への合格付与・trials.jsonl の行編集・分母の増減・前向き枠の増設判断
- 配信品質KPI（指摘6）は別議題のまま

## 4. 1周目の実行手順（検収後・同セッション）

1. `scripts/kpi_pending_resolutions.py` 新設（--audit / --apply。apply は R0〜R4 機械分のみ書く・前後で G1/G2 検査）
2. 78件へ適用（74件が機械確定・resolutions.jsonl 生成）
3. 未整理4件（PEAD系譜）のカード提示 → ユーザー裁定 → resolution 行 append → 78/78 到達
4. `daily_screen.py` に稼働状況1行+audit 配線 → 翌朝ジョブで実走確認
5. catalog:372-381 更新（+ params.resolution_path 置換の裁定反映）+ 本タスクに結果記録 + 意味単位 commit
