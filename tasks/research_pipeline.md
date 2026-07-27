# Grok API インフルエンサー勝率リサーチ パイプライン

## ステータス: 全フェーズ完了（ホライズン到来待ち）
**最終更新**: 2026-03-13

---

## 実装タスク

| # | タスク | ステータス | 備考 |
|---|--------|-----------|------|
| 0 | インフラ整備 (requirements, docker-compose, config) | ✅ 完了 | xai-sdk, pydantic追加、XAI_API_KEY環境変数追加 |
| 1 | Grok Discovery エクステンション | ✅ 完了 | extensions/tier1_collection/grok_discoverer/ |
| 2 | SignalExtractor モジュール | ✅ 完了 | collector/signal_extractor.py |
| 3 | 営業日計算ユーティリティ | ✅ 完了 | collector/business_days.py |
| 4 | ResearchStore & ResearchScorecardBuilder | ✅ 完了 | research_store.py, research_scorecard.py |
| 5 | オーケストレータスクリプト | ✅ 完了 | scripts/research_influencers.py |

## 検証タスク

| # | タスク | ステータス | 備考 |
|---|--------|-----------|------|
| V1 | Python構文チェック (全7ファイル) | ✅ 完了 | 全ファイル OK |
| V2 | JSON/YAML検証 | ✅ 完了 | config_schema.json, extension.yaml OK |
| V3 | Docker build | ✅ 完了 | xai-sdk 1.8.1, pydantic 2.12.5 インストール確認 |
| V4 | Phase 1 (discover) 実行 | ✅ 完了 | 18候補発見 |
| V5 | Phase 2 (collect) 実行 | ✅ 完了 | 3人分52ツイート収集 (purazumakoi:8, kabuknight:6, susakisiki:38) |
| V6 | Phase 3 (evaluate) 実行 | ✅ 完了 | xAI Grok API使用、4シグナル抽出 (6085.T x3, 7203.T x1) |
| V7 | Phase 4 (report) 実行 | ✅ 完了 | scorecard.json + report.html 生成 |
| V8 | HTMLレポート ブラウザ確認 | ✅ 完了 | 構造・データ整合性確認済み (2026-03-13) |

## 解決済みブロッカー

### xAI API クレジット未購入 → 解決済み
- クレジット購入完了。全Phase動作確認済み

### ANTHROPIC_API_KEY → 不要化
- SignalExtractor を xAI Grok API (grok-3-mini-fast) に変更
- XAI_API_KEY のみで全Phase動作可能

## 次のアクション

### ホライズン到来後の再評価
- **5BD**: 2026-03-18〜20（最短で3/18に最初の結果が出る）
- **20BD**: 2026-04-08〜10
- 再評価コマンド: `docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase evaluate`
- その後: `--phase report` でレポート再生成

## 再評価手順（ホライズン到来後）

```bash
# 5BD結果: 2026-03-18以降 / 20BD結果: 2026-04-08以降
source .envrc
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase evaluate
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase report
```

## フルパイプライン再実行手順（新規候補でやり直す場合）

```bash
source .envrc

# 1. インフルエンサー発見
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase discover --keywords "日本株 高配当" "グロース株 成長株"

# 2. ツイート収集（上位3人）
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase collect --max-collect 3

# 3. シグナル抽出+評価
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase evaluate

# 4. レポート生成
docker compose -f docker-compose.vnc.yml run --rm -e XAI_API_KEY="$XAI_API_KEY" xstock-vnc python scripts/research_influencers.py --phase report
```

## 変更ファイル一覧

### 既存ファイル変更
- `requirements.txt` — xai-sdk>=0.4.0, pydantic>=2.0 追加
- `docker-compose.yml` — XAI_API_KEY (xstock, setup)
- `docker-compose.mac.yml` — XAI_API_KEY (xstock, setup)
- `docker-compose.vnc.yml` — XAI_API_KEY
- `collector/config.py` — DISCOVERY_CONFIG, RESEARCH_KEYWORDS
- `configs/extensions.enabled.yaml` — tier1.grok_discoverer

