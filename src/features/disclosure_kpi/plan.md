# plan: 有報＋決算説明会資料から「何が売れると業績が上がるか」を取る——味の素 1 社の通し（P-INF-34 第 1 周）

## 目的
会社ごとに「何が売れると（品切れになると）利益が増えるか」を、有価証券報告書（EDINET・XBRL の CSV）と決算説明会資料から KPI の言葉で取り、期ごとに需要／期待／未判定／物なしを付けて、センターピン台帳に足す前の**ステージング**（`output/yuho/`）に出す。この周は味の素（2802）1 社で工程①〜④を通し、**1 社あたりの費用（入力・出力トークン）と埋まり方**を実測して 50 社の可否を裁定に戻す。設計の正本= vault `02_Ai/influx/notes/influx-disclosure-kpi-ledger-2026-10-07.md`（v2.1）・追跡= `tasks/disclosure_kpi_ledger.md`。

## 成功基準
- [ ] `data/edinet/yuho/2802/<docID>.sections.json` が直近 3 期分（S100Y992・S100VXJA・S100TNWR）できる。各ファイルで読む節 7 つのうち 5 つ以上に本文があり、`facts.segments` に報告セグメント 3 つ × 当期・前期の売上と利益が入る
- [ ] `output/yuho/2802/<docID>.read.json` が 3 本でき、各ファイルに `usage.input_tokens`・`usage.output_tokens` と `rows`（1〜3 行・各行に segment／item／kpi_name／quote／quote_section）がある
- [ ] `output/yuho/2802/products.json` に 1 行以上の品目行があり、各行の `periods[]` が 3 期分で、期ごとに `share_pct`・`share_basis`・`profit_measure`・`cond_a/b/c`・`label`・`source.doc_id` を持つ。`summary.md` に費用の式〈入力トークン合計 × 単価 ＋ 出力トークン合計 × 単価〉と実測値が書かれる
- [ ] `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_yuho_fetch tests.test_yuho_judge` が PASS、一括ゲート `python -m unittest discover -s tests` も全件 PASS
- [ ] `data/center_pin/center_pin.jsonl` は 1 バイトも変わらない（`git diff --stat data/center_pin/` が空）

