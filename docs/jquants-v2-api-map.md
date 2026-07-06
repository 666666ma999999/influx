# J-Quants API V2 エンドポイントマップ

**作成日**: 2026-07-05
**検証環境**: Standard プラン契約済み・APIキー有効（`~/.zshrc` の `JQUANTS_API_KEY`）
**レート制限**: Standard = 120 リクエスト/分（`/v2/fins/summary`, `/v2/fins/details` のみ個別に 60 リクエスト/分の制限が別枠で適用）
**仕様ページ**: https://jpx-jquants.com/ja/spec/ （JS レンダリングの SPA。各ページの Markdown 版は `<path>.md` で取得可能）
**認証方式**: `x-api-key` ヘッダーに API キーを設定（V1 のトークン方式は廃止）

## 前提: Standard プランで使えない主要エンドポイント（要注意）

契約ごとの提供可否表（`/ja/spec/data-spec`）で確認した結果、以下は **Premium 限定** であり Standard では 403 になる想定。バックテスト実装で誤って前提にしないよう明記する。

| データ | 理由 |
|---|---|
| 前場四本値 (`/equities/bars/daily/am`) | Premium のみ |
| 財務諸表 BS/PL/CF (`/fins/details`) | Premium のみ |
| 配当金情報 (`/fins/dividend`) | Premium のみ |
| 先物四本値 (`/derivatives/bars/daily/futures`) | Premium のみ |
| オプション四本値 (`/derivatives/bars/daily/options`) | Premium のみ |
| 売買内訳データ (`/markets/breakdown`) | Premium のみ |
| 株価四本値の前場/後場別カラム（`MO/MH/... AO/AH...`）| 日通しの `O/H/L/C` 等は Standard で取得可だが、前場/後場別カラムのみ Premium 限定 |

逆に **日経225オプション四本値** (`/derivatives/bars/daily/options/225`) は Standard で 10 年前まで取得可能（先物・オプション本体とは扱いが異なる）。

## エンドポイント一覧

| データ名 | V2 パス | Standard での提供可否・格納期間 | 主要パラメータ | 疎通検証 | 備考 |
|---|---|---|---|---|---|
| 上場銘柄一覧 | `GET /v2/equities/master` | 10年前まで（データ格納 2008/5/7〜） | `code`, `date`（両方 Optional、無指定時は当日全銘柄） | **200確認済み**（`date=20200601`、過去日付で全銘柄一覧を取得） | code省略で全銘柄、date省略で当日/翌営業日時点。Light以上は翌営業日データも取得可 |
| 株価四本値 | `GET /v2/equities/bars/daily` | 10年前まで（データ格納 2008/5/7〜） | `code` or `date`（いずれか必須）, `from`, `to`, `pagination_key` | **200確認済み**（`date=20240105` で **全上場銘柄 4335件を1リクエストで取得、pagination_key なし**） | `date` 単独指定で全銘柄一括取得可能。`code` 単独なら1銘柄の全期間データを1回で取得可（大きい場合は `pagination_key` で継続） |
| 前場四本値 | `GET /v2/equities/bars/daily/am` | **Premium限定**（Standard未提供） | `code` | 未検証（Standard対象外のため403想定、今回は実行せず） | 直近データのみ（翌日6時頃まで）。ヒストリカルな前場データが必要な場合は株価四本値の `MO/MH/ML/MC` 等（これもPremium限定カラム）を参照 |
| 投資部門別情報 | `GET /v2/equities/investor-types` | 10年前まで（データ格納 2008/1/16〜） | `section`, `from`, `to` | 未検証（必須項目外のため今回はcurl未実施。仕様上はStandard提供対象） | 市場区分再編後の名称に統一済み |
| 決算発表予定日 | `GET /v2/equities/earnings-calendar` | 全プランで取得可（直近データのみ） | `pagination_key` のみ（**code/date による絞り込み不可**） | **200確認済み** | 3月期・9月期決算会社のみ対象。翌営業日発表予定の全銘柄が返る仕様で、過去日付を遡っての取得はできない |
| 取引カレンダー | `GET /v2/markets/calendar` | 翌年末〜10年前まで（データ格納 翌年末〜2008/1/1） | `hol_div`, `from`, `to` | **200確認済み**（`from=20240101&to=20240110`） | `HolDiv` は休日区分コード |
| 財務情報 | `GET /v2/fins/summary` | 10年前まで（データ格納 2008/7/7〜） | `code` or `date`（いずれか必須）, `cursor`（**Premium限定**）, `pagination_key` | **200確認済み**（`date=20230130`） | **個別レートリミット 60req/分**（プラン共通の120とは別枠）。`cursor` 差分取得はPremium限定 |
| 財務諸表(BS/PL/CF) | `GET /v2/fins/details` | **Premium限定**（Standard未提供） | `code` or `date`, `cursor`, `pagination_key` | 未検証（Standard対象外） | 個別レートリミット60req/分。XBRLタクソノミの冗長ラベル（英語）キー形式 |
| 配当金情報 | `GET /v2/fins/dividend` | **Premium限定**（Standard未提供） | `code` or `date`, `from`, `to` | 未検証（Standard対象外） | |
| 信用取引週末残高 | `GET /v2/markets/margin-interest` | 10年前まで（データ格納 2012/2/10〜） | `code` or `date`（いずれか必須）, `from`, `to` | **200確認済み**（`code=86970&date=20240301`） | ★事前調査で誤って試行した `/v2/markets/weekly_margin_interest` は不正解。正しいパスは `margin-interest` |
| 日々公表信用取引残高 | `GET /v2/markets/margin-alert` | 10年前まで（データ格納 2008/5/8〜） | `code` or `date`（いずれか必須）, `from`, `to` | **200確認済み**（`date=20240208`） | 日々公表銘柄に指定された銘柄のみ収録（全銘柄ではない） |
| 空売り残高報告 | `GET /v2/markets/short-sale-report` | 10年前まで（データ格納 2013/11/7〜） | `code` / `disc_date` / `disc_date_from`+`disc_date_to` / `calc_date`（いずれか必須） | **200確認済み**（`calc_date=20240731`） | 残高割合0.5%以上の報告のみ |
| 業種別空売り比率 | `GET /v2/markets/short-ratio` | 10年前まで（データ格納 2008/11/5〜） | `s33` or `date`（いずれか必須）, `from`, `to` | **200確認済み**（`date=20241025`） | 33業種コード単位 |
| 売買内訳データ | `GET /v2/markets/breakdown` | **Premium限定**（Standard未提供） | `code` or `date`, `from`, `to` | 未検証（Standard対象外） | データ格納 2015/4/1〜 |
| 指数四本値 | `GET /v2/indices/bars/daily` | 10年前まで（データ格納 2008/5/7〜、一部指数はPremium限定） | `code` or `date`（いずれか必須）, `from`, `to` | **200確認済み**（`date=20240105`、複数指数コードを一括取得） | 配信対象指数コード一覧は `/ja/spec/idx-bars-daily/indexcodes` |
| TOPIX指数四本値 | `GET /v2/indices/bars/daily/topix` | 10年前まで（データ格納 2008/5/7〜） | `from`, `to` | **200確認済み**（`from=20240101&to=20240105`） | TOPIX専用、`code` パラメータ不要 |
| 日経225オプション四本値 | `GET /v2/derivatives/bars/daily/options/225` | 10年前まで（データ格納 2008/5/7〜） | 未確認（今回curl未実施、必須外） | 未検証 | 先物・通常オプションと異なりStandardで提供対象 |
| 先物四本値 | `GET /v2/derivatives/bars/daily/futures` | **Premium限定**（Standard未提供） | — | 未検証（Standard対象外） | |
| オプション四本値 | `GET /v2/derivatives/bars/daily/options` | **Premium限定**（Standard未提供） | — | 未検証（Standard対象外） | |

