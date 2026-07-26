# 米国株 Tier1（価格データ基盤）— 設計と限界

作成: 2026-07-26 / 状態: 実装済み（最小構成）
実装: `scripts/us_price_fetch.py` / 種リスト: `config/us_universe_seed.json` / 出力: `data/us/`

---

## 0. この文書の要点（先に結論）

- 米国株レーンは **Tier1（価格のみ・軽い）に限定**して着手した。Tier2（Sharadar 等の有料フル複製）は
  **日本株で正式合格レシピ 1 本が出るまで保留**（ユーザー指示 2026-07-25）。
- Tier1 のデータは **記述分析専用**。§6 の正式レシピ検定（`docs/stock-algo-kpi-catalog.md`）には
  **使えない**。理由は生存者バイアス（§3）。台帳 `data/kpi_trials/` にも**不算入**。
- 本実装は無料・APIキー不要・Python 標準ライブラリのみ（Docker-Only 方針で追加ライブラリを増やさない）。

---

## 1. 構成

```
scripts/us_price_fetch.py          収集器（jq_fetch.py と同じ Canonical Collector 型）
config/us_universe_seed.json       初期ティッカー種リスト（96 件・手動ハードコード）
data/us/prices/<TICKER>.csv        Canonical 日次 OHLCV（gitignore・再取得で復元可）
data/us/receipts.jsonl             取得受領証跡（append-only・追跡対象）
```

### Canonical CSV スキーマ（provider に依らず同一）

| 列 | 内容 |
|---|---|
| `date` | 取引所ローカル（米東部）の取引日 `YYYY-MM-DD` |
| `open` / `high` / `low` / `close` | 四本値。**調整方針は提供元依存**（§4） |
| `adj_close` | 分割＋配当調整済み終値（yahoo 経路のみ。stooq 経路は空） |
| `volume` | 出来高 |
| `provider` | 行の出所（`stooq` / `yahoo`）。混在した場合に後から追跡できるよう行単位で持つ |

### jq_fetch.py から踏襲した契約

- **冪等**: 再実行しても、行数・初日・最終日が同じで数値も実質同一なら書き込まない（`unchanged`）。
- **存在スキップ**: `--skip-existing` で既存銘柄はネットワークアクセスもせずスキップ。
- **receipt 証跡**: 取得時刻(JST)・件数・期間・sha256・URL・provider・status を 1 行ずつ append。
- **レートリミット尊重**: リクエスト間隔 **1.0 秒**（`REQUEST_INTERVAL_SECONDS`）。
- **失敗は記録して続行**: 1 銘柄が失敗しても残りを処理し、receipt に `error` として残す。
- **アトミック書き込み**: `.tmp` に書いて `os.replace`。

### 使い方

```bash
python3 scripts/us_price_fetch.py --tickers AAPL,MSFT        # 個別指定
python3 scripts/us_price_fetch.py                            # 既定 = config/us_universe_seed.json 全件
python3 scripts/us_price_fetch.py --tickers-file <path>       # .json / .txt 両対応
python3 scripts/us_price_fetch.py --status                    # 取得済み状態（ネットワーク未使用）
python3 scripts/us_price_fetch.py --provider yahoo --limit 5  # 動作確認
python3 scripts/us_price_fetch.py --force --tickers AAPL      # 調整係数の遡及改訂を疑うとき
```

---

## 2. データ提供元（provider）と 2026-07-26 の実測

`--provider auto`（既定）は **stooq → yahoo** の順に試す。

### stooq（第一候補・現状は取得不可）

`https://stooq.com/q/d/l/?s=aapl.us&i=d`（APIキー不要の CSV）。

**実測結果（2026-07-26・本実装の疎通確認）**: stooq.com / stooq.pl とも CSV を返さず、
**JavaScript によるボット検証ゲート**（SHA-256 の proof-of-work を解いて `/__verify` に POST させる HTML）を返した。
`Content-Security-Policy` と `X-Robots-Tag: noindex, nofollow` 付きの、提供元が意図的に設置したアクセス制御。

→ **本スクリプトはこのゲートを迂回しない。** 検出したら `status="provider_challenge"` として
receipt に記録し、次の provider にフォールバックする。stooq 経路は「提供元がゲートを外したら
そのまま動く」状態でコードを残してある。

