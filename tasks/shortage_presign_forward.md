# 品薄予兆の前向き測定（P-INF-43 b）— Wikipedia 閲覧数の週次見張り ＋ 公式告知・業界予測の RSS を news_shock 系に足し、60 日で空振り率を測る

- Phase: P-INF-43（親= P-INF-42 遡り測定 `tasks/archive/shortage_presign_event_ledger.md`・4 モデル議論 vault `02_Ai/influx/notes/influx-shortage-presign-4model-2026-10-09.md` §9）
- 裁定: 2026-10-10 オーナー「b」（vault `wiki/meta/decisions.md` 2026-10-10 エントリ・日報 P-INF-43）
- 設計レビュー: 2026-10-10 Codex gpt-6-astra（read-only・指摘 14 件・全件反映＝§進捗の対応表）

## 答える問い

オーナー原文: 「b」＝ P-INF-43 の選択肢 b **「a の Wikipedia 閲覧数の週次見張り ＋ 公式告知・業界予測（TrendForce・メーカー IR・業界紙）の RSS 収集を既存 news_shock 系に足し、前向き 60 日で〈発火数／発火後 60 日以内に ① が起きた割合（空振り率）〉を測る」**（① = 店頭・フリマ・EC で品薄・値上がり）。

## 対象と数え方

- **源= 4 行で報告**: Wikipedia 閲覧数（S-C の昇格）／発行元 RSS・公式告知（メーカー・官公庁の feed）／発行元 RSS・業界予測（調査会社・業界紙の feed）／Google News ja 検索（一般媒体経由・族は語彙で付与）。オーダーの「公式告知・業界予測の RSS」は、発行元 feed が取れる源と媒体経由にしか取れない源（任天堂・ソニー等は RSS なし）に分かれるため、公式性を機械で保証できる前者と後者を別行にする（混ぜると「公式告知」の意味が変わる）。
- **母集団（発火）**: known_date（実際に知った日＝便の実行日 JST）が **[2026-10-13（火）, 2026-12-12) の 60 日**に入るエピソード。それ以前の週・記事は基準線づくりのみ。各発火は known_date+60 日で採点（遅着データの猶予 14 日）＝最終集計は 2027-02-24 まで。
- **分母**: 〈的中＋空振り〉。〈追認（① が発火より先）〉〈未確定〉〈未観測（計器の台帳に穴）〉〈採点不能（計器なし）〉は分母から外し件数を報告する。
- **単位= エピソード**（Wikipedia= 題名ごと・初発火の週から 56 日以内は同一／RSS= 〈源の種別×主題×族〉ごと・初 hit の known_date から 7 日以内は同一・連鎖させない）。併せて〈独立した ① の件数（distinct onset）〉〈主題数〉も報告する（DRAM 4 題名が同じ ① に当たる等、エピソード数は独立試行でない）。
- **件数突合**: 便ごとに〈渡した題名数／判定できた題名数〉〈クエリ数・feed 数／取得できた数〉を run_summary に残し、欠測は「事象なし」と区別する（rules/10 検証戦略）。

## 成功基準

1. 事前登録（本ファイル §事前登録 ＋ `configs/wiki_pv_watch.json`・`configs/presign_forward.json`・`configs/news_shock.json` の `presign` ブロック）と実装が **初便（2026-10-13 09:30）より先に commit** されている（手動の疎通便は dry-run か、台帳を書かない形で行う）。
2. `scripts/wiki_pv_watch.py` の疎通便で全登録題名の week_obs（judged の有無つき）＋ run_summary が出る（渡した題名数＝判定できた題名数＋error 数）。
3. `scripts/news_shock_collect.py --lane presign` の疎通便で run_summary が出て、**既存レーン（`--lane` 省略）の挙動・台帳・`--selftest` が不変**。固定反例 4 件（§事前登録 B）がテストで非発火／negated になる。
4. `scripts/presign_forward_eval.py` が事前宣言行を 1 回だけ書き、採点は冪等（同じ (source, episode_id, spec_version) の確定行は 1 行）。固定 fixture で〈hit／miss／confirm_after／pending／unobserved／unscorable〉と lookback_partial・複数計器 OR・比較率が再現する。
5. Docker 一括ゲート（`python -m unittest discover -s tests`）が既存 768 件（2026-10-10 実測）＋新規で全通過。
6. 文書同期: `influx-architecture.md` §4・§7、`docs/pipeline-map.md`（機械再生成）、CLAUDE.md 主要コマンド。

