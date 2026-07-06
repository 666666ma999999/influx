#!/usr/bin/env python3
"""日証金（日本証券金融）バルク CSV の日次アーカイバ。

taisyaku.jp は最新 1 営業日分のみを常に同一 URL で上書き配信するため
（過去分は一切保持されない）、日次でダウンロードして gzip 圧縮のうえ
自前アーカイブする。株式バックテスト用の再生成不可データなので、
確報の差し替えを検知したら上書きせず連番サフィックスで両方残す。

対象データセット（いずれも Shift_JIS/CP932・CRLF・ダブルクォート囲み）:
    zandaka       貸借取引残高（日付列: 申込日, 例 2026/07/02）
    shina         品貸料率（逆日歩）（日付列: 貸借申込日, 例 20260702）
    meigara       貸借取引対象銘柄（日付列: 貸借申込日, 例 20260706）
    seigenichiran 制限措置一覧（日付列なし。現行措置のスナップショット）

依存はすべて標準ライブラリ。pip install は一切不要（launchd からホスト
python3 で直接実行するため）。

Usage:
    python3 scripts/jsf_daily_archive.py                     # 全データセット取得
    python3 scripts/jsf_daily_archive.py --datasets zandaka shina
    python3 scripts/jsf_daily_archive.py --status             # 直近実行サマリー表示
"""
from __future__ import annotations

import argparse
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
DATA_ROOT = PROJECT_ROOT / "data" / "jsf"
LOG_PATH = DATA_ROOT / "archive_log.jsonl"

