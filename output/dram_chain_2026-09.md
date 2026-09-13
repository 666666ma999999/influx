# DRAM不足 → 受益3階層と「まだ上がっていない」一覧（叩き台 v1・2026-09-13）

> **状態: 叩き台（検収前）**。オーダー= X投稿 https://x.com/paurooteri/status/2098222534441467921（本文「これはヤバいな DRAMが伸びすぎ」・2026-09-11・画像/スレッドなし）を起点に、①上がる会社（主要会社）②その主要取引先（関連会社）③さらにその取引先（関連会社の関連会社）を一覧化し、④まだ上がっていない産業・上がった産業の中で上がっていない株を出す。
> 冒頭 ask の裁定（2026-09-13）: 市場= 日本株＋海外主役／上昇判定= 直近3ヶ月の騰落 vs 指数／帰属= 広く載せて関門通過状況を列で示す／置き場= 本ファイル。

## 0. 3行サマリ（平易）

1. **DRAM を作る側（Micron・Nanya・Winbond）と、その「後工程・検査」（アドバンテスト・KOKUSAI・住友ベーク）は株価がもう反応済み**。韓国2社（Samsung・SK hynix）は3ヶ月では指数並みだが直近1ヶ月で +17%/+30% と急騰中。
2. **まだ上がっていない産業は3つ**: 〈前工程の装置（日本の東エレク・SCREEN・ローツェ、米国の ASML・Lam・AMAT・KLA）〉〈日本の材料（信越・東京応化・扶桑化学・日東紡・HOYA・ステラケミファ）〉〈台湾のメモリモジュール（ADATA・Transcend・Apacer＝利益は記録的なのに株価は指数割れ）〉。
3. **上がった産業の中で取り残された株**: 後工程ではディスコ（3ヶ月 −23.5%）・TOWA・BESI・UniTest、DRAM本体では Samsung・SK hynix、基板ではイビデンが上がる中の日東紡。**「上がっていない＝割安」ではない**（ディスコ等は個別要因の裏取りが未了・§5 注意）。

判定の物差し: 日本株= 2026-06-10→09-10 の騰落率（分割補正済）− TOPIX 同期間 +5.4%。海外= 同期間の騰落率 − 現地指数（S&P500 +4.5%／KOSPI −9.0%／台湾加権 +8.6%。香港・蘭・中国は指数未取得＝絶対値で仮判定）。超過がマイナスなら「未反応」。

---

## 1. 前提と市況の起点（二次資料）

- Samsung/SK hynix の完成品 DRAM 在庫が 10 日分未満（TechTimes 2026-09-07）。汎用 DRAM 契約価格は 1Q26 +90〜95% q/q・2Q26 +58〜63%（TrendForce 経由）。HBM4 は従来 DRAM 比 3 倍のウエハを消費し、2026 年の HBM は 3 社とも完売。
  - https://www.techtimes.com/articles/326837/20260907/memory-runs-dry-samsung-sk-hynix-drop-below-10-day-supply-hbm4-devours-capacity.htm
  - https://www.networkworld.com/article/4113772/samsung-warns-of-memory-shortages-driving-industry-wide-price-surge-in-2026.html
- 手元の価格台帳（`data/price_watch/universe_weekly.jsonl`）は NAND スポットのみ登録で **DRAM スポット系列は未登録**（実測）。DRAM 価格の起点日は自前データでは特定できない（§7 残タスク）。
- 帰属ルール: `docs/price-watch-universe.md` §0a/§0b（受益カード5関門。セグメント営業利益 ≥30%＝確証／10〜30%＝暫定／買う側＝符号−）。既存台帳 `configs/x_shortage_map.json` subjects[dram] の 19 行（受益13＋買う側6）は判定をそのまま転記し、覆していない。
- 凡例: 符号 ＋=受益／−=逆風／0=中立。関門判定= 確証／暫定／未確認／却下。株価判定= 反応済／未反応（超過3M の符号）。

---

## 2. 3階層の一覧（L1 主要会社 → L2 関連会社 → L3 関連会社の関連会社）

### L1 主要会社（DRAM不足で直接利益が伸びる会社・26社）

### L1-a DRAMメーカー本体（海外7社）

| 階層 | 会社（コード・市場） | DRAM不足との関係 | 取引相手 | 符号 | 関門判定 | 出所URL | 3M% | 超過3M(pt) | 株価判定 |
|---|---|---|---|---|---|---|---:|---:|---|
| L1 | Samsung Electronics（005930.KS・韓国） | DRAM世界1位（2Q26 シェア39%）・HBM4 を Nvidia Rubin へ供給。DS部門営業利益が全社の約94%（1Q26） | 売り先= Nvidia・Broadcom・ハイパースケーラー・自社MX | ＋ | 確証（E2: DS部門利益≒全社利益・売り手） | https://counterpointresearch.com/en/insights/ai-demand-reshapes-dram-rankings-in-q2-2026 ／ https://www.digitimes.com/news/a20260730VL224/sk-hynix-hbm-tester-wafer-advantest.html | 海外=§4 | – | – |
| L1 | SK hynix（000660.KS・韓国／2026年 米ADR上場 F-1 提出） | HBM 1位（1Q26 56〜58%）・Nvidia 依存 1Q26 約15%（2025年通年 約24%）・2026年分完売 | 売り先= Nvidia（HBM4 Rubin）・米国売上65% | ＋ | 確証（E1: 顧客集中を F-1 で開示・売り手） | https://www.sec.gov/Archives/edgar/data/0002120882/000119312526299963/d32785d424b4.htm ／ https://finance.yahoo.com/technology/ai/articles/sk-hynix-hbm-empire-powers-153110376.html | 海外=§4 | – | – |
| L1 | Micron Technology（MU・NASDAQ） | DRAM 3位（2Q26 25%・DRAM売上は2Q25比5倍）・HBM4 量産中・上位10顧客で売上の過半、1顧客が17%（FY26 1Q・CMBU） | 売り先= Nvidia（HBM4）・データセンター顧客（売上56%） | ＋ | 確証（E1: 10-K/10-Q 顧客集中開示・売り手） | https://www.sec.gov/Archives/edgar/data/723125/000072312525000028/mu-20250828.htm ／ https://investors.micron.com/static-files/502c03ac-dd06-4c88-9441-02ebfe6ff6fa | 海外=§4 | – | – |
| L1 | Nanya Technology（2408.TW・台湾） | 汎用DRAM専業。1Q26 ASP +70% q/q で純利益 NT$260億（過去最高）・2Q26 ASP +60% q/q | 売り先= モジュールメーカー（ADATA等）・PC/民生 | ＋ | 確証（E1: 会社が ASP 上昇→利益を決算説明で明示） | https://www.digitimes.com/news/a20260413PD229/nanya-technology-dram-profit-demand-2026.html ／ https://www.investing.com/news/transcripts/earnings-call-transcript-nanya-technology-posts-q2-2026-profit-surge-on-pricing-93CH-4786239 | 海外=§4 | – | – |
| L1 | Winbond（2344.TW・台湾） | 特殊DRAM/NOR。会社が「DRAM価格は6月までに約4倍・2027年まで予約済み」と表明 | 売り先= 車載・産機・民生（レガシーDRAM） | ＋ | 確証（E1: 会社発言の価格感応度・売り手） | https://www.trendforce.com/news/2026/02/11/news-winbond-expects-dram-prices-to-jump-nearly-4x-by-june-2026-capacity-booked-through-2027/ | 海外=§4 | – | – |
| L1 | CXMT 長鑫存儲（非上場・IPO準備中） | 中国DRAM。2Q26 シェア7%・前年比+716%・1Q26 290k wpm→年末350k | 売り先= 中国OEM（推定）／仕入= 国産装置（Naura/AMEC 等） | ＋（投資不可） | 暫定（E5 二次資料のみ・非上場） | https://counterpointresearch.com/en/insights/ai-demand-reshapes-dram-rankings-in-q2-2026 ／ https://www.trendforce.com/news/2026/04/21/news-china-memory-expansion-lifts-domestic-equipment-suppliers-amec-wuhan-jingce-hwatsing-acm-in-focus/ | 海外=§4 | – | – |
| L1 | Powerchip PSMC（6770.TW・台湾） | 台湾DRAM（Nanya/Winbond と合算で1Q26 2.2%） | 売り先= 未確認 | ＋ | 未確認（DRAM 比率・利益寄与を一次で未取得） | https://counterpointresearch.com/en/insights/global-dram-and-hbm-market-share | 海外=§4 | – | – |

### L1-b 既存台帳（日本株・受益13行＝tier 転記）

