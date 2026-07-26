#!/usr/bin/env python3
"""米国株 Tier1（価格データ基盤）の日次OHLCVフェッチャー。

日本株の `scripts/jq_fetch.py`（J-Quants）と同じ Canonical Collector 型に揃える:
冪等・存在スキップ・receipt 証跡（取得時刻/件数/sha256/URL）・レートリミット尊重・
raw を素直に保存（重い加工は下流に持たせる）。

依存はすべて Python 標準ライブラリ（urllib.request / csv / json / hashlib）。
pip install は一切不要（Docker-Only 方針で追加ライブラリを増やさないため）。

**重要な限界（詳細は docs/us-tier1-price-foundation.md）**:
本フェッチャーが集めるデータは無料の一般向け提供元に依存しており、上場廃止銘柄の
カバレッジが担保されない（＝生存者バイアスが乗る）。J-Quants で構築した日本株スタック
（上場廃止込み・PIT）とは**同格ではない**。用途は記述分析（相場環境の把握・仮説の目視）
までであり、§6 の正式レシピ検定には使えない。

データ提供元（provider）:
    stooq   https://stooq.com/q/d/l/?s=<ticker>.us&i=d の CSV。APIキー不要。
            2026-07-26 の実測では stooq.com / stooq.pl とも JavaScript による
            ボット検証ゲート（proof-of-work チャレンジ）を返し、素の HTTP クライアント
            では CSV を取得できなかった。本スクリプトは**チャレンジを迂回しない**。
            チャレンジ応答を検出した場合は status="provider_challenge" として
            receipt に記録し、次の provider（auto 指定時）へフォールバックする。
    yahoo   https://query1.finance.yahoo.com/v8/finance/chart/<ticker> の JSON。
            APIキー不要。2026-07-26 実測で AAPL 11,495 行（1980-12-12〜）を取得できた。
            非公式エンドポイントであり提供元の裁量で変更・停止しうる（要一次確認）。

出力（provider に依らず同一スキーマの Canonical CSV）:
    data/us/prices/<TICKER>.csv
        date,open,high,low,close,adj_close,volume,provider
        - date      取引所ローカル（米東部）の取引日 YYYY-MM-DD
        - close     提供元が返す終値。yahoo では**分割調整済み・配当未調整**
        - adj_close 分割＋配当調整済み終値（yahoo のみ。stooq 経路では空）
    data/us/receipts.jsonl   取得ごとの受領証跡（append-only）

Usage:
    python3 scripts/us_price_fetch.py --tickers AAPL,MSFT
    python3 scripts/us_price_fetch.py --tickers-file config/us_universe_seed.json
    python3 scripts/us_price_fetch.py --provider yahoo --limit 5
    python3 scripts/us_price_fetch.py --status
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
DATA_ROOT = PROJECT_ROOT / "data" / "us"
DEFAULT_OUTDIR = DATA_ROOT / "prices"
RECEIPTS_PATH = DATA_ROOT / "receipts.jsonl"
DEFAULT_UNIVERSE = PROJECT_ROOT / "config" / "us_universe_seed.json"

JST = datetime.timezone(datetime.timedelta(hours=9))

TIMEOUT_SECONDS = 30
# リクエスト間隔の下限（無料・無契約の提供元に対する自制。指定要件は「最低1.0秒」）
REQUEST_INTERVAL_SECONDS = 1.0
SERVER_ERROR_WAIT_SECONDS = 5
SERVER_ERROR_MAX_RETRIES = 2

USER_AGENT = "influx-us-tier1/0.1 (research; contact via repo owner)"

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1=0&period2=9999999999&interval=1d&events=div%2Csplit"
)

CSV_HEADER = ["date", "open", "high", "low", "close", "adj_close", "volume", "provider"]

# 米国株の日次バーのタイムスタンプは取引所ローカル 09:30（EST=UTC-5 / EDT=UTC-4）。
# UTC から 5 時間引けば 08:30〜09:30 に収まり、DST の別なく暦日が変わらないため、
# zoneinfo（環境によっては tzdata 不在）に依存せず取引日を確定できる。
US_MARKET_DATE_SHIFT_SECONDS = 5 * 3600

# 冪等判定の相対許容差。実測（2026-07-26・AAPL を連続2回取得）で、提供元の adj_close は
# 同一日・同一内容でも呼び出しごとに相対 1e-6 程度ゆらぐ（float32 精度に由来する再計算差）。
# バイト一致を冪等条件にすると毎回「更新」と誤判定されるため、
#   ① 行数・初日・最終日が変わらず ② 全数値の相対差がこの許容内
# のときだけ「変化なし（書き込まない）」と判定する。分割・配当調整のやり直し等の
# 実質的な遡及改訂は 1e-4 を大きく超えるため、この閾値で検出できる。
REVISION_RTOL = 1e-4


class ProviderChallenge(Exception):
    """提供元がボット検証ゲート（JS チャレンジ等）を返した。迂回はせず記録して次へ進む。"""


class ProviderError(Exception):
    """提供元からデータを取得できなかった（HTTP エラー・空応答・パース失敗）。"""


def now_jst() -> datetime.datetime:
    """現在時刻を JST で返す（zoneinfo 非依存、固定オフセットのみ）。"""
    return datetime.datetime.now(JST)


def http_get(url: str) -> Tuple[bytes, int]:
    """URL を GET し、(body, status) を返す。成功後は必ず自制インターバルを置く。

    5xx・接続エラーは SERVER_ERROR_WAIT_SECONDS 秒待って最大 SERVER_ERROR_MAX_RETRIES 回再試行。
    4xx は即 ProviderError（銘柄が存在しない等、再試行しても直らないため）。

    Args:
        url: 取得先 URL。

    Returns:
        (レスポンスボディ, HTTP ステータスコード)。

    Raises:
        ProviderError: リトライ上限に達した、または 4xx が返った場合。
    """
    retries = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                body = resp.read()
                status = resp.status
            time.sleep(REQUEST_INTERVAL_SECONDS)
            return body, status
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:200].decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — 診断用の best effort
                detail = ""
            time.sleep(REQUEST_INTERVAL_SECONDS)
            if e.code >= 500:
                retries += 1
                if retries > SERVER_ERROR_MAX_RETRIES:
                    raise ProviderError(f"HTTP {e.code} リトライ上限到達: {detail}") from None
                print(
                    f"WARN: HTTP {e.code}。{SERVER_ERROR_WAIT_SECONDS}秒待機してリトライ"
                    f"({retries}/{SERVER_ERROR_MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(SERVER_ERROR_WAIT_SECONDS)
                continue
            raise ProviderError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            retries += 1
            if retries > SERVER_ERROR_MAX_RETRIES:
                raise ProviderError(f"接続エラー リトライ上限到達: {e}") from None
            print(
                f"WARN: 接続エラー（{e}）。{SERVER_ERROR_WAIT_SECONDS}秒待機してリトライ"
                f"({retries}/{SERVER_ERROR_MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(SERVER_ERROR_WAIT_SECONDS)
            continue


# --- provider: stooq ---------------------------------------------------------


def stooq_symbol(ticker: str) -> str:
    """AAPL -> aapl.us（Stooq の米国株シンボル表記。BRK.B のドットはハイフン）。"""
    return ticker.strip().lower().replace(".", "-") + ".us"


def fetch_stooq(ticker: str) -> Tuple[List[Dict[str, str]], str]:
    """Stooq の日次 CSV を取得して Canonical 行リストに正規化する。

    Args:
        ticker: 大文字ティッカー（例 "AAPL"）。

    Returns:
        (行リスト, 使用した URL)。行は CSV_HEADER のキーを持つ dict。

    Raises:
        ProviderChallenge: ボット検証ゲートの HTML が返った場合（迂回しない）。
        ProviderError: 取得・パースに失敗した場合。
    """
    url = STOOQ_URL.format(symbol=urllib.parse.quote(stooq_symbol(ticker)))
    body, _ = http_get(url)
    text = body.decode("utf-8", errors="replace").strip()

    lowered = text[:2000].lower()
    if text.startswith("<") or "requires javascript" in lowered or "<script" in lowered:
        raise ProviderChallenge(
            "Stooq がボット検証ゲート（JS チャレンジ）を返しました。本スクリプトは迂回しません"
        )
    if "exceeded" in lowered and "limit" in lowered:
        raise ProviderError(f"Stooq レート/日次上限応答: {text[:120]}")
    if not text.lower().startswith("date,"):
        raise ProviderError(f"Stooq が CSV ヘッダを返しませんでした: {text[:120]}")

    rows: List[Dict[str, str]] = []
    for rec in csv.DictReader(io.StringIO(text)):
        date = (rec.get("Date") or "").strip()
        if not date:
            continue
        rows.append({
            "date": date,
            "open": (rec.get("Open") or "").strip(),
            "high": (rec.get("High") or "").strip(),
            "low": (rec.get("Low") or "").strip(),
            "close": (rec.get("Close") or "").strip(),
            # Stooq の CSV は調整済み終値の別カラムを持たない（調整方針は提供元依存・要一次確認）
            "adj_close": "",
            "volume": (rec.get("Volume") or "").strip(),
            "provider": "stooq",
        })
    if not rows:
        raise ProviderError("Stooq が 0 行を返しました")
    return rows, url


# --- provider: yahoo ---------------------------------------------------------


def _fmt(value: Optional[float]) -> str:
    """浮動小数を CSV 用文字列にする（None は空文字、整数値は小数点なし）。"""
    if value is None:
        return ""
    if isinstance(value, (int,)) or float(value).is_integer():
        return str(int(value))
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def fetch_yahoo(ticker: str) -> Tuple[List[Dict[str, str]], str]:
    """Yahoo Finance chart API（v8・キー不要）から全期間の日次バーを取得する。

    close は提供元仕様で「分割調整済み・配当未調整」、adjclose は「分割＋配当調整済み」。
    どちらも遡及改訂されうる（PIT ではない）点は docs/us-tier1-price-foundation.md に明記。

    Args:
        ticker: 大文字ティッカー（例 "AAPL"）。

    Returns:
        (行リスト, 使用した URL)。

    Raises:
        ProviderChallenge: HTML のボット検証ゲートが返った場合。
        ProviderError: 取得・パースに失敗した場合。
    """
    url = YAHOO_URL.format(symbol=urllib.parse.quote(ticker.strip().upper()))
    body, _ = http_get(url)
    text = body.decode("utf-8", errors="replace")
    if text.lstrip().startswith("<"):
        raise ProviderChallenge("Yahoo が HTML（ボット検証ゲートの可能性）を返しました")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProviderError(f"JSON パース失敗: {e}") from None

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ProviderError(f"provider error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise ProviderError("result が空")

    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = (indicators.get("quote") or [{}])[0]
    adjcloses = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []

    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []

    rows: List[Dict[str, str]] = []
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        if close is None:
            # 休場・データ欠損の穴埋め行は落とす（下流で欠損補間しないための素直な扱い）
            continue
        day = datetime.datetime.utcfromtimestamp(ts - US_MARKET_DATE_SHIFT_SECONDS).date()
        rows.append({
            "date": day.isoformat(),
            "open": _fmt(opens[i] if i < len(opens) else None),
            "high": _fmt(highs[i] if i < len(highs) else None),
            "low": _fmt(lows[i] if i < len(lows) else None),
            "close": _fmt(close),
            "adj_close": _fmt(adjcloses[i] if i < len(adjcloses) else None),
            "volume": _fmt(volumes[i] if i < len(volumes) else None),
            "provider": "yahoo",
        })
    if not rows:
        raise ProviderError("有効な日次バーが 0 行")
    return rows, url


PROVIDERS = {"stooq": fetch_stooq, "yahoo": fetch_yahoo}
# 「Stooq を第一候補」の指定に従い auto はこの順で試す
AUTO_ORDER = ["stooq", "yahoo"]


# --- 保存・冪等判定 ----------------------------------------------------------


def rows_to_csv_bytes(rows: List[Dict[str, str]]) -> bytes:
    """Canonical 行リストを CSV バイト列にする（date 昇順・UTF-8・LF 固定）。"""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=CSV_HEADER, lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda r: r["date"]):
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def read_existing_summary(path: Path) -> Optional[Dict[str, object]]:
    """既存 CSV の {rows, first_date, last_date, sha256, by_date} を返す。

    by_date は {日付: 行dict} で、冪等判定（既存 vs 新規の突合）に使う。
    ファイルが無い・空・壊れている場合は None（＝新規取得扱い）。
    """
    if not path.exists():
        return None
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    rows = [r for r in csv.DictReader(io.StringIO(text)) if (r.get("date") or "").strip()]
    if not rows:
        return None
    dates = sorted(r["date"] for r in rows)
    return {
        "rows": len(rows),
        "first_date": dates[0],
        "last_date": dates[-1],
        "sha256": hashlib.sha256(content).hexdigest(),
        "by_date": {r["date"]: r for r in rows},
    }


def _values_differ(old: str, new: str) -> bool:
    """CSV 上の 1 セルが実質的に変わったかを判定する（数値は REVISION_RTOL の相対許容つき）。"""
    old, new = (old or "").strip(), (new or "").strip()
    if old == new:
        return False
    if not old or not new:
        return True
    try:
        old_f, new_f = float(old), float(new)
    except ValueError:
        return True
    scale = max(abs(old_f), abs(new_f))
    if scale == 0:
        return False
    return abs(old_f - new_f) / scale > REVISION_RTOL


def has_material_change(existing: Dict[str, object], new_rows: List[Dict[str, str]]) -> bool:
    """既存 CSV と新規取得行の間に実質的な差分があるか。

    「行数・初日・最終日が同じ」かつ「全数値の相対差が REVISION_RTOL 以内」なら差分なし
    （＝書き込まない）。提供元の float ゆらぎで毎回書き換わるのを防ぐための判定
    （詳細は REVISION_RTOL のコメント）。

    Args:
        existing: `read_existing_summary` の戻り値。
        new_rows: 今回取得した Canonical 行リスト。

    Returns:
        実質的な差分があれば True。
    """
    dates = sorted(r["date"] for r in new_rows)
    if (
        existing["rows"] != len(new_rows)
        or existing["first_date"] != dates[0]
        or existing["last_date"] != dates[-1]
    ):
        return True
    by_date = existing["by_date"]  # type: ignore[index]
    for row in new_rows:
        old_row = by_date.get(row["date"])
        if old_row is None:
            return True
        for field in CSV_HEADER[1:]:
            if _values_differ(old_row.get(field, ""), row.get(field, "")):
                return True
    return False


def write_csv_atomic(path: Path, content: bytes) -> None:
    """同一ディレクトリの .tmp に書いてから os.replace でアトミックに配置する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    with open(tmp_path, "wb") as f:
        f.write(content)
    os.replace(tmp_path, path)