## 影響範囲

- 新規: `scripts/wiki_pv_watch.py`・`scripts/wiki_pv_watch_run.sh`・`configs/wiki_pv_watch.json`・`config/launchd/com.influx.wiki-pv-watch.plist`・`scripts/presign_forward_eval.py`・`configs/presign_forward.json`・`tests/test_wiki_pv_watch.py`・`tests/test_news_shock_presign.py`・`tests/test_presign_forward_eval.py`・台帳 `data/wiki_pv_watch/ledger.jsonl`・`data/news_shock/presign_log.jsonl`・`data/shortage_presign/forward_eval.jsonl`・報告 `output/shortage_presign/forward_report.md`
- 変更: `configs/news_shock.json`（`presign` ブロック追加のみ・`version: 2` と既存キーは 1 バイトも変えない）・`scripts/news_shock_collect.py`（`--lane presign` の追加・既定経路は不変）・`scripts/news_shock_run.sh`（presign 便を fail-soft で追加）・`influx-architecture.md`・`docs/pipeline-map.md`・`CLAUDE.md`

## 変更禁止ファイル

- `data/shortage_presign/events.json`（P-INF-42 の結果＝凍結）
- `scripts/news_shock_eval.py`・`scripts/price_universe_check.py`・`scripts/price_watch_forward.py`・`scripts/flea_panel_track.py`・`configs/flea_panel.json`・`configs/price_universe_sources.json`・`configs/x_shortage_map.json`
- `data/news_shock/news_log.jsonl` の既存行・`data/price_watch/forward_log.jsonl`・`data/price_watch/universe_weekly.jsonl`・`data/flea_panel/*`（読むだけ）

## 全体図

```
[源]                                      [台帳]                                   [採点]
Wikipedia PV API ─ wiki_pv_watch.py ─▶ data/wiki_pv_watch/ledger.jsonl ──────┐
  (火曜 09:30・launchd 新設)                                                   ├─▶ presign_forward_eval.py ─▶ data/shortage_presign/forward_eval.jsonl
Google News ja RSS（42 主題）┐                                                 │        ▲ ① の計器（値から状態〈成立/不成立/不明〉を作る）   output/shortage_presign/forward_report.md
発行元 RSS/Atom 6 本 ────────┴ news_shock_collect.py --lane presign ─▶ presign_log.jsonl ┘  ├ data/flea_panel/daily.jsonl（消費財・相対 1.2 倍・事象日= window）
  (既存 com.influx.news-shock 07:20/19:00 の runner に同乗)                               └ data/price_watch/universe_weekly.jsonl（B2B・週次= 値の 1.2 倍／月次= 前年同月比 +20%）
```

- 担い手: 収集 2 本はホスト `/usr/bin/python3`・標準ライブラリのみ（news_shock と同じ作法）。採点は wiki runner の末尾（週 1）と手動。テストは Docker。
- 止まる線: API 429/5xx は Retry-After で 6 回まで→題名ごとに status=error 行（便は続行）。Google 側 0 件連続は run_summary の items で見える（recall 崩壊）。採点は計器の台帳に穴がある日を「不明」として扱い、① なしと混同しない。
- 既知の穴: ① の計器は消費財 22 クエリ（flea_panel）と B2B 系列（price_universe）に限られ、たまごっち・HDD・CPU は採点不能。消費財の ① は「固定クエリの成約中央値の上昇」という代理指標（型番・容量の構成変化でも成立する）。企業名の題名（ニコン等）は企業ニュースでも鳴る（空振りとして数える）。flea_panel は 9/23 開始のため 10/28 以前の発火は追認の確認が部分的（lookback_partial で可視化）。

## 事前登録（2026-10-10 固定・結果を見てから変えない。変更は version を上げ、変更日以降の発火にだけ適用）

### A. Wikipedia 閲覧数の週次見張り（`configs/wiki_pv_watch.json` v1）

