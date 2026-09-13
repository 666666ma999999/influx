# DRAM 海外L1 5社 → 取引先の木（一次資料 実読）

取得日: 2026-09-13。取得手段: SEC EDGAR は curl（UA "influx-research contact@example.com"）、各社IRサイトは curl / WebFetch。PDF は pdftotext で全文化して該当節を実読。firecrawl 不使用。
ラベル: **一次**=本人の開示文書の引用 ／ **一次(相手側)**=取引相手の開示文書に名指しで載っている ／ **推定（二次）**=報道・調査会社（本ファイルでは根拠に使わない・参考としてのみ末尾に列挙）。
原文はすべて保存済み: `scratchpad/dram_src/`（`*.txt` が全文化したもの）。

---

## 1. Micron Technology (MU)

**出所**: Form 10-K FY2025（会計年度末 2025-08-28・提出 2025-10-03）https://www.sec.gov/Archives/edgar/data/723125/000072312525000028/mu-20250828.htm（取得 2026-09-13・`dram_src/mu_10k.txt`）

**a. 主要販売先（一次・匿名・比率のみ）**
- Note 28 Certain Concentrations: "Revenue from one customer was 17% (primarily included in the CMBU segment) of total revenue for 2025. Revenue from one customer was 10% (primarily included in the MCBU, AEBU, and CMBU segments) of total revenue for 2024. No customer accounted for 10% or more of total revenue in 2023."（CMBU = Cloud Memory Business Unit）
- Item 1: "In each of the last three years, approximately one-half of our total revenue was from our top ten customers."
- Risk Factors: "In 2025, over half of our total revenue came from our top ten customers. Among our end markets, approximately one-half of our total revenue was concentrated in the data center end market."
- 顧客名の名指し: **無し**（10-K 全文で NVIDIA/AMD/Broadcom の顧客としての名指しは 0 件・grep 実測）。

**b. 主要仕入先（一次・名指し無し＝未開示）**
- Note 28: "We generally have multiple sources of supply for our raw materials and production equipment; however, only a limited number of suppliers are capable of delivering certain raw materials and production equipment that meet our standards and, in some cases, materials or production equipment are provided by a single supplier."
- Risk Factors: 供給品目の列挙のみ "chemicals, silicon wafers, gases, photoresists, semiconductors, substrates, lead frames, printed circuit boards, targets, and reticle glass blanks"。ASML/Applied/Lam/TEL の名指し: **無し**（grep 0 件）。

**c. HBM の売り先（一次根拠）**
- NVIDIA 10-K FY2026（一次(相手側)）: "We purchase memory from SK Hynix Inc., Micron Technology, Inc., and Samsung." → Micron→NVIDIA
- Micron プレスリリース 2025-06-12 "Micron HBM Designed into Leading AMD AI Platform"（一次）https://investors.micron.com/news-releases/news-release-details/micron-hbm-designed-leading-amd-ai-platform : "the integration of its HBM3E 36GB 12-high offering into the upcoming AMD Instinct™ MI350 Series solutions" → Micron→AMD
- AMD ブログ 2025-06-12（一次(相手側)）https://www.amd.com/en/blogs/2025/amd-instinct-mi350-series-and-beyond-accelerating-the-future-of-ai-and-hpc.html : "These GPUs deliver leading memory capacity (288GB HBM3E from Micron and Samsung Electronics)"
- 10-K 本文の HBM 記述: "In 2025, we delivered samples of HBM4 36GB 12-high to multiple key customers to power next-generation AI platforms."（顧客名なし）
- 未取得: Micron 2024-02-26 の HBM3E→NVIDIA H200 プレスリリース原本（investors.micron.com の該当 URL が 404。二次報道のみヒット＝根拠に採用せず）

**d. L2→L3**: Micron の L2 は匿名のため、c で一次確認できた NVIDIA / AMD を L2 とする。L3 は §6 参照。

---