def append_receipt(record: Dict[str, object]) -> None:
    """receipts.jsonl に 1 行追記。書き込み失敗は本処理を止めず警告のみ（監視が本処理を壊さない原則）。"""
    try:
        RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RECEIPTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARN: receipts 書き込み失敗: {e}", file=sys.stderr)


def fetch_one(
    ticker: str, outdir: Path, providers: List[str], run_id: str,
    skip_existing: bool, force: bool = False,
) -> str:
    """1 銘柄を取得・保存し、receipt を残す。戻り値は status 文字列。

    冪等契約:
      - `--skip-existing` 指定時、既存ファイルがあればネットワークアクセスもせずスキップ。
      - 既定では取得したうえで既存 CSV と突合し、**行数・初日・最終日が同じで数値も
        実質同一なら書き込まない**（status="unchanged"）。差分があるときだけアトミックに
        置き換えるため、何度再実行してもファイルの mtime は無意味に動かない。
      - `--force` 指定時は突合結果によらず必ず書き直す。

    Args:
        ticker: 大文字ティッカー。
        outdir: 出力ディレクトリ。
        providers: 試す provider 名を優先順に並べたリスト。
        run_id: 実行 ID（receipt の突合用）。
        skip_existing: True なら既存ファイルがある銘柄をネットワーク前にスキップ。
        force: True なら差分の有無によらず書き直す。

    Returns:
        "saved" | "unchanged" | "skipped_exists" | "provider_challenge" | "error"
    """
    path = outdir / f"{ticker}.csv"
    ts_start = now_jst()
    existing = read_existing_summary(path)

    if skip_existing and existing is not None:
        append_receipt({
            "run_id": run_id, "ts": ts_start.isoformat(), "ticker": ticker,
            "provider": None, "url": None, "status": "skipped_exists",
            "rows": existing["rows"], "first_date": None, "last_date": existing["last_date"],
            "sha256": existing["sha256"], "bytes": None, "error": None,
        })
        print(f"[{ticker}] skipped_exists（{existing['rows']} 行 / 最終 {existing['last_date']}）")
        return "skipped_exists"

    errors: List[str] = []
    for provider in providers:
        try:
            rows, url = PROVIDERS[provider](ticker)
        except ProviderChallenge as e:
            errors.append(f"{provider}: challenge: {e}")
            append_receipt({
                "run_id": run_id, "ts": now_jst().isoformat(), "ticker": ticker,
                "provider": provider, "url": None, "status": "provider_challenge",
                "rows": None, "first_date": None, "last_date": None,
                "sha256": None, "bytes": None, "error": str(e),
            })
            print(f"WARN: [{ticker}] {provider}: {e}", file=sys.stderr)
            continue
        except ProviderError as e:
            errors.append(f"{provider}: {e}")
            print(f"WARN: [{ticker}] {provider} 取得失敗: {e}", file=sys.stderr)
            continue

        content = rows_to_csv_bytes(rows)
        digest = hashlib.sha256(content).hexdigest()
        # rows は provider 由来で未ソートの可能性があるため日付を並べ直して端点を取る
        sorted_dates = sorted(r["date"] for r in rows)
        first_date, last_date = sorted_dates[0], sorted_dates[-1]

        if existing is not None and not force and not has_material_change(existing, rows):
            append_receipt({
                "run_id": run_id, "ts": now_jst().isoformat(), "ticker": ticker,
                "provider": provider, "url": url, "status": "unchanged",
                "rows": len(rows), "first_date": first_date, "last_date": last_date,
                # sha256 はディスク上の実ファイルの値（＝証跡）。fetched_sha256 は今回の取得
                # ペイロードの値で、提供元の float ゆらぎのぶん一致しないのが正常。
                "sha256": existing["sha256"], "fetched_sha256": digest,
                "bytes": len(content), "error": None,
            })
            print(f"[{ticker}] unchanged（{len(rows)} 行 / 最終 {last_date} / {provider}）")
            return "unchanged"

        write_csv_atomic(path, content)
        append_receipt({
            "run_id": run_id, "ts": now_jst().isoformat(), "ticker": ticker,
            "provider": provider, "url": url, "status": "saved",
            "rows": len(rows), "first_date": first_date, "last_date": last_date,
            "sha256": digest, "bytes": len(content), "error": None,
        })
        print(
            f"[{ticker}] saved: {len(rows)} 行 {first_date}〜{last_date} "
            f"({provider}) -> {path.relative_to(PROJECT_ROOT)}"
        )
        return "saved"

    append_receipt({
        "run_id": run_id, "ts": now_jst().isoformat(), "ticker": ticker,
        "provider": None, "url": None, "status": "error",
        "rows": None, "first_date": None, "last_date": None,
        "sha256": None, "bytes": None, "error": " | ".join(errors) or "no provider attempted",
    })
    print(f"WARN: [{ticker}] 全 provider 失敗（次回再実行で再取得されます）", file=sys.stderr)
    return "error"


