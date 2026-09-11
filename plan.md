# influx プロジェクト半年計画 (2026-05 〜 2026-10)

**採用構成**: 株インフルエンサー投稿の**収集・分類・投稿インフラ**に責務集中
**策定日**: 2026-04-18（初版）/ 2026-04-22（責務分離リファクタ、記事生成系は `make_article` プロジェクトへ移管）
**対象期間**: **M0 (2026-04-20 開始、緊急対応)** → M1 (2026-05) 〜 M6 (2026-10)

---

> ## 🚨 現在地マーカー (2026-04-22)
>
> **最上位前提「勝率インフル投稿の到達性」の復旧中。** 現状:
> - ✅ T0.1 Cookie 更新完了（2026-04-21、両アカウント `import_chrome_cookies.py` 経由で認証成功）
> - ⏳ T0.2 非活動チェック再実行（`python scripts/check_inactive_accounts.py`）
> - ⏳ T0.3 Grok 20BD 再評価 + score 算出
>
> **次アクション**: `## M0` セクションの T0.2 から継続。

---

## 本プロジェクトの責務（プロジェクト間の分界）

| 責務 | 所管プロジェクト |
|---|---|
| **X インフル投稿の収集・ノイズ除去・7 カテゴリ分類・勝率評価** | **influx（本プロジェクト）** |
| **Cookie 運用・`daily_pipeline.py`・`check_inactive_accounts.py`** | **influx** |
| **投稿インフラ（`compose.py`・`review.html`・`run.py`・`account_routing`・`impression_tracker`）** | **influx** |
| **長文記事生成・教師データ蓄積（Material Bank / Pattern Library）・良記事テンプレ設計・10000 imp KPI** | **`make_article` プロジェクト**（`/Users/masaaki_nagasawa/Desktop/biz/make_article/`） |
| **Grok アーキテクトスタイル学習・3 テーマ戦略（investment / tech_tips / ceo_perspective）** | **`make_article`** |
| **連携①（make_article → influx）** | make_article `/post-article` → influx の `run.py`（予約投稿 API） |
| **連携②（influx → make_article）** | 分類済ツイート素材 API（M2 以降で実装予定・タスク未起票）＋ 読者反応 JSONL（T6.4 成果物 `output/reader_feedback.jsonl`、Pattern Library 還流用） |
| **運用アカウント戦略（`@twittora_` 主軸・`@kabuki666999` 準主軸、bio・投稿頻度・KPI）** | 横断 SSoT [`../account_strategy.md`](../account_strategy.md)（本 plan.md には数値を重複させない。M4 日次パイプラインは SSoT の頻度に従う） |

---

## 目的

**株で勝つ情報を収集することに特化する**（2026-08-29 オーナー裁定）。X の投稿・商品価格の系列・企業の開示から「投資判断に効く一次情報」を集め、勝てる候補を出せる状態を保つ。
**非ゴール**: 記事の執筆・長文生成（→ `make_article`）／投稿の予約・実行・アカウント運用（→ `autopost`・2026-05-01 に物理分離）／集めた投稿の7カテゴリ分類（旧パイプライン＝2026-08-29 に退役判断・精度は不合格のまま停止）。
現況の機能は `influx-architecture.md`（X収集基盤）と `influx-stock-algo-architecture.md`（株アルゴ研究）が正本。**本ファイルの以下 M0〜M6 は 2026-04 時点の旧構成（収集＋分類＋投稿インフラ）前提で、現行と一致しない。**

### 肝ツリー v1（叩き台・2026-09-11 全資産棚卸しで中・小を肉付け・検収待ち。v0= オーナー「OK・記録して」同日・敵対レビュー wf_4a5d7fcd-a17 一致7・報告= vault 02_Ai/influx/reports/influx-kimo-tree-adversarial-review-2026-09-11.md）

幹は「目的に対して何を増やすか」で切る。**市場（日本／米国）と全体／個別は幹にせず属性タグ**（3体一致: 市場と向きは直交属性＝幹にすると海外受益カード15枚が2本の幹に属し、KPI と多重比較の分母は日本株専用の単一台帳で共有できない）。幹・枝の言葉はオーナー原文 2026-09-11 を逐語で置き、AI が動かした箇所は〔AI〕。中・小は repo 全資産（scripts 248・tasks 67・docs/configs 331・data/output 60・vault 26）の棚卸しから紐付けた**既存物の指し先のみ**（本文は各正本・全件表は vault 報告書 §付録）。**下の M0〜M6・成功基準表は 2026-04 旧構成＝現在値の根拠にしない。**