## 2. SK hynix (000660.KS)

**出所**: Form 424B4（米国 ADS 上場目論見書・2026年）https://www.sec.gov/Archives/edgar/data/2120882/000119312526299963/d32785d424b4.htm（取得 2026-09-13・`dram_src/skh_424b4.txt`・約102万字）

**a. 主要販売先（一次・匿名・比率のみ）**
- "Customers, Sales and Marketing": "Our two largest customers represented 14.8% and 12.4%, respectively, of our total revenue in the first quarter of 2026 and our largest customer represented 23.9% of our total revenue in 2025."
- 監査済財務諸表 note 4: "For the year ended December 31, 2025, revenue of ₩23,260,076 million (2024: ₩10,902,817 million), or 23.9% (2024: 16.5%) of the Group's revenue, is derived from an external customer A. For the year ended December 31, 2023, no revenue derived from a single customer reached over 10%."
- Risk Factors: "A substantial portion of our sales is attributable to a limited number of customers located in the United States and China."
- 地域別売上 2025: United States 68.8% ／ China 19.7% ／ Asia(その他) 7.4% ／ Europe 2.0% ／ Korea 2.0%
- 顧客名の名指し: 顧客 A は**匿名**。名指しは c の NVIDIA のみ。

**b. 主要仕入先（一次・国のみ・社名は未開示）**
- "Raw Materials and Supplies": "The raw materials used in our semiconductor fabrication process include polished silicon wafers, chemicals, metals such as titanium and aluminum, gases and subsidiary materials. Wafers are the most significant raw material in terms of cost, representing approximately 10% of our cost of sales in recent years. … We source most of our raw materials, including wafers, from suppliers in Korea, Japan and the United States." "We are not dependent on any one supplier for a substantial portion of our raw material requirements"
- 装置: "we also depend on a limited number of manufacturers in the Netherlands, the United States and Japan for our key equipment. … our purchases of high-end equipment have historically been limited to several manufacturers. In periods of high market demand, the lead times from order to delivery of such equipment can be over one year."
- ASML/Applied/Lam/TEL の名指し: **無し**（grep 0 件。SK siltron は取締役経歴としてのみ登場）。

**c. HBM の売り先（一次根拠）**
- 本目論見書: "In June 2026, we announced a technology partnership with NVIDIA Corporation ("NVIDIA") to advance next-generation memory aligned with NVIDIA's AI infrastructure roadmap, which also includes the supply of memory semiconductors. The two companies expect to collaborate on memory technology for NVIDIA's platforms such as Vera Rubin AI supercomputers, Vera CPUs, RTX Spark-powered PCs and Jetson Thor robotic computing platforms."
- NVIDIA 10-K FY2026（一次(相手側)）: "We purchase memory from SK Hynix Inc., Micron Technology, Inc., and Samsung."
- 市場地位（IDC 引用・目論見書内）: "In the HBM market, we were ranked first globally based on revenue with a market share of 56.4% in the first quarter of 2026"
- 未取得: SK hynix ニュースルームの NVIDIA 提携リリース単体（news.skhynix.com 該当 URL 404）。目論見書本文で代替。

**d. L2→L3**: L2 = NVIDIA（§6.1）。顧客 A（23.9%）は匿名のため L3 を伸ばさない。

---

## 3. Samsung Electronics (005930.KS)

**出所**: Samsung Electronics Business Report 2025（Full Year・英訳版）https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2025_4Q_Interim_Report.pdf（取得 2026-09-13・358頁・`dram_src/samsung_2025_fy.txt`）

**a. 主要販売先（一次・全社ベース・名指しあり）**
- "E. Major customers: In 2025, major customers (listed alphabetically) included Alphabet, Apple, Deutsche Telekom, Hong Kong Techtronics, and Supreme Electronics. Sales to the five major customers accounted for approximately 15% of total sales."
- 注意: 全社（DX/DS/SDC/Harman 合算）の上位5社であり、**DRAM 部門（DS）単独の顧客ではない**。Hong Kong Techtronics・Supreme Electronics（台湾 8299）は半導体流通業者なので DS 側の売り先とみられるが、本文書にその明示は無い（→この一文は未確認）。

