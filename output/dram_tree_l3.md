# DRAM ツリー L3: TSMC / Intel / Applied Materials の主要取引先（一次資料 実読）

作成 2026-09-13。出所は全て SEC EDGAR の年次報告書本文（curl で取得・HTML→テキスト化して実読）。
推定は書かない。報告書が社名を出していない箇所は「匿名」と明記。取得できなかった項目は「未取得」。

---

## 1. TSMC（Taiwan Semiconductor Manufacturing Co., Ltd.）

**出所**: Form 20-F FY2025（会計年度 2025-12-31・提出日 2026-04-16・accession 0001628280-26-025362）
https://www.sec.gov/Archives/edgar/data/1046179/000162828026025362/tsm-20251231.htm

### 主要販売先（顧客集中）
- 顧客名は **開示なし（匿名 Customer A/B/C）**。
- リスク要因の記述（原文）:
  > "our ten largest customers in 2023, 2024 and 2025 accounted for approximately, 70%, 76% and 78% of our net revenue in the respective year. Our largest customer in 2023, 2024 and 2025 accounted for 25%, 22% and 19% of our net revenue in the respective year. Our second largest customer in 2023, 2024 and 2025 accounted for 11%, 12%, and 17% of our net revenue in the respective year."
- 財務諸表注記「Major customers representing at least 10% of net revenue」（NT$ 百万・%）:

| 顧客 | 2023 | 2024 | 2025 |
|---|---|---|---|
| Customer A | 10% 未満（NA） | 352,271.2 / 12% | 726,974.3 / 19% |
| Customer B | 546,550.9 / 25% | 624,345.5 / 22% | 645,178.7 / 17% |
| Customer C | 241,152.4 / 11% | 10% 未満（NA） | 10% 未満（NA） |

  （注: Customer A は 2024→2025 で売上 2.06 倍。リスク要因側の「largest customer 19%（2025）」= Customer A、「second largest 17%」= Customer B に対応。社名は本文のどこにも出ない）
- 売掛金集中: "As of December 31, 2024 and 2025, the Company's ten largest customers accounted for 93% and 84% of accounts receivable, respectively."
- 顧客集中の背景（原文）: "This customer concentration results in part from the changing dynamics of the electronics industry with the structural shift to HPC and smartphone applications ... there is a growing trend among system companies designing their own semiconductors and working directly with the semiconductor foundries"

### プラットフォーム別売上（NT$ 百万・構成比）
| プラットフォーム | 2023 | 2024 | 2025 |
|---|---|---|---|
| High Performance Computing | 934,769 / 43% | 1,476,891 / 51% | 2,192,931 / 58% |
| Smartphone | 814,914 / 38% | 1,005,130 / 35% | 1,110,816 / 29% |
| Internet of Things | 161,917 / 8% | 165,516 / 6% | 191,047 / 5% |
| Automotive | 133,654 / 6% | 139,323 / 5% | 186,667 / 5% |
| Digital Consumer Electronics | 47,000 / 2% | 47,961 / 1% | 47,997 / 1% |
| Others | 69,482 / 3% | 59,487 / 2% | 79,596 / 2% |
| 合計 | 2,161,736 | 2,894,308 | 3,809,054 |

- 原文: "The increase in our net revenue from 2024 to 2025 mainly came from High Performance Computing of NT$716,040 million, or a 48% year-over-year increase, and from Smartphone of NT$105,686 million, or a 11% year-over-year increase."

### 地域別売上（顧客所在地・構成比）
| 地域 | 2023 | 2024 | 2025 |
|---|---|---|---|
| North America | 1,470,215 / 68% | 2,031,326 / 70% | 2,875,270 / 75% |
| Asia Pacific（中国・日本除く） | 174,947 / 8% | 284,308 / 10% | 329,269 / 9% |
| China | 267,154 / 12% | 331,673 / 11% | 327,503 / 9% |
| Japan | 132,072 / 6% | 144,240 / 5% | 150,428 / 4% |
| EMEA | 117,348 / 6% | 102,761 / 4% | 126,584 / 3% |