### アドオン契約が必要な系統（Standard基本プランには含まれない）

| データ名 | V2パス | 備考 |
|---|---|---|
| 株価分足 | `GET /v2/equities/bars/minute` | 「株価 分足・ティック」アドオン契約者のみ。個別レートリミット60req/分 |
| 株価ティック | （API提供なし、CSVダウンロードのみ） | data-spec上もAPIリンクがなくCSV提供のみ |
| TDnet/適時開示インデックス一覧 | `GET /v2/td/list` | 「TDnet/適時開示情報」アドオン契約者のみ。個別レートリミット100req/分 |
| TDnet/適時開示ファイル取得 | `GET /v2/td/files` | 同上 |
| TDnet/適時開示インデックス一括DL | `GET /v2/td/bulk` | 同上 |

### 一括CSVダウンロード系（Bulk）

| データ名 | V2パス | 備考 |
|---|---|---|
| ダウンロード可能ファイル一覧 | `GET /v2/bulk/list` | 未検証 |
| ファイルダウンロード用URL取得 | `GET /v2/bulk/get` | 未検証。過去データの一括取得に推奨（レートリミット節約） |

## V1 → V2 パス対応表（migration-v1-v2 より抜粋・完全版）

| データセット | V1 | V2 |
|---|---|---|
| 株価四本値 | `/v1/prices/daily_quotes` | `/v2/equities/bars/daily` |
| 前場四本値 | `/v1/prices/prices_am` | `/v2/equities/bars/daily/am` |
| 決算発表予定日 | `/v1/fins/announcement` | `/v2/equities/earnings-calendar` |
| 投資部門別情報 | `/v1/markets/trades_spec` | `/v2/equities/investor-types` |
| 上場銘柄一覧 | `/v1/listed/info` | `/v2/equities/master` |
| 先物四本値 | `/v1/derivatives/futures` | `/v2/derivatives/bars/daily/futures` |
| オプション四本値 | `/v1/derivatives/options` | `/v2/derivatives/bars/daily/options` |
| 日経225オプション四本値 | `/v1/option/index_option` | `/v2/derivatives/bars/daily/options/225` |
| 売買内訳データ | `/v1/markets/breakdown` | `/v2/markets/breakdown` |
| 取引カレンダー | `/v1/markets/trading_calendar` | `/v2/markets/calendar` |
| 日々公表信用取引残高 | `/v1/markets/daily_margin_interest` | `/v2/markets/margin-alert` |
| 信用取引週末残高 | `/v1/markets/weekly_margin_interest` | `/v2/markets/margin-interest` |
| 業種別空売り比率 | `/v1/markets/short_selling` | `/v2/markets/short-ratio` |
| 空売り残高報告 | `/v1/markets/short_selling_positions` | `/v2/markets/short-sale-report` |
| 指数四本値 | `/v1/indices` | `/v2/indices/bars/daily` |
| TOPIX指数四本値 | `/v1/indices/topix` | `/v2/indices/bars/daily/topix` |
| 財務諸表(BS/PL/CF) | `/v1/fins/fs_details` | `/v2/fins/details` |
| 財務情報 | `/v1/fins/statements` | `/v2/fins/summary` |
| 配当金情報 | `/v1/fins/dividend` | `/v2/fins/dividend` |