```
株で勝つ情報を収集する（目的）
├ 前提（幹の外・心構え）: 忍耐がまず必要／買い時ではなく「未来的にこうなるからナンバーワン株が1番上がるはず」と考える／勝てるものを買えば良い
│   └ 受け皿0（repo・vault とも該当ノートなし＝ここにだけ書く）
│
├ 大A 共通化する部分
│   ├ A1 データを整備して傾向を見る〈原文〉
│   │   ├ 土台部品: measure_base_rate.py（+20%到達率の正本・66本が import）／kpi_features.py／collector/business_days.py／x_trade500_universe.py（取引高TOP500 母集団）
│   │   ├ 銘柄→中心価格の台帳: data/center_pin/center_pin.jsonl 977社（08-15）・output/center_pin_types.md
│   │   └ 網の面積: coverage_census.py → output/coverage_census.md（08-19 で古い・手動）
│   ├ A2 一次情報の収集（X投稿・商品価格・企業開示）〔AI: 敵対レビュー一致①〕
│   │   ├ X投稿: price_watch_collect（毎日22:10・50クエリ・ledger 2271行）／sedori_trend（月曜09:00）／recollect_account（インフル前向き・火/月）／price_watch_discover（日曜10:40・新商品名の候補キュー）
│   │   ├ 商品価格: price_universe_check（月曜11:00・69系列・universe_weekly 1645行）＋monthly_sources／tokyosteel_scrap／food_bridge_fetch／news_shock_collect（RSS・07:20/19:00＋2h probe・20クエリ・news_log 1583行）／driver_discover_boj（手動・日銀CGPI/SPPI）
│   │   ├ 企業開示・需給: tdnet_index_fetch（毎朝07:15・index 969週分）／edinet_fetch（平日18:45・大量保有）／jsf_daily_archive（平日12:30/19:30・日証金4系統）／jq_fetch（J-Quants・手動＋ライブラリ）
│   │   └ 米国〔属性タグ〕: us_price_fetch.py（**定期未配線・07-26 で停止**・Yahoo 規約違反確定 us-tier1:226）／config/us_universe_seed.json 96・AI仮置き／us_watchlist（火曜10:30・インフル投稿のみ）
│   ├ A3 収集の健全性（止まった系統が0か）〔AI 追加・棚卸しで INBOX 未処理9件中5件がここに集中〕
│   │   ├ 系統の生存表: 14系統（定期10・生存10・停止疑い1= 米国株価・定期なし3）→ 報告書 §付録 data
│   │   └ 既知の故障: okasira-forward が毎回0件で success／評価パーサ（bookmarks_keyword_common.py:557・期限超過）／xbuzz-tracer 空振り／food_bridge_fetch.py:66 が TDnet 直叩き／research-weekly・tob-monthly・kpi-loop-weekly 未ロード
│   └ A4 属性タグ（幹にしない）〈原文〉: 全体の動き or 個別／日本株 or 米国株／受益の型（center_pin pin_type）／海外上場（foreign_forward・15枚・対TOPIX台帳に入れない）
│
├ 大B 上昇サインだけを検知する
│   ├ B1 大きく変動しそうな KPI を見つける〈原文〉
│   │   ├ 台帳: docs/stock-algo-kpi-catalog.md §2 66本（未検証45／pending8／fail7／reject4／枠F候補1／死に筋確定1）
│   │   ├ 生成器 39本（稼働1・手動38）: 価格系 strev/high52/range_breakout/sh_dip／決算系 PEAD/進捗率/上方修正/SUE／需給系 margin/shortcover/shortup_lowrise／イベント系 activist/tob/event_batch/round23〜38／出来高 volshock（v1〜v3）
│   │   └ pending の主戦: volshock_5x lift2.34／shortcover_turn 2.29／進捗率 2.20／strev 1.83／uprev 1.72／high52 1.52／S高初押し 3.08（n=246）
│   ├ B2 200日線〈原文・上昇側のみ〉: 200日線奪回クロス= **fail**（lift0.95）／200日線位置フィルタ= pending（チャンピオン構成の一部）／200週線乖離帯= 記述測定完了（tasks/sma200w_descriptive.md）
│   ├ B3 商品価格・品薄・ニュースからの上昇サイン〔AI〕
│   │   ├ X値上がり z判定: price_watch_alert（watch_log 47行）→ price_watch_forward（forward_log 82行・凍結v1）
│   │   ├ ニュース供給ショック: news_shock（事前登録凍結 2026-08-16・前向き中）
│   │   ├ 受益カード: configs/x_shortage_map.json 38 subjects（5関門・機械validate）／§16w 海外15枚／§16h「X投稿の波→2段目の上昇に有効」／§16c 言及レーンは「先行しない」
│   │   └ 不採用の記録: 納期逼迫（§16p）・在庫3本（§16t）・電力（§16b）・医薬二次波及（pharma-secondary-map 3連鎖不成立）
│   └ B4 仮説の供給〔AI〕: harvest_master_posts（名人投稿→仮説）／catalog §8 バックログ／architecture「仮説の在庫化= **現在停止**」／候補ファクトリー kpi_screen_batch= **現在禁止**
│
├ 大C 売るタイミングを検知する／下がりそうな条件（保有株の売却とショート用）
│   ├ C1 保有株の降り方〔AI: 既存で唯一実測済み〕: E1= SMA200 割れ翌朝手仕舞い／E3= β調整ストップ（catalog §7-A 第12周・固定%より優位）・kpi_exit_study／sue_exit_study／round15_e1・paper_eval の -8%損切り＋20営業日クローズ
│   ├ C2 リリースされる大きなニュースが無くなった〈原文〉: **実体0**。素材= news_log／first_seen_probe（「出現」の逆＝枯渇は未定義）
│   ├ C3 大口が全部売った〈原文〉: tasks/bigflow_preregister.md（立花 e支店 API・照会待ち・前向き0行）／data/activist_dictionary.json（07-07）／EDINET 変更報告の減少側は未使用
│   ├ C4 X で呟かれる回数が非常に増えている〈原文〉: x_mention_extract（毎日22:10・mentions 2714行・mention_alerts 133行）— 売りサインとしては未検証（§16c「ほぼ後追い」）
│   ├ C5 新規株限定でロックアップ期間〈原文〉: **実体0**（上昇側で不採用 catalog:2718・下落側は未着手）
│   ├ C6 全体の動き〈原文〉: TOPIX 200日線レジーム= reject（bull限定ゲート）／analyze_calendar_effects「節目×ブレッドス警戒日」（手動）／材料例= Yahoo マーケットAIトピックス
│   └ C7 買わないための負けフィルタ〔AI: catalog §4 15本＋§2 11本〕: 信用買残パンパン= reject／公募増資・MSワラント・立会外分売・日々公表・増担保解除・空売り価格規制・MAX効果・IVOL・信用評価損益率= **9/11 未検証**
│
├ 大D 勝てそうなモデルを構築する
│   ├ D1 買っているインフルエンサーの株に対しての KPI 収集〈原文〉
│   │   ├ 前向き: fxnia-forward（月曜10:30）／okasira-forward（月曜11:15・**0件バグ**）／us_watchlist（火曜・rolling14日・凍結仕様「ロングのみ」）／nia_youtube_rss
│   │   ├ 採点器: influencer_candidate_score／leaderboard／rescore／pick_profile（手動・台帳不算入）・vault influx-influencer-ledger（08-04 手貼り）
│   │   ├ KPI 化: catalog §2-G 7本= **全未検証**（prospective 蓄積待ち）／docs/influencer-winrate-spec.md F1〜F6
│   │   └ 停止・却下の記録: research_weekly（Grok 廃止・未ロード）／逆引き発掘= 偽陽性製造装置（第17R）／自動発見・遡及採点はやらない（2026-07-27）／「勝率と EV は無相関」（winrate-ev-summary §1）
│   ├ D2 1番手の後に2番手・3番手が上がる因果が証明されるかを見る（1番手が米国株の可能性も）〈原文〉
│   │   ├ 検証済み: spillover_backtest= **unsupported**（2026-08-10 終端・符号逆）／kpi_sector_momentum= fail（lift0.81）
│   │   ├ 前向き中: pair_forward（KPI×KPI ペア・277行・正式化は12ヶ月後）
│   │   └ 未着手: catalog §2-F テーマ先行→波及ラグ（テーマ辞書なし）／1番手=米国の検証（米国価格が止まっている＝A2）
│   ├ D3 検定ループの規律〔AI〕: 事前登録 SHA-256 → 敵対レビュー GO → in-sample → verdict → 前向き（architecture §2）・Bonferroni 分母= trials.jsonl 114行（**07-29 で停止**）・resolutions 82・fingerprints dedup・EV estimand v2・**未解決: winrate-ev-summary §7「現行の合格基準は儲からない方を選んでいた」**
│   └ D4 前向き競走〔AI〕: config/paper_watchlist.json 19／ledger 1036行（決着918試合 251勝667敗 平均-2.8%）／tob_ledger 81（累計EV +2.72%）／pair 277／枠S・枠F／初回判定 最短 2027-08／候補 13 family
│
└ 大E 判断材料を毎朝届ける〔AI 追加・オーナー OK〕
    ├ E1 毎朝の面: daily_screen（平日07:30）→ build_daily_reco／build_recipe_shelf／build_shikomi_list → vault ミラー（daily-reco・paper_today・recipe-shelf・paper-ledger 08:45）
    ├ E2 死活監視: kpi_clock_sla（平日08:45・sla_log 41）／run_log 39・hashchain
    ├ E3 読み方の面: vault influx-morning-3min（**07-29 で古い**）／influx-kpi-cockpit（**07-16 停滞**・棚と不一致）
    └ E4 個別銘柄への適用: notes/influx-ihi-7013-add-buy-eval（叩き台）
```