- API: `…/per-article/ja.wikipedia/all-access/user/<title>/daily/<start>/<end>`（UA 固定・1 題名 1 要求/便・直近 10 週分）。429 は Retry-After で 6 回まで。
- 週= 月曜〜日曜（UTC 日付）。7/7 日そろった週だけ判定。**発火規則**: 週合計 S ≥ 2.0 × 直前 8 週（7/7 日・最低 6 週）の中央値 B、かつ B ≥ 200/週。
- 判定の一意キー= (title, week_start)。便ごとに「取得範囲内で week_end ≤ 前日 UTC」の未判定週を全て判定し、**初回判定が最終**（PV の後日訂正・再実行で覆さない）。7/7 日そろわない週・基準 6 週に満たない週は judged=false で記録し、10 週の取得範囲内なら後の便で再判定、範囲を出たら unjudgeable で確定。
- known_date= 判定した便の実行日（JST）。**score_eligible（鮮度条件）= week_end ≥ known_date−8 日 の週だけ**（初便が過去 10 週をまとめて判定する分は基準線＝score_eligible=false）。収集区間（延長込み）の判定は収集器でなく採点器が known_date で行う（2026-10-10 Codex 実装レビュー #1/#4 で明確化＝収集器は presign_forward.json を読まない・延長期間の発火が落ちない・設定不読で黙って false にならない）。エピソードの初発火が鮮度条件外（遅れて判定した古い週）なら、そのエピソードは採点しない（2 発目以降は first_in_episode=false・遅れて見つけた急騰は予兆として扱わない＝意図どおり）。
- エピソード= 題名ごと・初発火の week_start から 56 日以内（差 56 日を含む・連鎖させない）。fire/week_obs 行に config_version・config_sha を焼き込む。
- 題名 50 本（転送は API で解決した最終題名）。主題は細分化（Nintendo Switch 2→switch2／PlayStation 5→ps5／ゲーム機→game-console〈両計器〉／ポケモンカードゲーム→pokeca／遊☆戯☆王→yugioh／ガンプラ→gunpla／ONE PIECE→onepiece-figure 等）＝記事の主題と ① の計器を 1 対 1 に寄せる。除外= 家電 19・電気料金 26・中古車 119・ヤーマン 161（2026-09 週換算・下限未満）／リザードン（一覧ページへの転送）／GeForce 50シリーズ（記事なし）／エアコン・オートバイ・冷凍食品・ハム（計器なし）。
- 便= 毎週火曜 09:30（`com.influx.wiki-pv-watch`・新設 plist・登録はオーナー）。基準線は 2026-08-03 から。

### B. 公式告知・業界予測の RSS（`configs/news_shock.json` → `presign` ブロック v1・`news_shock_collect.py --lane presign`）

- 取得経路 2 種（新しい実行経路は作らない）: ①Google News RSS 検索（ja/JP・`when:2d`）× 主題 42 本（主題語 OR）×（検索句 16 本 OR）＝ source_class `gnews-media` ②発行元 RSS/Atom 6 本（2026-10-10 到達実測）: TrendForce Semiconductors feed（10 件）／Samsung Newsroom（50）／SK hynix Newsroom en（10）／NVIDIA Newsroom（4）／鶏鳴新聞（10）／経産省リリース Atom（25）＝ source_class `feed-official`（maker/government）と `feed-industry`（industry/trade-press）。到達しなかった源（記録）: TrendForce `/presscenter/rss` 404／Micron・WDC・Sandisk IR 403／Kioxia・Nintendo・Sony・TakaraTomy・Bandai RSS 404or403／農水省 403／日経 404。
- 判定順: ①主題語（ASCII= 単語境界・日本語= 部分一致・`exclude` 語〈ジャパン・パンダ・たまごっち 等〉を本文から除いてから照合）②凍結語彙（全一致語を記録）③方向語が要る語彙（契約価格・価格予測・forecast・outlook・contract price・QoQ・persist）は方向語（上昇・不足・increase・rise・hike・tight・shortage…）を伴う時だけ有効 ④否定語（見送り・撤回・据え置き・値下げ・下落・to fall・decline・drop・cut…）を含む記事は status=negated で台帳に残し発火にしない。**固定反例 4 件**: 「ジャパン航空が運賃の価格改定」→パン非該当／「たまごっちの抽選販売」→鶏卵非該当／「パンの価格改定を見送り」→negated／「DRAM contract price forecast to fall 10% QoQ」→negated。
- 語彙族（凍結）: official_notice（値上げ／価格改定／生産終了／出荷停止／出荷調整／供給不足／抽選販売／購入制限／入荷未定／販売休止／受注停止／price increase／price hike／end of life／EOL／discontinue／allocation／supply shortage／supply constraint／lottery）／industry_forecast（価格予測／上昇見通し／上昇見込み／値上がり見込み／契約価格／forecast／outlook／contract price／to rise／QoQ／persist）。「高騰」は実況語なので入れない。族の付与は source_class ごと（feed-official は official 語彙優先・feed-industry は forecast 語彙優先・gnews は official 優先）・全該当族を families_all に残す。
- 同一性: article_id= sha(source_id|正規化 URL)・重複排除の鍵= **(source_id, 主題, 正規化 URL)**＝同一記事でも主題ごとに 1 行（feed 1 本が多主題を持つため・2026-10-10 実装者の照会で明確化・初便前）・便をまたぐ再書き込みは防ぐ（別源は別行）。known_date= 最初に取得した便の JST 日付（以後不変）。pubdate は RFC822/ISO8601→UTC 日付・解釈不能は便の日付＋pubdate_parsed=false。event_id= sha(主題|族|pubdate 日付|source_class)。エピソード= (source_class, 主題, 族) ごと・初 hit の known_date から 7 日未満（day0〜6 は同一・7 日目から新規・連鎖させない。Wikipedia の「56 日以内（含む）」とは別規則・2026-10-10 Codex 質問 1 で明記）。`exclude` 語は本文照合だけに使い検索クエリには入れない。実装の判定順は ①主題語→④否定語→②語彙→③方向語（否定語は type=negated のラベルを決めるだけで hit の有無は変わらない・同 質問 4）。判定に使った title+desc の先頭 300 字を台帳に残す。
- 便= 既存 `com.influx.news-shock`（07:20／19:00）の runner に presign 便を fail-soft で追加（launchd 変更なし）。**収集開始日ゲート**: known_date < `presign_forward.json` scoring.collect_interval.start（2026-10-13）の間は presign 便をスキップ（exit 0・台帳に書かない・dry-run は走る）。設定が読めなければ非ゼロ終了（commit 直後の便が区間前の hit とエピソードを台帳に残さないため・2026-10-10 明確化）。台帳= `data/news_shock/presign_log.jsonl`（`news_log.jsonl` には書かない）。