JST = datetime.timezone(datetime.timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TIMEOUT_SECONDS = 30
MAX_RETRIES = 3  # 初回 + 3 リトライ = 最大 4 試行
RETRY_INTERVAL_SECONDS = 20
HEADER_SCAN_LIMIT = 50  # ヘッダー行探索の走査上限（安全弁）

# dataset 名 -> (URL, 日付列名 or None)
DATASETS: dict[str, dict[str, Optional[str]]] = {
    "zandaka": {
        "url": "https://www.taisyaku.jp/data/zandaka.csv",
        "date_column": "申込日",
    },
    "shina": {
        "url": "https://www.taisyaku.jp/data/shina.csv",
        "date_column": "貸借申込日",
    },
    "meigara": {
        "url": "https://www.taisyaku.jp/data/meigara.csv",
        "date_column": "貸借申込日",
    },
    "seigenichiran": {
        "url": "https://www.taisyaku.jp/data/seigenichiran.csv",
        "date_column": None,
    },
}


def now_jst() -> datetime.datetime:
    """現在時刻を JST で返す（zoneinfo 非依存、固定オフセットのみ）。"""
    return datetime.datetime.now(JST)


def fetch_with_retry(
    url: str,
    timeout: int = TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
    retry_interval: int = RETRY_INTERVAL_SECONDS,
) -> bytes:
    """指定 URL のバイト列を取得。失敗時は retry_interval 秒あけて最大 max_retries 回再試行。"""
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(retry_interval)
    raise RuntimeError(f"取得失敗（{max_retries + 1} 回試行）: {last_error}") from last_error


def normalize_date_value(value: str) -> Optional[str]:
    """'2026/07/02' や '20260702' を YYYYMMDD 文字列に正規化。解釈不能なら None。"""
    value = value.strip().strip('"')
    if value.isdigit() and len(value) == 8:
        return value
    parts = value.split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        y, mo, d = parts
        if len(y) == 4:
            return f"{int(y):04d}{int(mo):02d}{int(d):02d}"
    return None


def extract_data_date(content: bytes, date_column: str) -> tuple[Optional[str], Optional[str]]:
    """CP932 CSV からヘッダー行（date_column を含む行）を走査で探し、直後のデータ行の値を抽出。

    データセットごとにヘッダー行の位置が異なる（zandaka は1行目、shina/meigara は
    タイトル・注記行を挟んだ数行目）ため、固定行番号ではなく列名一致で探索する。

    Returns:
        (data_date, error_message) のタプル。成功時は (YYYYMMDD文字列, None)。
    """
    try:
        text = content.decode("cp932")
    except UnicodeDecodeError as e:
        return None, f"cp932 decode failed: {e}"

    reader = csv.reader(io.StringIO(text))
    date_col_idx: Optional[int] = None
    for i, row in enumerate(reader):
        if date_column in row:
            date_col_idx = row.index(date_column)
            break
        if i >= HEADER_SCAN_LIMIT:
            break
    if date_col_idx is None:
        return None, f"date column '{date_column}' not found within first {HEADER_SCAN_LIMIT} rows"

    try:
        data_row = next(reader)
    except StopIteration:
        return None, "no data row after header"
    if date_col_idx >= len(data_row):
        return None, f"date column index {date_col_idx} out of range for data row {data_row!r}"

    raw_value = data_row[date_col_idx]
    normalized = normalize_date_value(raw_value)
    if normalized is None:
        return None, f"failed to normalize date value: {raw_value!r}"
    return normalized, None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_gz_sha256(path: Path) -> str:
    """gzip ファイルを解凍し、解凍後バイト列の sha256 を返す。破損 gzip は EOFError を送出する。"""
    with gzip.open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def safe_gz_sha256(path: Path) -> Optional[str]:
    """read_gz_sha256 のフォールバック版。破損ファイル（truncated gzip 等）で例外が出ても
    処理を止めず None を返す（呼び出し側は None を「ハッシュ不一致」として扱い、新規保存に進む）。"""
    try:
        return read_gz_sha256(path)
    except (OSError, EOFError) as e:
        print(f"WARN: 既存ファイル読込失敗（破損の可能性）: {path} ({e})", file=sys.stderr)
        return None


def latest_dataset_file(dataset_dir: Path) -> Optional[Path]:
    """dataset_dir 内の最新保存ファイルを返す（ファイル名が YYYYMMDD 固定幅なので辞書順ソートで正しく時系列になる）。"""
    if not dataset_dir.exists():
        return None
    files = sorted(dataset_dir.glob("*.csv.gz"))
    return files[-1] if files else None


def write_gz(path: Path, content: bytes) -> None:
    """元バイト列をそのまま gzip 圧縮して保存（文字コード変換はしない）。

    同一ディレクトリの一時ファイルに書いてから os.replace でアトミックに配置する。
    書き込み中のクラッシュ/電源断で truncated な .csv.gz が正規パスに残ることを防ぐ
    （無人 launchd 運用で再生成不可データを扱うため、破損ファイル放置は許容できない）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    with gzip.open(tmp_path, "wb") as f:
        f.write(content)
    os.replace(tmp_path, path)


def save_dataset(dataset_dir: Path, dataset: str, date_str: str, content: bytes, new_hash: str) -> tuple[str, Optional[Path]]:
    """重複判定つきで保存。Returns: (status, saved_path)。

    1. 最新保存ファイルとハッシュ一致 → skipped_dup（週末・祝日の同一内容再配信も正常系としてここで吸収）
    2. 同名（同日付）ファイルが既に存在し、ハッシュも一致 → skipped_dup
    3. 同名ファイルが存在しハッシュが異なる（確報差し替え） → 上書きせず _rN 連番で保存
    4. 該当ファイルなし → 通常保存
    """
    dataset_dir.mkdir(parents=True, exist_ok=True)

    latest = latest_dataset_file(dataset_dir)
    if latest is not None and safe_gz_sha256(latest) == new_hash:
        return "skipped_dup", None

    target = dataset_dir / f"{dataset}_{date_str}.csv.gz"
    if not target.exists():
        write_gz(target, content)
        return "saved", target

    if safe_gz_sha256(target) == new_hash:
        return "skipped_dup", None

    rev = 2
    while True:
        candidate = dataset_dir / f"{dataset}_{date_str}_r{rev}.csv.gz"
        if not candidate.exists():
            write_gz(candidate, content)
            return "saved", candidate
        if safe_gz_sha256(candidate) == new_hash:
            return "skipped_dup", None
        rev += 1


def append_log(record: dict) -> None:
    """archive_log.jsonl に1行追記。書き込み失敗は本処理を止めず警告のみ（監視が本処理を壊さない原則）。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARN: archive_log 書き込み失敗: {e}", file=sys.stderr)


def archive_dataset(dataset: str, run_id: str) -> dict:
    """1データセットを取得・保存し、ログレコードを返す（失敗しても他データセットを止めない）。"""
    cfg = DATASETS[dataset]
    ts = now_jst().isoformat()

    try:
        content = fetch_with_retry(cfg["url"])
    except Exception as e:
        record = {
            "run_id": run_id, "ts": ts, "dataset": dataset, "status": "error",
            "data_date": None, "bytes": None, "file": None, "error": str(e),
        }
        append_log(record)
        return record

    new_hash = sha256_bytes(content)
    date_column = cfg["date_column"]
    if date_column is None:
        data_date = now_jst().strftime("%Y%m%d")
        print(f"[{dataset}] WARN: 日付列なし（設計通り・スナップショット系）。実行日 {data_date} を使用", file=sys.stderr)
    else:
        data_date, warn = extract_data_date(content, date_column)
        if data_date is None:
            data_date = now_jst().strftime("%Y%m%d")
            print(f"[{dataset}] WARN: 日付抽出失敗（{warn}）。実行日 {data_date} を使用", file=sys.stderr)

    try:
        status, saved_path = save_dataset(DATA_ROOT / dataset, dataset, data_date, content, new_hash)
    except OSError as e:
        record = {
            "run_id": run_id, "ts": ts, "dataset": dataset, "status": "error",
            "data_date": data_date, "bytes": None, "file": None, "error": str(e),
        }
        append_log(record)
        return record

    record = {
        "run_id": run_id,
        "ts": ts,
        "dataset": dataset,
        "status": status,
        "data_date": data_date,
        "bytes": len(content),
        "file": str(saved_path.relative_to(PROJECT_ROOT)) if saved_path else None,
        "error": None,
    }
    append_log(record)
    return record


def _read_log_records() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    records: list[dict] = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        print(f"WARN: archive_log 読み込み失敗: {e}", file=sys.stderr)
    return records


def print_status() -> None:
    """直近実行のサマリーと、データセットごとの保存済みファイル数を表示。"""
    records = _read_log_records()
    if not records:
        print("archive_log.jsonl が存在しないか空です（未実行）")
    else:
        last_run_id = records[-1]["run_id"]
        print(f"直近 run_id: {last_run_id}")
        for r in records:
            if r["run_id"] != last_run_id or r.get("dataset") is None:
                continue
            print(
                f"  [{r['dataset']}] {r['ts']} status={r['status']} "
                f"data_date={r['data_date']} file={r['file']} error={r['error']}"
            )

    print()
    print("保存済みファイル数:")
    for dataset in sorted(DATASETS):
        d = DATA_ROOT / dataset
        count = len(list(d.glob("*.csv.gz"))) if d.exists() else 0
        print(f"  {dataset}: {count} 件")


def main() -> int:
    parser = argparse.ArgumentParser(description="日証金 日次データアーカイバ")
    parser.add_argument(
        "--datasets", nargs="+", choices=sorted(DATASETS),
        help="取得対象データセット（省略時は全件）",
    )
    parser.add_argument("--status", action="store_true", help="直近実行サマリーを表示して終了")
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0

    targets = args.datasets or sorted(DATASETS)
    run_id = uuid.uuid4().hex
    append_log({
        "run_id": run_id, "ts": now_jst().isoformat(), "dataset": None,
        "status": "run_start", "data_date": None, "bytes": None, "file": None, "error": None,
    })

    results = []
    for dataset in targets:
        record = archive_dataset(dataset, run_id)
        results.append(record)
        print(
            f"[{dataset}] status={record['status']} data_date={record['data_date']} "
            f"file={record['file']} error={record['error']}"
        )

    if results and all(r["status"] == "error" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