| 大 | 役割1行（何をどれだけ増やせるか） | 現在値の再現 | 2026-09-11 の値 |
|---|---|---|---|
| A | 一次情報が週にいくつ台帳へ届くか。数より「止まった系統が0か」 | 系統の生存表（報告書 §付録 data・`wc -l`＋最終更新日） | 定期10系統中 生存10・停止疑い1（米国株価 07-26）・定期なし3 |
| B | 上昇サインの KPI で正式合格に近づく family 数 | `sed -n '5p' output/recipe_shelf.md` | 候補 13 family 競走中・初回判定 最短 2027-08 |
| C | 売り・下落条件で事前登録まで進んだ本数 | `ls tasks/*preregister*.md` のうち下落側 | 0 本（大口フローは照会待ち・負けフィルタ 9/11 未検証） |
| D | 正式合格 1本→2本（階段式） | `sed -n '5p' output/recipe_shelf.md; wc -l < data/kpi_trials/trials.jsonl` | 実戦投入可 0本／試行台帳 114 行（07-29 停止） |
| E | 毎朝の面が営業日に欠けず出るか | `wc -l < data/monitoring/run_log.jsonl; head -3 output/daily_reco.md` | 39 行・直近 success |

棚卸しで見つかった正本のズレ（§8 へ移送・結論は書かない）: pipeline-map の未ロード4本→実測3本（edinet-tob はロード済み）／architecture:222「us_price_fetch は launchd 登録中」→実際は us_watchlist が呼ぶのは recollect_account と nia_youtube_rss のみ／configs/extensions.enabled.yaml に退役済み9件が enabled／config/fdr_sim_spec.draft.json が FROZEN のまま（起案は棄却済み）／output/research が 07-08 で停止（research-weekly 未ロード）。

## プロジェクトの軸

### 投稿頻度
**1 日 1 投稿**（マルチアカウント運用時は各アカウント 1 投稿/日）

### 成功基準（最重要）
> **有益な情報を大量に仕分け、正確に記事テンプレートへ振り分ける**

| 要素 | 測定方法 | 測定者 | 閾値 |
|---|---|---|---|
| **仕分けの量** | `output/collection_metrics.jsonl` の日次合計 | T1.7 自動 | 日次 ≥ **100 ツイート/日**（M1 でベースライン確定） |
| **分類の精度** | `scripts/measure_f1.py` で 7 カテゴリ macro/micro F1 | T2.0 自動 + 月次 | macro F1 ≥ **0.80**（M2）→ **0.85**（M6） |
| **記事振り分けの正確性** | 月次サンプリング（30 件/月、compose 出力）を人手レビュー | ユーザー or 委託 | 誤振り分け率 < **5%**（M6 T6.5） |
| **収集→素材提供の時間** | `pipeline_log/{date}.jsonl` の `collect_start_at` → `classify_done_at` 差分 | T1.9 自動 | 通常時 ≤ 30 分（収集量に比例） |

### KPI

| 優先軸 | 内容 |
|---|---|
| KPI 1 | 1 日 1 投稿の `engagement_rate` の継続的向上（投稿インフラの質担保） |
| KPI 2 | 分類 F1（7 カテゴリ × Gold Set、月次計測） |
| KPI 3 | 手動運用時間の削減率 |
| KPI 4 | 勝率の高いインフルエンサー特定数（N 名以上、暫定 N=5） |
| 補助 | 日次収集量・ノイズ率・カテゴリ別ドラフト生成数・誤振り分け率 |

