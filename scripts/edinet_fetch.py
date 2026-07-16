#!/usr/bin/env python3
"""EDINET API v2 大量保有報告書・変更報告書 メタデータフェッチャー（カタログ§2-E #11用）。

docs/stock-algo-kpi-catalog.md の §2-E「アクティビスト大量保有出現」を実装するための
一次データ取得層。`/api/v2/documents.json`（書類一覧+メタデータ, type=2）を営業日ごとに叩き、
大量保有報告書・変更報告書（docTypeCode 350/360）に該当するレコードのみを抽出して
`data/edinet/YYYYMMDD.json.gz` に保存する（アトミック・冪等・run_idログ。jq_fetch.py と同流儀）。

依存はすべて標準ライブラリ。pip install は一切不要。

認証（2026-07-07 実疎通確認済み・実キーで動作確認済み）:
    EDINET API v2 は無効/未指定キーでも **HTTP 200** を返し、実際のエラーは JSON body 内の
    "StatusCode" フィールドに埋め込まれる（例: `{"StatusCode": 401, "message": "Access denied
    due to invalid subscription key..."}`）。したがって jq_fetch.py のような HTTP ステータス
    ベースの分岐だけでは検出できず、本モジュールは JSON body も必ず検査する。
    キーは **クエリパラメータ `Subscription-Key` で正解**（実キーでの疎通確認済み・
    2026-07-07。ヘッダー方式は試していない）。

**重要な制約（2026-07-07 実データで発見・確定）**: `/documents.json` は**「本日から遡って
約5年」のローリングウィンドウ**でしか大量保有報告書系(ordinanceCode=060)のメタデータを
返さない。5年より古い日付は該当日の総件数(count)は正しく返るが、個々のレコードの
filerName/docTypeCode/docDescription等が全てnullになる（境界を日単位で特定済み:
2021-07-06は0件・2021-07-07から通常どおり取得可能=本日2026-07-07からきっかり5年前）。
書類本体(`/documents/{docID}`)も同様に404で取得不能（縦覧期間経過による削除と推定）。
したがって `data/edinet/` は実質 **2021-07-07以降のみ有効なデータ**となる（それ以前は
0件の空ファイルとして保存され、これは正常な結果である）。

**docTypeCode 350/360 の分類は実データで訂正**: 当初想定（350=新規/360=変更）は誤りで、
実際は **docTypeCode=350 が「大量保有報告書」(新規)と「変更報告書」の両方を含み、
docTypeCode=360は「訂正報告書」（既存の大量保有報告書・変更報告書の訂正のみ）**。
新規/変更/訂正の判別は `docTypeCode` ではなく `docDescription` のテキスト内容で行う
必要がある（scripts/kpi_activist_signals.py の分類ロジック参照）。

利用規約/仕様書（確認日2026-07-16）:
    本モジュールが叩く `https://api.edinet-fsa.go.jp/api/v2` は、EDINET閲覧サイト
    (https://disclosure2.edinet-fsa.go.jp/ 。2026-07-16 curl疎通確認済み・HTTP200)の
    「API機能」からAPIキーを登録した際に提示される利用規約に同意した上で発行される、
    公式に提供された機械取得経路である（本プロジェクトのAPIキーは2026-07-07登録・
    当時の利用規約に同意済み。BASE_URL自体も2026-07-07から継続して実疎通確認済み）。
    **正確な規約ページの個別URLはこのdocstring作成時点(2026-07-16)では確定できていない**
    （EDINET閲覧サイトはJavaScript SPAのため、curlによる静的HTML走査では規約ページへの
    直接リンクを機械的に抽出できなかった。事実確認ルール上、未確認のURLは記載しない）。
    正確な規約条文の参照が必要な場合は、APIキー登録時にユーザーが同意した規約ページ
    （EDINET閲覧サイトのAPI機能メニューから遷移）を参照すること。

Usage:
    python3 scripts/edinet_fetch.py                                    # 既定期間を全取得（大量保有）
    python3 scripts/edinet_fetch.py --start 20210707 --end 20260707     # 実際に取得可能な範囲
    python3 scripts/edinet_fetch.py --status                           # 期待件数 vs 取得済み件数
    python3 scripts/edinet_fetch.py --probe                            # 疎通確認のみ（1日分・保存しない）
    python3 scripts/edinet_fetch.py --fetch-code-master                # EDINETコード→証券コード マスタ取得

documents_all データセット（カタログ§8 BL-1「親子上場解消・TOB先回り」用の一次データ。
2026-07-16 team lead指示で追加・Codexレビュー指摘を受け同日 tob→documents_all に再設計）:
    大量保有報告書とは独立した別データセット。当日の書類一覧を **docDescriptionでの
    事前絞り込みをせず全件・無加工のまま** `data/edinet/documents_all/YYYYMMDD.json.gz`
    に保存する（大量保有側の `data/edinet/YYYYMMDD.json.gz` とは完全に分離。`--dataset`
    を指定しない既存呼び出しの挙動は一切変わらない）。TOBキーワード
    （`TOB_KEYWORDS`＝「公開買付」「自己株券買付状況報告書」）によるマッチは**ログ集計・
    --probe表示専用**（filtered_countとしてfetch_log.jsonlと--probeに残るのみで、保存対象
    の絞り込みには一切使わない＝将来の分類層の入口として、docDescriptionによる事前除外が
    不可逆な偽陰性を生まないようにする設計。2026-07-16 Codexレビュー指摘で全件保存に変更）。

    **直近5営業日は毎回強制再取得する**（ファイル存在チェックだけでのスキップだと、
    18:45の日次実行時点で未提出・未反映・後日訂正された書類を永久に取りこぼす穴になる
    ため。2026-07-16 Codexレビュー指摘）。内容（docID集合+ハッシュ）が旧ファイルと
    変化していれば旧ファイルを `<date>.rev<連番>.json.gz` として退避してから新版を保存
    （上書きで消さない）。変化がなければ何もしない。直近5営業日より前は、
    `data/edinet/documents_all/` に保存済みファイルが無い日（＝欠損日）だけを取得する
    （5年分を毎日フルスキャンしてAPIを叩き直すことはしない。既存日はネットワーク呼び出し
    もfetch_logへの追記も発生しない）。

    python3 scripts/edinet_fetch.py --dataset documents_all --probe               # 疎通確認（直近営業日）
    python3 scripts/edinet_fetch.py --dataset documents_all --start 20210707 --end 20260716
    python3 scripts/edinet_fetch.py --dataset documents_all                       # 日次実行の既定形（--end省略時は当日まで自動）
    python3 scripts/edinet_fetch.py --dataset documents_all --status
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import uuid
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: write_json_gz/read_json_gz/now_jst/カレンダー系を再利用)

DATA_ROOT = PROJECT_ROOT / "data" / "edinet"
LOG_PATH = DATA_ROOT / "fetch_log.jsonl"

BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
TIMEOUT_SECONDS = 30

# レート制御（団体からの公式レート制限公表なし。team lead指示「1-2req/秒」に収まる間隔を採用）
REQUEST_INTERVAL_SECONDS = 0.7
HTTP_429_WAIT_SECONDS = 60
HTTP_429_MAX_RETRIES = 5
SERVER_ERROR_WAIT_SECONDS = 20
SERVER_ERROR_MAX_RETRIES = 3

# 2026-07-07 実データで確定: documents.json は本日から遡って約5年しかメタデータを返さない
# （5年より古い日付は該当日の総件数は返るが個々のレコードが全てnullになる。境界を日単位で
# 特定済み: 2021-07-06は0件・2021-07-07から取得可能）。DEFAULT_START はこの実測境界に合わせる。
DEFAULT_START = "20210707"
DEFAULT_END = "20260707"

PROGRESS_EVERY = 50

# カタログ§2-E #11「大量保有報告書・変更報告書」対象の docTypeCode（実データで確認済み・
# 2026-07-07）。ordinanceCode=060(大量保有府令)の全レコードはこの2値のいずれかに収まる
# ことを実データで確認済み。ただし **350=新規/360=変更 という当初想定は誤り**:
# 実際は docTypeCode=350 が「大量保有報告書」(新規)と「変更報告書」の両方を含み、
# docTypeCode=360 は「訂正報告書」（既存の大量保有報告書・変更報告書に対する訂正のみ）。
# 新規/変更/訂正の判別は scripts/kpi_activist_signals.py 側で docDescription のテキストを
# 見て行う（本モジュールは350/360をまとめて素通しするだけで良い）。
TARGET_DOC_TYPE_CODES = {"350", "360"}

# --- documents_all データセット（カタログ§8 BL-1用・2026-07-16 team lead指示で追加、
# 同日Codexレビュー指摘を受け tob→documents_all に再設計） ---
# 公開買付関連書類は単一のordinanceCode/docTypeCodeに収まらない（届出書・意見表明報告書・
# 対質問回答報告書・撤回届出書・自己株券買付状況報告書等、複数の書類体系にまたがる）。
# docDescriptionによる事前絞り込みは将来の再分類を不可能にする偽陰性が不可逆なため、
# 保存は当日の書類一覧を全件・無加工で行う。TOB_KEYWORDS はログ集計・--probe表示専用
# （保存対象の絞り込みには使わない）。
ALL_DOCS_DATA_ROOT = DATA_ROOT / "documents_all"
TOB_KEYWORDS = ("公開買付", "自己株券買付状況報告書")
ALL_DOCS_DEFAULT_START = DEFAULT_START  # 大量保有と同じ5年ローリング窓の実測境界(2021-07-07)を再利用
# 直近何営業日を「毎回強制再取得（既存ファイルがあっても再取得し、変化があれば旧版をrev退避）」
# 対象にするか。18:45の日次実行時点で未提出・未反映の書類が後から追加されるケースを
# カバーする窓（2026-07-16 Codexレビュー指摘）。
RECENT_FORCE_REFETCH_DAYS = 5

# EDINETコード -> 証券コード 変換用の公式マスタ（EDINET提出者一覧、無料・APIキー不要の静的ファイル）。
# 大量保有報告書のリストAPIは secCode が null のことが多く（提出者=株主自身は非上場が通常のため）、
# 対象会社は issuerEdinetCode で示される。これを証券コードへ変換するために必要
# （2026-07-07 実疎通確認済み・APIキー不要）。
EDINET_CODE_MASTER_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
EDINET_CODE_MASTER_PATH = DATA_ROOT / "edinet_code_master.csv"

ZSHRC_KEY_RE = re.compile(r'^export EDINET_API_KEY="(.*)"\s*$', re.MULTILINE)

PROBE_SAMPLE_DATE = "20220601"  # --probe / キー未設定時の疎通確認に使う固定サンプル日


class AuthError(Exception):
    """認証エラー（HTTP 401/403、または EDINET 特有の「HTTP200+body内StatusCode401/403」）。"""


def get_api_key() -> Optional[str]:
    """EDINET_API_KEY を環境変数優先、無ければ ~/.zshrc から取得する。

    jq_fetch.get_api_key() と異なり見つからなくても sys.exit しない（呼び出し側が
    「キー未設定時は1回だけプローブしてから待機報告する」を実装できるようにするため）。
    """
    key = __import__("os").environ.get("EDINET_API_KEY")
    if key:
        return key
    zshrc_path = Path.home() / ".zshrc"
    try:
        text = zshrc_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    matches = ZSHRC_KEY_RE.findall(text)
    return matches[-1] if matches else None


def _inject_api_key(params: dict, api_key: Optional[str]) -> dict:
    """クエリパラメータに Subscription-Key を追加する（唯一の注入箇所。ヘッダー方式に
    変更が必要と判明した場合はここだけ直せばよい設計）。"""
    query = dict(params)
    if api_key:
        query["Subscription-Key"] = api_key
    return query


def edinet_get_json(
    path: str, params: dict, api_key: Optional[str], interval: float = REQUEST_INTERVAL_SECONDS
) -> dict:
    """EDINET API に GET し、JSON をパースして返す。

    EDINET は認証エラー等でも HTTP 200 を返し、実エラーは JSON body の "StatusCode" に
    埋め込まれる（2026-07-07 実疎通確認済み）。本関数は HTTP レベルのエラーと
    body 埋め込みエラーの両方を検査する。

    Raises:
        AuthError: 401/403（HTTPレベル・埋め込みレベルいずれか）。
        RuntimeError: それ以外のエラーでリトライ上限に達した場合。
    """
    query = _inject_api_key(params, api_key)
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(query)}"

    retries_429 = 0
    retries_server = 0
    while True:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                body = resp.read()
            parsed = json.loads(body)

            # --- body 埋め込みエラーの検査（"metadata" キーが無く "StatusCode" がある形） ---
            if isinstance(parsed, dict) and "metadata" not in parsed and "StatusCode" in parsed:
                embedded_status = parsed.get("StatusCode")
                msg = parsed.get("message", "")
                if embedded_status in (401, 403):
                    raise AuthError(f"EDINET StatusCode {embedded_status}: {msg}")
                if embedded_status == 429:
                    retries_429 += 1
                    if retries_429 > HTTP_429_MAX_RETRIES:
                        raise RuntimeError(f"EDINET StatusCode 429 リトライ上限到達: {msg}")
                    print(
                        f"WARN: EDINET StatusCode 429（レート制限）。{HTTP_429_WAIT_SECONDS}秒待機して"
                        f"リトライ({retries_429}/{HTTP_429_MAX_RETRIES})",
                        file=sys.stderr,
                    )
                    time.sleep(HTTP_429_WAIT_SECONDS)
                    continue
                if isinstance(embedded_status, int) and embedded_status >= 500:
                    retries_server += 1
                    if retries_server > SERVER_ERROR_MAX_RETRIES:
                        raise RuntimeError(f"EDINET StatusCode {embedded_status} リトライ上限到達: {msg}")
                    print(
                        f"WARN: EDINET StatusCode {embedded_status}。{SERVER_ERROR_WAIT_SECONDS}秒待機して"
                        f"リトライ({retries_server}/{SERVER_ERROR_MAX_RETRIES})",
                        file=sys.stderr,
                    )
                    time.sleep(SERVER_ERROR_WAIT_SECONDS)
                    continue
                raise RuntimeError(f"EDINET StatusCode {embedded_status}: {msg}")

            # --- 正常系: metadata.status も念のため確認 ---
            metadata = parsed.get("metadata") if isinstance(parsed, dict) else None
            if metadata is not None and str(metadata.get("status", "200")) != "200":
                raise RuntimeError(
                    f"EDINET metadata.status={metadata.get('status')}: {metadata.get('message', '')}"
                )

            time.sleep(interval)
            return parsed
        except urllib.error.HTTPError as e:
            body = e.read()
            msg = body.decode("utf-8", errors="replace")
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: {msg}") from None
            if e.code == 429:
                retries_429 += 1
                if retries_429 > HTTP_429_MAX_RETRIES:
                    raise RuntimeError(f"HTTP 429 リトライ上限到達: {msg}")
                print(f"WARN: HTTP 429。{HTTP_429_WAIT_SECONDS}秒待機してリトライ", file=sys.stderr)
                time.sleep(HTTP_429_WAIT_SECONDS)
                continue
            if e.code >= 500:
                retries_server += 1
                if retries_server > SERVER_ERROR_MAX_RETRIES:
                    raise RuntimeError(f"HTTP {e.code} リトライ上限到達: {msg}")
                print(f"WARN: HTTP {e.code}。{SERVER_ERROR_WAIT_SECONDS}秒待機してリトライ", file=sys.stderr)
                time.sleep(SERVER_ERROR_WAIT_SECONDS)
                continue
            raise RuntimeError(f"HTTP {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            retries_server += 1
            if retries_server > SERVER_ERROR_MAX_RETRIES:
                raise RuntimeError(f"接続エラー リトライ上限到達: {e}")
            print(f"WARN: 接続エラー（{e}）。{SERVER_ERROR_WAIT_SECONDS}秒待機してリトライ", file=sys.stderr)
            time.sleep(SERVER_ERROR_WAIT_SECONDS)
            continue


def append_log(record: dict) -> None:
    """fetch_log.jsonl に1行追記する（書き込み失敗は本処理を止めない）。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARN: fetch_log 書き込み失敗: {e}", file=sys.stderr)