### C. ① の前向き定義（`configs/presign_forward.json` v1・主題→計器は 1 か所）

- **状態モデル**: ① は事象日ごとの状態〈成立／不成立／不明〉。**開始日（onset）= 成立の日 t で、直前 lookback（日次・週次 14 日／月次 45 日）内の観測に成立が無く不成立が 1 つ以上ある日**（不明は不成立に数えない）。lookback が全て不明なら onset_unknown。成立が続く間は同一の ①。
- **消費財（flea_panel）**: 事象日= `window`（成約対象日）・観測可能日= `date`。x_t= `sold_median`（`sold_n_top100` ≥ 3 の日・それ以外は不明）。m_t= t−6〜t の x の中央値（最低 4 日）。b_t= t−63〜t−8 の x の中央値（**最低 14 日**＝9/23 開始のため 28 日では 10/28 まで判定不能になる）。成立 iff m_t ≥ 1.2×b_t。`list_prices` に定価がある query は m_t ≥ 1.2×定価（P-INF-42 の ① と同形・**2026-10-10 時点 登録なし**= Switch 2 の公式価格ページは JS 描画で機械取得不可・PS5 本体は 55,000 円版と標準版が混在）。
  - オーダーとの差: P-INF-42 の ① は「定価の 1.2 倍」。前向きは**自前基準線の 1.2 倍**で代用する（宣言済みの既定値。初便より前なら定価登録で切替可・以後は version 2 として変更日以降の発火にだけ適用）。これは「固定クエリの成約中央値の上昇」の代理指標（構成変化でも成立）と承知のうえで ① と扱う。
- **B2B・週次系列（price_universe）**: `universe_weekly.jsonl` の status=ok 行（同一 (id,date) は最後の行）。x_t= value。b_t= t−63〜t−8 の値の中央値（最低 4 観測）。成立 iff x_t ≥ 1.2×b_t。**本発火行（forward_log firing/rise）は使わない**＝米・鶏卵・小麦・コーヒーは上昇アラートが無効（閾値 999）で本発火が出ない・本発火は開始日でも非成立の記録でもない。
- **B2B・月次系列**（rice-aitai／egg-tokyo-m／cgpi-*）: 同一 (id, src_date) は最初に観測した行（公表を知った日）。台帳に 2026-08 以降しか無く 12 か月中央値が作れないため、収集器が源の履歴から計算済みの **前年同月比 yoy_pct ≥ +20%** を成立（1.2 倍と同義）。null は不明。
- 計器なし主題（tamagotchi・hdd・cpu-logic）: 採点不能（発火数のみ）。