**M2 着手ゲート**: Gold Set F1 ≥ 0.80

---

## 7 カテゴリ（株テーマの投資判断シグナル、T1.0 完了）

1. オススメしている資産・セクター (`recommended_assets`)
2. 個人で購入・保有している資産 (`purchased_assets`)
3. 申し込んだ IPO (`ipo`)
4. 市況トレンドに関する見解 (`market_trend`)
5. 高騰している資産 (`bullish_assets`)
6. 下落している資産 (`bearish_assets`)
7. 警戒すべき動き・逆指標シグナル (`warning_signals`)

**カテゴリ → テンプレート対応表**（`collector/config.py` の `CATEGORY_TEMPLATE_MAP`、T1.0 完了）:

| カテゴリ | 主テンプレート | 備考 |
|---|---|---|
| `recommended_assets` | `hot_picks` | 銘柄名含有のみ採用 |
| `purchased_assets` | `trade_activity` | `hot_picks` との重複停止 |
| `ipo` | `hot_picks`（IPO サブ） | 「IPO」必須タグ |
| `market_trend` | `market_summary` | |
| `bullish_assets` | `hot_picks` | `is_contrarian=True` は `warning_signals` へ（逆神方針 2026-04-19） |
| `bearish_assets` | `market_summary` | |
| `warning_signals` | `contrarian_signal` | |
| カテゴリ外（決算） | `earnings_flash` | M5 で 7 カテゴリ統合 or 廃止判断 |

---

## パイプライン（収集 → 素材提供 → 投稿インフラ）

```mermaid
graph LR
  A[インフル投稿収集<br/>collector/] --> N[ノイズ/重複除去]
  N --> B[7 カテゴリ分類<br/>classifier]
  B --> S[有益度スコアリング]
  S --> C[compose.py<br/>短文ドラフト生成]
  C --> D[review.html<br/>承認 UI]
  D --> E[run.py<br/>X 予約投稿]
  E --> F[impression_tracker<br/>1h/4h/24h]
  F -.学習フィードバック.-> C
  F -.F1 監視.-> B

  subgraph "make_article プロジェクト"
    MA[長文記事生成<br/>/generate-x-article]
  end
  S -.素材 API.-> MA
  MA -.完成ドラフト.-> D
```

| ステップ | 実装 | 現状 |
|---|---|---|
| 記事生成（短文） | `extensions/tier3_posting/cli/compose.py` | 7 テンプレート実装済み・稼働中 |
| 記事生成（長文） | `make_article` の `/generate-x-article` | 別プロジェクト、influx は素材 API で連携 |
| 管理画面 | `extensions/tier3_posting/ui/review.html` + `server.py` | カレンダー UI 稼働 |
| 予約投稿 | `extensions/tier3_posting/cli/run.py` | 稼働中、マルチアカウント対応済 |

---

## 前提条件（クリティカル依存）

> **最上位前提**: 勝率が高いインフルエンサーの投稿が取得できないと、株ニュースとして成立しない。
> 低勝率/ノイジーなインフル投稿は下流の分類・compose・投稿がどれだけ高品質でも「参考に値するニュース」にならない。

| 前提 | 説明 | 成立条件 |
|---|---|---|
| **勝率インフル投稿の到達性**（最上位） | 高勝率（Grok `score ≥ 70`、現行 TOP5）が (a) アクティブ投稿、(b) 非公開/凍結/BAN/ブロックされず、(c) Cookie 経由で取得可能 | TOP5 のうち **4 名以上が週次投稿中 + 全員 Cookie 経由で取得可能** |
| **収集量** | 日次で 7 カテゴリに十分なシグナル | 日次 **100 ツイート/日以上** |
| **アクティブインフル** | 登録 30 アカウントの直近 7 日以内投稿 | **20 アカウント以上**アクティブ |
| **インフル鮮度** | 勝率の低いアカウントを除外 + 新規高勝率候補の発掘 | Grok 20BD 再評価を四半期ごと |
| **Cookie の有効性** | 収集 Cookie が失効していない | 期限切れ 7 日前に手動更新（`import_chrome_cookies.py`） |
| **Gold Set** | F1 計測の拠り所 | 35 件（M1）→ 100 件（M6） |

---

## 基本方針

「収集 → ノイズ除去 → 分類 → 有益度評価 → 記事作成 → 管理画面承認 → X 予約投稿 → 効果計測 → 学習」のゴールデンパスに工数の 80% を投下。

**現状の最大ボトルネック**:
1. 上流の収集量・リスト鮮度が計測されていない（T1.7 完了済）
2. 分類精度を測る Gold Set がない → 35 件生成済、人手ラベリング待ち
3. Grok 20BD 再評価が期限超過（M0 T0.3 で再実施）

M1 で土台（カテゴリ整理・Gold Set・収集量モニタリング・Grok 再評価）を一気に固め、M2 で学習ループとノイズ除去を稼働させる。

---

## 現状スナップショット (2026-04-24 更新)

> **アカウント運用状況**（詳細は [`../account_strategy.md`](../account_strategy.md)）:
> - `@twittora_` — 14年休眠中、復活予定（make_article 側 `tasks/account_strategy_kickoff.md` で下書き準備中）
> - `@kabuki666999` — 週2投稿目標（火木 20:00）。bio 改修案1 適用予定（`tasks/kabuki_strategy_sync.md`）

## 現状スナップショット (2026-04-22 更新)

> **⚠️ 最上位前提が崩壊中** — TOP5 特定不能 / Grok 20BD 期限超過 / 到達性未測定。§M0 参照。

### 資産（引き継ぎ可能）