# --- ティッカー一覧の読み込み ------------------------------------------------


def load_tickers_file(path: Path) -> List[str]:
    """ティッカー一覧を JSON（config/us_universe_seed.json 形式）またはプレーンテキストから読む。

    JSON は `{"tickers": [{"ticker": "AAPL", ...}, ...]}` または文字列配列に対応。
    テキストは 1 行 1 ティッカー・`#` 以降はコメント。

    Raises:
        SystemExit: ファイルが読めない・形式が解釈できない場合（exit code 2）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"FATAL: ティッカー一覧を読めません: {e}", file=sys.stderr)
        sys.exit(2)

    if path.suffix.lower() == ".json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"FATAL: JSON パース失敗: {e}", file=sys.stderr)
            sys.exit(2)
        entries = obj.get("tickers", obj) if isinstance(obj, dict) else obj
        if not isinstance(entries, list):
            print("FATAL: tickers が配列ではありません", file=sys.stderr)
            sys.exit(2)
        out: List[str] = []
        for entry in entries:
            if isinstance(entry, str):
                out.append(entry.strip().upper())
            elif isinstance(entry, dict) and entry.get("ticker"):
                out.append(str(entry["ticker"]).strip().upper())
        return out

    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.upper())
    return out


def print_status(outdir: Path) -> None:
    """取得済み CSV の銘柄数・行数・期間を表示する（読み取り専用・ネットワーク未使用）。"""
    print(f"=== 米国株 Tier1 価格キャッシュ状態（{outdir}） ===")
    if not outdir.exists():
        print("未取得（ディレクトリなし）")
        return
    files = sorted(outdir.glob("*.csv"))
    if not files:
        print("未取得（CSV なし）")
        return
    total_rows = 0
    for path in files:
        summary = read_existing_summary(path)
        if summary is None:
            print(f"{path.stem:<8} 破損または空")
            continue
        total_rows += int(summary["rows"])
        print(f"{path.stem:<8} {summary['rows']:>7} 行  最終 {summary['last_date']}")
    print(f"--- 銘柄 {len(files)} / 合計 {total_rows} 行 ---")
    if RECEIPTS_PATH.exists():
        with open(RECEIPTS_PATH, encoding="utf-8") as f:
            print(f"receipts: {sum(1 for _ in f)} 行 ({RECEIPTS_PATH.relative_to(PROJECT_ROOT)})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="米国株 Tier1 日次OHLCVフェッチャー（無料・APIキー不要・標準ライブラリのみ）"
    )
    parser.add_argument("--tickers", default=None, help="カンマ区切りのティッカー（例 AAPL,MSFT）")
    parser.add_argument(
        "--tickers-file", default=None,
        help=f"ティッカー一覧ファイル（.json または .txt）。省略時 {DEFAULT_UNIVERSE.name}",
    )
    parser.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help=f"出力先（既定 {DEFAULT_OUTDIR}）")
    parser.add_argument(
        "--provider", choices=["auto", "stooq", "yahoo"], default="auto",
        help="データ提供元。auto は stooq→yahoo の順にフォールバック（既定 auto）",
    )
    parser.add_argument("--limit", type=int, default=None, help="先頭 N 銘柄だけ処理（動作確認用）")
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="既存 CSV がある銘柄はネットワークアクセスせずスキップ",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="既存 CSV と差分が無くても書き直す（調整係数の遡及改訂を疑うときの手動リフレッシュ用）",
    )
    parser.add_argument("--status", action="store_true", help="取得済み状態を表示して終了")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    if not outdir.is_absolute():
        outdir = PROJECT_ROOT / outdir

    if args.status:
        print_status(outdir)
        return 0

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        source = Path(args.tickers_file) if args.tickers_file else DEFAULT_UNIVERSE
        if not source.is_absolute():
            source = PROJECT_ROOT / source
        tickers = load_tickers_file(source)

    # 順序を保ったまま重複排除
    seen = set()
    tickers = [t for t in tickers if not (t in seen or seen.add(t))]
    if args.limit is not None:
        tickers = tickers[: args.limit]
    if not tickers:
        print("FATAL: 対象ティッカーが 0 件です", file=sys.stderr)
        return 2

    providers = AUTO_ORDER if args.provider == "auto" else [args.provider]
    run_id = uuid.uuid4().hex
    print(
        f"[us_price_fetch] run_id={run_id} 対象 {len(tickers)} 銘柄 "
        f"provider={'/'.join(providers)} -> {outdir}"
    )
    append_receipt({
        "run_id": run_id, "ts": now_jst().isoformat(), "ticker": None,
        "provider": "/".join(providers), "url": None, "status": "run_start",
        "rows": len(tickers), "first_date": None, "last_date": None,
        "sha256": None, "bytes": None, "error": None,
    })

    tally: Dict[str, int] = {}
    for idx, ticker in enumerate(tickers, start=1):
        status = fetch_one(ticker, outdir, providers, run_id, args.skip_existing, args.force)
        tally[status] = tally.get(status, 0) + 1
        if idx % 25 == 0 or idx == len(tickers):
            print(f"進捗: {idx}/{len(tickers)} ({idx / len(tickers) * 100:.1f}%)")

    summary = " / ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    print(f"[us_price_fetch] 完了: {summary}")
    append_receipt({
        "run_id": run_id, "ts": now_jst().isoformat(), "ticker": None,
        "provider": None, "url": None, "status": "run_end",
        "rows": len(tickers), "first_date": None, "last_date": None,
        "sha256": None, "bytes": None, "error": None if not tally.get("error") else summary,
    })
    # 全滅（1 銘柄も取得できず全て error）のときだけ非ゼロ終了
    return 1 if tally.get("error", 0) == len(tickers) else 0


if __name__ == "__main__":
    sys.exit(main())