### 主要仕入先
- **社名の名指しなし**（ASML・Tokyo Electron・Shin-Etsu・SUMCO 等は本文に登場しない。Applied Materials は取締役の経歴欄にのみ登場・取引先としての記述ではない）。
- 装置（原文）: "Our operations and ongoing expansion plans depend on our ability to obtain necessary equipment and related services available from a limited number of suppliers." / "Some of the equipment is available from a limited number of suppliers and/or is manufactured in relatively limited quantities ... We work closely with manufacturers that provide equipment customized to our needs for certain advanced technologies."
- 原材料（原文）: "Our manufacturing processes use many raw materials, primarily silicon wafers, chemicals, gases and various types of precious metals. Although most of our raw materials are available from multiple suppliers, some materials are purchased through sole-sourced suppliers." / "The most important raw material used in our production is silicon wafer ... the majority of our raw wafers are supplied by a limited number of suppliers located in **Taiwan, Japan, Germany, and Singapore**."
- 装置の種類（列挙）: "...deposition ("CVD") equipment, chemical mechanism polish ("CMP") equipment, testers and probers."

---

## 2. Intel Corporation

**出所**: Form 10-K FY2025（会計年度 2025-12-27・提出日 2026-01-23・accession 0000050863-26-000011）
https://www.sec.gov/Archives/edgar/data/50863/000005086326000011/intc-20251227.htm

### 主要販売先（顧客集中）
- 本文表は匿名（Customer A/B/C）だが、**XBRL タグで A = Dell Inc.、B = Lenovo Group Limited と名指し**（`intc:DellIncMember` / `intc:LenovoGroupLimitedMember`）。Customer C は XBRL でも社名なし（HP Inc. の名指しは **本 10-K にはない**。HP Inc. は特許訴訟の記述にのみ登場）。
- 原文: "Collectively, our three largest customers accounted for 43% of our net revenue in 2025, 45% of our net revenue in 2024 and 40% of our net revenue in 2023." / "In 2025, substantially all of the revenue from our three largest customers was generated from the sale of platforms and other components by our Intel Products operating segments."

| 顧客 | FY2025 | FY2024 | FY2023 |
|---|---|---|---|
| Customer A = **Dell Inc.**（XBRL） | 19% | 19% | 19% |
| Customer B = **Lenovo Group Limited**（XBRL） | 12% | 14% | 11% |
| Customer C（匿名） | 12% | 12% | 10% |
| 3社合計 | 43% | 45% | 40% |

- 売掛金: "net accounts receivable balances from our three largest customers (47% as of December 27, 2025 and December 28, 2024)"
- 顧客側の変化（原文）: "Industry trends, such as the increase in AI workloads and shift of data center workloads to the public cloud, have increased the significance and purchasing power of certain customers"

### 地域別売上（請求先所在地・$ 百万）
| 地域 | FY2025 | FY2024 | FY2023 |
|---|---|---|---|
| United States | 15,757 | 12,994 | 13,958 |
| China | 12,694 | 15,532 | 14,854 |
| Singapore | 9,535 | 10,187 | 8,602 |
| Taiwan | 7,672 | 7,804 | 6,867 |
| Other regions | 7,195 | 6,584 | 9,947 |

### 主要仕入先（名指しあり）
- **ASML**（EUV 露光装置の唯一供給者）: "ASML Holding N.V. (ASML) is currently the sole supplier of EUV lithography tools that we are deploying in our Intel 4, Intel 3, Intel 18A and planned future leading-edge manufacturing process nodes."
- **TSMC**（外部ファウンドリ・タイル供給）: "We expect to continue to use TSMC and other third-party foundry suppliers for various key tiles in a number of current and future products" / "Some of our most advanced current and future products are or will be either exclusively manufactured by TSMC or reliant upon critical components, including various compute die, manufactured by TSMC." / "We have no long-term contract with TSMC"
- 供給網一般: "we rely on a global supply chain encompassing thousands of suppliers worldwide ... In some cases, however, we are reliant upon sole-source providers, such as with the EUV lithography tools manufactured by ASML ... or providers that are substantially concentrated in a single country, such as with certain rare earth minerals critical to the functioning of a range of technology products and processes where China is the primary source of global supply."
- 長期購入契約: "To obtain future supply of certain materials and components, particularly substrates, and third-party foundry manufacturing capacity, we have entered into arrangements with some of our suppliers that involve long-term purchase commitments and/or large prepayments."（基板の供給元社名は未記載）