def _to_iso_date(date_str: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD（EDINET の date パラメータ形式）。"""
    return f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"


def fetch_documents_for_date(date_str: str, api_key: Optional[str], run_id: str) -> dict:
    """指定営業日(YYYYMMDD)の書類一覧を取得し、docTypeCode 350/360 のみ抽出して保存する（既存ならスキップ）。

    Returns:
        {"status": "skipped_exists"|"saved", "total_count": Optional[int],
         "filtered_count": Optional[int], "file": Optional[str]}
    """
    path = DATA_ROOT / f"{date_str}.json.gz"
    if path.exists():
        return {"status": "skipped_exists", "total_count": None, "filtered_count": None, "file": None}

    resp = edinet_get_json("/documents.json", {"date": _to_iso_date(date_str), "type": "2"}, api_key)
    results = resp.get("results") or []
    filtered = [r for r in results if str(r.get("docTypeCode")) in TARGET_DOC_TYPE_CODES]

    out_obj = {
        "date": date_str,
        "total_count": len(results),
        "filtered_count": len(filtered),
        "results": filtered,
    }
    jq_fetch.write_json_gz(path, out_obj)  # Canonical Module: アトミック gzip 保存を再利用
    return {
        "status": "saved",
        "total_count": len(results),
        "filtered_count": len(filtered),
        "file": str(path.relative_to(PROJECT_ROOT)),
    }


def _matches_tob_keywords(doc_description) -> Optional[bool]:
    """docDescription がTOB関連キーワード（TOB_KEYWORDS）を含むか判定する。

    documents_all データセットでは保存対象の絞り込みには使わない（ログ集計・--probe表示専用）。

    Returns:
        True/False: docDescriptionがstrで判定できた場合。None: docDescriptionがstrでない
        （None含む）。呼び出し側（_fetch_all_documents）で「Noneは正常」「None以外の
        非strは真の異常」に切り分けて集計する（2026-07-16 team lead追補）。
    """
    if not isinstance(doc_description, str):
        return None
    return any(kw in doc_description for kw in TOB_KEYWORDS)


def _content_signature(results: list) -> tuple:
    """results（EDINET APIレスポンスのレコードリスト）のdocID集合と正規化ハッシュを返す。

    直近営業日の強制再取得モードで「内容が本当に変化したか」を判定するために使う。
    docID集合だけでなくレコード内容のハッシュも見るのは、同一docIDのレコードが
    withdrawalStatus等のフィールド更新だけで書き換わるケース（撤回・訂正の反映）を
    docID集合の比較だけでは取りこぼすため。

    非dict要素（レコード自体が壊れている異常なレスポンス）が混じっていても
    AttributeErrorでクラッシュしないよう、dictでない要素は repr() を安全な代替キーとして
    使う（2026-07-16 Codexレビュー4巡目指摘: 旧実装は全要素に無条件で r.get() を呼んでいた）。

    Returns:
        (doc_ids: frozenset[str], digest: str)
    """
    doc_ids = frozenset(
        str(r.get("docID")) if isinstance(r, dict) else f"__non_dict_record__:{r!r}"
        for r in results
    )
    canonical = json.dumps(results, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return doc_ids, digest


def _fetch_all_documents(date_str: str, api_key: Optional[str]) -> tuple:
    """指定営業日の書類一覧を取得する（保存はしない）。TOBキーワード該当数・null件数・
    真のパース失敗数もログ集計用に計算して返す（保存対象の絞り込みには使わない）。

    **parse_failures の定義（2026-07-16 team lead追補で確定）**: docDescription=None は
    EDINETの正常な状態であり「パース失敗」ではない（モジュールdocstring冒頭の「重要な制約」
    参照: 5年ローリング窓境界付近の書類はfilerName/docTypeCode/docDescription等が全て
    nullになる。実データで境界付近76〜95%がnullであることを確認済み・当初実装は
    これを丸ごとparse_failuresに計上しており過大だった）。したがって None は
    null_description_count として別枠でカウントし、parse_failures は「レコード自体が
    dictでない」「docDescriptionがNoneでもstrでもない想定外の型」等、真に異常なケースのみに
    限定する。

    APIレスポンスの "results" キー自体がlistでない異常応答（EDINET側の仕様外レスポンス）は
    RuntimeErrorを送出する（2026-07-16 Codexレビュー4巡目指摘: 旧実装は `or []` でfalsy値
    のみ救っており、truthyだが型が違う値〔例: 文字列〕はそのまま素通しして後続の
    for文でレコードごとの文字を誤ってイテレートする恐れがあった。呼び出し元はRuntimeError
    を既存の例外ハンドラで捕捉し、当該日をstatus=errorとして次の日へ継続する）。

    Returns:
        (results: list, tob_filtered_count: int, null_description_count: int, parse_failures: int)

    Raises:
        RuntimeError: "results" がlistでない場合。
    """
    resp = edinet_get_json("/documents.json", {"date": _to_iso_date(date_str), "type": "2"}, api_key)
    results = resp.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        raise RuntimeError(
            f"[{date_str}] APIレスポンスの'results'がlistでない（型={type(results).__name__}）"
        )
    tob_filtered_count = 0
    null_description_count = 0
    parse_failures = 0
    for r in results:
        if not isinstance(r, dict):
            parse_failures += 1
            continue
        doc_description = r.get("docDescription")
        if doc_description is None:
            null_description_count += 1
            continue
        matched = _matches_tob_keywords(doc_description)
        if matched is None:
            # docDescriptionがNoneではないのにstrでもない = 想定外の型 = 真の異常
            parse_failures += 1
        elif matched:
            tob_filtered_count += 1
    return results, tob_filtered_count, null_description_count, parse_failures


def _needs_catchup(path: Path) -> bool:
    """documents_all の欠損日キャッチアップ対象判定。

    ファイルが無い、または既存ファイルが壊れて読めない、または既存ファイルの
    parse_failures>0（＝partial保存だった）場合は再取得対象とする（2026-07-16 Codexレビュー
    4巡目指摘: parse_failures>0の日もファイル自体は保存されるため、直近5営業日窓の外に
    出るとファイル存在チェックだけでは「既存」として永久スキップされ、partial状態が
    固定化してしまう穴があった）。
    """
    if not path.exists():
        return True
    try:
        obj = jq_fetch.read_json_gz(path)
    except Exception:
        return True  # 壊れたファイルも再取得対象にする（自己修復）
    if not isinstance(obj, dict):
        return True  # gzip/JSONは読めるがトップレベルがdictでない異常ファイルも再取得（Codex GO時MINOR対応）
    return (obj.get("parse_failures") or 0) > 0


def fetch_documents_all_for_date(date_str: str, api_key: Optional[str], run_id: str) -> dict:
    """指定営業日(YYYYMMDD)の書類一覧を **全件・無加工** で取得し
    data/edinet/documents_all/{date}.json.gz に保存する（既存かつparse_failures=0ならスキップ
    ＝ネットワーク呼出なし。既存でもpartial(parse_failures>0)だった日は再取得する）。

    直近5営業日より前の欠損日キャッチアップ専用（直近5営業日は
    fetch_documents_all_for_date_forced() を使うこと）。この関数が対象とする「直近5営業日
    より前」の日は縦覧期間中の書類が実務上ほぼ確定済みのため、正常保存済み（parse_failures=0）
    ファイルは無条件で信頼してスキップしてよい。

    Returns:
        {"status": "skipped_exists"|"saved", "total_count": Optional[int],
         "filtered_count": Optional[int], "null_description_count": Optional[int],
         "parse_failures": Optional[int], "file": Optional[str], "rev_file": None}
    """
    path = ALL_DOCS_DATA_ROOT / f"{date_str}.json.gz"
    if not _needs_catchup(path):
        return {
            "status": "skipped_exists", "total_count": None, "filtered_count": None,
            "null_description_count": None, "parse_failures": None, "file": None, "rev_file": None,
        }

    results, tob_filtered_count, null_description_count, parse_failures = _fetch_all_documents(date_str, api_key)
    out_obj = {
        "date": date_str,
        "total_count": len(results),
        "tob_filtered_count": tob_filtered_count,
        "null_description_count": null_description_count,
        "parse_failures": parse_failures,
        "results": results,
    }
    jq_fetch.write_json_gz(path, out_obj)  # Canonical Module: アトミック gzip 保存を再利用
    return {
        "status": "saved",
        "total_count": len(results),
        "filtered_count": tob_filtered_count,
        "null_description_count": null_description_count,
        "parse_failures": parse_failures,
        "file": str(path.relative_to(PROJECT_ROOT)),
        "rev_file": None,
    }


def fetch_documents_all_for_date_forced(date_str: str, api_key: Optional[str], run_id: str) -> dict:
    """直近営業日用: 既存ファイルの有無に関わらず必ずAPIを叩き直し、内容(docID集合+ハッシュ)が
    旧ファイルと変化していれば旧ファイルを `<date>.rev<連番>.json.gz` として退避してから
    新版を保存する（上書きで消さない）。変化がなければ何もしない（rev ファイルは増えない）。

    18:45の日次実行時点では未提出の書類（当日夜間〜翌営業日提出分の反映遅延、後日の訂正・
    撤回提出等）が後から追加される場合があり、ファイル存在チェックだけでのスキップは
    永久欠損の穴になる（2026-07-16 Codexレビュー指摘）。直近5営業日だけ強制再取得することで
    この穴を塞ぐ。

    Returns:
        {"status": "saved"|"updated"|"unchanged", "total_count": int,
         "filtered_count": int, "null_description_count": int, "parse_failures": int,
         "file": Optional[str]（"unchanged"時はNone）, "rev_file": Optional[str]}
    """
    path = ALL_DOCS_DATA_ROOT / f"{date_str}.json.gz"
    results, tob_filtered_count, null_description_count, parse_failures = _fetch_all_documents(date_str, api_key)
    new_ids, new_hash = _content_signature(results)

    rev_file = None
    if path.exists():
        old_obj = jq_fetch.read_json_gz(path)
        old_ids, old_hash = _content_signature(old_obj.get("results", []))
        if old_ids == new_ids and old_hash == new_hash:
            return {
                "status": "unchanged", "total_count": len(results), "filtered_count": tob_filtered_count,
                "null_description_count": null_description_count, "parse_failures": parse_failures,
                "file": None, "rev_file": None,
            }
        rev_n = 1
        while (path.with_name(f"{date_str}.rev{rev_n}.json.gz")).exists():
            rev_n += 1
        rev_path = path.with_name(f"{date_str}.rev{rev_n}.json.gz")
        path.replace(rev_path)  # 旧版を退避（上書きで消さない）
        rev_file = str(rev_path.relative_to(PROJECT_ROOT))
        status = "updated"
    else:
        status = "saved"

    out_obj = {
        "date": date_str,
        "total_count": len(results),
        "tob_filtered_count": tob_filtered_count,
        "null_description_count": null_description_count,
        "parse_failures": parse_failures,
        "results": results,
    }
    jq_fetch.write_json_gz(path, out_obj)  # Canonical Module: アトミック gzip 保存を再利用（新版はtmp+os.replaceで確定）
    return {
        "status": status,
        "total_count": len(results),
        "filtered_count": tob_filtered_count,
        "null_description_count": null_description_count,
        "parse_failures": parse_failures,
        "file": str(path.relative_to(PROJECT_ROOT)),
        "rev_file": rev_file,
    }


# 書類本体取得(/api/v2/documents/{docID})の type パラメータ（EDINET公式仕様書準拠の想定値。
# 実キー未入手のため未検証）。1=XBRL, 2=PDF, 3=代替書面等, 4=英文, 5=CSV。
BODY_TYPE_EXTENSIONS = {"1": "xbrl.zip", "2": "pdf", "3": "zip", "4": "zip", "5": "csv.zip"}
DOCUMENT_BODIES_DIR = DATA_ROOT / "bodies"


def fetch_document_body(doc_id: str, body_type: str, api_key: Optional[str]) -> Path:
    """書類本体（ZIP/PDF）を1件だけ生バイト列のまま保存する（中身のCSV/XBRLは解析しない）。

    カタログ§2-E #11の実装ノート「対象発行会社・提出者・増減方向・重要提案行為等の判定に
    リスト一覧メタデータだけでは不十分な場合、書類本体の追加取得が必要」を受けた準備関数。
    **意図的にここでは中身を解析しない**（実サンプルが1件も無い状態でCSV/XBRLのカラム名を
    推測実装すると、サイレントに誤ったデータを生成するリスクが高いため。scripts/
    kpi_activist_signals.py の実装ノート参照）。まず本関数で実サンプルを1件取得し、
    人間の目 or 次回のビルダーが中身を確認してから、専用のパーサーを別途実装すること。

    Args:
        doc_id: EDINET docID（例: "S100XXXX"）。
        body_type: "1"(XBRL)/"2"(PDF)/"5"(CSV) 等。EDINET仕様書準拠の想定値（未検証）。
        api_key: get_api_key() の戻り値。

    Returns:
        保存先パス（data/edinet/bodies/{doc_id}_{body_type}.{ext}）。

    Raises:
        AuthError: 認証エラー。
        RuntimeError: その他のHTTPエラー。
    """
    query = _inject_api_key({"type": body_type}, api_key)
    url = f"{BASE_URL}/documents/{doc_id}?{urllib.parse.urlencode(query)}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read()
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}: {err_body.decode('utf-8', errors='replace')}") from None
        raise RuntimeError(f"HTTP {e.code}: {err_body.decode('utf-8', errors='replace')}")

    # EDINET特有の「HTTP200+JSON body内でエラーを表現する」形に対応（ZIP/PDFはJSONではないため
    # 通常は該当しないが、エラー時はJSONメッセージが返ってくることがあるため検査する）。
    # 2026-07-07 実疎通で判明: 認証エラーは {"StatusCode":401,...}（トップレベル）だが、
    # 縦覧期間経過等で書類が既に存在しない場合は {"metadata":{"status":"404","message":"Not Found"}}
    # （documents.json と同じ metadata.status 形）で返る。両形式とも検査しないと、
    # このエラーJSONをそのまま「本体ファイル」として保存してしまう（実際にこのバグを1件確認・修正）。
    if body[:1] in (b"{", b"["):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "StatusCode" in parsed:
                status = parsed.get("StatusCode")
                msg = parsed.get("message", "")
                if status in (401, 403):
                    raise AuthError(f"EDINET StatusCode {status}: {msg}")
                raise RuntimeError(f"EDINET StatusCode {status}: {msg}")
            metadata = parsed.get("metadata") if isinstance(parsed, dict) else None
            if metadata is not None and str(metadata.get("status", "200")) != "200":
                raise RuntimeError(
                    f"EDINET metadata.status={metadata.get('status')}: {metadata.get('message', '')}"
                    f"（縦覧期間経過等で書類本体が既に取得不能な可能性）"
                )
        except json.JSONDecodeError:
            pass  # ZIP/PDFの生バイト列がたまたま '{'/'[' で始まっただけ（通常はJSONではない）

    ext = BODY_TYPE_EXTENSIONS.get(body_type, "bin")
    out_path = DOCUMENT_BODIES_DIR / f"{doc_id}_{body_type}.{ext}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_bytes(body)
    tmp_path.replace(out_path)
    return out_path


def fetch_edinet_code_master(force: bool = False) -> Path:
    """EDINETコード→証券コード変換用の公式マスタ(EdinetcodeDlInfo.csv)をダウンロードする。

    APIキー不要の静的ZIP配布（2026-07-07 実疎通確認済み）。ZIP内のCSVはShift_JISのため
    UTF-8に再エンコードして保存する（先頭のダウンロード実行日行はそのまま維持）。

    大量保有報告書のリストAPIは secCode が null のことが多く（提出者=株主自身は非上場が
    通常のため）、対象会社は issuerEdinetCode で示される。本マスタでEDINETコード->証券コード
    へ変換する（scripts/kpi_activist_signals.py の load_edinet_code_master() が読み込む）。

    Args:
        force: True の場合、既存ファイルがあっても再ダウンロードする（マスタは日次更新のため）。

    Returns:
        保存先パス（data/edinet/edinet_code_master.csv）。
    """
    if EDINET_CODE_MASTER_PATH.exists() and not force:
        return EDINET_CODE_MASTER_PATH

    req = urllib.request.Request(EDINET_CODE_MASTER_URL)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        zip_bytes = resp.read()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"EDINETコードマスタZIPにCSVが見つかりません: {zf.namelist()}")
        raw = zf.read(names[0])

    # cp932(Windows-31J)を使用（strict shift_jisでは一部の拡張文字がデコード不能なため。
    # 実データでデコードエラーを確認し修正済み・2026-07-07）。
    text = raw.decode("cp932")
    EDINET_CODE_MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = EDINET_CODE_MASTER_PATH.with_name(EDINET_CODE_MASTER_PATH.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(EDINET_CODE_MASTER_PATH)
    return EDINET_CODE_MASTER_PATH


def inspect_cached_date(date_str: str, dataset: str = "holdings") -> None:
    """既に取得済みの1営業日分のキャッシュを人間向けに要約表示する（読み取り専用・API未呼出）。

    実キー入手後、初回取得分の docTypeCode 分布・filerName/secCode の実際の表記を
    目視確認するための準備コマンド（team lead指示「実レスポンスでフィールド名・コード体系を
    確認して合わせる」の実行手段）。dataset="documents_all" 指定時は data/edinet/documents_all/
    を参照する。
    """
    data_root = ALL_DOCS_DATA_ROOT if dataset == "documents_all" else DATA_ROOT
    path = data_root / f"{date_str}.json.gz"
    if not path.exists():
        raise SystemExit(f"FATAL: {path} が見つかりません。先にこの日付を取得してください。")
    obj = jq_fetch.read_json_gz(path)
    results = obj.get("results", [])
    print(f"=== {date_str} [{dataset}] キャッシュ内容 ===")
    if dataset == "documents_all":
        print(
            f"total_count(取得時点)={obj.get('total_count')} "
            f"tob_filtered_count={obj.get('tob_filtered_count')} "
            f"null_description_count={obj.get('null_description_count')}（EDINETの正常状態） "
            f"parse_failures={obj.get('parse_failures')}（真の異常のみ）"
        )
    else:
        print(f"total_count(取得時点)={obj.get('total_count')} filtered_count(保存分)={obj.get('filtered_count')}")
    from collections import Counter
    type_counts = Counter(str(r.get("docTypeCode")) for r in results)
    print(f"docTypeCode分布: {dict(type_counts)}")
    print(f"サンプル最大5件:")
    for r in results[:5]:
        print(
            f"  docID={r.get('docID')} docTypeCode={r.get('docTypeCode')} "
            f"filerName={r.get('filerName')!r} secCode={r.get('secCode')!r} "
            f"docDescription={r.get('docDescription')!r} submitDateTime={r.get('submitDateTime')!r}"
        )


def run_probe(
    api_key: Optional[str], run_id: str, dataset: str = "holdings", sample_date: Optional[str] = None
) -> int:
    """1日分だけ実際に叩いて疎通確認する（保存はしない）。キー有無どちらでも使える。

    dataset="documents_all" の場合、TOBキーワード該当は大量保有と異なり出現頻度が低く
    固定サンプル日だと0件になりやすいため、sample_date未指定時は直近の営業日を自動選択する。

    Returns:
        0: 正常応答（keyが有効） / 2: 認証エラー（キー未設定 or 無効） / 1: その他のエラー
    """
    if sample_date is None:
        if dataset == "documents_all":
            calendar_days = jq_fetch.load_calendar_days(api_key=None, run_id=None)  # type: ignore[arg-type]
            today_str = jq_fetch.now_jst().strftime("%Y%m%d")
            recent = jq_fetch.business_days_in_range(calendar_days, ALL_DOCS_DEFAULT_START, today_str)
            sample_date = recent[-1] if recent else PROBE_SAMPLE_DATE
        else:
            sample_date = PROBE_SAMPLE_DATE

    kind = "probe" if dataset == "holdings" else f"probe_{dataset}"
    print(f"[probe:{dataset}] date={sample_date} (iso={_to_iso_date(sample_date)}) にプローブ実行中...")
    try:
        if dataset == "documents_all":
            # 本処理と同じ経路を再利用（非list/非dict耐性を本処理と統一・Codex GO時MINOR対応）
            results, _tob_count, _null_desc_count, _parse_failures = _fetch_all_documents(sample_date, api_key)
            filtered = [r for r in results if isinstance(r, dict) and _matches_tob_keywords(r.get("docDescription"))]
            label = "docDescriptionにTOBキーワード該当（保存は全件・このフィルタは集計専用）"
        else:
            resp = edinet_get_json("/documents.json", {"date": _to_iso_date(sample_date), "type": "2"}, api_key)
            results = resp.get("results") or []
            filtered = [r for r in results if str(r.get("docTypeCode")) in TARGET_DOC_TYPE_CODES]
            label = "docTypeCode 350,360 該当"
        print(f"[probe:{dataset}] 成功: 全{len(results)}件 / {label} {len(filtered)}件")
        if filtered:
            print(f"[probe:{dataset}] サンプルレコード(1件目): {json.dumps(filtered[0], ensure_ascii=False)[:500]}")
        append_log({
            "run_id": run_id, "ts": jq_fetch.now_jst().isoformat(), "kind": kind, "date": sample_date,
            "status": "probe_ok", "count": len(results), "file": None, "error": None,
        })
        return 0
    except AuthError as e:
        print(f"[probe:{dataset}] 認証エラー: {e}", file=sys.stderr)
        append_log({
            "run_id": run_id, "ts": jq_fetch.now_jst().isoformat(), "kind": kind, "date": sample_date,
            "status": "probe_auth_error", "count": None, "file": None, "error": str(e),
        })
        return 2
    except RuntimeError as e:
        print(f"[probe:{dataset}] エラー: {e}", file=sys.stderr)
        append_log({
            "run_id": run_id, "ts": jq_fetch.now_jst().isoformat(), "kind": kind, "date": sample_date,
            "status": "probe_error", "count": None, "file": None, "error": str(e),
        })
        return 1


_CANONICAL_DATE_FILE_RE = re.compile(r"^\d{8}$")


def print_status(start: str, end: str, dataset: str = "holdings") -> None:
    """期待件数（期間内営業日数） vs 取得済み件数を表示する（読み取り専用・API未呼出）。

    documents_all データセットの `<date>.revN.json.gz` 退避ファイルはカウント対象外
    （正本は常に `<date>.json.gz` の1本のみ。revファイルはあくまで変更履歴のアーカイブ）。
    """
    data_root = ALL_DOCS_DATA_ROOT if dataset == "documents_all" else DATA_ROOT
    calendar_days = jq_fetch.load_calendar_days(api_key=None, run_id=None)  # type: ignore[arg-type]
    expected = jq_fetch.business_days_in_range(calendar_days, start, end)
    actual = (
        sum(
            1 for p in data_root.glob("*.json.gz")
            if _CANONICAL_DATE_FILE_RE.match(p.name.removesuffix(".json.gz"))
            and start <= p.name.removesuffix(".json.gz") <= end
        )
        if data_root.exists()
        else 0
    )
    print(f"=== EDINET[{dataset}] キャッシュ状態（{start} 〜 {end}） ===")
    print(f"documents: {actual}/{len(expected)} 件（期間内期待値・営業日ベース）")


def main() -> int:
    parser = argparse.ArgumentParser(description="EDINET API v2 メタデータフェッチャー（大量保有報告書 / documents_all）")
    parser.add_argument(
        "--dataset", choices=["holdings", "documents_all"], default="holdings",
        help="取得データセット（既定 holdings=大量保有報告書・変更報告書＝従来の挙動。"
             "documents_all=当日の書類一覧を全件・無加工保存＝カタログ§8 BL-1用・"
             "data/edinet/documents_all/ に別保存）",
    )
    parser.add_argument(
        "--start", default=None,
        help=f"取得開始日 YYYYMMDD（既定は dataset により異なる。"
             f"holdings={DEFAULT_START} / documents_all={ALL_DOCS_DEFAULT_START}）",
    )
    parser.add_argument(
        "--end", default=None,
        help=f"取得終了日 YYYYMMDD（既定は dataset により異なる。holdings={DEFAULT_END} / documents_all=当日・動的）",
    )
    parser.add_argument("--status", action="store_true", help="期待件数 vs 取得済み件数を表示して終了")
    parser.add_argument("--probe", action="store_true", help="1日分だけ疎通確認して終了（保存しない）")
    parser.add_argument(
        "--inspect", metavar="YYYYMMDD",
        help="既に取得済みの1営業日分の内容（docTypeCode分布・サンプルfilerName/secCode）を表示して終了"
             "（実キー入手後、初回取得分の実データ確認用。--datasetで参照先を切替）",
    )
    parser.add_argument(
        "--fetch-body", metavar="DOC_ID",
        help="指定docIDの書類本体を1件だけ生バイト列で data/edinet/bodies/ に保存して終了"
             "（中身の解析はしない・--body-typeと併用。CSVスキーマ調査の準備用）",
    )
    parser.add_argument("--body-type", default="5", help="--fetch-body用のtype（既定5=CSV。1=XBRL,2=PDF）")
    parser.add_argument(
        "--fetch-code-master", action="store_true",
        help="EDINETコード→証券コード変換マスタ(EdinetcodeDlInfo.csv)を取得して終了"
             "（APIキー不要・静的ファイル。data/edinet/edinet_code_master.csv に保存）",
    )
    parser.add_argument("--force", action="store_true", help="--fetch-code-master併用時、既存でも再取得する")
    args = parser.parse_args()

    # --start/--end の実効値を dataset ごとに決定する。holdings は従来の argparse 既定値と
    # 完全に同一（DEFAULT_START/DEFAULT_END）なので既存呼び出しの挙動は一切変わらない。
    # documents_all は --end 省略時に当日(JST)まで動的に伸びる（launchd日次ジョブでの毎日catch-up用）。
    if args.dataset == "documents_all":
        start = args.start or ALL_DOCS_DEFAULT_START
        end = args.end or jq_fetch.now_jst().strftime("%Y%m%d")
    else:
        start = args.start or DEFAULT_START
        end = args.end or DEFAULT_END

    if args.status:
        print_status(start, end, dataset=args.dataset)
        return 0

    if args.fetch_code_master:
        try:
            out_path = fetch_edinet_code_master(force=args.force)
            print(f"[fetch-code-master] 保存完了: {out_path}")
            return 0
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            print(f"FATAL: EDINETコードマスタ取得失敗: {e}", file=sys.stderr)
            return 1

    if args.inspect:
        inspect_cached_date(args.inspect, dataset=args.dataset)
        return 0

    run_id = uuid.uuid4().hex
    api_key = get_api_key()

    if args.fetch_body:
        if api_key is None:
            print("FATAL: --fetch-body には EDINET_API_KEY が必要です", file=sys.stderr)
            return 2
        try:
            out_path = fetch_document_body(args.fetch_body, args.body_type, api_key)
            print(f"[fetch-body] 保存完了: {out_path}（中身は未解析。手動で展開して確認してください）")
            return 0
        except AuthError as e:
            print(f"FATAL: 認証エラー: {e}", file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(f"FATAL: 取得失敗: {e}", file=sys.stderr)
            return 1

    if args.probe:
        return run_probe(api_key, run_id, dataset=args.dataset)

    if api_key is None:
        print(
            "WARN: EDINET_API_KEY が環境変数にも ~/.zshrc にも見つかりません。"
            "ユーザーが登録中の可能性があるため、キー無しで1回プローブして実エラーを記録します。",
            file=sys.stderr,
        )
        probe_rc = run_probe(None, run_id, dataset=args.dataset)
        print(
            "FATAL: EDINET_API_KEY 未設定のため取得を開始できません。"
            "取得後 `export EDINET_API_KEY=\"...\"` を ~/.zshrc に追記して再実行してください"
            "（本スクリプトはキー設定後、追加の変更なしにそのまま動作する設計です）。",
            file=sys.stderr,
        )
        return 2 if probe_rc == 2 else probe_rc or 2

    append_log({
        "run_id": run_id, "ts": jq_fetch.now_jst().isoformat(), "kind": None, "date": None,
        "status": "run_start", "count": None, "file": None, "error": None, "dataset": args.dataset,
    })

    calendar_days = jq_fetch.load_calendar_days(api_key=None, run_id=None)  # type: ignore[arg-type]
    dates = jq_fetch.business_days_in_range(calendar_days, start, end)

    if args.dataset == "documents_all":
        # 直近RECENT_FORCE_REFETCH_DAYS営業日（実行時点の実日付基準。--start/--endの指定範囲とは
        # 独立に「本当の今日」から見て直近か」で決める）は毎回強制再取得、それより前は欠損日のみ
        # キャッチアップする（5年分を毎日フルスキャンしてAPIを叩き直すことはしない。2026-07-16
        # Codexレビュー指摘）。
        true_today = jq_fetch.now_jst().strftime("%Y%m%d")
        recent_all = jq_fetch.business_days_in_range(calendar_days, ALL_DOCS_DEFAULT_START, true_today)
        recent_window = set(recent_all[-RECENT_FORCE_REFETCH_DAYS:])
        recent_dates = [d for d in dates if d in recent_window]
        older_dates = [d for d in dates if d not in recent_window]
        missing_older = [d for d in older_dates if _needs_catchup(ALL_DOCS_DATA_ROOT / f"{d}.json.gz")]
        skipped_older = len(older_dates) - len(missing_older)
        work_dates = recent_dates + missing_older
        print(
            f"[edinet:documents_all] 対象営業日 {len(dates)} 件（{start} 〜 {end}）: "
            f"直近{len(recent_dates)}件は強制再取得、それ以前は欠損{len(missing_older)}件のみ取得"
            f"（既存{skipped_older}件はスキップ・ネットワーク呼出/ログ追記なし）"
        )
    else:
        work_dates = dates
        print(f"[edinet:{args.dataset}] 対象営業日 {len(dates)} 件（{start} 〜 {end}）")
        recent_window = set()

    kind_label = "documents" if args.dataset == "holdings" else "documents_all"
    total = len(work_dates)
    for idx, date_str in enumerate(work_dates, start=1):
        ts = jq_fetch.now_jst().isoformat()
        try:
            if args.dataset == "documents_all":
                if date_str in recent_window:
                    result = fetch_documents_all_for_date_forced(date_str, api_key, run_id)
                else:
                    result = fetch_documents_all_for_date(date_str, api_key, run_id)
                save_outcome = result["status"]
                parse_failures = result.get("parse_failures") or 0
                # 修正3(2026-07-16 Codexレビュー指摘・同日team lead追補で定義修正): parse_failures>0
                # （＝docDescription=None以外の真に異常なレコードが存在した日）は成功扱いに
                # 紛れ込ませず status を明示的に "partial" にする。docDescription=None自体は
                # EDINETの正常状態（5年ローリング窓境界付近）であり null_description_count に
                # 別枠計上するのみで partial 判定には使わない（実際の保存結果は save_outcome に
                # 残すため情報は失われない）。
                log_status = "partial" if parse_failures > 0 else save_outcome
                record = {
                    "run_id": run_id, "ts": ts, "kind": kind_label, "date": date_str,
                    "status": log_status, "save_outcome": save_outcome,
                    "count": result.get("filtered_count"), "total_count": result.get("total_count"),
                    "null_description_count": result.get("null_description_count"),
                    "parse_failures": result.get("parse_failures"), "rev_file": result.get("rev_file"),
                    "file": result.get("file"), "error": None,
                }
            else:
                result = fetch_documents_for_date(date_str, api_key, run_id)
                record = {
                    "run_id": run_id, "ts": ts, "kind": kind_label, "date": date_str,
                    "status": result["status"], "count": result.get("filtered_count"),
                    "file": result.get("file"), "error": None,
                }
        except AuthError as e:
            append_log({
                "run_id": run_id, "ts": ts, "kind": kind_label, "date": date_str,
                "status": "auth_error", "count": None, "file": None, "error": str(e),
            })
            print(f"FATAL: 認証エラー（APIキーが無効または失効している可能性）: {e}", file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(f"WARN: [{date_str}] 取得失敗（次回再実行で再取得されます）: {e}", file=sys.stderr)
            record = {
                "run_id": run_id, "ts": ts, "kind": kind_label, "date": date_str,
                "status": "error", "count": None, "file": None, "error": str(e),
            }
        except Exception as e:
            # 修正5(c)(2026-07-16 Codexレビュー4巡目指摘): AuthError/RuntimeError以外の想定外の
            # 例外（1日分の異常データに起因するAttributeError等）でも当該日をerror記録して
            # 次の日へ継続する。旧実装はここを捕捉しておらず、1日の異常データで残り全営業日の
            # 処理とログが失われる（クラッシュ）恐れがあった。
            print(
                f"WARN: [{date_str}] 想定外エラー（次回再実行で再取得されます）: "
                f"{type(e).__name__}: {e}", file=sys.stderr,
            )
            record = {
                "run_id": run_id, "ts": ts, "kind": kind_label, "date": date_str,
                "status": "error", "count": None, "file": None, "error": f"{type(e).__name__}: {e}",
            }
        append_log(record)
        if idx % PROGRESS_EVERY == 0 or idx == total:
            print(f"進捗: {idx}/{total} ({idx / total * 100:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