### 新規ファイル
- `collector/business_days.py`
- `collector/signal_extractor.py`
- `extensions/tier1_collection/grok_discoverer/__init__.py`
- `extensions/tier1_collection/grok_discoverer/extension.yaml`
- `extensions/tier1_collection/grok_discoverer/config_schema.json`
- `extensions/tier1_collection/grok_discoverer/extension.py`
- `extensions/tier1_collection/grok_discoverer/research_store.py`
- `extensions/tier1_collection/grok_discoverer/research_scorecard.py`
- `scripts/research_influencers.py`

---

## 2026-07-26 打ち止め再検証（X検索精度・最小実行版）— 確定知見

> 出所: plan `~/.claude/plans/kind-questing-scone.md`（敵対レビュー2系統で縮小承認）。
> スクリプト: `scripts/_tmp_c0_censoring.py` / `_tmp_a_lite.py` / `_tmp_c_lite.py`（使い捨て・untracked・
> 全数値は Codex 独立再計算で照合済み、onset 窓の暦日バグ1件を検出→修正済み）。
> 標本注記: uncovered=188投稿/150アカ（新規144人）は急騰銘柄の事前窓収集＝条件付き標本。
> 記述的トリアージであり PASS 認定には使わない。corpus=57アカ/1,538投稿（20251001〜20260717）。

### 結論: 打ち止め維持を支持（偽陽性説を確定・見逃し説は起点近傍で不支持）

1. **偽陽性説の確定（A-lite）**: 散弾（1投稿3銘柄以上）除外＋スパム指紋（テンプレ語彙6アカ・
   数学装飾文字4アカ）適用後、uncovered 150アカの pass_episodes（独立episode≥2）遷移は
   PASS→FAIL 10 / PASS→PASS 3 / FAIL→FAIL 137。**散弾群の before PASS 10アカは after 0 に全滅**。
   生存3アカは手読みで全て非予測（製錬所訪問回想/優待報告/利確事後報告）＝
   **クリーン後の実質的な事前コール者はゼロ**。「当てた」ように見えた主体は散弾水増しで説明できる。
2. **見逃し説は起点近傍で不支持（C-lite・完全観測 episode 限定 primary）**: 真の起点 t0
   （120営業日最安値翌日・episode_start より中央値117日前）直前の窓では corpus 言及がほぼ無い:
   **origin_10bd = 0/208 episode、origin_21bd = 2/190**（current 窓は 28/307）。
   起点で仕込みを語る発信者は手元コーパスに存在しない。
3. **onset 窓（t0〜episode_start 前日）は 37/199 episode・新規8銘柄**を拾うが、手読みでは
   大半がニュース転載・結果報告・列挙スレ・目標株価テンプレ（nikkeisignal 等）で、真の事前テーゼは
   drdebuneko のエンプラス（6941）ピア・キャッチアップ等ごく少数。窓長 exposure 差の注記あり。
4. **副産物（次プラン候補・恒久実装するなら）**:
   - スレッド型散弾（1投稿1-2銘柄×同日多数）が per-post 指標をすり抜ける: 同日 distinct codes≥5 が
     uncovered 29アカ / corpus 20アカ。per-day 指標の併用が必要
   - NFKC バグ実在確認: 数学装飾数字（𝟒𝟓𝟖𝟔等）が direct_codes を素通り（uncovered に4アカ）。
     採点器の恒久修正は「NFKC 前 styled 判定→フラグ」方式が敵対レビュー推奨
   - corpus の codes/post は direct のみ最大2だが社名解決込み最大4（散弾閾値の前提訂正）