| 領域 | 資産 |
|---|---|
| 分類 | 7 カテゴリ体系 + `CATEGORY_TEMPLATE_MAP`（T1.0 完了） |
| 収集 | `collector/x_collector.py` + 30 アカウント定義 + Cookie 運用 |
| 投稿 | `extensions/tier3_posting/` エクステンション化（compose/run/track/manage） |
| Cookie 運用 | `scripts/import_chrome_cookies.py`（macOS Chrome 抽出、唯一の確実経路） |
| マルチアカウント | `account_routing.py`（`CATEGORY_ACCOUNT_MAP` 自動導出、T5.3 完了） |
| スケジューラ | `scripts/scheduler/{plist, crontab.txt}` |

### パイプライン別

| ステップ | 稼働度 | 課題 |
|---|---|---|
| 収集 | ◯ | 収集量モニタリング稼働（T1.7 完了）、Cookie 期限切れ通知（T1.5 完了） |
| ノイズ除去 | ✗ | **未実装**。M2 T2.3 |
| 分類 | ◯ | 7 カテゴリ統一済（T1.0 完了）、Gold Set 35 件生成済（人手ラベリング待ち） |
| 有益度評価 | ✗ | **未実装**。M2 T2.4 |
| 記事作成 (`compose.py`) | △ | 7 テンプレ稼働、experiment_id 付与済 |
| 管理画面 (`review.html`) | ◯ | `engagement_rate` 表示実装済（T1.4 完了） |
| 予約投稿 (`run.py`) | ◯ | 稼働、CookieExpiredError 対応済、account_routing カテゴリベース化済 |
| 効果計測 | △ | `track.py` 統合済（1h/4h/24h スケジュール）、ダッシュボード未整備 |
| 学習ループ | ✗ | rewrite→few-shot 還流は `make_article` 側で対応 |

### 完了済みタスク

- T1.0 カテゴリ整理 + `CATEGORY_TEMPLATE_MAP` + `apply_contrarian_override`
- T1.1 `track.py` と `impression_tracker` 統合
- T1.2 投稿後 1h/4h/24h スケジュール追跡
- T1.3 `/api/bookmarks` パスバグ修正
- T1.4 `engagement_rate` を `review.html` に表示
- T1.5 Cookie 期限切れ時 `CookieExpiredError` 送出
- T1.6 Gold Set 候補 35 件生成（人手ラベリング待ち）
- T1.7 日次収集量モニタリング（`collection_metrics.jsonl`）
- T2.0 `measure_f1.py` 実装（7 カテゴリ macro/micro F1 + recall 95% CI）
- T4.1 `daily_pipeline.py` 実装
- T5.3 `account_routing` カテゴリベース化
- T6.5 `audit_routing.py` 実装
- T0.1 Cookie 更新（`import_chrome_cookies.py` 経由、2026-04-21）

---

## 依存関係図

```mermaid
graph TD
  M0[🚨 M0 前提崩壊の緊急復旧<br/>Cookie/TOP5/到達性] --> M1[M1 土台固め]
  M1 --> M2[M2 学習ループ + 品質フィルタ]
  M1 --> M3[M3 勝率発見拡張]
  M2 --> M5[M5 テンプレ拡充 + ER 交絡制御]
  M3 --> M5
  M4[M4 daily_pipeline 自動化] --> M5
  M1 --> M6[M6 ダッシュボード + F1 月次]
  M5 --> M6
```

---

## M0: 前提崩壊の緊急復旧 (2026-04-20 開始)
<!-- phase-id: phase-m0 — tasks/m0_execution.md から参照される安定アンカー -->
<a id="phase-m0"></a>

**Why**: 最上位前提が崩壊状態では M1 以降全工程が空転する。

**着手ゲート**: 本セクション Exit Criteria 全項目達成 = M1 着手の前提。

### Tasks

#### P0（即時、前提崩壊中）

- **T0.1** ✅ **Cookie 鮮度更新**（2026-04-21 完了）
  - VNC Playwright 経路は X bot 検知で突破不能と判明 → 旧 `refresh_cookies_vnc.py` / `setup_profile.py` / `setup_from_chrome.py` 削除
  - **確立した唯一の経路**: `scripts/import_chrome_cookies.py`（macOS Chrome + openssl + Keychain）
  - 両アカウント認証成功（Docker 側で x.com/home 到達確認）
- **T0.2** ⏳ **非活動チェック再実行**
  - `python scripts/check_inactive_accounts.py` 実行
  - `output/inactive_check_result.json` 当日付生成、非活動アカウント数を stderr に出力
- **T0.3** ⏳ **Grok 20BD 再評価 + `score` 算出実装**
  - 先行: `python scripts/research_influencers.py --phase evaluate --dry-run --limit 1`
  - `research_scorecard.py` に `score` フィールド算出ロジック追加（例: `score = win_rate * min(trackable / 10, 1.0) * 100`）
  - 本番: `--phase evaluate` → `--phase report`

#### P1（M1 前、ロード保全）

- **T0.4** `INACTIVE_THRESHOLD_DAYS` を 30 → 7 に修正（`collector/inactive_checker.py:19`）
- **T0.5** `INFLUENCER_GROUPS` に `grok_score` / `is_priority` フィールド追加、score ≥ 70 に `is_priority=True`
- **T0.6** `CookieExpiredError` SST 統一（収集系 `x_collector.py` / `inactive_checker.py` にも適用）

#### P2（M1 前、補充フロー半自動化）

- **T0.7** `scripts/promote_grok_candidates.py` 新規: `research_scorecard.json` の score 上位を読み、`INFLUENCER_GROUPS` 未登録の diff を stdout 出力
- **T0.8** `INFLUENCER_GROUPS` に `group_reserve` 予備 5 名追加（`is_active: False` + `is_reserve: True`）

### Exit Criteria（M1 着手ゲート）

1. ✅ Cookie が当日付更新済み、`cookies.json` mtime 7 日以内
2. `output/inactive_check_result.json` が当日付生成、TOP5 のうち 4 名以上アクティブ
3. `research_scorecard.json` に `score` フィールド付与、**score ≥ 50 が 2 名以上**特定
   - 2026-04-24 Decision: 第 2 回パイプライン（3ヶ月拡大）の構造的上限が 60.0（@t_ryoma1985）と判明。score=`win_rate × min(trackable/10, 1.0) × 100` 式で「勝率 70%+ かつ trackable 10+」が必要だが、trackable≥10 群は勝率 31-45% に留まる。70 閾値は現データ構造では不可能と確認 → 閾値 70→50 緩和、必要 5 名→2 名緩和
