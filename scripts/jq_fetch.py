#!/usr/bin/env python3
"""J-Quants API V2 の生データフェッチャー（ベースレート計測用のローカルキャッシュ構築）。

2016年〜現在の全銘柄日次データを日付単位で `data/jquants/` にキャッシュする。
レスポンス JSON はそのまま gzip 保存し、CSV 変換等の加工は一切行わない
（加工ロジックは Phase B の集計側に持たせる・Canonical Module 原則）。

冪等・再開可能が最重要要件: 既に保存済みのファイルは無条件でスキップするため、
呼び出し側は完走するまで何度でも同じコマンドを再実行してよい。

対象データセット（詳細は docs/jquants-v2-api-map.md 参照）:
    calendar  取引カレンダー         -> data/jquants/calendar.json.gz（全期間・1ファイル）
    master    上場銘柄マスタ         -> data/jquants/master/YYYYMMDD.json.gz（各月最終営業日）
    topix     TOPIX 日足             -> data/jquants/topix.json.gz（全期間・1ファイル）
    bars      株価四本値（全銘柄）   -> data/jquants/bars/YYYYMMDD.json.gz（営業日ごと）
    fins      財務情報（決算短信）   -> data/jquants/fins/YYYYMMDD.json.gz（営業日ごと。
              /v2/fins/summary は 60req/分の別枠レート制限のためリクエスト間隔を自制）
    margin    信用取引週末残高       -> data/jquants/margin/YYYYMMDD.json.gz（各暦週の最終営業日ごと。
              週次データのため date=営業日ループは大半が空になる。実測で「date単独指定は市場全銘柄を
              1リクエストで返す」「基準日は金曜、祝日で金曜休場の週はThu/Wed等に繰り上がる」ことを
              確認済み〔=calendar由来の週最終営業日と完全一致・過去10年の非Friday基準日17件で検証済み〕。
              公表日フィールドはレスポンスに無いため、基準日+4営業日を保守的な使用可能日とする）
    shortsale 空売り残高報告         -> data/jquants/shortsale/YYYYMMDD.json.gz（営業日ごと。disc_date
              〔公表日〕パラメータで市場全銘柄を1リクエストで取得。レスポンス自体にDiscDate（公表日）
              フィールドを含むため使用可能日はDiscDate+1営業日で確定できる）

依存はすべて標準ライブラリ。pip install は一切不要。

Usage:
    python3 scripts/jq_fetch.py                                  # 全データ種別を既定順で取得
    python3 scripts/jq_fetch.py --only calendar
    python3 scripts/jq_fetch.py --only bars --start 20260629 --end 20260703
    python3 scripts/jq_fetch.py --status                          # 期待件数 vs 取得済み件数を表示
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
DATA_ROOT = PROJECT_ROOT / "data" / "jquants"
LOG_PATH = DATA_ROOT / "fetch_log.jsonl"

JST = datetime.timezone(datetime.timedelta(hours=9))

BASE_URL = "https://api.jquants.com"
TIMEOUT_SECONDS = 30

# レート制御（Standard 上限 120req/分 に対し余裕を持たせて自制）
REQUEST_INTERVAL_SECONDS = 0.6
# fins/summary のみ 60req/分の別枠レート制限が適用されるため個別に間隔を広げる
REQUEST_INTERVAL_SECONDS_FINS = 1.1
HTTP_429_WAIT_SECONDS = 60
HTTP_429_MAX_RETRIES = 5
SERVER_ERROR_WAIT_SECONDS = 20
SERVER_ERROR_MAX_RETRIES = 3

# 取引カレンダーの休日区分コード（HolDiv）。1=営業日, 2=東証半日立会日 はいずれも
# 取引が成立する日なので「営業日」として扱う（0=非営業日, 3=非営業日〔年末年始等〕は除外）。
# 実 API 疎通で 2024-01-03（年始）が "3" で返ることを確認済み。
BUSINESS_HOLDIV = {"1", "2"}

DEFAULT_START = "20160801"
CALENDAR_EARLIEST_ATTEMPT = "20080101"  # データ格納開始日（プラン制限で 400 になれば自動繰り上げ）
TOPIX_EARLIEST_ATTEMPT = "20080101"

PROGRESS_EVERY = 50

# 実 API 疎通で確認した 400 エラーメッセージ形式:
#   "Your subscription covers the following dates: 2016-07-05 ~ . If you want more data, ..."
PLAN_LIMIT_RE = re.compile(r"covers the following dates:\s*(\d{4}-\d{2}-\d{2})")
ZSHRC_KEY_RE = re.compile(r'^export JQUANTS_API_KEY="(.*)"\s*$', re.MULTILINE)


class AuthError(Exception):
    """認証エラー（401/403）。APIキーが無効・失効している場合に送出。即座に fatal 扱い。"""


class PlanLimitError(Exception):
    """契約プランの遡及可能期間より古い日付を指定した場合の 400 エラー。

    Attributes:
        earliest_date: エラーメッセージから抽出した「利用可能な最古日付」(YYYYMMDD)。
            抽出できなかった場合は None。
    """

    def __init__(self, message: str, earliest_date: Optional[str]) -> None:
        super().__init__(message)
        self.earliest_date = earliest_date


def now_jst() -> datetime.datetime:
    """現在時刻を JST で返す（zoneinfo 非依存、固定オフセットのみ）。"""
    return datetime.datetime.now(JST)


def get_api_key() -> str:
    """JQUANTS_API_KEY を環境変数優先、無ければ ~/.zshrc から取得する。

    キーの値は絶対にログ・stdout・保存ファイルに出力しない。

    Returns:
        APIキー文字列。

    Raises:
        SystemExit: どちらからも取得できない場合（exit code 2）。
    """
    key = os.environ.get("JQUANTS_API_KEY")
    if key:
        return key

    zshrc_path = Path.home() / ".zshrc"
    try:
        text = zshrc_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    matches = ZSHRC_KEY_RE.findall(text)
    if matches:
        return matches[-1]

    print("FATAL: JQUANTS_API_KEY が環境変数にも ~/.zshrc にも見つかりません", file=sys.stderr)
    sys.exit(2)


def _extract_message(body: bytes) -> str:
    """エラーレスポンスの JSON body から message フィールドを抽出。パース不能なら生テキストを返す。"""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "message" in parsed:
            return str(parsed["message"])
    except json.JSONDecodeError:
        pass
    return body.decode("utf-8", errors="replace")


def http_get_json(
    path: str, params: dict, api_key: str, interval: float = REQUEST_INTERVAL_SECONDS
) -> dict:
    """J-Quants API に GET リクエストを送り、JSON をパースして返す。

    429 は HTTP_429_WAIT_SECONDS 秒待機して最大 HTTP_429_MAX_RETRIES 回まで再試行。
    5xx・接続エラーは SERVER_ERROR_WAIT_SECONDS 秒待機して最大 SERVER_ERROR_MAX_RETRIES 回まで再試行。
    401/403 は AuthError、プラン制限の 400 は PlanLimitError を送出（呼び出し側で処理）。

    Args:
        path: `/v2/...` 形式のエンドポイントパス。
        params: クエリパラメータ（値が None のキーは送信しない）。
        api_key: `x-api-key` ヘッダーに設定するAPIキー。
        interval: リクエスト成功後に待機する秒数。エンドポイント別のレート制限に
            合わせて呼び出し側から指定する（例: fins/summary は別枠60req/分のため広め）。

    Returns:
        レスポンス JSON をパースした dict。

    Raises:
        AuthError: 401/403（認証エラー）。
        PlanLimitError: 400 かつプラン制限メッセージと判定できた場合。
        RuntimeError: それ以外のエラーでリトライ上限に達した場合。
    """
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE_URL}{path}?{query}"

    retries_429 = 0
    retries_server = 0
    while True:
        try:
            req = urllib.request.Request(url, headers={"x-api-key": api_key})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                body = resp.read()
            time.sleep(interval)
            return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            msg = _extract_message(body)
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: {msg}") from None
            if e.code == 400:
                m = PLAN_LIMIT_RE.search(msg)
                if m:
                    raise PlanLimitError(msg, m.group(1).replace("-", ""))
                raise RuntimeError(f"HTTP 400: {msg}")
            if e.code == 429:
                retries_429 += 1
                if retries_429 > HTTP_429_MAX_RETRIES:
                    raise RuntimeError(f"HTTP 429 リトライ上限到達: {msg}")
                print(
                    f"WARN: HTTP 429（レート制限）。{HTTP_429_WAIT_SECONDS}秒待機してリトライ"
                    f"({retries_429}/{HTTP_429_MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(HTTP_429_WAIT_SECONDS)
                continue
            if e.code >= 500:
                retries_server += 1
                if retries_server > SERVER_ERROR_MAX_RETRIES:
                    raise RuntimeError(f"HTTP {e.code} リトライ上限到達: {msg}")
                print(
                    f"WARN: HTTP {e.code}。{SERVER_ERROR_WAIT_SECONDS}秒待機してリトライ"
                    f"({retries_server}/{SERVER_ERROR_MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(SERVER_ERROR_WAIT_SECONDS)
                continue
            raise RuntimeError(f"HTTP {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            retries_server += 1
            if retries_server > SERVER_ERROR_MAX_RETRIES:
                raise RuntimeError(f"接続エラー リトライ上限到達: {e}")
            print(
                f"WARN: 接続エラー（{e}）。{SERVER_ERROR_WAIT_SECONDS}秒待機してリトライ"
                f"({retries_server}/{SERVER_ERROR_MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(SERVER_ERROR_WAIT_SECONDS)
            continue


def fetch_paginated(
    path: str, params: dict, api_key: str, interval: float = REQUEST_INTERVAL_SECONDS
) -> dict:
    """pagination_key が返る限り全ページを連結して1つの dict にまとめる。

    今回検証した date 単独指定の bars/master ではキーは発生しなかったが、
    calendar/topix の全期間取得や fins/summary 等で発生する可能性に備え汎用対応する。

    Args:
        interval: `http_get_json` に渡すリクエスト間隔秒数（エンドポイント別レート制限用）。
    """
    merged_data: list = []
    pagination_key: Optional[str] = None
    while True:
        query = dict(params)
        if pagination_key:
            query["pagination_key"] = pagination_key
        resp = http_get_json(path, query, api_key, interval=interval)
        merged_data.extend(resp.get("data", []))
        pagination_key = resp.get("pagination_key")
        if not pagination_key:
            break
    return {"data": merged_data}


def write_json_gz(path: Path, obj: dict) -> None:
    """dict を JSON 化して gzip 圧縮保存。同一ディレクトリの .tmp に書いてから os.replace でアトミックに配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    content = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with gzip.open(tmp_path, "wb") as f:
        f.write(content)
    os.replace(tmp_path, path)