### D. 60 日採点と合格線（`scripts/presign_forward_eval.py`・事前宣言行を 1 回だけ台帳へ）

- **計器ごとの判定表**（K= known_date）: 1. K 以前の lookback 内の最後の判定可能日が成立、または onset ∈ [K−28, K] → confirm_after（① が先）／2. (K, K+60] の最初の成立の開始が onset（開始日あり）→ hit（lead_days= onset−K・検出遅延= 観測可能日−事象日 を別記）／2b. その開始が onset_unknown（lookback 全不明＝開始日を特定できない成立）→ confirm_after（onset_unknown=true・① が先の可能性を否定できないため hit/miss の分母外。初便前の v1 明確化 2026-10-10・S の解釈 1「hit にも confirm にも数えず miss/pending 側へ」を置換＝成立しているのに miss になる矛盾を除く）／3. 採点日 < K+60 → pending／4. (K, K+60] の判定可能観測が十分（日次 ≥42 日・週次 ≥6・月次 ≥2）→ miss／5. 採点日 < K+60+14 → pending（遅着猶予）・以後 unobserved。lookback が全て不明なら confirm_after にせず 2 以降へ進み lookback_partial=true。
- **採点対象の発火**: fire/hit 行の first_in_episode かつ（wiki は）score_eligible（鮮度）かつ known_date ∈ その源の収集区間（延長された源は延長区間）。区間判定は採点器が行う（Codex 実装レビュー #1）。
- **複数計器の束ね**: OR。いずれかの計器が hit → hit（確定）／それ以外でいずれかが pending → pending（未確定・後日 hit に変わり得るため confirm_after でも確定しない）／全計器が確定した後の優先= confirm_after > miss > unobserved（2026-10-10 Codex 質問 3 で旧「hit > confirm_after > pending > miss > unobserved」を置換・pending > miss は不変）。計器なし= unscorable。hit の onset は最も早い計器。題名の `instruments_override` があればそれを使う。
- **状態遷移**: pending → {hit, miss, confirm_after, unobserved} の 1 回だけ。確定行は (source, episode_id, spec_version) で 1 行。
- **比較率**: 同じ主題・同じ計器・同じ判定表を、収集区間内の毎週火曜 T を疑似発火（K=T）として適用し hit/(hit+miss) を主題ごとに出す。源の比較率= その源の発火数で重み付けした加重平均（追認除外・OR・観測可能性を発火側と揃える）。
- **合格線（源ごと・今決める）**: 採点できた (H+M) ≥ 5 かつ 的中率 ≥ 50% かつ 的中率−比較率 ≥ 20 ポイント → 合格＝**見張りを続ける運用上の便宜基準であって有効性の証明ではない**（N=5 では偶然でも 3/5 以上が出る確率 ≈ 32%）。報告に Wilson 95% 区間・distinct onset 数・主題数を併記。(H+M) < 5 → 判定不能。≥ の比較は浮動小数の誤差許容つき（7/10−1/2 のような境界例を落とさない・Codex 実装レビュー #3）。
- **区間と延長**: 収集= known_date ∈ [2026-10-13, 2026-12-12)。2026-12-12 に源ごとの採点対象エピソード数（pending 含む）< 5 なら、その源だけ 2027-02-10（排他）まで 1 回延長。最終採点= 最後の発火の known_date+74 日。採点スクリプトは wiki runner の末尾（週 1・収集が失敗しても走る）と手動で走る（冪等・台帳は `.eval.lock` で排他＝Codex 実装レビュー #2）。

### E. 台帳スキーマ（実装者はこの形で書く）