4. `INFLUENCER_GROUPS` に `grok_score` / `is_priority` 付与、**score ≥ 50 の全員**を `group_grok_top` に明示（現状: @t_ryoma1985 60.0 / @serikura 50.0 の 2 名、tiebreak: `avg_return_pct` 20BD）
   - 2026-04-24 Decision: Exit #3 緩和に連動。構造的上限 60.0 の現データでは TOP5 を埋めても中身は score < 50 の低信頼候補になるため、閾値連動で必要数も 2 名に揃える
5. `INACTIVE_THRESHOLD_DAYS=7` に変更済
6. `CookieExpiredError` が収集系でも raise
7. `group_reserve` に予備 5 名登録

### Cookie 更新 恒久対策（M0 完了後の次フェーズで実装）

**3 層アーキテクチャ**（plan.md M0 → M1 移行時に実装、合計約 3 時間）:

| Layer | 責務 | 追加ファイル |
|---|---|---|
| 1. 早期検知 | 毎朝 Cookie 健全性チェック（残日数 + 実 HTTP 認証）、失効前に macOS 通知 | `scripts/cookie_health_check.py`、`scheduler/com.influx.cookie_health.plist`、`daily_pipeline.py` Step 0 フック |
| 2. 自動更新 | Chrome 全プロファイル走査 + twid 自動照合 + 一括更新の 1 コマンド | `scripts/refresh_all_cookies.py`（`import_chrome_cookies.py` 再利用） |
| 3. フォールバック | Chrome ログアウト時の二次経路 | `scripts/import_safari_cookies.py`（Binarycookies パース） |

**KPI 寄与**: 最上位前提の復元。M1 以降の全計画の土台確保。

---

## M1: 土台固め (2026-05)

**Why**: 成功基準「仕分け × 正確な振り分け」を測る/支える基盤が全て未整備。

**Tasks**（T1.0-T1.7 は 2026-04-18 完了、残 T1.8-T1.9）

- **T1.8** Grok 20BD 再評価 + `config.py` 反映（M0 T0.3 で先行対応）
- **T1.9** 収集→素材提供の時間計測基盤
  - `daily_pipeline.py` 開始時に `collect_start_at` を `output/pipeline_log/{date}.jsonl` に記録
  - `classify_tweets.py` 完了時に `classify_done_at` を記録、差分を `collect_to_classify_sec` として保存
  - 40 分超過で stderr 警告（M4 ゲート）+ ステップ別所要秒を出力

**Exit Criteria**
1. `pipeline_log/{date}.jsonl` に時刻記録が出力される
2. `INFLUENCER_GROUPS` が Grok 再評価結果を反映済
3. M0 T0.1-T0.8 全て完了

**KPI 寄与**: 計測基盤確立、上流品質の安定化

---

## M2: 学習ループ + 品質フィルタ (2026-06)

**着手ゲート**: **Gold Set F1 ≥ 0.80**（T2.0 で計測、未達時は段階的フォールバック）

**フォールバック戦略**:
1. Few-shot 5-10 件追加、Gold Set 50 件化
2. ルールベース重み増、prompt 改善
3. モデル変更（`claude-sonnet-4-6`）+ Gold Set 100 件化
4. ゲート緩和（macro F1 ≥ 0.70）+ ユーザー判断

**Tasks**
- **T2.2** `engagement_rate` 高位ドラフトの自動ブックマーク化
- **T2.3** **ノイズ/重複除去フィルタ + 偽陽性ガード**
  - 同一アカウントの連日同内容（類似度 > 0.9）除外
  - 定型句 denylist
  - `output/noise_filter_audit.jsonl` に通過/除外ログ、月次 100 件サンプリングで誤除去率 < 5%
- **T2.4** **有益度スコアリング**
  - スコア要素: 銘柄名/ティッカー含有 + カテゴリ信頼度 + 重複なし + `min_faves` 超過率
  - 閾値以上のみ `compose.py` 素材に使用
- **T2.5** **A/B テスト基盤（experiment_id）**
  - `compose.py` 生成時にドラフトに experiment_id（template_version + scoring_version）付与
  - `PostStore` → `impressions.jsonl` に experiment_id 記録
  - `scripts/analyze_experiments.py` で experiment 別 ER 集計

**Exit Criteria**
1. F1 ≥ 0.80 を Gold Set で達成
2. ノイズ率 < 10%
3. 有益度スコア上位のみがドラフト素材
4. experiment_id が投稿～インプレッションに一貫付与

**KPI 寄与**: F1 向上、engagement_rate 向上（素材品質改善）、ノイズ率低減

---

## M3: 勝率発見の拡張 (2026-07)

**Tasks**
- **T3.1** `performance_tracker` と `research_scorecard` の統合（Canonical 化）
- **T3.2** `compose.py` の `win_rate_ranking` テンプレートを週次自動生成に組み込み
- **T3.3** 新規勝率候補の月次発掘（Grok 月次サブセット → 3 ヶ月観察後に本組入り判定）

**Exit Criteria**: 勝率 TOP5 が config 反映、`win_rate_ranking` 週次自動生成、月次候補追加フロー稼働

**KPI 寄与**: 勝率インフル特定数 +3 以上

---

## M4: 日次 1 投稿パイプライン自動化 (2026-08)

**Why**: 1 日 1 投稿を安定運用するには収集 → 分類 → 記事作成を 1 コマンド化し、カテゴリ当日 0 件時のフォールバックが必要。