| 階層 | 会社（コード・市場） | DRAM不足との関係 | 取引相手 | 符号 | 関門判定（台帳転記） | 出所URL | 3M% | 超過3M(pt) | 株価判定 |
|---|---|---|---|---|---|---|---:|---:|---|
| L1 | ディスコ（6146・東証P） | 切断・研削・研磨装置＝単一セグメント（営業利益100%）。会社が HBM を需要牽引役と名指し | 最大顧客= TSMC（売上11.0%）・メモリ3社（推定） | ＋ | 確証（E2 100%・台帳 confirmed 2026-07-28） | https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260422/20260420506569.pdf | -23.5 | -28.9 | 未反応 |
| L1 | 日本マイクロニクス（6871・東証P） | メモリ向けプローブカード＝セグメント利益 104.5%（連結比126%）。HBM需要を明記 | 売り先= SK hynix・Samsung・Micron（海外比率約90%・顧客別%は未確認） | ＋ | 確証（E2・台帳 confirmed） | https://equity.jiji.com/storage/tdnet/140120260213559700.pdf ／ https://aiinfra-lab.com/ai-company/1567/ | +8.0 | +2.6 | 反応済 |
| L1 | KOKUSAI ELECTRIC（6525・東証P） | バッチ成膜装置。DRAM向け＝装置売上28%（連結売上の18.3%）・4Qは DRAM 向け改造サービス増 | 売り先= 韓国・台湾・中国メモリ（顧客別%は未確認） | ＋ | 暫定（10〜30%帯・台帳 provisional） | https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260513/20260513527733.pdf ／ https://japanir.jp/company/company-6525/ir/6525-20260513-02_wp_financial_summary/ | +12.6 | +7.2 | 反応済 |
| L1 | アドバンテスト（6857・東証P） | メモリテスタ＝売上15.2%（DRAM用途90%）。主軸は SoC テスタ。FY26 予想を1兆7,140億円へ上方修正 | 売り先= SK hynix・Samsung・Micron（HBMテスト）／韓国勢（UniTest等）が HBM4 ウエハテスタで競合 | ＋ | 暫定（10〜30%帯・台帳 provisional） | https://www.advantest.com/document/ja/investors/ir-library/result/JE_BIZ_260427_slide.pdf ／ https://www.digitimes.com/news/a20260730VL224/sk-hynix-hbm-tester-wafer-advantest.html | +34.4 | +29.0 | 反応済 |
| L1 | 東京エレクトロン（8035・東証P） | DRAM向け＝SPE新規装置の31%（連結売上の22.5%）。韓国向け売上 3割増 | 売り先= Samsung・SK hynix・Micron・TSMC | ＋ | 確証（台帳 confirmed 2026-08-30） | https://www.tel.co.jp/ir/irta3a00000006g5-att/fy26q4transcript-j.pdf ／ https://www.nikkei.com/article/DGXZQOUB2911C0Z20C26A4000000/ | -14.6 | -20.0 | 未反応 |
| L1 | SCREEN HD（7735・東証P） | 枚葉洗浄装置＝SPE利益が連結の100.2%。短信が「DRAM向け装置売上が増加」と名指し | 売り先= メモリ3社・TSMC（顧客別%は未確認） | ＋ | 確証（E2・台帳 confirmed） | https://www.screen.co.jp/download_file/ee1eb20f-1101-4520-b6ae-0358321647a3/317 | +0.5 | -4.9 | 未反応 |
| L1 | ローツェ（6323・東証P） | ウエハ搬送ロボット/EFEM＝装置事業利益 102.7%。メモリ内訳は非開示。会社は「メモリ価格高騰」を調達リスクにも明記 | 売り先= Samsung グループ・TSMC・Applied Materials（モジュール供給） | ＋ | 暫定（台帳 provisional） | https://www.rorze.com/wp_rorze/wp-content/uploads/2026/04/20260409_2026_Q4.pdf ／ https://finance.yahoo.co.jp/news/detail/eb8f7552f6620212e5a8f4940dc346dc6f675b72 | -5.6 | -11.0 | 未反応 |
| L1 | SUMCO（3436・東証P） | 300mmウエハ。DRAM→ウエハ増→増益の連鎖が資料に無く 8/15 再監査で却下。ただし業界は2026年に2回値上げ（累計15%超）で環境は改善 | 売り先= メモリ3社・TSMC（%未確認） | ＋（台帳は却下） | 却下（台帳 rejected 2026-08-30・覆さない） | https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260210/20260202545202.pdf ／ https://www.zaikei.co.jp/article/20260825/867236.html | -4.4 | -9.8 | 未反応 |
| L1 | 東京応化工業（4186・東証P） | フォトレジスト（EUV/ArF）。単一セグメントで用途別非開示 | 主要顧客= TSMC（有報）・メモリ向け%は未確認 | ＋ | 暫定（台帳 provisional） | https://www.tok.co.jp/application/files/1517/7061/1184/q4_2512.pdf | -11.4 | -16.8 | 未反応 |
| L1 | 扶桑化学工業（4368・東証P） | CMP スラリー用コロイダルシリカ＝電子材料利益 84.5%（連結比）。DRAM別内訳は非開示 | 売り先= CMPスラリーメーカー経由でメモリ3社（%未確認） | ＋ | 暫定（台帳 provisional） | https://fusokk.co.jp/wp-content/uploads/2026/05/168b2b886957b97114fd57141daf403d.pdf | -22.7 | -28.1 | 未反応 |
| L1 | レゾナック HD（4004・東証P） | 後工程材料（封止材・ダイボンド材）＋前工程材料。半導体・電子材料コア利益 99.4%、メモリ内訳非開示 | 売り先= SK hynix ほか HBM 3社（二次資料）・Nvidia 系 | ＋ | 暫定（台帳 provisional） | https://www.resonac.com/sites/default/files/2026-02/tanshin2025q4.pdf ／ https://www.postation.jp/news/hbm-skhynix-samsung-micron-japan-investment | +4.4 | -1.0 | 未反応 |
| L1 | 住友ベークライト（4203・東証P） | 封止用エポキシ＝半導体関連材料 事業利益 51.9%。用途別非開示 | 売り先= OSAT・メモリ3社（%未確認） | ＋ | 暫定（台帳 provisional） | https://www.sumibe.co.jp/ir/library/result/files/2026/0511_01.pdf | +22.8 | +17.4 | 反応済 |
| L1 | 日東紡績（3110・東証P） | 低誘電ガラスクロス＝電子材料利益 93.1%。受益はAIサーバ基板枚数であり DRAM価格は算式に入らない | 売り先= 基板メーカー（イビデン等）→ Nvidia 系 | ＋（間接） | 暫定（台帳 provisional・DRAM直結でない） | https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260512/20260512525575.pdf | -17.2 | -22.6 | 未反応 |

### L1-c 既存台帳 traps（日本株・買う側6行＝sign − 転記）

| 階層 | 会社（コード・市場） | DRAM不足との関係 | 取引相手 | 符号 | 関門判定（台帳転記） | 出所URL | 3M% | 超過3M(pt) | 株価判定 |
|---|---|---|---|---|---|---|---:|---:|---|
| L1 | シャープ（6753・東証P） | Dynabook 等で DRAM/NAND は仕入原価 | 買い先= メモリ3社（%未確認） | − | 台帳 trap（pin=DRAM/NAND価格・sign −） | configs/x_shortage_map.json traps（一次URL無し） | +5.2 | -0.2 | 未反応 |
| L1 | ソニーグループ（6758・東証P） | G&NS ハード原価。メモリ高騰は減益要因 | 買い先= メモリ3社 | − | 台帳 trap | 同上 | +7.5 | +2.2 | 反応済 |
| L1 | エレコム（6750・東証P） | 中国生産のドル建て仕入。メモリ高はコスト | 買い先= モジュール/メモリ | − | 台帳 trap | 同上 | +9.5 | +4.1 | 反応済 |
| L1 | ダイワボウ HD（3107・東証P） | 法人PC出荷台数がピン。PC値上げ（Dell +15〜20%・Lenovo 1月〜）は台数を抑える | 買い先= PCメーカー（Dell/Lenovo/HP 等） | − | 台帳 trap | https://www.trendforce.com/news/2025/12/05/exclusive-memory-crunch-hits-pcs-dell-hikes-prices-15-20-mid-december-lenovo-from-january-2026/ | +8.1 | +2.7 | 反応済 |
| L1 | デンソー（6902・東証P） | 車載メモリを買う側。2026年は車載 DRAM/NAND の供給不足が報道 | 買い先= メモリ3社・Winbond（車載）／売り先= トヨタ | − | 台帳 trap | https://www.eetimes.com/automakers-face-memory-shock-as-ai-uses-up-semiconductor-supply/ | +0.1 | -5.2 | 未反応 |
| L1 | ビックカメラ（3048・東証P） | 仕入難と販売数量減として効く（既存店売上がピン） | 買い先= PC/スマホメーカー・モジュール | − | 台帳 trap | configs/x_shortage_map.json traps | -4.6 | -10.0 | 未反応 |

