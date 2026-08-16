# news_shock_v1 — 商品名つき供給ショックの前向き観察（**凍結 2026-08-16・Codex 第4審 GO**）

> **位置づけ**: 前向き観察のみ（お金を張らない・成績によるGO判定なし・**trials.jsonl と
> screening_batches.jsonl には不算入**＝専用台帳 `data/news_shock/news_log.jsonl` のみに記録）。
> 入場条件の正本= `docs/price-watch-universe.md` §16u（2026-08-16 オーナー裁定「作る」）。
> 正式検定に載せる場合は、前向きデータが貯まった後に catalog §7-X 単発事前登録＋Codex GO を別途経る。

## 1. 仮説（凍結）

商品名つき供給ショックのニュース（禁輸・輸出制限・スト・鉱山事故・不可抗力・攻撃による供給支障）は、
当該商品の**価格直結型（符号+）受益銘柄**の株価上昇に先行または同時進行する
（銅ケース 2026-08-04〜07 の後ろ向き観察 n=1 に基づく仮説・だから前向きで測る）。

## 2. 発火条件（凍結・実装= scripts/news_shock_collect.py）

- 収集: Google News RSS・18クエリ（`configs/news_shock.json` queries）・1日2回（07:20 / 19:00 JST）
- 判定: **商品名 AND 凍結語彙**の両方をタイトル+要約に含む記事のみ（vocab v2・
  strike 系語彙は「strike gold / struck gold」を含む本文では無効＝慣用句ガード）
- 重複排除: (term, link) で永続・台帳は append-only
- 帰属: series_ids → 受益カード（`configs/price_universe_sources.json` の sign=+ かつ
  confirmed/provisional）のみ。ニュース本文からの銘柄直接推定は禁止（§16u 規則2）
- **版の焼き込み**: 全発火行に `config_version` と `config_sha`（config の sha256 先頭12桁）を保存
  ＝語彙変更後も母集団を機械で分離再現できる（Codex 指摘対応・実装済み）
- **凍結 sha256**: configs/news_shock.json = `57ba12012285d04b5877d9edd065156e7c57bbc7bcb977548646bb53bdced282`（凍結 2026-08-16）。本ファイル自身の sha は
  `tasks/news_shock_lane.md` に記帳（自己参照を避けるため）
- **観測開始境界**: 凍結時刻より前の台帳行は存在しない（凍結時に台帳を初期化して開始）
- 語彙・クエリの変更は config version を上げて**新バージョンとして並走**（過去行の再解釈禁止）

## 3. 成績の測り方（凍結・評価行の追記仕様・実装= scripts/news_shock_eval.py 済み）

- **基準日規則（一意）**: 発火 run_at(UTC) を JST へ変換し、**時刻が 09:00 より前なら当日・
  09:00 以後なら翌日**を起点に、営業日（`data/jquants/bars` のファイル実在日）まで繰り上げた日。
  エントリー相当値=その日の**始値 AdjO**（07:20 定時発火→当日寄り・19:00 定時発火→翌営業日寄り。
  手動実行もこの同一規則で処理される＝look-ahead なし）
- AdjO 欠損（売買停止等）は**翌営業日へ1日だけ繰延**。繰延先の営業日データが未到来のうちは
  **結論を書かず保留**（次回 run で再評価）・到来してなお欠損なら `evaluation_skipped` 行で確定
- **測定**: エントリー始値 → +5 / +20 営業日の終値 AdjC。**TOPIX は entry_day（繰延後の実エントリー日）
  の終値→評価日終値**（TOPIX に始値系列が無いため・この非対称と起点は凍結して固定）。
  超過 pt = 銘柄% − TOPIX%
- `type: "evaluation"` 行として同台帳へ追記（**(link, code, window) で冪等**・append-only）
- 対象銘柄: 発火行に記録された beneficiaries 全件（選別しない・confirmed/provisional を列で区別）
- **評価器は観測開始前に実装・固定テスト済み**（selftest 7件: 09:00境界×2・式・false_positive 除外・
  AdjO欠損の繰延・繰延先未到来の保留・冪等）。毎 run の末尾で自動実行（runner 相乗り・fail-soft＋失敗通知）。
  仕様と実装が食い違ったら本ファイルが正

## 4. 読み取り（凍結）