> **要一次確認**: このゲートが恒久的なものか、IP/レート起因の一時的なものかは未確認。
> 定常運用に stooq を使いたい場合は、提供元の利用条件（`stooq.com` のデータ利用規約）を
> 一次確認したうえで、正規の取得手段があるかを確かめること。

### yahoo（フォールバック・実際に動いている経路）

`https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>?period1=0&period2=9999999999&interval=1d&events=div,split`
（APIキー不要の JSON）。

**実測結果（2026-07-26）**: AAPL 11,495 行（1980-12-12〜2026-07-24）を取得。`adjclose` と
分割・配当イベントを含む。

> **要一次確認**: これは公開ドキュメントのない**非公式エンドポイント**であり、提供元の裁量で
> 変更・停止・遮断されうる。商用利用可否・再配布可否も未確認。この不確実性が
> 「Tier1 は記述分析まで」という位置づけの根拠のひとつ。
> なお取得データを repo にコミットしないよう `data/us/prices/` は gitignore 済み。

---

## 3. 【最重要】生存者バイアス — 日本株スタックと同格ではない

日本株スタックは J-Quants の**調整済み OHLCV・上場廃止銘柄込み・PIT（point-in-time）**で構築されている。
米国株 Tier1 はこのいずれも満たさない。

| 観点 | 日本株（J-Quants） | 米国株 Tier1 |
|---|---|---|
| 上場廃止銘柄 | **含む** | **含まない/カバレッジ不十分** |
| PIT（各時点の構成再現） | 可 | **不可** |
| universe の出所 | API の上場銘柄マスタ（月末スナップショット） | 手動ハードコード（AI 仮置き・§5） |
| 検定への使用 | 可（§6 正式レシピ検定） | **不可** |

**バイアスが入る二重の経路:**

1. **提供元側**: Stooq は上場廃止銘柄のカバレッジが不十分（＝現存銘柄中心）。Yahoo も
   廃止ティッカーの体系的な提供を保証しない。過去に破綻・買収・上場廃止した銘柄が欠ける。
2. **universe 側**: `config/us_universe_seed.json` は「2026-07 時点で存在が知られている銘柄」の
   静的リスト。**過去に消えた銘柄を定義上 1 件も含まない**（詳細は §5）。

**帰結（守るべき運用ルール）:**

- ✅ 使ってよい: 相場環境の記述（ベンチマーク推移・セクター相対・ボラティリティ水準の目視）、
  仮説の粗い当たり付け、日本株との相関チェック。
- ❌ 使ってはいけない: 勝率・EV・リフトの推定、レシピの合否判定、`data/kpi_trials/` への台帳登録、
  §6 の評価プロトコルへの投入。**生存者バイアスは成績を系統的に上振れさせる**ため、
  ここで出た数字は「良く見えて当然」であり判断材料にならない。
- 米国株で検定をやるなら **Tier2 契約が前提**（§6 の完全性プロトコルを米国株で満たすには、
  上場廃止込み・PIT の universe が必須）。

---

## 4. 調整（分割・配当）の扱い — 提供元依存

**提供元ごとに定義が違い、遡及改訂もされる。** 列の意味を確かめずに混ぜないこと。

- **yahoo**: `close` は**分割調整済み・配当未調整**、`adjclose` は**分割＋配当調整済み**。
  （実測: AAPL 1980-12-12 の `close`=0.128348 は分割調整後の値。名目の当時株価ではない）
  → リターン計算に使うなら `adj_close`、値幅・出来高との整合を見るなら `close`。
- **stooq**: CSV に調整済み終値の別カラムが無い。既定で何がどこまで調整されているかは**要一次確認**。
- **PIT ではない**: 調整済み系列は将来の分割・配当で**過去の値ごと書き換わる**。
  「その日に見えていた価格」を再現しない。イベントスタディで過去日の水準を扱うときの落とし穴。
- 本実装は**提供元の値をそのまま保存する**（加工は下流に持たせる Canonical Module 原則）。
  ただし提供元の float 精度に起因して同一内容でも呼び出しごとに相対 1e-6 程度ゆらぐため、
  冪等判定は相対許容差 `REVISION_RTOL = 1e-4` で行う（バイト一致では毎回更新扱いになる）。
  1e-4 を超える変化＝実質的な遡及改訂は検出され、書き換わる。

---

## 5. universe の限界

`config/us_universe_seed.json`（96 件）は **AI が列挙した手動ハードコード**であり、
S&P500 の実際の構成銘柄リストとは**突合していない**（出所種別: AI 仮置き）。