**b. 主要仕入先（一次・DS 部門は名指しあり）**
- "3. Production materials — A. Key production materials": "For the DS Division, key materials include chemicals and wafers, supplied by Soulbrain, SILTRONIC, etc."
- 表（DS Division・2025 仕入額 KRW 100mil・部門内構成比・主要供給者）:
  - Chemical（Disk processing）29,900・17.1%・**Soulbrain, DongWoo Fine-Chem, etc.**
  - Wafer（Semiconductor disk）21,625・12.4%・**SILTRONIC, SK Siltron, etc.**
  - Others 122,907・70.5%
- 参考（他部門）: DX の Mobile AP = Qualcomm, MediaTek／Harman の SoC = **Nvidia, Intel**（Harman が Nvidia の顧客）。
- 装置メーカー（ASML 等）の名指し: **無し**。

**c. HBM の売り先（一次根拠）**
- AMD ブログ 2025-06-12（一次(相手側)）: "288GB HBM3E from Micron and Samsung Electronics" → Samsung→AMD Instinct MI350
- NVIDIA 10-K FY2026（一次(相手側)）: "We purchase memory from SK Hynix Inc., Micron Technology, Inc., and Samsung." → Samsung→NVIDIA
- 事業報告書本文: "actively address customer demand with the timely and expanded supply of competitive HBM4 targeting new GPU"・"HBM4 base-die mass production has commenced, backed by stable yields from the 4nm process"（顧客名なし）
- 未取得: Samsung ニュースルームの HBM 供給リリース（検索ヒットは二次報道のみ）。

**d. L2→L3**: L2 = Apple（§6.4）／NVIDIA（§6.1）／AMD（§6.2）。Alphabet・Deutsche Telekom・Hong Kong Techtronics・Supreme Electronics の L3 は未取得。

---

## 4. Nanya Technology (2408.TW)

**出所**: 2025 Annual Report（英語版）https://www.nanya.com/en/Activity?Action=Get_IRannualreport_FileName&Id=25（取得 2026-09-13・187頁・`dram_src/nanya_2025_ar.txt`。一覧 API: `/en/Activity?Action=Get_dtIRannualreport`）

**a. 主要販売先（一次・匿名・比率のみ）**
- "(IV) Suppliers/Customers Accounted for at Least 10% of Annual Procurement/Sales — 1. Major Customers for the Last Two Years"（NT$ thousands）
  - 2025: **B Company** 8,452,304・12.7%・関係 N/A ／ **C Company** 7,739,763・11.6%・N/A ／ Other 75.7% ／ Net sales 66,586,520
  - 2024: **A Company** 5,843,858・17.1% ／ **B Company** 4,079,123・12.0% ／ Other 70.9% ／ Net sales 34,131,667
  - "The changes in the sales amount and proportion are mainly due to changes in customers' product demand."

**b. 主要仕入先（一次・10%以上は無し）**
- "2. Major Suppliers for the Last Two Years": 2025・2024 とも該当 "─"（10% 以上の供給者なし）・Net purchase 12,127,713（2025）／11,817,079（2024）・"Analysis of Changes: None"
- "(III) Supply of Raw Materials": "Raw materials include silicon wafers and chemicals such as photoresist, special gases, and abrasives, and they are provided by the world's leading semiconductor material suppliers from Japan, the U.S. and Taiwan"（社名なし）。調達は "Formosa Technology E-Market Place" の入札。
- 装置メーカー名指し: **無し**（"U.S. equipment suppliers" への依存に言及のみ）。

**c. HBM**: 該当なし（HBM を製品化していない旨の一次記述は本報告書で確認できず・**未確認**）。
**d. L2→L3**: L2 が匿名のため伸ばせない。