---

### L2 関連会社（L1 の主要取引先・重複は1行に集約・47行）

### L2-a L1（DRAMメーカー）の仕入先＝売り手（装置・材料・検査・基板）

| 階層 | 会社（コード・市場） | DRAM不足との関係 | 取引相手（どの L1 の何） | 符号 | 関門判定 | 出所URL | 3M% | 超過3M(pt) | 株価判定 |
|---|---|---|---|---|---|---|---:|---:|---|
| L2 | ASML（ASML・NASDAQ/AMS） | EUV 露光機の唯一供給者。SK hynix が High-NA EXE:5200B を DRAM で初導入・Samsung は 2028 年 High-NA DRAM | 売り手→ Samsung・SK hynix・Micron（CXMT は輸出規制で除く） | ＋ | 暫定（E4 顧客発表・EUV 比率はロジック主） | https://www.kedglobal.com/korean-chipmakers/newsView/ked202609080013 ／ https://drrobertcastellano.substack.com/p/sk-hynix-first-to-install-high-na | 海外=§4 | – | – |
| L2 | Lam Research（LRCX・NASDAQ） | エッチ/成膜。Micron・Samsung 向けが成長ドライバー | 売り手→ Micron・Samsung・SK hynix | ＋ | 暫定（二次資料のみ） | https://patentpc.com/blog/top-chip-making-equipment-companies-asml-applied-materials-and-lam-research-market-data | 海外=§4 | – | – |
| L2 | Applied Materials（AMAT・NASDAQ） | 成膜・CMP等。中国売上縮小が課題 | 売り手→ メモリ3社・ローツェ（モジュール仕入） | ＋ | 暫定（二次資料のみ） | https://247wallst.com/investing/2026/08/26/applied-materials-china-problem-is-getting-worse/ | 海外=§4 | – | – |
| L2 | KLA（KLAC・NASDAQ） | 検査・計測。High-NA 用アクティニック検査でレーザーテックと並ぶボトルネック | 売り手→ メモリ3社 | ＋ | 暫定（二次資料のみ） | https://internet-pros.com/blog/high-na-euv-lithography-asml-twinscan-2026/ | 海外=§4 | – | – |
| L2 | Hanmi Semiconductor（042700.KS・韓国） | HBM TC ボンダ世界1位（累計シェア71.2%・2025年3Q）。2026-06-08 SK hynix から HBM4 用 442億ウォン受注。SK 内シェアは 20〜30% へ低下予測 | 売り手→ SK hynix（HBM4）・Micron（HBM3E） | ＋ | 暫定（E4 受注開示・SK 依存高） | https://www.trendforce.com/news/2026/06/09/news-sk-hynix-reportedly-places-44-2bn-won-tc-bonder-order-with-hanmi-accelerating-hbm4-ramp-up/ ／ https://www.digitimes.com/news/a20260813VL208/hbm4-hanwha-hanmi-sk-hynix-equipment.html | 海外=§4 | – | – |
| L2 | ASMPT（0522.HK・香港） | SK hynix の HBM4 TC ボンダ約50台のうち半分を供給。Samsung も評価中 | 売り手→ SK hynix・Samsung（評価） | ＋ | 暫定（二次資料・受注額非開示） | https://www.digitimes.com/news/a20251215VL207/sk-hynix-asmpt-hbm4-wafer-bonder-patent.html ／ https://lumenalpha.substack.com/p/besi-asmpt-hanmi-and-hanwha-what | 海外=§4 | – | – |
| L2 | BE Semiconductor（BESI.AS・AMS） | Micron が HBM4 TCB を BESI 単独へ切替と報道・Samsung はハイブリッド接合で採用検討 | 売り手→ Micron・Samsung | ＋ | 暫定（二次資料） | https://lumenalpha.substack.com/p/besi-asmpt-hanmi-and-hanwha-what | 海外=§4 | – | – |
| L2 | Hanwha Semitech（Hanwha Vision 489790.KS 傘下・ティッカー要確認） | HBM4 TC ボンダで Hanmi と特許係争。SK hynix では現在未使用 | 売り手→ SK hynix（停滞） | 0〜＋ | 未確認（上場主体の特定と受注） | https://www.digitimes.com/news/a20260813VL208/hbm4-hanwha-hanmi-sk-hynix-equipment.html | 海外=§4 | – | – |
| L2 | UniTest（086390.KQ・韓国） | Digital Frontier と合わせ SK hynix から HBM4 ウエハテスタ4件・計2,478億ウォン受注（2026年） | 売り手→ SK hynix（HBM4 テスト）・アドバンテストと競合 | ＋ | 暫定（E4 受注開示） | https://www.digitimes.com/news/a20260730VL224/sk-hynix-hbm-tester-wafer-advantest.html | 海外=§4 | – | – |
| L2 | Semes（Samsung 100%子会社・非上場） | Samsung の TCB を内製供給（TC ボンダ累計シェア13.1%） | 売り手→ Samsung | ＋（投資不可） | 暫定 | https://lumenalpha.substack.com/p/besi-asmpt-hanmi-and-hanwha-what | 海外=§4 | – | – |
| L2 | Wonik IPS（240810.KQ・韓国） | ALD 等の国産装置。Samsung・SK hynix 両社で認定済み・Samsung が出資 | 売り手→ Samsung・SK hynix | ＋ | 暫定（依存度%は未確認） | https://www.envisioning.com/research/substrate/south-korea__semiconductor-equipment-localization | 海外=§4 | – | – |
| L2 | PSK（319660.KQ・韓国） | アッシング装置。Samsung 系の主要装置サプライヤー | 売り手→ Samsung・SK hynix | ＋ | 未確認（依存度・受注） | https://www.envisioning.com/research/substrate/south-korea__semiconductor-equipment-localization | 海外=§4 | – | – |
| L2 | Soulbrain（357780.KQ・韓国） | 高純度フッ化水素等のエッチング材。Samsung が4.8%出資 | 売り手→ Samsung・SK hynix | ＋ | 暫定（依存度%は未確認） | https://www.businesskorea.co.kr/news/articleView.html?idxno=34523 | 海外=§4 | – | – |
| L2 | Dongjin Semichem（005290.KS・韓国） | フォトレジスト。Samsung が4.8%出資 | 売り手→ Samsung・SK hynix | ＋ | 暫定（依存度%は未確認） | https://www.businesskorea.co.kr/news/articleView.html?idxno=34523 | 海外=§4 | – | – |
| L2 | 信越化学工業（4063・東証P） | 300mm ウエハ世界1位。会社が「DRAM は供給不足・メモリメーカーが将来のウエハ確保に動く」と発言。2026年に2回値上げ・1,200億円増設 | 売り手→ メモリ3社・TSMC | ＋ | 暫定（E4 決算電話会議・ウエハは事業の一部） | https://www.shinetsu.co.jp/wp-content/uploads/2025/07/20260428_summary_J.pdf ／ https://finance.biggo.jp/news/fd450131-ec9c-4768-a1ab-d3ca10d944b3 | -13.9 | -19.3 | 未反応 |
| L2 | GlobalWafers（6488.TW・台湾） | 台湾ウエハ3社が3年ぶり全サイズ値上げ交渉入り | 売り手→ メモリ3社・Nanya/Winbond | ＋ | 暫定（二次資料） | https://www.zaikei.co.jp/article/20260825/867236.html | 海外=§4 | – | – |
| L2 | TOWA（6315・東証P） | HBM 用樹脂圧縮成形装置。FY23 に韓国メモリ2社へ22台。SK hynix が下期 HBM4 増設で装置追加発注を協議中 | 売り手→ SK hynix・Samsung | ＋ | 暫定（E4 社長発言・2026年受注額は未確認） | https://www.digitimes.com/news/a20240424PD201/japan-equipment-materials-hbm-expansion-sk-hynix.html ／ https://www.trendforce.com/news/2026/08/10/news-samsungs-hbm4-yield-reportedly-hits-80-as-race-to-supply-vera-rubin-heats-up-sk-hynix-labor-talks-add-a-twist/ | -17.8 | -23.2 | 未反応 |
| L2 | レーザーテック（6920・東証P） | EUV マスク検査。メモリの EUV 層増（SK hynix 1c DRAM で6層）で間接受益 | 売り手→ マスクショップ・Samsung/SK hynix（直接比率は未確認） | ＋ | 未確認（メモリ向け比率） | https://www.lasertec.co.jp/en/ir/individuals/euv.html ／ https://www.tweaktown.com/news/106957/sk-hynix-ramps-1c-dram-to-6-euv-layers-preps-for-high-na-designs-destroy-samsung-in-hbm/index.html | -10.7 | -16.0 | 未反応 |
| L2 | Naura 北方華創（002371.SZ・中国） | CXMT 向け国産装置（エッチ・成膜）。CXMT 設置装置の国産比率 40〜50% | 売り手→ CXMT | ＋ | 暫定（二次資料） | https://www.trendforce.com/news/2026/04/21/news-china-memory-expansion-lifts-domestic-equipment-suppliers-amec-wuhan-jingce-hwatsing-acm-in-focus/ | 海外=§4 | – | – |
| L2 | AMEC 中微（688012.SS・中国） | CXMT 向けエッチ装置（コア工程で国産比率60%超） | 売り手→ CXMT | ＋ | 暫定（二次資料） | 同上 | 海外=§4 | – | – |
| L2 | Piotech 拓荊（688072.SS・中国） | CXMT 向け成膜装置 | 売り手→ CXMT | ＋ | 暫定（二次資料） | 同上 | 海外=§4 | – | – |
| L2 | Hwatsing 華海清科（688120.SS・中国） | CXMT 向け CMP | 売り手→ CXMT | ＋ | 暫定（二次資料） | 同上 | 海外=§4 | – | – |
| L2 | ACM Research（ACMR・NASDAQ／688082.SS） | CXMT 向け洗浄装置 | 売り手→ CXMT | ＋ | 暫定（二次資料） | 同上 | 海外=§4 | – | – |
| L2 | Wuhan Jingce 精測電子（300567.SZ・中国） | CXMT 向け検査・計測 | 売り手→ CXMT | ＋ | 暫定（二次資料） | 同上 | 海外=§4 | – | – |