5. **recall は本検証の対象外**（手元データは発見済み投稿のみ。未発見アカ・未取得投稿は測れない）。
   検索網の拡張（57アカ制約）を再検討する場合は盲検 recall 監査を別途事前登録すること。

### 次アクション裁定待ち

- 推奨: **打ち止め維持**（発掘レーンの供給は FDR / 急騰前兆モデル優先を継続）
- 代替1: 採点器の防御的恒久修正のみ実施（NFKC styled フラグ・per-day 散弾指標・遷移表方式。
  ゲート定義は変えない小玉）
- 代替2: onset 帯の標的新規収集（起点〜急騰間の発信者を狙う。ただし本検証で発信自体が僅少と
  実測されたため期待値低）

---

## 2026-07-26 半導体高騰メカニズム検証（X値上がり投稿→eBay sold→株）— 確定知見

> 出所: workflow wf_f71d3e59-719（Ground 4 + Verify 2 agents・415k tokens）+ 設計書v0への精密敵対レビュー。
> 全数値は agent 実測（repo）または出典URL付き外部ソース。

### 結論: 仮説チェーンは「順序を1箇所修正すればワークする」

1. **実測時系列**: DDR5スポット+307%（2025-09起点・TrendForce）→ X投稿/秋葉原小売波及（2025-10・PC Watch 14,700→32,700円）→ **日本株の急騰集中は2025-12〜2026-04**（手元426件実測: 電気機器月次 4→2→10→14→25件ピーク・この4ヶ月で64/101件=63%）。「去年10月から」のユーザー観察は株の主要動意より約2ヶ月早く有効だが、最速は現物価格指数（さらに約1ヶ月先行）。
2. **チェーン検証（敵対レビュー）**: X投稿→eBay sold は共通原因（スポット高騰）の並列症状で因果ではない / eBay sold→機関の需要可視化は実証なし / 「取引量増→高騰」は論理不成立（出来高は方向を持たない）。**修正版: 現物スポット/契約指数＋ガイダンス上方修正が主シグナル・X投稿とeBay soldは過熱ゲージ（サイクル後期の利確側シグナル）として逆利用**。
3. **X検出手法（今日から可・追加課金0）**: 固定クエリバッテリー方式（値上がり/品薄語彙×カテゴリシード30-50本・since/until 1日窓固定・min_faves 2段0/100・-filter:nativeretweets）→日次件数zスコア（7-14日+曜日補正）→ tracer型高頻度巡回へ昇格。min_faves はWeb検索UI限定＝Cookie経路のみ可（公式APIは演算子非対応・従量$0.005/readで月$1,500級）。実装は x_search_collect_twittora クエリ差し替え+keywords_ledger 台帳型+x_watchlist_tracer 転用。
4. **eBay sold の機械取得**: 正規API（Marketplace Insights）は個人ほぼ不可・遡及90日。スクレイプはToS違反リスク。セラーなら Terapeak（無料・sold 3年・手動UI）が合法最良。補完: TrendForce公開週次/Yahooリアルタイム急上昇/BCNランキング/Keepa(Amazon・€19/月〜)。
5. **レーン⑦（theme_real_price）設計書v0への精密敵対レビュー裁定: 独立レーン新設 NO**。
   - M1: Stage0 veto が件数ベースで交絡（semi_nピーク2026-03=25はtotalピーク61と同月共動・シェアは19.8→24.8%微増のみ）→ シェア/非半導体対照＋ON翌月以降の超過に要修正
   - M2: 実効n=ON月数（期待約1.2ヶ月/年）は§6付記III最下層スナイパー枠（月5.6）を2桁下回り機械降格が着手前確定＋文脈条件trialの宿主（正式合格KPI）が0本＝**現行規約で正式合格への出口が無い**
   - M3: 日経半導体株指数30∩TOP500 マッピングは動機の核（JDI+478%/太陽誘電+176%等の非構成中小型）を捨てる疑い（構成表未実測・AI推定）→ G1に捕捉率実測必須
   - M4: スポット価格と株のリード/ラグ実測はrepoにゼロ・無料遡及ソース（2016〜週次）の実在も未確認 → **次の1手は30-60分のソース一次確認のみ・確認前のファイル新設全凍結**（前例: Yahoo規約違反でUS Tier1停止 7aeff16）
   - M5: ④TDnet（¥0・数千イベント/年・ticker直付き・PIT済）に証拠量/工数比で2-3桁劣後
   - 推奨=吸収A: レーン行・事前登録文書なし。ソース確認→取れたら受動記録2ファイルのみ。正式検証は正式合格1本目 or TDnet正式登録時に文脈条件ペア対照（§7-J・α非消費）で起こす / 代替=吸収B: 完全見送り・TDnet設計に半導体セクター層別を1本