**Tasks**
- **T4.1** `scripts/daily_pipeline.py` 拡張（既存実装をマルチカテゴリ対応化）
  - `collect_tweets` → `classify_tweets` → `noise_filter` → `scoring` → `compose` → `merge_all_dates` → viewer 更新を 1 コマンド
  - **当日カテゴリ 0 件フォールバック**: `weekly_report` / `win_rate_ranking` / 前日高 ER 再投稿案 の順で代替ドラフト生成
- **T4.2** 収集→素材提供の所要時間自動チューニング
  - `pipeline_log` 過去 14 日の時間分布を分析、ボトルネックステップ特定
  - 目標: P95 ≤ 40 分
- **T4.3** 使い捨てスクリプト archive/ 移動（`merge_codex_batches.py` 日付ハードコード除去、`merge_llm_classifications.py` archive）

**Exit Criteria**
1. `python scripts/daily_pipeline.py` 1 コマンドでドラフトが承認待ち状態
2. 収集→素材提供 P95 ≤ 40 分
3. 0 件日もフォールバックで必ず 1 件以上生成

**KPI 寄与**: 運用時間削減、1 日 1 投稿の安定稼働

---

## M5: テンプレート拡充 + ER 交絡制御 (2026-09)

**Tasks**
- **T5.1** `engagement_rate` ログから高位パターン分析 → 新テンプレート追加
  - **A/B テスト経由で因果検証**（M2 T2.5 の experiment_id 基盤を活用）
  - 統計的有意差（p < 0.05、n ≥ 14 投稿/バリアント）で本採用
- **T5.2** **engagement_rate の交絡要因制御**
  - 投稿時間帯（曜日 × 時間）のクロス集計で時間帯偏向を解消
  - 話題ジャンル（カテゴリ別）混在による混同を分離
  - アカウント別投稿時間帯最適化
- **T5.3** ✅ `account_routing` カテゴリベース化（完了済 — 完了済みタスク参照）
- **T5.4** `earnings_flash` を 7 カテゴリ体系に統合 or 廃止判断

**Exit Criteria**: 新テンプレの ER が既存平均 +10%（A/B で p < 0.05）、時間帯/ジャンル交絡が排除された ER 評価レポート

**KPI 寄与**: engagement_rate 向上、マルチアカウント運用の実効化

---

## M6: 品質ダッシュボード + F1 月次計測 (2026-10)

**Tasks**
- **T6.1** `scripts/build_dashboard.py` 拡張
  - imp 推移 / テンプレ別 ER / インフル勝率 / 日次収集量 / ノイズ率 / experiment_id 別 ER / 収集→素材提供時間分布 を追加
- **T6.2** 分類 F1 スコアの月次計測 + Gold Set 100 件拡充
  - `measure_f1.py` を月次 cron 化、F1 推移を記録
  - 使用分類器パス（`llm_classifier` / `ml_classifier` / `ensemble_classifier`）を明示ログ化
  - Gold Set を 100 件以上に拡充（信頼区間 ±0.05 まで縮小）
- **T6.3** `collector/` の位置づけ明示化（CLAUDE.md / README.md に core/shared 相当として明記）
- **T6.4** **読者反応読解パイプライン**（リプライ/QT/RT 内容分析）
  - 投稿後 24-48h でリプライ・QT・RT 本文を `impression_tracker/scraper.py` 拡張で取得
  - LLM で「刺さった表現/批判ポイント/誤情報指摘」を分類し `output/reader_feedback.jsonl`
  - 月次レポートで刺さりポイントを要約 → `make_article` 側の Pattern Library 改善に還流
- **T6.5** **誤振り分け率の月次サンプリング監査** ✅ 基盤実装済（`audit_routing.py`）
  - 月 30 件を人手レビュー、誤振り分け率推移をダッシュボード表示

**Exit Criteria**
1. 月次レビューで全 KPI が 1 画面で確認可能
2. Gold Set 100 件達成
3. 誤振り分け率 < 5% を 3 ヶ月連続維持
4. 分類 F1 macro ≥ 0.85
5. 読者反応読解パイプライン稼働、`make_article` 側への還流経路確立

**KPI 寄与**: 全 KPI 可視化

---

## コストモデル（月額見積、M4 完了想定）

| 項目 | 概算 | 備考 |
|---|---|---|
| LLM 分類（Claude Haiku） | $3〜$15 | 日次 100-300 件 × バッチ 20 |
| LLM 記事生成（短文 compose.py、Haiku） | $5〜$20 | 1 日 1 投稿 × 7 テンプレ × few-shot |
| Grok API（月次再評価） | $5〜$30 | バッチサイズ次第、dry-run キャッシュで圧縮可 |
| Cookie 手動更新オペ | 月 0.5h（人件費） | `import_chrome_cookies.py` 自動抽出 |
| **合計（API）** | **$13〜$65/月** | M2 着手前に dry-run で実測確定 |

> 長文記事生成（make_article）・LLM Critic のコストは `make_article` プロジェクト側で計上。

**コスト超過時フォールバック**: 月 $100 超で (a) compose を 7 → 3 テンプレに削減、(b) Grok 再評価を四半期 → 半年に変更

---

## 法的・X TOS リスク方針

- **収集**: ログイン済み Cookie でのブラウザ閲覧のみ。X API 非使用、自動ログイン禁止（CLAUDE.md feedback）
- **要約 → 投稿**: 原文を実質的に変形（要約・統合・解釈追加）、原文コピー禁止
- **引用元明示**: 投稿本文に source URL 含有 or handle 明記
- **X TOS §3 自動化条項**: 投稿の自動実行はユーザー承認後の予約投稿に限定（compose → review.html 承認 → run.py の 3 ステップ運用を維持）
- **コンティンジェンシー**: X API 制限強化や TOS 変更時は `scripts/manual_collect.py`（仮）への切替手順を M6 までに準備

---

## Non-Goals（この半年ではやらない）