### L2-b L1（DRAMメーカー）の販売先＝買い手（GPU・サーバ・PC・スマホ・クラウド・モジュール）

| 階層 | 会社（コード・市場） | DRAM不足との関係 | 取引相手（どの L1 の何） | 符号 | 関門判定 | 出所URL | 3M% | 超過3M(pt) | 株価判定 |
|---|---|---|---|---|---|---|---:|---:|---|
| L2 | NVIDIA（NVDA・NASDAQ） | HBM 最大の買い手。HBM コスト増で粗利 74%→4Q 71〜72% へ低下ガイダンス。AI サーバ価格を15%超値上げして転嫁・供給コミット 2,790億ドル | 買い手← SK hynix（HBM4 60〜70%）・Samsung（25〜30%）・Micron | −（転嫁で緩和） | 暫定（E1: 会社ガイダンスで原価側と明示） | https://www.matterfact.com/newsletter/2026-08-27-ai-accelerators-019-nvidia-guides-higher-cuts-margins ／ https://www.tomshardware.com/pc-components/dram/nvidia-reportedly-warns-biggest-customers-of-15-percent-price-hikes-on-ai-servers ／ https://finance.yahoo.com/sectors/technology/articles/nvidia-certifies-samsung-sk-hynix-133001560.html | 海外=§4 | – | – |
| L2 | AMD（AMD・NASDAQ） | MI シリーズで HBM を購入（Micron の副次顧客） | 買い手← Micron・Samsung・SK hynix | − | 未確認（HBM 原価比率） | https://beth-kindig.medium.com/micron-stock-up-120-ytd-what-the-hbm-memory-leader-plans-for-2026-2ba022138136 | 海外=§4 | – | – |
| L2 | Broadcom（AVGO・NASDAQ） | カスタム AI ASIC 向け HBM。Samsung と長期 HBM 供給契約と報道 | 買い手← Samsung | − | 未確認（契約は二次資料のみ） | https://siliconanalysts.com/market/nvidia-sk-hynix-samsung-broadcom-lock-in-950b-hbm-supply-agreements-securing-nex-2026-07-25 | 海外=§4 | – | – |
| L2 | Hon Hai 鴻海（2317.TW・台湾） | AI サーバ組立シェア約40%。メモリ高で売上増・粗利率低下（5〜8%帯） | 買い手← メモリ3社（サーバDRAM）／売り手→ Nvidia・ハイパースケーラー・Apple | − | 暫定（業界記事で粗利圧迫を確認） | https://www.digitimes.com/news/a20260513PD237/ai-server-odm-business-revenue.html | 海外=§4 | – | – |
| L2 | Quanta 広達（2382.TW・台湾） | Nvidia AI サーバ受注の25〜30%。メモリ高で運転資本・粗利圧迫 | 買い手← メモリ3社／売り手→ ハイパースケーラー | − | 暫定（同上） | https://www.digitimes.com/news/a20260515PD225/ai-server-odm-revenue-foxconn-quanta.html | 海外=§4 | – | – |
| L2 | Wiwynn 緯穎（6669.TW・台湾） | 部材高で粗利圧迫・メモリ購買方式を変更 | 買い手← メモリ3社／売り手→ Meta・Microsoft 等 | − | 暫定（同上） | https://www.digitimes.com/news/a20260508PD205/odm-wiwynn-component-revenue-profit.html | 海外=§4 | – | – |
| L2 | Wistron 緯創（3231.TW・台湾） | AI サーバ ODM（2025年売上1兆NT$超） | 買い手← メモリ3社／売り手→ Nvidia・Dell | − | 暫定（同上） | https://www.digitimes.com/news/a20260109PD249/revenue-ai-server-foxconn-wistron-quanta.html | 海外=§4 | – | – |
| L2 | Super Micro（SMCI・NASDAQ） | AI サーバ OEM。Nvidia 値上げの転嫁側 | 買い手← メモリ3社 | − | 未確認（メモリ原価比率） | https://www.amcompute.com/blog/best-oems-for-ai-gpu-servers | 海外=§4 | – | – |
| L2 | Dell（DELL・NYSE） | PC を 15〜20% 値上げ。COO が「メモリ価格上昇はかつてない速さ」。AI サーバ受注は記録 | 買い手← メモリ3社／売り先→ 法人・ダイワボウ等の販社 | − | 暫定（E4 経営者言及） | https://www.trendforce.com/news/2025/12/05/exclusive-memory-crunch-hits-pcs-dell-hikes-prices-15-20-mid-december-lenovo-from-january-2026/ ／ https://www.techtimes.com/articles/326190/20260901/dell-q2-earnings-test-record-ai-backlog-meets-its-first-margin-reckoning.htm | 海外=§4 | – | – |
| L2 | Lenovo（0992.HK・香港） | 2026年分の DRAM を先行確保しつつ 1月から PC 値上げ | 買い手← メモリ3社 | − | 暫定（E4 会社通知） | https://www.pcworld.com/article/3060746/lenovo-stockpiled-enough-ram-for-2026-its-still-raising-pc-prices.html | 海外=§4 | – | – |
| L2 | HP Inc.（HPQ・NYSE） | PC 原価に占めるメモリ 15〜18%（前年の2倍）。値上げで対応 | 買い手← メモリ3社 | − | 暫定（業界記事） | https://thegadgetflow.com/blog/ram-shortage-2026-how-lenovo-hp-dell-apple-and-other-pc-makers-face-rising-costs/ | 海外=§4 | – | – |
| L2 | HPE（HPE・NYSE） | サーバ OEM。メモリ原価増 | 買い手← メモリ3社 | − | 未確認 | https://www.amcompute.com/blog/best-oems-for-ai-gpu-servers | 海外=§4 | – | – |
| L2 | Apple（AAPL・NASDAQ） | プレミアム端末で DRAM が原価の23%（2Q26）。長期契約でヘッジ | 買い手← Samsung・SK hynix・Micron（LPDDR） | −（軽微） | 暫定（業界推計） | https://www.koreajoongangdaily.com/business/apple-xiaomi-talk-smartphone-price-hikes-samsung-quietly-cushions-the-blownbsp/12763401 | 海外=§4 | – | – |
| L2 | Xiaomi（1810.HK・香港） | 出荷の過半が200ドル未満＝メモリ原価比率が最大（Omdia が最も脆弱と指摘） | 買い手← メモリ3社・CXMT（推定） | − | 暫定（Omdia 二次資料） | https://www.koreajoongangdaily.com/business/apple-xiaomi-talk-smartphone-price-hikes-samsung-quietly-cushions-the-blownbsp/12763401 | 海外=§4 | – | – |
| L2 | Microsoft（MSFT・NASDAQ） | CFO が capex のうち 250億ドルをメモリ・部材高騰分と説明 | 買い手← サーバ ODM／HBM は Nvidia 経由 | −（capex 膨張） | 暫定（E4 CFO 発言） | https://www.tomshardware.com/tech-industry/big-tech/big-techs-ai-spending-plans-reach-725-billion | 海外=§4 | – | – |
| L2 | Alphabet（GOOGL）・Amazon（AMZN）・Meta（META）（NASDAQ） | 4社合計 capex 2026 年 7,250億ドル（+77%）。メモリ比率が 2027 年に約50%へ | 買い手← サーバ ODM・メモリ3社（直接契約） | −（capex 膨張） | 暫定（アナリスト推計） | https://cryptobriefing.com/memory-share-hyperscaler-capex-2027/ | 海外=§4 | – | – |
| L2 | ADATA 威剛（3260.TW・台湾） | モジュール。在庫 NT$350億を積み、利益17倍・粗利 55.7%・8月売上過去最高 | 買い手← Nanya・Samsung・SK hynix・Micron（チップ）／売り先→ 小売・OEM | ＋（在庫評価益） | 暫定（E5 報道・在庫評価に価格が入る=関門A通過） | https://www.digitimes.com/news/a20260907PD242/adata-team-group-memory-module-inventory-revenue-2027.html ／ https://www.tomshardware.com/tech-industry/taiwanese-memory-module-makers-raise-880-million-to-stockpile-chips | 海外=§4 | – | – |
| L2 | Transcend 創見（2451.TW・台湾） | 1月純利益 +1578% y/y | 買い手← メモリ3社・Nanya | ＋（在庫評価益） | 暫定（報道） | https://www.digitimes.com/news/a20260320PD223/transcend-memory-chips-price-profit-2026.html | 海外=§4 | – | – |
| L2 | Apacer 宇瞻（8271.TW・台湾） | モジュール。売上記録更新 | 買い手← メモリ3社・Nanya | ＋（在庫評価益） | 未確認（利益寄与） | https://wccftech.com/memory-manufacturers-earned-more-in-q1-than-all-of-last-year-prices-to-spiral-up/ | 海外=§4 | – | – |
| L2 | Team Group 十銓（ティッカー要確認・台湾） | 8月売上過去最高・2027年向け在庫積み増し | 買い手← メモリ3社・Nanya | ＋（在庫評価益） | 未確認（上場コード） | https://www.digitimes.com/news/a20260907PD242/adata-team-group-memory-module-inventory-revenue-2027.html | 海外=§4 | – | – |