6. corpus_all の欠落は実質2.5ヶ月（2025-11=0・2026-05=0・2026-06=1件・検証者実測）＝X投稿数の過去検証不可の根拠強化。

### 次アクション裁定待ち（ユーザー）

- 吸収A（推奨・レビュー裁定）: DRAM現物指数の無料遡及ソース一次確認（30-60分タイムボックス）→ 取れた場合のみ受動記録の最小2ファイル
- 吸収B: 見送り・④TDnet の表題マッチKPIに「半導体/電子部品×業績修正」層別を組み込み
- X値上がり検出クエリバッテリーは裁定と独立に実装可（検出ツール/過熱ゲージ用途・0.5-2日・追加課金0）

### 2026-07-27 M4「現物価格の無料遡及ソース」一次確認（タイムボックス実施・統括役）

**結論: 週次のDRAM現物指数を無料で遡及取得できる経路は、今回の確認範囲では『取得不能 or 未確認』。月次の代理指標（物価指数）なら無料入手の可能性が残る。**

| 候補ソース | 結果 | 出所 |
|---|---|---|
| **FRED**（半導体/メモリ PPI・月次） | **取得不能**: 検索結果ページ・データtxtとも **HTTP 403**（bot対策）。ブラウザなら閲覧できる可能性はあるが、**自動取得は今回の経路では不可＝未確認** | fred.stlouisfed.org（2026-07-27 実行） |
| **firecrawl 経由の横断検索** | **ツール実行失敗**（2回連続） | 本セッション実測 |
| **日銀 時系列統計データ検索（CGPI）** | **フラットファイルの無料DLは有り**と明記（登録要否は未記載）。ただし**電子部品・半導体の細目の有無／最古年は未確認** | stat-search.boj.or.jp（2026-07-27 取得） |
| **日銀 CGPI リリースページ** | 提供は**PDFのみ**（CSV/フラットファイルの記載なし）。2020年基準=2022年〜／2015年基準=2019-2022。**品目別の半導体細目は本文PDFを開かないと不明** | boj.or.jp/en/statistics/pi/cgpi_release（同上） |

**M4への回答**: 「スポット価格と株のリード/ラグ実測」に必要な**週次・2016年〜の無料遡及系列は確認できなかった**。日銀CGPIは月次かつ細目未確認で、DDR5スポット+307%のような**現物スポットの粒度には届かない可能性が高い**（推測:）。
**⇒ 事前宣言どおり、レーン⑦向けのファイル新設は行わない（凍結を継続）。** 吸収A の前提（無料遡及ソースが取れる）は**現時点で満たされていない**ため、実質 **吸収B（見送り・TDnet側に半導体セクター層別を1本入れる）** が既定路線となる。
※未確認事項（次に誰かが調べるなら）: 日銀CGPI品目別CSVの半導体細目・最古年／FREDをブラウザ経路で取得できるか／TrendForce等の公開週次の遡及可否。**確認できるまでレーン⑦は起こさない。**

### 2026-07-27 追補: 卸値→株のリード/ラグ実測（吸収A一次確認の結果）