1. **長文記事生成** — `make_article` プロジェクトの責務
2. **10000 imp 追求 / 教師データ戦略 / Grok アーキテクトスタイル学習** — `make_article` の責務
3. **CLI レイヤーのフレームワーク経由化**（compose.py の registry/EventBus 経由化は次期対応）
4. **CI/CD 基盤・自動テスト整備**
5. **`ml_classifier`/`ensemble_classifier` の全面リファクタリング**（M6 で使用経路ログ化のみ）
6. **BTC 分析スクリプト群の機能追加**
7. **新規 X アカウントの追加**（既存 2 アカウントの品質向上優先）
8. **Cookie 自動ログイン**: ボット検出リスクのため永久禁止
9. **投稿頻度の拡大（1 日 2 投稿以上）**: 品質優先

---

## リスクレジスター

| リスク | 影響範囲 | 影響度 | 緩和策 |
|---|---|---|---|
| **高勝率インフル投稿の到達性喪失**（TOP5 非公開化/凍結/BAN/ブロック/投稿停止） | **前提崩壊 → 全 M 空転** | **最高** | 週次 `check_inactive_accounts.py`、Grok 候補から即時繰り上げ、`group_reserve` 予備 5 名ストック |
| **インフル非活動化/Cookie 失効で収集量低下 → ニュース在庫切れ** | 全 M | **最高** | T1.7 日次モニタリング + 閾値警告、T0.3 Grok 再評価、週次 `check_inactive_accounts.py` |
| **Gold Set 35 件が偏った標本で F1 過大評価** | M2 ゲート | 高 | T1.6 カテゴリバランス強制、維持者月次 5 件追加、M6 で 100 件拡充 |
| **Gold Set が LLM 自己参照で F1 過大評価** | M2 ゲート誤通過 | **最高** | T1.6 中立性ルール（LLM 出力非提示・人手付与・時期層化）完了、M3/M6 拡充 |
| **Cookie 期限切れで `ImpressionScraper` / `XPoster` 停止** | 1 日 1 投稿途絶 | 高 | T0.1 `CookieExpiredError` 完了、恒久対策節の Layer 1 早期検知 |
| X DOM 変更で取得壊れ | 学習データ汚染 | 高 | `scraper.py` selector fallback |
| カテゴリ再ラベリングで過去互換破綻 | T1.0 | 中 | `migrate_categories.py` 完了済 |
| `is_contrarian` 精度低で警告シグナル汚染 | M2-M5 | 中 | gihuboy 50 ツイート手動レビュー（ペンディング） |
| ノイズ除去フィルタが過剰 | M2 T2.3 | 中 | denylist 保守的開始、誤除去率 < 5% 維持 |
| Grok API コスト過大 | T0.3, T3.3 | 中 | バッチ上限 + dry-run、キャッシュ優先 |
| **F1 ≥ 0.80 未達の無限ループ** | M2 着手不可 | 高 | 4 段階フォールバック（M2 セクション記述） |
| **ER 交絡要因で因果証明不能** | KPI 1 | 高 | experiment_id 基盤 + M5 T5.1/T5.2 A/B + 時間帯/ジャンル制御 |
| **人手作業累積で運用コスト逆転** | KPI 3 | 中 | M1 開始時に手動作業ベースライン記録、M6 で削減率測定 |
| **「100 ツイート/日」未確認のまま閾値運用** | 成功基準形骸化 | 中 | T1.7 完了後 2 週間で閾値再設定 |
| **誤振り分け率の測定者未定義** | 成功基準検証不能 | 中 | M6 T6.5 月次 30 件サンプリング（基盤完了、実施ペンディング） |
| **LLM/Grok コスト超過 / X TOS 変更** | 全 M 停止 | 中 | コストモデル節フォールバック、手動収集切替準備 |
| **読者反応が `make_article` に還流せず表面的改善のみ** | KPI 1 品質劣化 | 中 | M6 T6.4 読者反応読解パイプライン + `make_article` との連携 |

---

## 実行ハンドオフ（次セッション以降）

本計画は設計完了。**次セッション開始時は必ず `## M0` の残タスク（T0.2/T0.3）から着手する。**

各 M の実装着手時の流れ:

1. 該当 M のタスクを `tasks/plan_M{N}.md` に転記、`task-progress` スキルで進捗管理
2. 2 ファイル以上変更時は `Explore + Implement + Verify` の SubAgent 体制で実装
3. 各 Exit Criteria をテスト・疎通確認で証明してから M を完了
4. **M0 残: P0（T0.2-T0.3）→ P1（T0.4-T0.6）→ P2（T0.7-T0.8）の順で実行**
5. **M1 着手ゲート: M0 Exit Criteria 全 7 項目達成**
6. **M2 着手ゲート: F1 ≥ 0.80**
7. M 完了時に `capture-improvement` スキルで改善を定量記録

## 関連ドキュメント

- `/Users/masaaki_nagasawa/Desktop/biz/influx/README.md` — プロジェクト概要・7 カテゴリ定義
- `/Users/masaaki_nagasawa/Desktop/biz/influx/CLAUDE.md` — プロジェクト全体方針
- `/Users/masaaki_nagasawa/Desktop/biz/influx/tasks/research_pipeline.md` — Grok リサーチパイプライン
- `/Users/masaaki_nagasawa/Desktop/biz/influx/tasks/lessons.md` — 学習事項蓄積
- `/Users/masaaki_nagasawa/Desktop/biz/influx/.claude/skills/refresh-x-cookies/SKILL.md` — Cookie 取得手順
- `/Users/masaaki_nagasawa/Desktop/biz/make_article/` — **姉妹プロジェクト**（長文記事生成・10000 imp 追求・3 テーマ戦略・Grok アーキテクトスタイル学習）
  - `make_article/CLAUDE.md` — make_article プロジェクト概要
  - `make_article/output/plans/planMD.md` — make_article の計画 MD（North Star・KPI・カテゴリ戦略、§10 「influx との連携契約」）
  - `make_article/output/plans/system.md` — システム設計（Phase 1 素材受領 / Phase 4 読者反応還流の連携点）