### L2-c 日本株 L1（装置・材料）の主要取引先で上記に無いもの

| 階層 | 会社（コード・市場） | DRAM不足との関係 | 取引相手（どの L1 の何） | 符号 | 関門判定 | 出所URL | 3M% | 超過3M(pt) | 株価判定 |
|---|---|---|---|---|---|---|---:|---:|---|
| L2 | TSMC（2330.TW・台湾） | ディスコ最大顧客（11.0%）・東京応化の主要顧客・TEL/SCREEN の大口。DRAM 自体は作らないが HBM を CoWoS で GPU に実装 | 買い手← ディスコ・東京応化・TEL・SCREEN・レゾナック／HBM は Nvidia 側で調達 | 0（DRAM 直接関係なし） | 暫定（有報で主要販売先を確認） | https://finance-frontend-pc-dist.west.edge.storage-yahoo.jp/disclosure/20260422/20260420506569.pdf ／ https://www.tok.co.jp/application/files/1517/7061/1184/q4_2512.pdf | 海外=§4 | – | – |
| L2 | イビデン（4062・東証P） | Nvidia GPU 用パッケージ基板の主供給者。日東紡のガラスクロスの売り先。FY26 営業利益 +30%・FY27 予想 +45% | 買い手← 日東紡（3110）・味の素（ABF）／売り手→ Nvidia | ＋（AI基板・DRAM 直結でない） | 暫定（E2 電子事業の伸びは開示・Nvidia 比率は未確認） | https://japanstockpulse.com/article/2026/05/11/4062-1.html ／ https://tribune.com.pk/story/2519040/nvidia-supplier-ibiden-considers-faster-expansion-to-meet-ai-demand | +18.9 | +13.5 | 反応済 |
| L2 | トヨタ自動車（7203・東証P） | デンソーの売り先。車載メモリ不足で減産リスク | 買い手← デンソー（6902） | − | 未確認（2026年の減産実績） | https://www.eetimes.com/automakers-face-memory-shock-as-ai-uses-up-semiconductor-supply/ | +6.4 | +1.0 | 反応済 |

---

### L3 関連会社の関連会社（主要 L2 の取引先・14社）

| 階層 | 会社（コード・市場） | DRAM不足との関係 | 取引相手（どの L2 の何） | 符号 | 関門判定 | 出所URL | 3M% | 超過3M(pt) | 株価判定 |
|---|---|---|---|---|---|---|---:|---:|---|
| L3 | 味の素（2802・東証P） | ABF（ビルドアップフィルム）。イビデン等の基板メーカーへ販売 | 売り手→ イビデン（L2）・Unimicron | ＋（AI基板経由・DRAM 直結でない） | 未確認（ABF の利益比率は本調査で未取得） | https://www.marketsandmarkets.com/ResearchInsight/gpu-substrate-companies.asp | +6.5 | +1.1 | 反応済 |
| L3 | Unimicron 欣興（3037.TW・台湾） | Nvidia 向け GPU 基板でイビデンと並ぶ | 売り手→ Nvidia（L2） | ＋（AI基板） | 未確認 | https://www.marketsandmarkets.com/ResearchInsight/gpu-substrate-companies.asp | 海外=§4 | – | – |
| L3 | HOYA（7741・東証P） | EUV マスクブランクス。レーザーテック検査の対象物・Samsung/SK の EUV 層増で需要 | 売り手→ マスクショップ・Samsung/SK hynix（L1 直販もあり） | ＋ | 未確認（メモリ向け比率） | https://www.kedglobal.com/korean-chipmakers/newsView/ked202609080013 | -10.0 | -15.4 | 未反応 |
| L3 | ステラケミファ（4109・東証P） | 高純度フッ化水素。韓国の Soulbrain 等と競合/供給（2019年 輸出管理で依存が顕在化） | 売り手→ Samsung・SK hynix（L1）・Soulbrain（L2）との関係は未確認 | ＋ | 未確認（現在の韓国向け比率） | https://www.businesskorea.co.kr/news/articleView.html?idxno=34523 | -30.1 | -35.5 | 未反応 |
| L3 | Carl Zeiss SMT（非上場・Zeiss 財団） | ASML の EUV 光学系 | 売り手→ ASML（L2） | ＋（投資不可） | 未確認 | https://internet-pros.com/blog/high-na-euv-lithography-asml-twinscan-2026/ | 海外=§4 | – | – |
| L3 | Intel（INTC・NASDAQ） | Dell/Lenovo/HP の CPU 供給元。PC 値上げ→台数減の影響を受ける側。イビデンの旧最大顧客（70〜80%→30%） | 売り手→ Dell・Lenovo・HP（L2）／買い手← イビデン | −（PC 台数減） | 未確認 | https://capitalblueprint.substack.com/p/ibiden-co-ltd-tyo-4062-deep-analysis | 海外=§4 | – | – |
| L3 | Qualcomm（QCOM・NASDAQ） | Xiaomi 等の SoC 供給元。スマホ出荷 2026 年 −2.6%（2Q26 −6%）の影響 | 売り手→ Xiaomi・Samsung MX（L2） | −（台数減） | 未確認 | https://bitubix.com/news/smartphone-shipments-q2-2026-memory-costs | 海外=§4 | – | – |
| L3 | MediaTek 聯発科（2454.TW・台湾） | 低価格スマホ SoC。メモリ原価比率43%帯の廉価機が最も縮む | 売り手→ Xiaomi ほか中国 OEM（L2） | −（台数減） | 未確認 | https://www.idc.com/resource-center/blog/global-memory-shortage-crisis-market-analysis-and-the-potential-impact-on-the-smartphone-and-pc-markets-in-2026/ | 海外=§4 | – | – |
| L3 | Oracle（ORCL・NYSE） | Nvidia の AI サーバ値上げ通知先（Microsoft・Google と並記） | 買い手← Nvidia・サーバ ODM（L2） | −（capex 膨張） | 暫定（報道） | https://www.tomshardware.com/pc-components/dram/nvidia-reportedly-warns-biggest-customers-of-15-percent-price-hikes-on-ai-servers | 海外=§4 | – | – |
| L3 | Inventec 英業達（2356.TW・台湾） | サーバ ODM。民生比率が高く AI ミックスで劣後 | 売り手→ HPE・Dell（L2） | − | 暫定（報道） | https://www.ldeepai.com/tech-hub/ai-server-odm-market-analysis-2026-growth-forecast/ | 海外=§4 | – | – |
| L3 | Macronix 旺宏（2337.TW・台湾） | NOR/NAND。Winbond の競合・記録的売上（DRAM 不足の波及） | 売り先→ 民生・車載（Winbond L1 と同じ買い手） | ＋（隣接） | 未確認（DRAM 直結なし） | https://wccftech.com/memory-manufacturers-earned-more-in-q1-than-all-of-last-year-prices-to-spiral-up/ | 海外=§4 | – | – |
| L3 | Teradyne（TER・NASDAQ） | アドバンテスト（L1）の競合。メモリテスト市場拡大の相乗り | 売り手→ メモリ3社 | ＋ | 未確認（メモリテスタ比率） | https://www.digitimes.com/news/a20260730VL224/sk-hynix-hbm-tester-wafer-advantest.html | 海外=§4 | – | – |
| L3 | Samsung MX（Samsung 005930.KS 内部門） | 自社メモリでクッション＝値上げ抑制。L1 Samsung と同一法人のため銘柄としては重複 | 買い手← Samsung DS（社内） | 0 | 暫定（報道） | https://www.koreajoongangdaily.com/business/apple-xiaomi-talk-smartphone-price-hikes-samsung-quietly-cushions-the-blownbsp/12763401 | 海外=§4 | – | – |
| L3 | Kingston（非上場・米） | モジュール世界最大手。ADATA/Transcend と同じ立場 | 買い手← メモリ3社 | ＋（在庫評価益・投資不可） | 未確認 | https://www.digitimes.com/news/a20251110PD215/2026-price-2025-nand-memory-module.html | 海外=§4 | – | – |