> 出所: workflow wf_93445736-5b0（TrendForce公開記事から DDR4 8Gb 週次スポット28点抽出・算術整合済 /
> J-Quants 週次系列 5銘柄×81週欠損0）。スクリプト= `scripts/_tmp_dram_leadlag_stock.py`（untracked）・
> 系列CSV= `output/price_watch_research/stock_weekly.csv`。

1. **卸値の実測タイムライン**: 第1波（DDR4 EOL）2025-03〜07 $1.45→$5.2、8月調整、**底=2025-09-03週**、
   **第2波起点=2025-09-10週**（+3.31%→9/17 +6.26%→9/24 +6.88%）、以後2026-07まで上昇（$41〜50）。
2. **ラグ実測（算出=日付差÷7・前提=spot変曲点とepisode_start・確度=概算/n=2波・勝者5銘柄選択）**:
   - 第1波: 卸値急加速6/11 → キオクシアep 8/7 = **8.1週** / SPE群ep 8/20-22 = 10.1週
   - 第2波: 卸値再上昇9/10 → KOKUSAI 11/21 = 10.3週 / キオクシア12/5 = 12.3週 /
     電気機器の月次立ち上がり12月 = 11.7週 / ADT 12/26 = 15.3週
   - X投稿増（10月中旬・間接証拠）= 卸値+5.0週
   - **結論: 卸値→株の急騰は約8〜15週（2〜3.5ヶ月）遅行。X検出（卸値+5週）経由でも株の主要動意まで
     約5〜10週の余裕がある** ＝ price_watch ツールの先行余地を実測で裏付け
3. **無料データ源の判定（Tier 2）**: 2016年〜の長期週次は**無料では取れない**（DXI/TrendForce履歴とも有償。
   TradingView/investing 非掲載を実確認）。無料の遡及限界= TrendForce記事の〜2023年後半。
   → レーン⑦のθ凍結（2016-2024の90pct）は無料経路では実行不能＝レーン正式化は独立データ確保が前提のまま。
   前向き記録の無料経路は TrendForce 週次記事 or GitHub スクレイパー堆積（2025-11〜・再配布物で規約リスク中）。
4. 注意: 大型SPE株の13週+30%は第1波起点（6/27-7/18）から既に点灯しており「株が卸値第2波に先行」した面もある
   （逆通説と整合）。ラグは「各波の卸値変曲→急騰episode認定」の向きで測った値。ピーク週は2026-06-07に
   右端打ち切りあり。

### 2026-07-27 追補2: 出遅れスクリーン単体はエッジを生まない（3/3棄却・5チェック関門の実戦検証）

> 出所: workflow wf_c70349ce-dc4（3銘柄並列深掘り+敵対検証・一次資料PDF実読・数値照合15箇所一致）。
> 半導体出遅れスクリーン（_tmp_semi_laggard_scan.py・27銘柄）上位3件に ②ドライバー③感応度⑤エッジ を適用。

- **トリケミカル4369**: ②PASS（半導体売上95%）③PASS（持分法正常化で経常+13〜16%概算）**⑤FAIL**
  （モルガン4回増額・コンセンサス+30%=公知 / 会社自身が減益ガイダンス=中国CXMT消費減+SK JV利益減）→ 棄却。
  唯一の再エントリー候補（条件: コンセンサス下方転換 or 信用残42倍の解消後に再スクリーニング）
- **信越化学4063**: **②FAIL**（Q1実測: 電材+188億を塩ビ−180億がほぼ全額相殺=「塩ビで売られる株」。
  ウエハは長期契約でスポット高が流入しないと会社明言）→ 棄却