- `data/wiki_pv_watch/ledger.jsonl`: `{"type":"week_obs","run_at","known_date","title","subject","week_start","week_end","days","sum","base_median8","base_n","ratio","judged":bool,"reason"|null,"fired":bool,"score_eligible":bool,"episode_id"|null,"config_version","config_sha"}` ／ `{"type":"fire","run_at","known_date","title","subject","week_start","week_end","sum","base_median8","ratio","episode_id","first_in_episode":bool,"score_eligible":bool,"config_version","config_sha"}` ／ `{"type":"error","run_at","title","error","config_version","config_sha"}` ／ `{"type":"run_summary","run_at","known_date","titles_planned","titles_ok","titles_error","weeks_judged","weeks_pending","weeks_unjudgeable","fires","new_episodes","fetch_start","fetch_end","config_version","config_sha"}`（weeks_pending／weeks_unjudgeable／fetch_start／fetch_end と error 行の版は実装で追加・2026-10-10 validator 質問 2）
- `data/news_shock/presign_log.jsonl`: `{"type":"hit"|"negated","lane":"presign","run_at","known_date","source_class","source_id","subject","matched_term","matched_vocab","matched_vocab_all","vocab_family","families_all","article_id","event_id","dup_event","episode_id","first_in_episode","title","desc_snippet","link","pubdate","pubdate_utc_date","pubdate_parsed","presign_version","config_sha"}` ／ `{"type":"error","lane":"presign","run_at","known_date","source_class","source_id","error"}` ／ `{"type":"run_summary","lane":"presign","run_at","known_date","queries_planned","queries_ok","feeds_planned","feeds_ok","items":{...},"hits","negated","new_episodes","presign_version","config_sha"}`
- `data/shortage_presign/forward_eval.jsonl`: `{"type":"preregistration","spec_version":1,"registered_at",...§C/§D の写し}` ／ `{"type":"score","scored_at","source_class","episode_id","subject","known_date","verdict","final":bool,"onset_date"|null,"instrument"|null,"lead_days"|null,"detect_lag_days"|null,"lookback_partial":bool,"onset_unknown":bool,"instrument_verdicts":{...},"spec_version"}`（2026-10-10 規則 2b で onset_unknown を追加・排他= `data/shortage_presign/.eval.lock`）（pending は便ごとに上書きせず最新 1 行だけ保つか、確定時のみ追記＝final=true の行は 1 つ）／ `{"type":"comparison","scored_at","subject","tuesdays","hit","miss","rate","spec_version"}`

## 実装タスク（SubAgent 3 本・並列・1 本 1 タスク・いずれも事前登録 config を読むだけで変更しない）

1. **W**: `scripts/wiki_pv_watch.py`（S-C 昇格・`_tmp_presign_backfill.py` の `wiki_daily/weekly_sums/s_c_wiki` を §A の規則どおり移植・`--dry-run`〈台帳を書かない〉`--date` を持つ）＋ `scripts/wiki_pv_watch_run.sh`（末尾で `presign_forward_eval.py` を fail-soft 実行）＋ `config/launchd/com.influx.wiki-pv-watch.plist`（火曜 09:30）＋ `tests/test_wiki_pv_watch.py`
2. **R**: `scripts/news_shock_collect.py --lane presign`（RSS/Atom 両対応・既定経路不変・`--dry-run`）＋ `scripts/news_shock_run.sh` 追記 ＋ `tests/test_news_shock_presign.py`（固定反例 4 件を含む）
3. **S**: `scripts/presign_forward_eval.py` ＋ `tests/test_presign_forward_eval.py`

## 進捗

- 2026-10-10: 裁定 b → 既存収集器の実読・到達性実測（RSS 27 URL・Wikipedia 61 題名）→ 事前登録 v0 → **Codex gpt-6-astra 設計レビュー 14 件（scratchpad `codex_design_review.md`）を全件反映**:
  | # | 指摘 | 反映 |
  |---|---|---|
  | 1 | 米・卵・小麦・コーヒーは上昇アラート無効で本発火が出ない | B2B ① を値から判定（週次= 1.2 倍・月次= yoy +20%）に変更 |
  | 2 | 本発火行は開始日でも非成立の記録でもない | 同上・状態〈成立/不成立/不明〉モデルを導入 |
  | 3 | flea の助走不足・不明の扱い | 基準 最低 14 日・不明は不成立に数えない・lookback_partial・猶予 14 日 |
  | 4 | 区分が排他でない・複数計器の規則なし | 判定表 1〜5 の順序・OR と優先順位・主題細分化（switch2/ps5…）・題名の instruments_override |
  | 5 | 比較率の条件不揃い | 同手続きの疑似発火（毎週火曜）・発火数で加重 |
  | 6 | N≥5 は弱い・独立性 | 合格線を「運用継続の便宜基準」と明記・Wilson 区間・distinct onset・主題数を併記 |
  | 7 | 公式/予測を語彙だけで分けると意味が変わる | source_class（feed-official／feed-industry／gnews-media）で別行・族は全該当を記録 |
  | 8 | ジャパン→パン等の誤発火・無方向語 | exclude 語・否定語・方向語の 3 規則＋固定反例 4 件 |
  | 9 | flea の date/window で反転 | 事象日= window・観測可能日= date・検出遅延を別記 |
  | 10 | RSS エピソード・known_date・記事同一性 | 7 日は初 hit 起点で非連鎖・article_id・pubdate UTC・解釈不能の扱い |
  | 11 | Wikipedia の再取得・欠便回収 | (title, week_start) キー・初回判定が最終・10 週の回収上限・score_eligible |
  | 12 | 凍結順序・版・再検証 | 初便より先に commit・fire 行に版と sha・desc_snippet・(source, episode_id, spec_version) キー |
  | 13 | 相対 1.2 倍は構成変化でも成立 | 代理指標と明記・capped は無関係・不明の扱い |
  | 14 | 10/13 は火曜・区間・延長の運用 | 半開区間 [10/13, 12/12)・延長判定日 12/12・最終採点日・採点の実行担当 |