---

## 3. 産業別の集計表（掲載社数・符号）（L1〜L3 L1 26＋L2 47＋L3 14＝87行・非上場含む・Samsung MX は Samsung と重複）

| 産業 | 掲載社数 | 受益＋ | 逆風− | 中立0 | 備考 |
|---|---|---|---|---|---|
| DRAM メーカー（L1） | 7 | 7 | 0 | 0 | CXMT は非上場・PSMC は未確認 |
| 前工程装置（露光・成膜・エッチ・洗浄・搬送） | 17 | 17 | 0 | 0 | 日本4（TEL・KOKUSAI・SCREEN・ローツェ）＋海外13（Semes 非上場含む） |
| 後工程・検査（ダイサ・プローブ・テスタ・TCB・モールド） | 11 | 10 | 0 | 1 | 日本5（ディスコ・マイクロニクス・アドバンテスト・TOWA・レーザーテック）＋海外6（Hanwha Semitech は0〜＋で中立扱い） |
| 材料（ウエハ・レジスト・スラリー・封止・ガス・マスク） | 14 | 13 | 0 | 0 | SUMCO は台帳「却下」で＋に数えない |
| 基板・ABF | 4 | 4 | 0 | 0 | 日東紡・イビデン・味の素・Unimicron（AI基板経由・DRAM 直結でない） |
| GPU・AI 半導体（買い手） | 3 | 0 | 3 | 0 | Nvidia・AMD・Broadcom |
| サーバ ODM/OEM（買い手） | 8 | 0 | 8 | 0 | 鴻海・Quanta・Wiwynn・Wistron・SMCI・Dell・HPE・Inventec |
| PC・スマホ・民生（買い手） | 7 | 0 | 7 | 0 | Lenovo・HP・Apple・Xiaomi・シャープ・ソニー・エレコム |
| クラウド（買い手） | 5 | 0 | 5 | 0 | MSFT・GOOGL・AMZN・META・ORCL |
| メモリモジュール（在庫評価益） | 5 | 5 | 0 | 0 | ADATA・Transcend・Apacer・Team Group・Kingston(非上場) |
| 半導体（CPU/SoC・台数減の側） | 3 | 0 | 3 | 0 | Intel・Qualcomm・MediaTek |
| 商社・小売・販社 | 2 | 0 | 2 | 0 | ダイワボウ・ビックカメラ（台帳 trap） |
| 自動車 | 2 | 0 | 2 | 0 | デンソー・トヨタ |
| ファウンドリ・隣接メモリ・光学 | 3 | 2 | 0 | 1 | TSMC(0)・Macronix(＋隣接)・Zeiss(＋非上場) |
| **合計** | **87行（重複・非上場込み）** | **58** | **30** | **2** | 集計は行単位の目安（重複行と非上場を含む） |

要点: 受益＋は「売り手（装置・材料・検査・モジュール在庫）」に集中し、買い手側は Nvidia のような価格転嫁力がある会社でも粗利ガイダンスを下げている（2026-08-27 決算）。日本株の受益＋のうち台帳「確証」は4社（ディスコ・マイクロニクス・TEL・SCREEN）。イビデン・信越・TOWA・レーザーテックは台帳外で暫定/未確認。

---

### 3a. 未確認・推定の一覧（26件）

| # | 項目 | 状態 |
|---|---|---|
| 1 | Micron の17%顧客の社名（10-Q は匿名） | 未確認（Nvidia と推定・出所なし） |
| 2 | PSMC（6770.TW）の DRAM 売上・利益比率 | 未確認 |
| 3 | 日本マイクロニクスの顧客別売上%（SK hynix/Samsung/Micron） | 未確認（有報「主要な販売先」を本調査で開けず） |
| 4 | KOKUSAI の顧客別売上%（Samsung/Micron） | 未確認 |
| 5 | SCREEN の顧客別売上% | 未確認 |
| 6 | 東京応化・扶桑化学・住友ベーク・レゾナックのメモリ向け売上% | 未確認（台帳でも非開示） |
| 7 | Hanwha Semitech の上場主体・ティッカー | 未確認（Hanwha Vision 489790.KS 傘下と推定） |
| 8 | Team Group の上場コード | 未確認 |
| 9 | PSK（319660.KQ）の Samsung 依存度と受注 | 未確認 |
| 10 | Wonik IPS・Soulbrain・Dongjin の Samsung/SK 依存度% | 未確認（出資4.8%のみ確認） |
| 11 | レーザーテックのメモリ向け比率 | 未確認 |
| 12 | TOWA の 2026 年 HBM 受注額 | 未確認（FY23 の22台のみ） |
| 13 | AMD・HPE・SMCI のメモリ原価比率 | 未確認 |
| 14 | Broadcom–Samsung HBM 長期契約の実在 | 未確認（Silicon Analysts 単独報道） |
| 15 | Micron→BESI 単独採用 | 未確認（Substack 二次資料） |
| 16 | イビデンの Nvidia 売上比率 | 未確認（Intel 30% までは二次資料） |
| 17 | 味の素 ABF の利益比率 | 未確認 |
| 18 | HOYA のメモリ向け EUV ブランクス比率 | 未確認 |
| 19 | ステラケミファの現在の韓国向け比率 | 未確認 |
| 20 | トヨタの 2026 年メモリ起因減産の実績 | 未確認 |
| 21 | Teradyne のメモリテスタ比率 | 未確認 |
| 22 | Macronix の DRAM 不足との直結 | 未確認（隣接メモリの波及として推定） |
| 23 | CXMT の販売先（Xiaomi・Lenovo と推定） | 未確認 |
| 24 | Apacer・Kingston の利益寄与 | 未確認 |
| 25 | Dell の AI サーバ側メモリ転嫁率 | 未確認 |
| 26 | Nanya 「NAND 3社が出資」の報道（01.co） | 本表に未採用（一次確認なし） |

台帳 traps 6行（シャープ・ソニー・エレコム・ダイワボウ・デンソー・ビックカメラ）は出所 URL が台帳に無く、
台帳の pin/note を根拠とした（一次資料の再取得は行っていない）。
有報「主要な販売先」の直接実読は本調査では 0 件（検索結果の要約経由）。日本株 L1 の取引先%は台帳既載分（ディスコ TSMC 11.0%）以外すべて未確認。

---



---

## 4. 海外銘柄の騰落（2026-06-10→09-10・Yahoo Finance chart API・未調整終値）

比較指数: S&P500 +4.5%（1M −2.1%）／KOSPI −9.0%（1M +11.7%）／台湾加権 +8.6%（1M +4.5%）。香港・蘭・中国上場は指数未取得＝絶対値で仮判定。ティッカー修正3件: GlobalWafers=6488.TWO／ADATA=3260.TWO／東進セミケム=005290.KQ。