def read_json_gz(path: Path) -> dict:
    with gzip.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def append_log(record: dict) -> None:
    """fetch_log.jsonl に1行追記。書き込み失敗は本処理を止めず警告のみ（監視が本処理を壊さない原則）。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARN: fetch_log 書き込み失敗: {e}", file=sys.stderr)


def fatal_auth_error(e: AuthError) -> None:
    print(f"FATAL: 認証エラー（APIキーが無効または失効している可能性）: {e}", file=sys.stderr)
    sys.exit(2)


# --- カレンダー・営業日導出 -------------------------------------------------


def fetch_calendar(api_key: str, run_id: str) -> None:
    """取引カレンダー全期間を取得して calendar.json.gz に保存（既存ならスキップ）。"""
    path = DATA_ROOT / "calendar.json.gz"
    if path.exists():
        print(f"[calendar] 既存ファイルをスキップ: {path}")
        append_log({
            "run_id": run_id, "ts": now_jst().isoformat(), "kind": "calendar", "date": None,
            "status": "skipped_exists", "count": None, "file": None, "error": None,
        })
        return

    today = now_jst().date()
    to_date = f"{today.year + 1}1231"
    from_date = CALENDAR_EARLIEST_ATTEMPT
    try:
        resp = fetch_paginated("/v2/markets/calendar", {"from": from_date, "to": to_date}, api_key)
    except PlanLimitError as e:
        if e.earliest_date is None:
            raise
        print(
            f"WARN: [calendar] プラン制限（{e}）。開始日を {e.earliest_date} に繰り上げてリトライ",
            file=sys.stderr,
        )
        resp = fetch_paginated("/v2/markets/calendar", {"from": e.earliest_date, "to": to_date}, api_key)

    if not resp["data"]:
        print("WARN: [calendar] 空レスポンス", file=sys.stderr)
    write_json_gz(path, resp)
    print(f"[calendar] saved: {len(resp['data'])} 件 -> {path.relative_to(PROJECT_ROOT)}")
    append_log({
        "run_id": run_id, "ts": now_jst().isoformat(), "kind": "calendar", "date": None,
        "status": "saved", "count": len(resp["data"]), "file": str(path.relative_to(PROJECT_ROOT)), "error": None,
    })


def fetch_topix(api_key: str, run_id: str) -> None:
    """TOPIX 日足全期間を取得して topix.json.gz に保存（既存ならスキップ）。"""
    path = DATA_ROOT / "topix.json.gz"
    if path.exists():
        print(f"[topix] 既存ファイルをスキップ: {path}")
        append_log({
            "run_id": run_id, "ts": now_jst().isoformat(), "kind": "topix", "date": None,
            "status": "skipped_exists", "count": None, "file": None, "error": None,
        })
        return

    to_date = now_jst().strftime("%Y%m%d")
    from_date = TOPIX_EARLIEST_ATTEMPT
    try:
        resp = fetch_paginated("/v2/indices/bars/daily/topix", {"from": from_date, "to": to_date}, api_key)
    except PlanLimitError as e:
        if e.earliest_date is None:
            raise
        print(
            f"WARN: [topix] プラン制限（{e}）。開始日を {e.earliest_date} に繰り上げてリトライ",
            file=sys.stderr,
        )
        resp = fetch_paginated("/v2/indices/bars/daily/topix", {"from": e.earliest_date, "to": to_date}, api_key)

    if not resp["data"]:
        print("WARN: [topix] 空レスポンス", file=sys.stderr)
    write_json_gz(path, resp)
    print(f"[topix] saved: {len(resp['data'])} 件 -> {path.relative_to(PROJECT_ROOT)}")
    append_log({
        "run_id": run_id, "ts": now_jst().isoformat(), "kind": "topix", "date": None,
        "status": "saved", "count": len(resp["data"]), "file": str(path.relative_to(PROJECT_ROOT)), "error": None,
    })


def load_calendar_days(api_key: str, run_id: str) -> list[tuple[str, str]]:
    """(YYYYMMDD, HolDiv) のタプルを日付昇順で返す。calendar.json.gz が無ければ先に取得する。"""
    path = DATA_ROOT / "calendar.json.gz"
    if not path.exists():
        fetch_calendar(api_key, run_id)
    obj = read_json_gz(path)
    days = [(rec["Date"].replace("-", ""), rec["HolDiv"]) for rec in obj["data"]]
    days.sort(key=lambda x: x[0])
    return days


def business_days_in_range(calendar_days: list[tuple[str, str]], start: str, end: str) -> list[str]:
    """[start, end] 内の営業日（HolDiv が 1 または 2）を日付昇順で返す。"""
    return [d for d, h in calendar_days if h in BUSINESS_HOLDIV and start <= d <= end]


def month_end_business_days_in_range(calendar_days: list[tuple[str, str]], start: str, end: str) -> list[str]:
    """各月の最終営業日を全カレンダーから導出し、[start, end] 内のものを日付昇順で返す。"""
    business_days = [d for d, h in calendar_days if h in BUSINESS_HOLDIV]
    last_of_month: dict[str, str] = {}
    for d in business_days:
        ym = d[:6]
        if ym not in last_of_month or d > last_of_month[ym]:
            last_of_month[ym] = d
    month_ends = sorted(last_of_month.values())
    return [d for d in month_ends if start <= d <= end]


def week_end_business_days_in_range(calendar_days: list[tuple[str, str]], start: str, end: str) -> list[str]:
    """各暦週（月曜起点のISO週）の最終営業日を全カレンダーから導出し、[start, end] 内のものを日付昇順で返す。

    信用取引週末残高（margin-interest）の基準日と一致させるための helper。
    実 API 疎通で「基準日は原則金曜、金曜が休場の週は木曜・水曜等その週最後の営業日に繰り上がる」
    ことを確認済み（過去10年の非金曜基準日17件全てが対応週の祝日と整合）。
    """
    business_days = [d for d, h in calendar_days if h in BUSINESS_HOLDIV]
    last_of_week: dict[tuple[int, int], str] = {}
    for d in business_days:
        dt = datetime.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        iso_year, iso_week, _ = dt.isocalendar()
        key = (iso_year, iso_week)
        if key not in last_of_week or d > last_of_week[key]:
            last_of_week[key] = d
    week_ends = sorted(last_of_week.values())
    return [d for d in week_ends if start <= d <= end]


# --- 日次スナップショット（bars / master 共通処理） --------------------------


def fetch_snapshot_for_date(
    path_key: str, endpoint: str, date_str: str, api_key: str,
    interval: float = REQUEST_INTERVAL_SECONDS, param_name: str = "date",
) -> dict:
    """kind に応じた日次スナップショット1件を取得・保存する（既存ならスキップ）。

    開示・取引がない日は data が空配列のレスポンスがそのまま空マーカーとして
    保存される（警告ログのみで正常系として扱う）。

    Args:
        interval: `fetch_paginated` に渡すリクエスト間隔秒数（エンドポイント別レート制限用）。
        param_name: クエリパラメータ名（既定 "date"。short-sale-report は "disc_date" を使用）。

    Returns:
        {"status": "skipped_exists"|"saved", "count": Optional[int], "file": Optional[str]}
    """
    path = DATA_ROOT / path_key / f"{date_str}.json.gz"
    if path.exists():
        return {"status": "skipped_exists", "count": None, "file": None}

    resp = fetch_paginated(endpoint, {param_name: date_str}, api_key, interval=interval)
    count = len(resp["data"])
    if count == 0:
        print(f"WARN: [{path_key}] {date_str} 空レスポンス", file=sys.stderr)
    write_json_gz(path, resp)
    return {"status": "saved", "count": count, "file": str(path.relative_to(PROJECT_ROOT))}


def run_daily_snapshot(
    kind: str, path_key: str, endpoint: str, dates: list[str], api_key: str, run_id: str,
    interval: float = REQUEST_INTERVAL_SECONDS, param_name: str = "date",
) -> None:
    """日付リストを順に処理し、各日をログに記録しながら進捗を表示する。

    プラン制限（PlanLimitError）で許可開始日が判明したら、以降その日付未満は
    API を呼ばずに即スキップする（「自動で開始日を繰り上げる」の実装）。

    Args:
        interval: `fetch_snapshot_for_date` に渡すリクエスト間隔秒数（エンドポイント別レート制限用）。
        param_name: `fetch_snapshot_for_date` に渡すクエリパラメータ名（既定 "date"）。
    """
    known_earliest: Optional[str] = None
    total = len(dates)
    for idx, date_str in enumerate(dates, start=1):
        ts = now_jst().isoformat()
        if known_earliest and date_str < known_earliest:
            record = {
                "run_id": run_id, "ts": ts, "kind": kind, "date": date_str,
                "status": "skipped_plan_limit", "count": None, "file": None, "error": None,
            }
        else:
            try:
                result = fetch_snapshot_for_date(
                    path_key, endpoint, date_str, api_key, interval=interval, param_name=param_name
                )
                record = {
                    "run_id": run_id, "ts": ts, "kind": kind, "date": date_str,
                    "status": result["status"], "count": result["count"], "file": result["file"], "error": None,
                }
            except PlanLimitError as e:
                if e.earliest_date:
                    known_earliest = e.earliest_date
                    print(
                        f"WARN: [{kind}] {date_str} プラン制限（{e}）。"
                        f"以降 {known_earliest} 未満の日付をスキップ",
                        file=sys.stderr,
                    )
                else:
                    print(f"WARN: [{kind}] {date_str} プラン制限だが開始日抽出失敗（{e}）。この日のみスキップ", file=sys.stderr)
                record = {
                    "run_id": run_id, "ts": ts, "kind": kind, "date": date_str,
                    "status": "skipped_plan_limit", "count": None, "file": None, "error": str(e),
                }
            except AuthError as e:
                append_log({
                    "run_id": run_id, "ts": ts, "kind": kind, "date": date_str,
                    "status": "auth_error", "count": None, "file": None, "error": str(e),
                })
                fatal_auth_error(e)
                return  # pragma: no cover — fatal_auth_error は sys.exit(2) する
            except RuntimeError as e:
                print(f"WARN: [{kind}] {date_str} 取得失敗（次回再実行で再取得されます）: {e}", file=sys.stderr)
                record = {
                    "run_id": run_id, "ts": ts, "kind": kind, "date": date_str,
                    "status": "error", "count": None, "file": None, "error": str(e),
                }
        append_log(record)
        if idx % PROGRESS_EVERY == 0 or idx == total:
            pct = (idx / total * 100) if total else 100.0
            print(f"進捗: {idx}/{total} ({pct:.1f}%)")


# --- --status ----------------------------------------------------------------


def print_status(start: str, end: str) -> None:
    """カレンダー基準の期待ファイル数 vs 取得済み数をデータ種別ごとに表示する（読み取り専用・API未呼出）。"""
    print(f"=== J-Quants キャッシュ状態（{start} 〜 {end}） ===")
    calendar_path = DATA_ROOT / "calendar.json.gz"
    topix_path = DATA_ROOT / "topix.json.gz"
    print(f"calendar: {'済' if calendar_path.exists() else '未取得'} ({calendar_path})")
    print(f"topix   : {'済' if topix_path.exists() else '未取得'} ({topix_path})")

    if not calendar_path.exists():
        print("(bars/master の期待件数はカレンダー取得後に算出できます)")
        return

    obj = read_json_gz(calendar_path)
    calendar_days = sorted((rec["Date"].replace("-", ""), rec["HolDiv"]) for rec in obj["data"])
    bars_expected = business_days_in_range(calendar_days, start, end)
    master_expected = month_end_business_days_in_range(calendar_days, start, end)

    week_expected = week_end_business_days_in_range(calendar_days, start, end)

    bars_dir = DATA_ROOT / "bars"
    master_dir = DATA_ROOT / "master"
    fins_dir = DATA_ROOT / "fins"
    margin_dir = DATA_ROOT / "margin"
    shortsale_dir = DATA_ROOT / "shortsale"
    bars_actual = sum(
        1 for p in bars_dir.glob("*.json.gz") if start <= p.name.removesuffix(".json.gz") <= end
    ) if bars_dir.exists() else 0
    master_actual = sum(
        1 for p in master_dir.glob("*.json.gz") if start <= p.name.removesuffix(".json.gz") <= end
    ) if master_dir.exists() else 0
    fins_actual = sum(
        1 for p in fins_dir.glob("*.json.gz") if start <= p.name.removesuffix(".json.gz") <= end
    ) if fins_dir.exists() else 0
    margin_actual = sum(
        1 for p in margin_dir.glob("*.json.gz") if start <= p.name.removesuffix(".json.gz") <= end
    ) if margin_dir.exists() else 0
    shortsale_actual = sum(
        1 for p in shortsale_dir.glob("*.json.gz") if start <= p.name.removesuffix(".json.gz") <= end
    ) if shortsale_dir.exists() else 0

    print(f"bars     : {bars_actual}/{len(bars_expected)} 件（期間内期待値）")
    print(f"master   : {master_actual}/{len(master_expected)} 件（期間内期待値）")
    # fins（財務情報）は営業日ごとに照会するが開示がある日のみレコードが載る（空マーカーも1件としてカウント）
    print(f"fins     : {fins_actual}/{len(bars_expected)} 件（期間内期待値。開示が無い日も空マーカー保存で1件扱い）")
    print(f"margin   : {margin_actual}/{len(week_expected)} 件（期間内期待値。各暦週の最終営業日単位）")
    print(f"shortsale: {shortsale_actual}/{len(bars_expected)} 件（期間内期待値。開示が無い日も空マーカー保存で1件扱い）")


# --- main ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="J-Quants API V2 生データフェッチャー")
    parser.add_argument("--start", default=DEFAULT_START, help=f"取得開始日 YYYYMMDD（デフォルト {DEFAULT_START}）")
    parser.add_argument("--end", default=None, help="取得終了日 YYYYMMDD（デフォルト今日）")
    parser.add_argument(
        "--only", choices=["calendar", "bars", "master", "topix", "fins", "margin", "shortsale"],
        help="取得対象を1種類に限定（省略時は calendar→master→topix→bars→fins→margin→shortsale の順で全て）",
    )
    parser.add_argument("--status", action="store_true", help="期待件数 vs 取得済み件数を表示して終了")
    args = parser.parse_args()

    end = args.end or now_jst().strftime("%Y%m%d")
    start = args.start

    if args.status:
        print_status(start, end)
        return 0

    api_key = get_api_key()
    run_id = uuid.uuid4().hex
    append_log({
        "run_id": run_id, "ts": now_jst().isoformat(), "kind": None, "date": None,
        "status": "run_start", "count": None, "file": None, "error": None,
    })

    targets = (
        [args.only] if args.only
        else ["calendar", "master", "topix", "bars", "fins", "margin", "shortsale"]
    )

    for target in targets:
        try:
            if target == "calendar":
                fetch_calendar(api_key, run_id)
            elif target == "topix":
                fetch_topix(api_key, run_id)
            elif target == "master":
                calendar_days = load_calendar_days(api_key, run_id)
                dates = month_end_business_days_in_range(calendar_days, start, end)
                print(f"[master] 対象月末営業日 {len(dates)} 件")
                run_daily_snapshot("master", "master", "/v2/equities/master", dates, api_key, run_id)
            elif target == "bars":
                calendar_days = load_calendar_days(api_key, run_id)
                dates = business_days_in_range(calendar_days, start, end)
                print(f"[bars] 対象営業日 {len(dates)} 件")
                run_daily_snapshot("bars", "bars", "/v2/equities/bars/daily", dates, api_key, run_id)
            elif target == "fins":
                calendar_days = load_calendar_days(api_key, run_id)
                dates = business_days_in_range(calendar_days, start, end)
                print(f"[fins] 対象営業日 {len(dates)} 件（60req/分の別枠制限のため間隔 {REQUEST_INTERVAL_SECONDS_FINS}秒）")
                run_daily_snapshot(
                    "fins", "fins", "/v2/fins/summary", dates, api_key, run_id,
                    interval=REQUEST_INTERVAL_SECONDS_FINS,
                )
            elif target == "margin":
                calendar_days = load_calendar_days(api_key, run_id)
                dates = week_end_business_days_in_range(calendar_days, start, end)
                print(f"[margin] 対象週末営業日 {len(dates)} 件（date単独指定で市場全銘柄を1リクエスト取得）")
                run_daily_snapshot("margin", "margin", "/v2/markets/margin-interest", dates, api_key, run_id)
            elif target == "shortsale":
                calendar_days = load_calendar_days(api_key, run_id)
                dates = business_days_in_range(calendar_days, start, end)
                print(f"[shortsale] 対象営業日 {len(dates)} 件（disc_date単独指定で市場全銘柄を1リクエスト取得）")
                run_daily_snapshot(
                    "shortsale", "shortsale", "/v2/markets/short-sale-report", dates, api_key, run_id,
                    param_name="disc_date",
                )
        except AuthError as e:
            append_log({
                "run_id": run_id, "ts": now_jst().isoformat(), "kind": target, "date": None,
                "status": "auth_error", "count": None, "file": None, "error": str(e),
            })
            fatal_auth_error(e)
        except PlanLimitError as e:
            print(f"WARN: [{target}] プラン制限で取得断念: {e}", file=sys.stderr)
            append_log({
                "run_id": run_id, "ts": now_jst().isoformat(), "kind": target, "date": None,
                "status": "plan_limit_error", "count": None, "file": None, "error": str(e),
            })
        except RuntimeError as e:
            print(f"WARN: [{target}] 取得失敗（次回再実行で再取得されます）: {e}", file=sys.stderr)
            append_log({
                "run_id": run_id, "ts": now_jst().isoformat(), "kind": target, "date": None,
                "status": "error", "count": None, "file": None, "error": str(e),
            })

    return 0


if __name__ == "__main__":
    sys.exit(main())