## バックテスト実装メモ

### 月次TOP500ユニバース構築・全銘柄取得の考え方

- **`equities/bars/daily` は `date` 単独指定で全上場銘柄（実測4335件・2024-01-05時点）を1リクエストで返す**。`pagination_key` も付与されなかった（1日分のペイロードはページング閾値を超えない）。したがって「1銘柄ずつ×全日付」ではなく「1日ずつ×全銘柄」でループする方がリクエスト効率が良い。
- `equities/master`（上場銘柄一覧）も同様に `date` 単独指定で該当日時点の全銘柄マスタを1リクエストで取得できる。月次のユニバース再構築（時価総額・市場区分でのフィルタ）はこの日次マスタ取得で対応可能。
- レートリミットのベストプラクティスとして公式ドキュメントも「多くのAPIは日付のみの指定で全銘柄のデータを取得でき、1銘柄ずつ×全日付の取得は避けるべき」と明記している（`/ja/spec/rate-limits`）。

### 概算リクエスト数（10年分・全銘柄の日次四本値）

- 東証の年間営業日数は概ね245日前後。10年 ≈ 2,450営業日。
- `date` 単独指定で1営業日=1リクエストなら、**全銘柄×10年分の日次四本値取得は約2,450リクエストで完結**する（銘柄ごとに分解する場合の約4,000リクエスト超よりも少ない）。
- Standard のレート上限120req/分をフルに使った場合の理論値は 2,450 ÷ 120 ≈ **20.5分**。ただし公式に「大幅超過で5分間の完全遮断」があるため、実運用では余裕を持たせ（例: 100req/分程度に自制、リトライ間隔を確保）た方が安全。数十分〜1時間程度を見込んでおくのが無難。
- 信用取引週末残高・空売り関連（`margin-interest` / `short-sale-report` / `short-ratio` / `margin-alert`）も `date` 単独指定で該当日の全銘柄・全業種データが返る設計になっている（レスポンス構造が `bars/daily` と同型）ため、同様に日次ループで全量取得が可能。
- `fins/summary`（財務情報）のみ個別レートリミット60req/分。決算短信は特定の開示日にまとまって発生するため、営業日ベースで全期間ループしても実際にデータが載っている日だけ処理すれば済み、リクエスト数は日次四本値ほど多くならない見込み（開示がない日は空配列が返る）。

### ページネーション・レート制御の実務ノート

- `pagination_key` が返ってきた場合のみ、同一クエリ条件で `pagination_key` を追加して再リクエストする（`/ja/spec/pagination`）。今回検証した「1日分・全銘柄」規模ではキーは発生しなかったため、日次バッチ単位なら基本的に1リクエストで完結すると想定してよい。
- 429（Too Many Requests）発生時は即座にリトライせず一定時間待機。大幅超過時は5分間の完全遮断がありうる。
- レスポンスはGzip圧縮されているが、`requests`/`axios` 等の標準HTTPクライアントは自動解凍するため、実装上の特別な対応は不要。
- 過去データの一括取得には `bulk/list` + `bulk/get`（CSVダウンロード）の活用が公式推奨だが、CSV版には調整済み株価（`AdjO/AdjH/...`）が含まれないため、`AdjFactor` を使って自前で調整計算する必要がある（`/ja/spec/eq-bars-daily/adj` に手順あり）。API経由なら調整済みカラムがそのまま含まれる。

## 未確認事項

- `日経225オプション四本値` の実疎通（curl未実施。バックテスト対象外のため優先度低）
- `投資部門別情報` の実疎通（仕様上はStandard提供対象だが今回はcurl未実施）
- `bulk/list` / `bulk/get` の実疎通・レスポンス形式
- 前場四本値・財務諸表・配当金情報・先物/オプション四本値・売買内訳データの403応答の実機確認（Standard契約のため意図的に未実施。403想定は`/ja/spec/data-spec`のプラン表に基づく推測）