| ティッカー | 銘柄 | 3M% | 1M% | 比較指数 | 超過3M(pt) | 株価判定 |
|---|---|---:|---:|---|---:|---|
| MU | Micron Technology, Inc. | +9.6 | +13.5 | SPX | +5.1 | 反応済 |
| 005930.KS | SamsungElec | -11.1 | +17.0 | KOSPI | -2.1 | 未反応 |
| 000660.KS | SK hynix | -9.5 | +30.5 | KOSPI | -0.5 | 未反応 |
| 2408.TW | NANYA TECHNOLOGY CORPORATION | +54.7 | +2.6 | TWII | +46.1 | 反応済 |
| 2344.TW | WINBOND ELECTRONIC CORP | +20.5 | +0.0 | TWII | +11.9 | 反応済 |
| 6770.TW | POWERCHIP SEMICONDUCTOR MANU | +14.1 | +8.7 | TWII | +5.5 | 反応済 |
| ASML | ASML Holding N.V. - New York | -2.7 | -2.7 | SPX | -7.2 | 未反応 |
| LRCX | Lam Research Corporation | -7.4 | -2.7 | SPX | -11.9 | 未反応 |
| AMAT | Applied Materials, Inc. | -8.7 | -13.0 | SPX | -13.2 | 未反応 |
| KLAC | KLA Corporation | -17.0 | -8.1 | SPX | -21.5 | 未反応 |
| 042700.KS | HANMISemi | -6.3 | +23.1 | KOSPI | +2.7 | 反応済 |
| 0522.HK | ASMPT | -6.2 | -1.5 | – | – | 未反応(絶対値) |
| BESI.AS | BE Semiconductor Industries  | -36.3 | -16.2 | – | – | 未反応(絶対値) |
| 086390.KQ | UniTest | -20.5 | +20.9 | KOSPI | -11.5 | 未反応 |
| 240810.KQ | WONIK IPS | +1.8 | +20.7 | KOSPI | +10.8 | 反応済 |
| 357780.KQ | Soulbrain | +1.0 | +9.1 | KOSPI | +10.0 | 反応済 |
| 005290.KQ | DONGJIN | -18.7 | +1.0 | KOSPI | -9.7 | 未反応 |
| 6488.TWO | GLOBALWAFERS CO LTD | +18.3 | +7.4 | TWII | +9.7 | 反応済 |
| 002371.SZ | NAURA | +3.1 | -16.0 | – | – | 反応済(絶対値) |
| 688012.SS | ADVANCED MICRO-FABRICATION E | +15.6 | -14.3 | – | – | 反応済(絶対値) |
| ACMR | ACM Research, Inc. | -9.5 | -8.8 | SPX | -14.0 | 未反応 |
| NVDA | NVIDIA Corporation | +9.0 | +0.4 | SPX | +4.5 | 反応済 |
| AMD | Advanced Micro Devices, Inc. | +11.3 | +7.2 | SPX | +6.8 | 反応済 |
| AVGO | Broadcom Inc. | -3.0 | -14.6 | SPX | -7.5 | 未反応 |
| 2317.TW | HON HAI PRECISION INDUSTRY | -4.6 | -5.1 | TWII | -13.2 | 未反応 |
| 2382.TW | QUANTA COMPUTER | -11.7 | +7.2 | TWII | -20.3 | 未反応 |
| 6669.TW | WIWYNN CORPORATION | +38.1 | +16.4 | TWII | +29.5 | 反応済 |
| 3231.TW | WISTRON CORPORATION | +17.1 | -4.1 | TWII | +8.5 | 反応済 |
| SMCI | Super Micro Computer, Inc. | +27.7 | +18.8 | SPX | +23.2 | 反応済 |
| DELL | Dell Technologies Inc. | +37.0 | +10.6 | SPX | +32.5 | 反応済 |
| 0992.HK | LENOVO GROUP | +40.3 | +11.2 | – | – | 反応済(絶対値) |
| HPQ | HP Inc. | +32.6 | +9.8 | SPX | +28.1 | 反応済 |
| HPE | Hewlett Packard Enterprise C | +21.4 | +1.0 | SPX | +16.9 | 反応済 |
| AAPL | Apple Inc. | +12.0 | +5.9 | SPX | +7.5 | 反応済 |
| 1810.HK | XIAOMI-W | -1.5 | -6.2 | – | – | 未反応(絶対値) |
| MSFT | Microsoft Corporation | +23.9 | -2.7 | SPX | +19.4 | 反応済 |
| GOOGL | Alphabet Inc. | -6.7 | -7.0 | SPX | -11.2 | 未反応 |
| AMZN | Amazon.com, Inc. | +5.8 | -9.4 | SPX | +1.3 | 反応済 |
| META | Meta Platforms, Inc. | +12.9 | +8.3 | SPX | +8.4 | 反応済 |
| 3260.TWO | ADATA TECHNOLOGY CO. LTD. | +4.4 | -1.0 | TWII | -4.2 | 未反応 |
| 2451.TW | TRANSCEND INFORMATION INC | -3.6 | -4.4 | TWII | -12.2 | 未反応 |
| 8271.TW | APACER TECHNOLOGY INC | +4.5 | -10.4 | TWII | -4.1 | 未反応 |
| 2330.TW | TAIWAN SEMICONDUCTOR MANUFAC | +8.6 | +2.9 | TWII | +0.0 | 反応済 |
| 3037.TW | UNIMICRON TECHNOLOGY | +12.4 | +0.2 | TWII | +3.8 | 反応済 |
| INTC | Intel Corporation | -6.3 | +2.9 | SPX | -10.8 | 未反応 |
| QCOM | QUALCOMM Incorporated | -7.5 | +9.1 | SPX | -12.0 | 未反応 |
| 2454.TW | MEDIATEK INC | +13.6 | +19.2 | TWII | +5.0 | 反応済 |
| ORCL | Oracle Corporation | -24.0 | +1.3 | SPX | -28.5 | 未反応 |
| 2356.TW | INVENTEC CORP | -5.4 | -5.2 | TWII | -14.0 | 未反応 |
| 2337.TW | MACRONIX INTERNATIONAL | -9.3 | -8.2 | TWII | -17.9 | 未反応 |
| TER | Teradyne, Inc. | +6.5 | +1.4 | SPX | +2.0 | 反応済 |

---

## 5. まだ上がっていない産業／上がった産業の中で上がっていない株（オーダー④）

### 5a. 産業別の株価反応（受益＋の産業のみ・行=銘柄・判定は §2/§4 の超過3M）

| 産業 | 反応済 | 未反応 | 産業の判定 |
|---|---|---|---|
| DRAM メーカー本体 | Micron +5.1pt・Nanya +46.1・Winbond +11.9・PSMC +5.5 | Samsung −2.1・SK hynix −0.5（ただし1Mは +17.0%/+30.5% で急騰中） | **反応済**（韓国2社だけ3M遅れ） |
| 前工程装置（露光・成膜・エッチ・洗浄・搬送） | KOKUSAI +7.2・Wonik IPS +10.8・NAURA/AMEC（絶対値＋） | 東エレク −20.0・SCREEN −4.9・ローツェ −11.0・ASML −7.2・Lam −11.9・AMAT −13.2・KLA −21.5・ACMR −14.0 | **未反応産業**（8/11 が指数割れ・日米とも） |
| 後工程・検査（ダイサ・プローブ・テスタ・TCB・モールド） | アドバンテスト +29.0・日本マイクロニクス +2.6・Hanmi +2.7・Teradyne +2.0 | ディスコ −28.9・TOWA −23.2・レーザーテック −16.0・UniTest −11.5・BESI（−36.3% 絶対値）・ASMPT（−6.2% 絶対値） | **上がった産業（テスタ系）の中で取り残しあり** |
| 材料（ウエハ・レジスト・スラリー・封止・フッ酸・マスク） | 住友ベーク +17.4・GlobalWafers +9.7・Soulbrain +10.0 | 信越 −19.3・東京応化 −16.8・扶桑化学 −28.1・レゾナック −1.0・HOYA −15.4・ステラケミファ −35.5・東進セミケム −9.7 | **未反応産業（日本の材料はほぼ全滅）** |
| 基板・ABF（AI サーバ経由・DRAM 直結でない） | イビデン +13.5・味の素 +1.1・Unimicron +3.8 | 日東紡 −22.6 | 反応済（日東紡のみ取り残し） |
| メモリモジュール（在庫評価益） | – | ADATA −4.2・Transcend −12.2・Apacer −4.1 | **未反応産業**（利益は記録的・株価は指数割れ） |
| NAND/隣接 | – | キオクシア −23.0（6M +196.8% の後の調整）・Macronix −17.9 | 参考（DRAM 直結でない） |

### 5b. 「まだ上がっていない」候補の一覧（買い判断はここから裏取り・順番= 関門判定の強い順）