- 2026-10-10 夜: 事前登録 commit 754bcb5（重複排除鍵の明確化 9233216）→ 実装 3 本（W: `scripts/wiki_pv_watch.py`+`wiki_pv_watch_run.sh`+`config/launchd/com.influx.wiki-pv-watch.plist`+`tests/test_wiki_pv_watch.py`／R: `news_shock_collect.py --lane presign`+`news_shock_run.sh` 追記+`tests/test_news_shock_presign.py`／S: `presign_forward_eval.py`+`tests/test_presign_forward_eval.py`）→ 初便前の v1 明確化 1 件（判定表 2b: 窓内の最初の成立が onset_unknown → confirm_after・分母外）→ **Codex gpt-6-astra 実装レビュー 1 回目（scratchpad `codex_impl_review.md`）: Critical 4・Warning 3・質問 5 を全件反映**:
  | # | 指摘 | 反映 |
  |---|---|---|
  | C1 | wiki の score_eligible が収集区間を AND＝延長期間の発火が全て採点外 | score_eligible は鮮度のみ・区間（延長込み）は採点器が known_date で判定（§A/§D・config 2 本の文面も） |
  | C2 | 採点器にロック無し＝同時実行で確定行が重複 | `.eval.lock` を読込前〜報告後まで排他 |
  | C3 | 合格線 20pt が浮動小数で落ちる（7/10−1/2） | 誤差許容つき比較＋境界テスト |
  | C4 | wiki が presign_forward.json 不読で黙って false を確定 | 依存を削除（C1 と同時に消滅） |
  | W1 | runner が収集失敗で採点器に届かない | rc 保存→採点器→rc で exit。疎通用 `PRESIGN_DRY_RUN=1` |
  | W2 | pu_monthly.source・id_field 未接続 | 消費 or 拒否 |
  | W3 | runner 連結・通知の証拠不足／presign selftest は `--lane presign --selftest` | 疎通便を runner 経由で実走・証拠を訂正 |
  | Q1 | RSS 7 日の境界 | 7 日未満（day0〜6）・wiki は 56 日含む（§A/§B 文面） |
  | Q2 | 初発火が鮮度外のエピソード | 採点しない＝意図どおり（§A 文面） |
  | Q3 | confirm_after と pending の束ね | hit 確定／pending は未確定／確定後 confirm_after > miss > unobserved（§D・config） |
  | Q4 | exclude の適用範囲・否定語の順 | 本文のみ・ラベルの差のみ（§B 文面） |
  | Q5 | 既存台帳の差分の帰属 | 定期便の追記＝本件のスクリプトは presign_log/ledger/forward_eval しか書かず dry-run で未作成を実測 |
  | 追加 | commit 直後の shock 便で presign 便が区間前に走る | 収集開始日ゲート（§B）＝10/13 より前はスキップ exit 0 |
- 次: Codex 2 回目（修正の確認）→ Docker 一括 → 疎通便 3 本＋runner 連結 → 文書同期 → commit → オーナーへ launchd 登録コマンド提示。

## Session Handoff

- 次: 実装→Docker 一括ゲート→疎通便（dry-run・台帳を書かない）→文書同期→commit→オーナーへ launchd 登録コマンドを提示（`launchctl load` は README どおりオーナー実行）。初便 2026-10-13 09:30 より前に全て commit 済みであること。