- **要一次確認**: 実際の指数構成が必要なら S&P Dow Jones Indices の公表構成、
  または SEC EDGAR 等の一次ソースで取得し直すこと。
- 時価総額順・ウェイト順であることは検証していない（並びは概ねの知名度順）。
- 静的リストのため構成変更・リバランスを追随しない。更新は手動。
- ETF（SPY/QQQ/SOXX/TLT/GLD/EWJ 等）を含むが、これは個別株分析用ではなく相場環境の記述用。

---

## 6. タイムゾーンの注意（米東部 vs JST）

- **CSV の `date` は米東部（America/New_York）の取引日**。JST ではない。
- 米東部は日本より **13 時間（EDT）または 14 時間（EST）遅い**。DST の切替日が日米で異なる。
  米国の取引日 `T` の大引け（16:00 ET）は **JST では翌日 `T+1` の朝 5:00（EDT）/ 6:00（EST）**。
  → 「日本時間の同じ日」で日本株と米国株を素朴に突き合わせると**1 日ずれる**。
  日米を結合するときは、どちらのローカル取引日に揃えるかを明示すること。
- **receipts.jsonl の `ts` は JST**（他の influx 収集器の証跡と揃えるため）。CSV の `date` とは基準が違う。
- 実装上の取引日導出: 提供元の日次バーの timestamp は取引所ローカル 09:30 なので、
  UTC から 5 時間引けば EST/EDT の別なく暦日が確定する（`US_MARKET_DATE_SHIFT_SECONDS`）。
  `zoneinfo`（環境によっては tzdata 不在）に依存しないための実装。
  **検証済み**: AAPL 全 11,495 行が月〜金のみ、かつ 2026-01-01 / 2026-07-03 / 2025-12-25 /
  2025-11-27（感謝祭）を含まないことを確認。

---

## 7. Tier2 へ進む条件

**着手条件 = 日本株で正式合格レシピ 1 本**（`docs/stock-algo-kpi-catalog.md` の階段式目標・
2026-07-21 ユーザー裁定「10本 → 正式合格1本を第一目標」に対応）。それまで Tier2 は保留。

Tier2 の想定内容（**いずれも料金・提供条件は未取得＝要一次確認**）:

- **Sharadar SEP（価格）+ SF1（ファンダメンタルズ）**: 上場廃止銘柄込み・PIT を謳う有料データセット。
  米国株で §6 の完全性プロトコルを満たすための本命候補。
  **要一次確認**: 現在の料金体系・個人利用可否・再配布条件・実際の廃止銘柄カバレッジ・
  PIT の定義（as-reported の保持方式）。本文書はいずれも一次確認していない。
- **SEC EDGAR（財務の一次ソース）**: 提出書類そのもの。
  **要一次確認**: 公式 API のレート制限・User-Agent 要件・**恒久的に無料で提供され続けるかどうか**。
  「政府提供だから永続」は推測であり、根拠として使わない。

Tier2 着手時に Tier1 から引き継げるもの: Canonical CSV スキーマ、receipt 証跡の型、収集器の骨格。
引き継げないもの: universe（PIT・廃止込みに全面差し替えが必要）、Tier1 で出した記述統計の数値。

---

## 8. 検証記録（2026-07-26）

| 項目 | 結果 |
|---|---|
| `python3 -m py_compile scripts/us_price_fetch.py` | OK |
| 実データ取得 AAPL | 11,495 行 / 1980-12-12〜2026-07-24 |
| 実データ取得 MSFT | 10,169 行 / 1986-03-13〜2026-07-24 |
| 実データ取得 SPY | 8,428 行 / 1993-01-29〜2026-07-24 |
| stooq 経路 | 全銘柄で `provider_challenge`（§2）→ yahoo へフォールバック |
| 冪等性（再実行） | 3 銘柄すべて `unchanged`（書き込みなし） |
| `--skip-existing` | ネットワークアクセスなしで `skipped_exists` |
| `--force` | 差分なしでも書き直す |
| 失敗時の継続 | 不正ティッカー混在で `error=1 / unchanged=1`（残りを処理して続行） |
| 取引日の正しさ | 全行が平日・主要休場日を含まない（§6） |
| 日本株パイプラインへの影響 | `data/jquants/` は未変更（git status で確認） |