---

## 5. Winbond Electronics (2344.TW)

**出所**: 2025 Annual Report（英語版）https://www.winbond.com/export/sites/winbond/about-winbond/investor/financial-information/annual-report/report/2025_Winbond_Annual_Report__EN.pdf（取得 2026-09-13・`dram_src/winbond_2025_ar.txt`）。⚠️ PDF の埋め込みフォントの都合で pdftotext 出力の大半が文字化け（例: "ϮϬϮϱ"="2025"）。下記の数値・社名は文字化けの規則（字形の一対一対応）から復号したもの。原本ページ（p.80 付近）での目視確認は**未実施**。

**a. 主要販売先（一次・名指し1社）**
- "(5) List of clients accounting for 10% or more of the company's total sales amount in either of the 2 most recent fiscal years": 復号 → "Winbond sold NT$11,176,472 thousand worth of goods to **Silicon Application Corp.** in 2025, accounting for 12.5% of consolidated sales for the year."（2024 年分の記載なし＝10%超なし と読める・未確認）

**b. 主要仕入先（一次・匿名・比率のみ）**
- "(4) List of suppliers accounting for 10% or more of the company's total purchases amount": 復号 →
  - 2025: **Supplier K** 3,477,494・17.2%・関係 Affiliates ／ **Supplier M** 2,526,603・12.5%・None ／ Other 70.3% ／ Net purchase 20,266,763
  - 2024: Supplier K 3,745,639・19.0%・Affiliates ／ Supplier M 2,094,529・10.6%・None ／ Net purchase 19,687,431
  - "Supplier change-related explanation: Adjustments to procurement ratios have been made in response to the Company's wafer input mix and procurement strategies."
- 装置メーカー名指し: **無し**（文字化け領域の grep は不完全＝未確認）。
- 2024 Annual Report（`winbond_2024_ar.pdf`）も取得したが同様に文字化けで表を抽出できず。

**c. HBM**: 該当なし。
**d. L2→L3**: Silicon Application Corp. の開示文書は未取得。

---

## 6. L2 各社の主要取引先（L3）

### 6.1 NVIDIA
**出所**: Form 10-K FY2026（会計年度末 2026-01-25・提出 2026-02-25）https://www.sec.gov/Archives/edgar/data/1045810/000104581026000021/nvda-20260125.htm（`dram_src/nvda_10k.txt`）
- 販売先（匿名）: "For fiscal year 2026, sales to one direct customer represented 22% of total revenue and sales to another direct customer represented 14% of total revenue, all of which were primarily attributable to the Compute & Networking segment." 間接顧客: "some individually representing 10% or more of our revenue"・"We estimate that one AI research and deployment company contributed to a meaningful amount of our revenue purchasing cloud services from our customers in fiscal year 2026."（社名なし）
- 仕入先（名指し）: "We utilize foundries, such as Taiwan Semiconductor Manufacturing Company Limited, or TSMC, and Samsung Electronics Co., Ltd., or Samsung, to produce our semiconductor wafers. We purchase memory from SK Hynix Inc., Micron Technology, Inc., and Samsung. We utilize CoWoS technology for semiconductor packaging. We engage with independent subcontractors and contract manufacturers such as Hon Hai Precision Industry Co., Ltd., Wistron Corporation, and Fabrinet to perform assembly, testing and packaging of our final products."
- → L3: **TSMC・Samsung（ファウンドリ）／SK hynix・Micron・Samsung（メモリ）／Hon Hai・Wistron・Fabrinet（組立）**。販売先は匿名。

