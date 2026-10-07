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