---

## 3. Applied Materials, Inc.

**出所**: Form 10-K FY2025（会計年度 2025-10-26・提出日 2025-12-12・accession 0001628280-25-056742）
https://www.sec.gov/Archives/edgar/data/6951/000162828025056742/amat-20251026.htm

### 主要販売先（顧客集中）
- **社名は開示なし**（TSMC / Samsung / Intel の名指しは本文に存在しない。XBRL も `amat:CustomerOneMember` / `amat:CustomerTwoMember` の匿名タグのみ）。
- 原文（Item 1 と財務諸表注記の両方に同文）: "During fiscal 2025, two customers accounted for approximately 19% and 15%, respectively, of our net revenue. During fiscal 2024, two customers accounted for approximately 12% and 11%, respectively, of our net revenue. During fiscal 2023, two customers accounted for approximately 19% and 15%, respectively, of our net revenue."

| 顧客 | FY2025 | FY2024 | FY2023 |
|---|---|---|---|
| Customer One（匿名） | 19% | 12% | 19% |
| Customer Two（匿名） | 15% | 11% | 15% |

### 半導体装置（Semiconductor Systems）の市場別構成比
| 市場 | FY2025 | FY2024 | FY2023 |
|---|---|---|---|
| Foundry, logic and other | 67% | 68% | 77% |
| DRAM | 26% | 28% | 17% |
| Flash memory (NAND) | 7% | 4% | 6% |

### 地域別売上（出荷先所在地・$ 百万・構成比）
| 地域 | FY2025 | FY2024 |
|---|---|---|
| China | 8,529 / 30% | 10,117 / 37% |
| Korea | 5,608 / 20% | 4,493 / 17% |
| Taiwan | 6,857 / 24% | 4,010 / 15% |
| Japan | 2,273 / 8% | 2,154 / 8% |
| Southeast Asia | 1,076 / 4% | 1,141 / 4% |
| Asia Pacific 小計 | 24,343 / 86% | 21,915 / 81% |
| United States | 3,063 / 11% | 3,818 / 14% |

（Taiwan 向けが前年比 +71%、Korea +25%、China -16%。顧客名は出ないため国別が実質の手がかり）

### 主要仕入先
- **社名の名指しなし**。
- 原文: "We utilize a distributed manufacturing model under which manufacturing and supply chain activities are conducted in various countries, including United States, Singapore, Japan, China, Korea, Taiwan, Israel and other countries in Asia and Europe. We use qualified vendors, including contract manufacturers, to supply parts, services and product support." / "Although we make reasonable efforts to assure that parts are available from multiple qualified suppliers, this is not always possible. Accordingly, some key parts may be obtained from only a qualified single supplier or a limited group of qualified suppliers."
- 供給網リスク: "exports of certain technologies to China, where a significant portion of our supply chain is located" / "limited availability of critical materials and minerals, including due to Chinese government restrictions on the export of certain rare earth minerals implemented in 2025"

---

## 取得サマリ
| 会社 | 報告書 | 販売先の名指し | 販売先の比率 | 仕入先の名指し |
|---|---|---|---|---|
| TSMC | 20-F FY2025（取得済） | なし（匿名 A/B/C） | 上位10社 78%・最大 19%・2位 17% | なし（地域のみ: 台湾・日本・独・シンガポールのウェハ供給者） |
| Intel | 10-K FY2025（取得済） | Dell（XBRL）・Lenovo（XBRL）・C は匿名 | 19% / 12% / 12%・3社 43% | ASML（EUV 唯一）・TSMC（タイル・先端品） |
| Applied Materials | 10-K FY2025（取得済） | なし（匿名2社） | 19% / 15% | なし |

未取得: なし（3社とも本文取得・実読済み）。ただし「名指しなし」は報告書が書いていないという意味であり、取引の不存在を示すものではない。