### 6.2 AMD
**出所**: Form 10-K FY2025（会計年度末 2025-12-27・提出 2026-02-04）https://www.sec.gov/Archives/edgar/data/2488/000000248826000018/amd-20251227.htm（`dram_src/amd_10k.txt`）
- 販売先: "No customer accounted for at least 10% of the Company's consolidated net revenue in fiscal years 2025 and 2024. One Client and Gaming segment customer accounted for 18% of consolidated net revenue in fiscal year 2023."（匿名）。売掛金: "One customer accounted for approximately 11% … of the total consolidated accounts receivable balance as of December 27, 2025"。名指し（セミカスタム）: "AMD semi-custom SoC products power the Sony PlayStation® 5, the Microsoft® Xbox Series S™ and X™ game consoles, as well as the recently revealed Valve Steam Machine PC."
- 仕入先（名指し）: "We utilize Taiwan Semiconductor Manufacturing Company Limited (TSMC) for the production of wafers for our HPC, FPGA and adaptive SoC products and GLOBALFOUNDRIES Inc. (GF) … Additionally, we utilize TSMC, United Microelectronics Corporation (UMC) and Samsung Electronics Co., Ltd. for the production of our ICs in the form of programmable logic devices." 後工程: "two ATMP joint ventures … with Tongfu Microelectronics Co., Ltd. The ATMP JVs, Siliconware Precision Industries Ltd. (SPIL) and King Yuan Electronics Company (KYEC) provide ATMP services"
- HBM 仕入（AMD ブログ 2025-06-12）: "288GB HBM3E from Micron and Samsung Electronics"
- → L3: **TSMC・GlobalFoundries・UMC・Samsung（前工程）／Tongfu・SPIL・KYEC（後工程）／Micron・Samsung（HBM）／Sony・Microsoft・Valve（セミカスタム顧客）**

### 6.3 Broadcom
**出所**: Form 10-K FY2025（会計年度末 2025-11-02・提出 2025-12-18）https://www.sec.gov/Archives/edgar/data/1730168/000173016825000121/avgo-20251102.htm（`dram_src/avgo_10k.txt`）
- 販売先（匿名）: "Direct sales to one semiconductor solutions customer, which is a distributor, accounted for 32% and 28% of our net revenue for fiscal years 2025 and 2024, respectively. We believe aggregate sales to our top five end customers, through all channels, accounted for approximately 40% of our net revenue for each of the fiscal years 2025 and 2024." "Sales to distributors accounted for 48% of our net revenue"。AI 顧客の属性: "Customers of these solutions are hyperscalers and companies with AI frontier models"（社名なし）
- 仕入先（名指し）: "The majority of our front-end wafer manufacturing operations is outsourced to external foundries, including Taiwan Semiconductor Manufacturing Company Limited ("TSMC"). We use third-party CMs for a significant majority of our assembly and test operations, including TSMC, Advanced Semiconductor Engineering, Inc., Foxconn Technology Group, Amkor Technology, Inc. and Siliconware Precision Industries Co., Ltd." "During fiscal year 2025, approximately 95% of the wafers manufactured by our CMs were produced by TSMC."
- HBM 仕入先の名指し: **無し**（10-K で SK hynix/Micron/Samsung の grep 0 件）→ DRAM 5社→Broadcom の一次根拠は本調査では**未取得**。
- → L3: **TSMC（ウエハ 95%）／ASE・Foxconn・Amkor・SPIL（組立）**。販売先は匿名。

### 6.4 Apple（Samsung の主要顧客として）
**出所**: Form 10-K FY2025（会計年度末 2025-09-27・提出 2025-10-31）https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm（`dram_src/aapl_10k.txt`）
- 仕入先: "certain components are currently obtained from single or limited sources" "A significant majority of the Company's manufacturing is performed in whole or in part by outsourcing partners located primarily in China mainland, India, Japan, South Korea, Taiwan and Vietnam"。社名の名指し（TSMC/Foxconn/Samsung 等）: **無し**（grep 0 件）。
- 販売先: 10% 以上の顧客開示なし。
- → L3: 名指し無し＝**未開示**。

---

## 7. 木（要約）