## 影響範囲
- 新規: `scripts/yuho_fetch.py`／`scripts/yuho_read.py`／`scripts/yuho_judge.py`／`scripts/ir_pdf_text.py`／`tests/test_yuho_fetch.py`／`tests/test_yuho_judge.py`／`tests/fixtures/yuho_mini.csv`
- 生成物（git 管理外）: `data/edinet/yuho/2802/`（zip・sections.json・ir/*.pdf・ir/*.txt）／`output/yuho/2802/`（read.json・products.json・summary.md）
- 更新: `tasks/disclosure_kpi_ledger.md`（進捗）
- 再利用（読むだけ）: `scripts/edinet_fetch.py` の `get_api_key()`・`_inject_api_key()`・`BASE_URL`／`collector/llm_classifier.py` の urllib 直叩きの型（SDK なし・`ANTHROPIC_API_KEY` は docker-compose が渡す）

## 変更禁止ファイル
- `data/center_pin/center_pin.jsonl`（列追加は検収 ⑤ の後・別周）
- `docs/price-watch-universe.md`（§0a の更新は列追加と同じ commit・別周）
- `scripts/edinet_fetch.py`（import して使うだけ）
- `configs/`・`config/`・`collector/`・`docker-compose*.yml`・`requirements.txt`（依存を増やさない。pypdf・requests は既にある）

## 全体図
```
軸①取得(yuho_fetch)      軸②読み(yuho_read)        軸③判定(yuho_judge)        軸④検収(次周)
 担い手: Docker python    担い手: Claude API(urllib)  担い手: Docker python       担い手: オーナー(カードUI)
 止まる線: EDINET 404/    止まる線: 応答が JSON でない 止まる線: 連結利益が null   止まる線: ○が0行
   鍵なし/csvFlag=0         →1回だけ再試行→失敗記録     →share=null・未判定         →台帳へ書かない
 既知の穴: 要素IDの年度差   既知の穴: 説明会資料の盛り   既知の穴: IFRS事業利益≠営業利益 既知の穴: 鮮度(年1回)
   /IFRSに生産販売実績なし    →有報を正・資料名を source   →profit_measure を必ず記録
```
- **データの流れ**: documents_all（docID）→ EDINET type 5 zip → sections.json（節本文＋数値 facts）→ ＋ ir/*.txt → read.json（LLM 候補行・usage）→ products.json（期ごとの判定）→ summary.md（費用・埋まり）

## タスク（粒度: 1 ファイル 1 論理変更・各 2〜5 分・バッチ最大 3 編集で検証）

### T1 `scripts/yuho_fetch.py`（新規）
- 関数: `find_yuho_docs(sec_code5: str, n: int, docs_dir=Path("data/edinet/documents_all")) -> list[dict]`（`*.json.gz` を走査・`.rev` 入りは除外・`docTypeCode=="120"` かつ `secCode==sec_code5`・`submitDateTime` 降順・同じ docID は 1 回。返す dict= docID／submitDateTime／periodStart／periodEnd／csvFlag）
- 関数: `fetch_csv_zip(doc_id, api_key, out_dir) -> Path`（`{BASE_URL}/documents/{docID}?type=5` に `_inject_api_key`・既存ファイルがあれば再取得しない・1 回ずつ）
- 関数: `extract_sections(zip_path) -> dict`（zip 内の `jpcrp030000-asr-*.csv` を UTF-16・タブ区切りで読む。**節**は要素 ID の部分一致: business_policy=`BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock`／mdna=`ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock`／risks=`BusinessRisksTextBlock`／segment_notes=`SegmentInformation`を含む TextBlock（IFRS は `NotesSegmentInformationConsolidatedFinancialStatementsIFRSTextBlock`）／business_results=`BusinessResultsOfGroupTextBlock`／business_description=`DescriptionOfBusinessTextBlock`／production_sales=`ProductionOrdersReceivedAndSales` を含む TextBlock（無ければ null）。**facts**は数値行から: 連結= `NetSalesIFRS|RevenueIFRS|NetSales`・`GrossProfitIFRS|GrossProfit`・`OperatingProfitLossIFRS|OperatingIncome`・`BusinessProfit`（コンテキストが `CurrentYearDuration`／`Prior1YearDuration` ちょうどのもの）。セグメント= コンテキストに `ReportableSegmentMember` を含む行のうち売上（NetSales|Revenue）と利益（`OperatingIncome|OperatingProfit|BusinessProfit|SegmentProfit` の順で最初に見つかった要素を `profit_measure` として全セグメントに同じ物を使う）。`ReconcilingItemsMember`・`TotalOfReportableSegments` は別キーに。会計基準= `jpigp_cor:` の要素があれば `IFRS` 無ければ `JGAAP`）
- 出力: `data/edinet/yuho/<code4>/<docID>.sections.json`＝`{code4, sec_code, doc_id, submit_date, period_start, period_end, accounting, sections:{key:{element_id, chars, text}|null}, facts:{consolidated:{ctx:{net_sales, gross_profit, operating_profit, business_profit}}, segments:{member:{label, ctx:{net_sales, segment_profit}}}, profit_measure, reconciling:{ctx:{segment_profit}}, total_segments:{ctx:{segment_profit}}}}`
- CLI: `python scripts/yuho_fetch.py --code 2802 --n 3 [--no-fetch]`。1 行ずつ `saved <path> sections=<有本文数>/7 segments=<数> accounting=<IFRS|JGAAP>` を出す
- 検証: `docker compose run --rm -e EDINET_API_KEY xstock python scripts/yuho_fetch.py --code 2802 --n 3` → 3 行の `saved`・S100Y992 は `sections=6/7 segments=3 accounting=IFRS`（期待値・2026-10-07 scratchpad 実測と一致すること）

### T2 `tests/fixtures/yuho_mini.csv` ＋ `tests/test_yuho_fetch.py`（新規）
- fixture: UTF-16 LE・タブ区切り・ヘッダ 9 列・15 行程度（TextBlock 3 本・連結 2 要素×2 期・セグメント 2 つ×売上/利益×2 期・Reconciling 1 行）。zip に詰めるのはテスト内で `zipfile` で作る
- test: `extract_sections` が sections 3 本の chars を正しく数え、facts.segments が 2 つ・profit_measure が利益要素 ID になる／`find_yuho_docs` が tmp の `documents_all` 偽物（gz 2 日分・.rev 1 つ）から 120 だけを降順で返す
- 検証: `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_yuho_fetch -v` → `OK`

### T3 `scripts/ir_pdf_text.py`（新規）
- 関数: `pdf_to_text(pdf_path) -> str`（pypdf・ページ区切りに `\n\n[page N]\n`）。CLI: `python scripts/ir_pdf_text.py --pdf data/edinet/yuho/2802/ir/FY2025_presentation.pdf` → 同名 `.txt` を隣に書き `pages=<n> chars=<n>` を出す。`--url <URL> --out <path>` で先に保存（urllib・1 回）
- 検証: 上の CLI で `.txt` ができ chars>1000（PDF は別エージェントが `data/edinet/yuho/2802/ir/` に置く。無い時はこのタスクを後回しにし、他を先に進める）

### T4 `scripts/yuho_read.py`（新規）
- 関数: `build_prompt(sections_json: dict, ir_text: str|None, max_ir_chars=20000) -> str`（叩き台 §4 の列を JSON で返させる指示＋節本文。説明会資料は末尾に「任意開示・有報と食い違う数字は有報を正」と添えて付ける）
- 関数: `call_claude(prompt, model, api_key, max_tokens=4000) -> dict`（`collector/llm_classifier.py` と同じ urllib 直叩き・`https://api.anthropic.com/v1/messages`・`anthropic-version: 2023-06-01`・応答の `usage` を必ず返す・JSON 抽出に失敗したら 1 回だけ再試行し、それでも失敗なら `rows=[]` と `error` を記録）
- 出力: `output/yuho/<code4>/<docID>.read.json`＝`{doc_id, model, usage:{input_tokens, output_tokens}, prompt_chars, rows:[{segment, item, kpi_name, kpi_value, kpi_unit, kpi_prev_value, share_scope, label_hint, quote, quote_section, expectation(bool)}], error}`
- CLI: `python scripts/yuho_read.py --code 2802 [--model claude-sonnet-5] [--dry-run]`（dry-run は prompt_chars と文字数÷1 の概算トークンを出して API を呼ばない）
- 検証: `docker compose run --rm xstock python scripts/yuho_read.py --code 2802 --dry-run` → 3 行 `S100Y992 prompt_chars=... est_tokens=...`。実走は `--model claude-sonnet-5` で 3 本の read.json・各 `usage` あり

### T5 `scripts/yuho_judge.py`（新規）
- 関数: `share_pct(seg_profit, consolidated_profit) -> float|None`（分母 null は None）／`judge_period(prev_facts, cur_facts, seg_member) -> dict`（cond_a= share≥30〈利益比 or 売上比のどちらか〉・cond_b= セグメント売上 +5% 以上 かつ 利益増・cond_c= 連結粗利率が前期比で下がっていない。どれかの数字が欠けたら該当 cond は None）／`label_of(conds, expectation) -> str`（3 つ True→需要／expectation→期待／どれか None→未判定／それ以外→需要条件不成立）
- 入力: 3 本の sections.json ＋ 3 本の read.json。行の対応は `segment` 名でセグメント member を引く（部分一致・引けなければ share=None）
- 出力: `output/yuho/<code4>/products.json`＝`{code4, products_ver:1, products_status:済|未判定|物なし, products:[{segment, item, kpi_name, share_scope, periods:[{period_end, doc_id, kpi_value, seg_sales, seg_profit, profit_measure, share_pct, share_basis, gross_margin, cond_a, cond_b, cond_c, label, quote, source:{doc_id, element_id}}]}]}` と `summary.md`（表＋費用の式と実測）
- 検証: `docker compose run --rm xstock python scripts/yuho_judge.py --code 2802` → `products.json` の品目行 ≥1・期 3 つ・`summary.md` に `input_tokens=` の合計行

### T6 `tests/test_yuho_judge.py`（新規）
- `share_pct`（分母 None→None・79.0 の例）／`judge_period`（3 条件 True→需要・cond_c False→需要条件不成立・欠測→未判定・expectation→期待）
- 検証: `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_yuho_judge -v` → `OK`

### T7 一括ゲートと記録
- `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest discover -s tests` 全件 PASS／`git diff --stat data/center_pin/` が空／`tasks/disclosure_kpi_ledger.md` の Progress を更新／意味単位で commit（scripts＋tests／tasks）

### T8 50 社の試走（2026-10-08 オーナー裁定: 鍵は入れない・セッション内の Sonnet サブエージェントで読む）
- 選定（既定）: center_pin 977 社 → J-Quants bars の時価総額 ≥ 50,000 百万円 → 物を売る業種（除外= 銀行・証券・保険・その他金融・不動産・サービス・情報通信・電気ガス・陸運・海運・空運・倉庫運輸・建設）→ 業種ごとに時価総額上位を 1 社ずつ回して 50 社。選定の記録= scratchpad `trial50/selection.json`（母集団・除外・50 社の一覧）
- 取得: `scripts/yuho_fetch.py --code <4桁> --n 3` を 50 社ぶん順に（Docker・`-e EDINET_API_KEY`）。失敗した社は `failed` に残す
- 説明会資料: 会社ごとに IR サイトから直近 3 期の通期決算説明会資料 PDF を探すサブエージェント（Web 読み取りのみ・見つからない社は `ir/NONE.txt` に理由を書いて「説明会資料なし」）→ `scripts/ir_pdf_text.py` で txt（名前= `FY<期首年>_presentation`）
- 読み: `scripts/yuho_read.py --code <4桁> --dry-run` でプロンプトを作り、**1 docID 1 体の Sonnet サブエージェント**（Claude Code の Agent・API 鍵なし）が JSON 配列を返す → scratchpad `yuho_pilot/to_read_json.py` で read.json（model に「代走」を明記）
- 判定: `scripts/yuho_judge.py --code <4桁>` を 50 社ぶん → 集計は yuho_judge に `--all`（`output/yuho/*/products.json` を束ねて `output/yuho/trial_summary.md`）を足す（新規ファイルは作らない）
- 成功基準の測り方: 埋まり率= `products_status=済` かつ `complete_products ≥ 1` の社 ÷（50 − 物なし）。「物なし」は人が付ける（機械は付けない）＝この試走では分母 50 で仮置きし、物なし候補を別に列挙する
- 検証: 件数の突合〈選んだ 50／取得できた N／読めた docID 数／判定できた社〉を summary に書く。読みの形は `normalize_rows` の warnings で数える


#### T8 の結果（2026-10-08 実測・詳細= vault §13b）
- 件数の突合: 選んだ 50／取得 150 docID（50 社 × 3 期・exit 0 = 50/50）／説明会資料の本文 146/150（うち 5333 の 3 本は別会社 5334 日本特殊陶業の資料＝正しくは 143・後追いで判明・判定への影響なし）／読み= Sonnet サブエージェント 150 体・応答 150/150（JSON 不成立 0・行 409・必須列欠落で 38 行破棄）／判定 50/50／`--all` → `output/yuho/trial_summary.md`
- 埋まり率= 48/50 = 0.96（分母は「物なし」を除く前・物なし候補 0）。未完の 2 社= HOYA・オリンパス（出所が説明会資料だけ）
- 品目ラベル 1,080 マス= 需要（区分）38／不成立 126／期待 2／未判定〈当期候補なし 672・区分数値なし 133・KPI 非数値 109〉
- 途中で直した取りこぼし: 報告セグメント member の複数形（19 社で segments=0）＋汎用 member の除外／連結売上の要素名 `Revenue2IFRS`（商社 3）・`SalesRevenuesIFRS`（トヨタ）。一括ゲート 705 件 OK（Codex 実装レビュー 2 回目 APPROVE）
- 区分数値なしの構造原因（17 社）= 単一セグメント 6／区分利益の尺度が営業利益でない 5／連結粗利なし 7／US GAAP で XBRL に数値なし 2 → 設計の裁定へ（P-INF-36）
- 運用の上限: サブエージェントの一斉投入は 5〜8 体（10 体で Lock 失敗）。150 体で約 1 時間（張り付き）＝962 社なら約 20 時間・無人化には API 鍵が要る

### T9 追う商品を会社ごとに固定して読み直す（2026-10-08 オーナー裁定 P-INF-37 ①a②a・P-INF-36 ②〜④ a・⑤= 直して同じ 50 社を読み直す）
根拠= vault §13c（4 モデル議論 wf_d18f023f-e71）。**読みの行の新しい列（T9a・T9b 共通の契約）**: `geo`（地域: 国内／海外／全体／地域名・不明は null）・`value_type`（数量／単価／金額／比率／受注／稼働率／その他）・`value_basis`（水準／前年比）・`status`（new=新しく見つけた／found=指名された品目の数字があった／not_disclosed=指名された品目の数字が当期の資料に無い）・`target_id`（指名行のみ）。read.json の先頭に `submit_date`（有報の提出日）と `mode`（base／nominated）。
- **T9a `scripts/yuho_read.py`**: ①基準期（3 期で最も古い有報）は上限 3 行を外し最大 10 行・開示があれば区分ごとに 1 行以上・数量／単価／受注／稼働率を金額より優先 ②`--make-targets --code X` で基準期の read.json から `output/yuho/<X>/targets.json`（target_id・segment・item・kpi_name・geo・value_type・value_basis）を作る ③後の期は targets を指名して読ませる（指名 1 件につき 1 行・当期の区分名で返す・無ければ status=not_disclosed）＋新しく見つけた品目を最大 3 行 ④XBRL に区分の数字が無い会社（US GAAP 等・facts.segments が空かつ区分の本文表がある）は本文のセグメント表から区分の売上・利益（当期・前期）を `segment_figures` として返させる ⑤代走用に `--dump-prompts`（prompts/<docID>.prompt.txt を書く）と `--import-responses`（<docID>.response.json → read.json・model に「代走」）を追加＝scratchpad の補助スクリプトを正規経路に吸収。tests/test_yuho_read.py に各 1 ケース。検証= `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_yuho_read`
- **T9b `scripts/yuho_judge.py`**: ①品目の束ね鍵= target_id があればそれ・無ければ（item・kpi_name・geo・value_type・value_basis）の正規化。区分は期ごとに持つ（区分の再編 17 社に対応）②ラベル追加= 「未判定（開示なし）」（指名したが資料に無い）・「対象外（新設前）」（新しく出た品目の、出る前の期・分母から除く）③`EXPECT_SECTIONS` に mdna・ir_presentation を追加（P-INF-37 ①a）④単一セグメントは**証拠がある時だけ**連結を区分とみなし構成比 100%（証拠= facts.single_segment・セグメント情報の本文の当期の単一の記載・読みの宣言の本文引用。区分 0 だけ・過年度の記載・否定文・報告セグメント変更の文は証拠にしない＝Codex T9 R1〜R3。P-INF-36 ②a・根拠欄に明記）⑤連結粗利の無い会社は条件 3 を「判定不能」として条件 1・2 で判定し根拠欄に「粗利なし・2 条件」（③a）。区分利益の尺度は既存 `_consolidated_profit` の同種連結利益を使う ⑥`segment_figures` があれば区分の数字の代わりに使い出所=「本文の表（読み）」（④a）⑦periods に submit_date ⑧`--all` に **3 期揃い率**（3 期とも数字のある品目 ÷ 品目数・社単位で 3 期揃いの品目を 1 つ以上持つ社の割合）と台帳の pin を出す。tests/test_yuho_judge.py に各 1 ケース。検証= 同上 `tests.test_yuho_judge`
- **T9c 一括ゲート＋Codex 実装レビュー**: `docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest discover -s tests` が全件 OK
- **T9d 50 社の読み直し**: 基準期 50 体 → `--make-targets` → 後の 2 期 100 体（指名読み）→ `--import-responses` → `yuho_judge --code` → `--all`。5333 は日本ガイシの説明会資料を取り直してから。**成功の物差し（②a）**= 3 期揃い率が T8（**5/360 品目・4/50 社**＝3 期とも数値で読めた品目の厳密な数え方。旧記載 7/360 は数値でない KPI も数えていた）から上がったか・「当期候補なし」672 が「開示なし」と「対象外（新設前）」に分かれたか。株価との遡り検証は 962 社で行う（50 社では処置群 30 社×期に届かない）
  - **結果（2026-10-08 19:30・完了）**: 3 期揃い **140/336 品目（41.7%）・36/50 社**（T8 5/360・4/50 から上昇）／指名マス 672＝found 330＋開示なし 342（「当期候補なし」は指名品目で 0・新しく出た品目の後の期だけ 65）／対象外（新設前）174／埋まり率 39/50（全期の出所が有報の節で揃う品目を要求＝説明会資料だけの期は数えない）。件数= 社 50/50・回答 150/150・取り込み error 0・行 1,144（必須列欠落 94 を捨てた）。詳細= vault §13d

### T10 50 社のまま穴 5 つを塞いで読み直す（2026-10-08 19:45 オーナー「c」＝P-INF-38 c・962 社展開はこの後）
根拠= vault §13d「残る穴」。既定値（非 blocking・宣言して進む）= ②説明会資料の数字は〈資料名＋引用〉があれば出所と数え、埋まり率は「有報だけ」と「有報＋説明会資料」の 2 本を並べる。
- **T10a 区分の旧→新（穴①）**: `config/yuho_segment_map.json`（新規・社ごとに〈この期から・旧区分名→新区分名・根拠= 有報の引用〉）を `scripts/yuho_judge.py` が読み、行の区分が当期の XBRL 区分に無い時だけ対応表で引き直す（引けた時は根拠欄に「区分名の対応表」）。`scripts/yuho_read.py` の指名プロンプトに**当期の XBRL 区分名の一覧**を載せ「区分はこの中から書け」と指示（8058 の書き換え対策）。対応表は 3 期で区分の集合が変わった社をサブエージェントが有報の「報告セグメントの変更」の文から作る（推測で埋めない・根拠なしは載せない）
- **T10b 説明会資料の出所（穴②）**: judge に `source_complete_any`（各期の出所が有報の節 か〈ir_source＋quote〉）を足し、`--all` に埋まり率 2 本（`fill_rate_yuho`＝従来・`fill_rate_any`）
- **T10c 概数・範囲（穴③）**: import で原文の文字列を `kpi_value_text` に残し、範囲「A～B」→中央値・「約 X／X 程度／X 前後」→X・「X 超／X 以上／X を超える」→X を数値化して `value_approx`（範囲／概数／下限）を付ける。兆・億・万の複合（「3 兆 210 億円」）は kpi_unit に換算。「台中盤」等の決められない語は None のまま text だけ残す。judge は value_approx を periods に載せ `--all` に概数の件数
- **T10d 期待を別欄（穴④）**: 回答の型に `expect_quote`・`expect_section` を追加（数字の根拠 `quote` と分ける）。judge の期待判定は expect_quote があればそれ・無ければ従来の quote（後方互換）
- **T10e 新しく出た品目を次の期から指名（穴⑤）**: `--make-targets --extend --code X` で真ん中の期の read.json の new 行を targets に追加（`added_from`＝doc_id・target_id は続き番号）。最後の期のプロンプトはそれも指名する。judge は追加品目の出る前の期を対象外（新設前）・真ん中= new・最後= found/開示なし
- **T10f 一括ゲート＋Codex 実装レビュー**（APPROVE まで）
- **T10g 読み直し**: 3 期とも同じ型にするため基準期も自分の targets で指名読み（new 0 件）→ 真ん中の期 → `--extend` → 最後の期。150 体。**成功の物差し**= ①3 期揃い 140/336 から下がらない（分母は追加品目で増える＝率と件数を両方）②区分の改名で開示なしになった品目が減る（8058・6501・7832 を名指しで確認）③KPI 非数値 100 が減る ④期待＝true の行は全て expect_quote あり ⑤真ん中の期に出た品目の最後の期の「当期候補なし」65 → 0 ⑥埋まり率 2 本を並べる。件数は〈渡した数／処理した数〉で突合
- **範囲外（今回は直さない・記録だけ）**: 「区分数値なし」156 の大半は改名でなく XBRL に区分利益が無い（7011・8031・6301・1605 等で seg_profit が None）＝区分利益の尺度の穴。穴①とは別物