| 区分 | 銘柄 | 関門判定 | 超過3M | 未反応の中身（この表で分かる範囲） | 次に裏取りする物 |
|---|---|---|---|---|---|
| 未反応産業 / 前工程装置 | 東京エレクトロン 8035 | 確証（台帳） | −20.0 | DRAM向け31%だが FY26/3 は営業利益 −10.4%（原材料高） | 4-6月期の受注（DRAM向け）と韓国向け比率 |
| 同 | SCREEN 7735 | 確証（台帳） | −4.9 | 短信が「DRAM向け装置売上が増加」と名指し | 1Q 受注・ファウンドリ向けの減速が続くか |
| 同 | ローツェ 6323 | 暫定 | −11.0 | 装置事業利益 103%・メモリ内訳非開示 | Samsung 向け比率（有報「主要な販売先」） |
| 同 | ASML / Lam / AMAT / KLA | 暫定（二次資料） | −7.2 / −11.9 / −13.2 / −21.5 | 中国売上縮小が重石（AMAT）・EUV はロジック主 | メモリ向け受注比率（各社決算のセグメント） |
| 未反応産業 / 材料 | 信越化学 4063 | 暫定（E4 会社発言） | −19.3 | 会社が「DRAM は供給不足」と明言・2回値上げ | ウエハ事業の利益比率（塩ビが主力なので薄まる） |
| 同 | 扶桑化学 4368 | 暫定（台帳） | −28.1 | CMP スラリー用シリカ＝電子材料利益 84.5% | DRAM 別内訳（非開示）・値上げの有無 |
| 同 | 東京応化 4186 | 暫定（台帳） | −16.8 | レジスト単一セグメント・主要顧客 TSMC | メモリ向け比率（未確認） |
| 同 | 日東紡 3110 | 暫定（台帳・DRAM 直結でない） | −22.6 | 受益は AI サーバ基板枚数・イビデンは上昇 | ガラスクロスの増産・単価 |
| 同 | HOYA 7741 / ステラケミファ 4109 | 未確認 | −15.4 / −35.5 | EUV ブランクス／高純度フッ酸。メモリ向け比率は未取得 | 比率が取れるまで候補外 |
| 未反応産業 / モジュール | ADATA 3260.TWO・Transcend 2451.TW・Apacer 8271.TW | 暫定（報道・在庫評価益） | −4.2 / −12.2 / −4.1 | 利益 17 倍・粗利 55.7%（ADATA）なのに株価は指数割れ | 在庫評価益の持続性（価格反落時は逆に損失） |
| 上がった産業の取り残し / 後工程 | ディスコ 6146 | 確証（台帳・営業利益100%） | −28.9 | 3M −23.5%・6M −23.2%。HBM を需要牽引役と名指し | **なぜ下げているか**（4-6月期・ガイダンス・TSMC 向け）を先に確認 |
| 同 | TOWA 6315 | 暫定 | −23.2 | HBM 用モールド装置。SK hynix と追加発注を協議中 | 2026 年の HBM 受注額（未確認） |
| 同 | BESI.AS / UniTest 086390.KQ | 暫定 | −36.3%（絶対値）/ −11.5 | TCB・HBM4 ウエハテスタ。受注報道はある | 受注→売上の時期・競合（Hanmi は上昇） |
| 上がった産業の取り残し / DRAM本体 | Samsung 005930.KS・SK hynix 000660.KS | 確証 | −2.1 / −0.5 | 3M は KOSPI 並み。1M で +17%/+30% と反応が始まった | 「未反応」と呼べるのは 6/10 起点のみ・既に動いている |

### 5c. 逆風側なのに上がっている（注意・買い候補ではない）

DRAM を買う側（符号−）のサーバ・PC メーカーが 3 ヶ月で大きく上昇: Dell +32.5pt・Lenovo +40.3%・HP +28.1pt・Wiwynn +29.5pt・SMCI +23.2pt。AI サーバ需要と値上げ転嫁が原価増を上回っている＝「DRAM 不足＝買い手が下がる」は 3 ヶ月の実測では成り立っていない。逆に Quanta −20.3pt・鴻海 −13.2pt・Inventec −14.0pt は指数割れ。

---

## 6. 出所の検証（別の目・curl 全件開封）

母集団 79 URL（重複除去）→ **79/79 開封**。到達＋主要語一致 58／PDF 社名一致（先頭3頁）13／不達 8／URL でない（台帳内部参照）9。B（到達したが主要語なし）0。
不達 8 本（行の会社）: Micron 投資家資料（403）／Nanya investing.com（403）／東エレク transcript PDF（404）／ローツェ Yahoo ニュース（404）／EE Times 車載メモリ（タイムアウト・デンソー/トヨタ）／internet-pros High-NA（403・KLA/Zeiss）／Medium Micron 記事（403・AMD）／Tribune イビデン（403）。→ これらの行は「出所未到達」として扱う。
注記: 到達 58 のうち 16 本は社名でなく HBM/DRAM の語のみで一致。digitimes は会員制で本文が一部のみの可能性。sec.gov 2 本は申告 UA で 200。詳細= scratchpad `url_check.md`（セッション限り）。

## 7. 限界と残タスク

- **有価証券報告書「主要な販売先」の直接実読は 0 件**（firecrawl 本日上限・WebSearch 要約経由）。日本株 L1 の顧客別 % は台帳既載のディスコ TSMC 11.0% 以外すべて未確認。
- Hanwha Semitech・Team Group はティッカー未確定。Micron の 17% 顧客は匿名開示（Nvidia は推定）。
- 海外騰落は Yahoo 未調整終値（分割・配当未補正）。香港・蘭・中国は指数未取得。
- DRAM スポット系列が自前台帳に無い → `price_universe_sources.json` へ dramexchange 系列を登録すれば起点日を機械で出せる（別 task・price-source-onboarding）。
- 「未反応」は 6/10 起点の 3 ヶ月窓に依存。起点を変えると Samsung/SK hynix・キオクシアは反応済に変わる。

再現: `python3 scripts/_tmp_dram_chain_returns.py <コード...> --base 20260910`（TOPIX= data/jquants/topix.json.gz・分割は AdjFactor 累積で補正）。海外= scratchpad `fetch_returns.py`（Yahoo chart API）。

---

## 8. 使った検索クエリ（WebSearch 32本／WebFetch 0本／firecrawl 不使用）

1. DRAM price surge September 2026 contract spot HBM shortage Micron SK hynix Samsung
2. DRAM market share 2026 Samsung SK hynix Micron Nanya Winbond CXMT
3. Micron 10-K largest customers suppliers 2025 percent of revenue
4. SK hynix HBM Nvidia share of revenue major customers 2026
5. SK hynix suppliers Hanmi Semiconductor TC bonder Soulbrain Wonik IPS 2026
6. ディスコ 主要な販売先 有価証券報告書 TSMC マイクロン 割合
7. 日本マイクロニクス 主要な販売先 SKハイニックス マイクロン 有価証券報告書
8. Nvidia HBM suppliers SK hynix Samsung Micron share 2026 Blackwell Rubin
9. AI server ODM Foxconn Quanta Wiwynn Supermicro Dell memory cost increase margin 2026
10. Nanya Technology Winbond DRAM price hike 2026 earnings
11. Micron largest customer Nvidia 17% revenue 10-Q fiscal 2026 HBM
12. Advantest memory tester customers SK hynix Micron Samsung HBM test demand fiscal 2026
13. 東京エレクトロン 主要顧客 サムスン SKハイニックス 売上 割合 有価証券報告書 2026
14. KOKUSAI ELECTRIC 主要販売先 サムスン マイクロン 割合 有価証券報告書
15. TOWA HBM compression molding SK hynix Samsung orders 2026 東和 6315
16. Ibiden Nvidia package substrate share revenue 2026 イビデン エヌビディア 依存
17. 信越化学 シリコンウエハー DRAM 需要 2026 決算 メモリ 300mm 値上げ
18. Samsung Electronics DS suppliers Semes Wonik IPS PSK Soulbrain Dongjin Semichem revenue dependence Samsung
19. PC makers Lenovo HP Dell raise prices memory DRAM cost 2026 margin
20. smartphone makers DRAM cost Apple Xiaomi Samsung MX margin memory prices 2026
21. memory module makers ADATA Transcend Kingston profit DRAM price surge 2026
22. Hanmi Semiconductor Hanwha Semitech ASMPT BESI HBM4 TC bonder share Samsung Micron 2026
23. 日本マイクロニクス SKハイニックス 売上高 依存 割合 プローブカード HBM 2026
24. アドバンテスト 主要顧客 SKハイニックス マイクロン メモリテスタ HBM シェア 2026 決算説明
25. KOKUSAI ELECTRIC DRAM 向け 売上 構成 サムスン 依存 2026年3月期 決算説明資料
26. ローツェ 主要販売先 有価証券報告書 サムスン SK 搬送 2026
27. Nvidia gross margin HBM memory cost increase 2026 Rubin memory price impact hyperscaler capex Microsoft Google Amazon
28. Micron suppliers ASML Applied Materials Lam Research KLA Hiroshima fab EUV Tokyo Electron 2026
29. Lasertec EUV mask inspection memory customers Samsung SK hynix DRAM EUV 2026 レーザーテック
30. 扶桑化学 レゾナック 住友ベークライト 日東紡 主要販売先 HBM メモリ向け 顧客 SK hynix 2026
31. CXMT suppliers Naura AMEC Piotech equipment DRAM expansion 2026 domestic equipment share
32. SK hynix Samsung DRAM shortage automotive memory Denso Toyota supply concerns 2026