- 初回読み取り: **発火 20 件到達 or 2026-11-16（3ヶ月）のどちらか早い方**。記述統計のみ
  （発火数・偽陽性率の目視分類・超過ptの分布）。しきい値判定はしない
- 6ヶ月（2027-02-16）で継続/停止をオーナー裁定に付す
- 偽陽性（供給ショックでない記事）は台帳に **`{"type": "false_positive", "link": <発火行のlink>}`**
  の別行を後から追記して記録（元行は書き換えない・評価器はこの形式のみを認識＝実装と一致）

## 5. 既知の限界（開示）

- RSS は過去に遡れない＝銅ケース自体の再現検証は不可（この仮説は n=1 の後ろ向き観察由来）
- Google News の収録範囲・遅延は未計測（発火時刻 ≠ ニュース発生時刻。pubdate を記録し実測する）
- 英語圏偏重（日本語語彙は入れてあるが日本語クエリは未設計）
- §16d の負の実測（「誰より早く」は効かない）が本レーンにも当てはまる可能性は排除されていない
  ——だから前向きで測る

## 6. v2 追補（凍結 2026-08-16・第3R敵対レビューR1対応・Codex v2審の必須項目を収載）

**v2 の変更**（実装= collector/config・selftest 28件）:
- RSS検索OR句を config `search_or_phrases` へ外出し（v1のハードコード修理）。**全ASCII語彙が
  いずれかの検索句と部分文字列関係を持つことを selftest で機械検証**（カバレッジ保証）
- 語彙追加: 攻撃系（missile/air/precision strike・airstrike・sabotage・blast・explosion）・
  閉鎖系（port/mine closure・suspends/halts operation・suspends export・mine collapse）
- 慣用句ガード凍結: strike gold / strike it rich / blast from the past / silver lining（実疎通で検出）・
  語彙側も単語境界照合（precisiON STRIKE 誤一致の修理）
- 施設クエリ2本（copper_mines 13施設・oil_chokepoints 8施設）。**facilities 21施設と or_terms は
  1:1 整合を selftest で機械検証**（未クエリ施設ゼロ）。施設クエリでは攻撃語ガードの設備語要件を
  施設名一致で充足とみなす
- **event_id（事象バケット）**: sha256(sorted(series)|vocab_family|施設subject|pubdate UTC日)[:12]。
  ⚠️ 真の事象IDでなく**日次バケットの代理**: 同日・同商品・同ファミリーの別事象は施設名が無い限り
  合流し、日跨ぎの継続事象は分裂する（既知の限界）。dup_event=true は同バケット2件目以降

**分析単位の凍結**（Codex 必須項目）:
- 指標は3つを別々に数える: ①発火行数 ②ユニーク event_id 数 ③通知件数（=①でなく②系）
- **§4「初回読み取りの発火20件」= ユニーク event_id 数**と定義変更（v1の曖昧さ解消）
- 代表行= 各 event_id の**最初の非 false_positive 行**。評価器は各 event_id につき代表行だけを
  評価し、先頭行が後日 FP 化されたら**次の非FP行へ自動で繰り上がる**（dup_event は情報フラグであり
  評価の除外条件ではない。v1行は event_id が無いため従来どおり行単位＝後方互換）
- false_positive 指定は **link 単位**（同 event_id の他行には自動では及ばない。バケット全体を
  除外する時は各行の link に対して行を追記する）

**v1/v2 の分離**（凍結）:
- 分離条件= `config_version` **と** `config_sha` の両方（行に焼き込み済み）
- v1 の観測3行は保持・再解釈禁止・**v2 の件数に混入させない**
- v2 開発中の dry-run 行は台帳から除去済み（v2 観測は下記凍結時刻から）

**凍結記録**:
- v2 観測開始時刻（UTC）= `2026-08-16T12:33:03Z`
- configs/news_shock.json (v2) sha256 = `99fc5ce4af43c36311842b46dce31f862d3ab8c01aafbf9d5490e8628de6f85d`
- 凍結対象= config 全体（search_or_phrases・vocab・vocab_families・facilities・queries）＋
  collector の慣用句/攻撃語ガード定数。本ファイル自身の sha は tasks/news_shock_lane.md に記帳
- 日本語語彙は en-US クエリ構成では取得経路なし＝日本語クエリ新設（将来のv3）まで未稼働と明示