```
Micron ──(匿名17% CMBU)──▶ ?
       └─(HBM 一次)──────▶ NVIDIA ──▶ TSMC / Samsung(foundry) / Hon Hai / Wistron / Fabrinet（販売先は匿名22%/14%）
                         ▶ AMD ─────▶ TSMC / GF / UMC / Samsung / Tongfu / SPIL / KYEC ／ 顧客 Sony / Microsoft / Valve
SK hynix ─(匿名A 23.9%)──▶ ?
       └─(2026-06 提携 一次)▶ NVIDIA（上と同じ L3）
Samsung ─(全社上位5社 15%)▶ Alphabet / Apple / Deutsche Telekom / Hong Kong Techtronics / Supreme Electronics
       └─(HBM3E 一次相手側)▶ AMD, NVIDIA
       ◀─(仕入 名指し)──── Soulbrain / DongWoo Fine-Chem（薬品）, SILTRONIC / SK Siltron（ウエハ）
Nanya ──(匿名 B 12.7% / C 11.6%)▶ ?          ◀── 仕入 10%超なし
Winbond ─(12.5%)────────▶ Silicon Application Corp.   ◀── Supplier K 17.2%(関係会社) / Supplier M 12.5%
Broadcom（L2候補）: DRAM 5社からの供給の一次根拠なし。L3 = TSMC 95% / ASE / Foxconn / Amkor / SPIL
```

---

## 8. 集計

**取得できた一次文書: 12 件**
1. Micron 10-K FY2025 ／ 2. SK hynix 424B4 2026 ／ 3. Samsung Business Report FY2025 ／ 4. Nanya 2025 Annual Report ／ 5. Winbond 2025 Annual Report（文字化け・復号） ／ 6. Winbond 2024 Annual Report（文字化け・未抽出） ／ 7. NVIDIA 10-K FY2026 ／ 8. AMD 10-K FY2025 ／ 9. Broadcom 10-K FY2025 ／ 10. Apple 10-K FY2025 ／ 11. Micron プレスリリース 2025-06-12（AMD MI350） ／ 12. AMD ブログ 2025-06-12（MI350）

**未取得（一次で当たれなかったもの）**
- Micron 2024-02-26 HBM3E→NVIDIA H200 プレスリリース原本（URL 404・二次報道のみ）
- SK hynix ニュースルームの NVIDIA 提携リリース原本（404・目論見書本文で代替済）
- Samsung ニュースルームの HBM 供給リリース（AMD/NVIDIA）（検索ヒットは二次のみ）
- Broadcom への DRAM 供給の一次根拠（Broadcom 10-K に社名なし）
- Alphabet・Deutsche Telekom・Hong Kong Techtronics・Supreme Electronics・Silicon Application Corp. の開示文書（L3 未展開）
- Winbond 2025 AR の原本ページ目視（復号値の裏取り）

**要点（推定なし・開示事実のみ）**
- DRAM 5社のうち、年次開示で**顧客名を明かしているのは Samsung（全社上位5社）と Winbond（1社）のみ**。Micron・SK hynix・Nanya は匿名比率のみ。
- 装置メーカー（ASML/Applied/Lam/TEL）の名指しは **5社とも 0 件**。材料の社名は Samsung のみ（Soulbrain・DongWoo Fine-Chem・SILTRONIC・SK Siltron）。
- HBM→GPU の一次根拠: NVIDIA 10-K が「SK hynix・Micron・Samsung からメモリを購入」と3社を名指し。AMD MI350 は Micron・Samsung を名指し（AMD ブログ・Micron PR）。

**参考（推定（二次）・根拠に不採用）**: TrendForce 2025-06-13「MI350 の HBM3E は Samsung/Micron の2社供給」、KED Global 2025-06-13「Samsung が AMD へ HBM3E 供給」、Tom's Hardware/HPCwire 2024-02-26「Micron HBM3E が NVIDIA H200 に採用」。