- **HOYA7741**: **②FAIL ③UNKNOWN**（EUVブランクスは「高位安定」=メモリ景気非連動と会社説明・半導体単独利益非開示）→ 棄却
- **確定知見**: 「セクター大相場×13週/52週低い」の出遅れスクリーンで浮く銘柄は**全件、遅れる構造的理由があった**
  （5713の教訓の再現・n=4/4）。5チェック関門は誤認を全件検出＝関門側の実効性は実戦確認。
  エッジは screen では作れず、「新規の実物価格変曲を市場より早く観測した瞬間」（price_watch の本線）にのみ発生しうる。

### 2026-07-27 追補3: 部品④（eBay 実物価格の自動監視）接続完了 — pokeca-invest 流用

- 実装先: `~/Desktop/biz/pokeca-invest`（scripts/hw-sold-weekly.ts + data/hw-sold/queries.json・commit は pokeca 側）
- 方式: ハードウェア8クエリ（DDR5/DDR4/RTX50xx/4090/SSD/Ryzen9）の **active 出品 ask 中央値**を週次台帳化
  （タイトル適合 regex・外れ値除去・battery_sha 凍結指紋・newly-listed 順固定）
- **重要実測: eBay の sold(落札済み)一覧は 2026-07 時点でログイン/認証壁**（"Sign in"/"Please verify yourself"）
  ＝無認証の sold 自動取得は不可。sold の正路はセラーの Terapeak 手動。
  ※副作用: pokeca 既存の PSA10 sold 系パイプラインも同壁で現在壊れている可能性が高い（pokeca 側の課題として申し送り）
- 初回実測（2026-07-27・7/8 ok）: DDR5 32GB kit $863.53・DDR5 64GB $803・RTX5090 $4,101・Ryzen9 9950X $677
  （DDR5 はチップスポット $50.8×16≒$813 と整合＝計測器として妥当）
- 週次運用（手動1行・実行場所 pokeca-invest）:
  `docker compose run --rm -v "$PWD/src/data:/app/src/data" scraper bash scripts/run-psa10-ebay.sh scripts/hw-sold-weekly.ts`
- 3点照合: 業界卸値（TrendForce 週次・最先行）→ X 投稿件数（influx price_watch・+5週）→ eBay ask（本レーン・小売末端）

### 2026-07-27 追補4: X値上がり監視のカバレッジ裁定（2AI敵対レビュー・独立一致）

> 出所: adversarial-review 軽量（A=同モデル別文脈 / B=異モデルGPT系・独立実行）。議題「固定30クエリ方式で急騰銘柄を全て特定できるか」。

- **判定（両者一致）: 否。全特定は原理的に不可能**。急騰426件の主因の大半（両者推定65〜80%）は
  実物価格でない（決算/業績修正・AIテーマ・TOB/資本政策・政策・医薬イベント・踏み上げ需給）。
- 上限概算（両者とも係数はAI推定・概算明示）: 実物価格主因は426件の約2〜3割 → X可視・語彙・z≥3・
  マッピングの直列リークで**実用検出上限は全体の1〜2割**、5チェック通過まで含めるとさらに減る。
- 検出器固有の穴（一致）: 商品語彙外（原油/ニッケル/パルプ等）・B2B市況はXに出ない・z≥3は
  バースト専用（じわ高は永遠に鳴らない）・「在庫逼迫/納期/引き合い」等の非価格表現・帰属チェーンの長さ。
- **裁定（一致）: 「全て特定」は目標として不適切。本方式は「商品市況起点レーン」として特化し、
  別レーン（①一次市況データ直監視=Xより約5週早い ②TDnet/EDINET開示監視=最大の型を直接捕捉
  ③株価・出来高の全銘柄逆引き）と並走させるのが正**。
- B独自の先行課題: 426件の急騰原因を100〜150件手作業分類しないと各レーンの上限・投資優先度が測れない。
- A独自の較正課題: 5チェック⑤エッジ基準は「screenで見えるものは織り込み済み」を含むため、screen由来の
  本パイプライン候補を構造的に全棄却しがち（実戦0/4）。エッジ判定の再較正は前向き記録とセットで検討。
