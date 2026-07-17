#!/usr/bin/env python3
"""X syndication API で研究マスター投稿の原文・media情報を後追い補完する。

output/research/masters_20260717/tweets_*.json は X の UI 自動翻訳経由で収集されて
おり、日本語原文が英訳で保存されている・長文が省略記号なしに切断されている・画像有無が
分からない、という欠損がある。各レコードの url（https://x.com/<user>/status/<id> 形式）
から tweet_id を取り出し、X syndication API（認証不要の公開エンドポイント。
cdn.syndication.twimg.com/tweet-result）で原文・media・引用/返信情報を取得し、
norm_* フィールドとして付加する。

入力ファイルは read-only（一切変更しない）。出力は
output/research/masters_20260717/normalized/<入力と同名>.json に書く。

冪等性: 出力ファイルが既に存在する場合、tweet_id をキーに前回 norm_status=="ok" だった
レコードは再取得せず流用する。失敗（not_found/error）だったレコードのみ再試行する。

Python標準ライブラリのみで動作する（pip install不要・Docker不要）。ホストで直接実行する。

Usage:
    python3 scripts/normalize_master_posts.py
    python3 scripts/normalize_master_posts.py --dry-run
    python3 scripts/normalize_master_posts.py --files "output/research/masters_20260717/tweets_tomoyaasakura__*.json" --limit 5
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
MASTERS_DIR = PROJECT_ROOT / "output" / "research" / "masters_20260717"
DEFAULT_INPUT_GLOB = str(MASTERS_DIR / "tweets_*.json")
OUTPUT_DIR = MASTERS_DIR / "normalized"
COMPLETENESS_PATH = OUTPUT_DIR / "_completeness.json"

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=ja&token=a"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
REQUEST_INTERVAL_SECONDS = 0.8
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 2
TIMEOUT_SECONDS = 15

TWEET_ID_RE = re.compile(r"/status/(\d+)")
JAPANESE_CHAR_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")

COMPLETENESS_FIELDS = ("total", "ok", "not_found", "error", "translated_count", "truncated_count")


def extract_tweet_id(url: str) -> Optional[str]:
    """url（x.com/twitter.com どちらの表記も可）から tweet_id を取り出す。"""
    if not url:
        return None
    m = TWEET_ID_RE.search(url)
    return m.group(1) if m else None


def has_japanese(text: str) -> bool:
    """テキストに日本語文字（ひらがな/カタカナ/漢字）が含まれるか。"""
    return bool(JAPANESE_CHAR_RE.search(text or ""))


def fetch_syndication(tweet_id: str) -> tuple[str, Optional[dict[str, Any]]]:
    """syndication API から1件取得する。

    Returns:
        (status, payload) のタプル。status は "ok" | "not_found" | "error:<要約>"。
        404（削除済み・非公開・存在しない tweet_id）は not_found として扱い、
        429/5xx は指数バックオフで最大 MAX_RETRIES 回まで再試行する。
    """
    url = SYNDICATION_URL.format(tweet_id=tweet_id)
    last_error: Optional[str] = None
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                body = resp.read()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as e:
                return f"error:json_decode:{e}", None
            return "ok", payload
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "not_found", None
            last_error = f"http_{e.code}"
            if e.code == 429 or e.code >= 500:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2**attempt))
                    continue
            return f"error:{last_error}", None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_error = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            return f"error:{last_error}", None
    return f"error:{last_error}", None


def build_norm_fields(
    original_record: dict[str, Any], status: str, payload: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """1レコード分の norm_* フィールドを組み立てる。"""
    fields: dict[str, Any] = {
        "norm_text": None,
        "norm_lang": None,
        "norm_media_count": 0,
        "norm_media_types": [],
        "norm_has_quote": False,
        "norm_reply_to": None,
        "norm_created_at": None,
        "norm_status": status,
        "norm_fetched_at": datetime.now(timezone.utc).isoformat(),
        "text_was_translated": False,
        "text_was_truncated": False,
    }
    if status != "ok" or not payload:
        return fields

    norm_text = payload.get("text")
    media_details = payload.get("mediaDetails") or []

    reply_to: Optional[str] = None
    parent = payload.get("parent")
    if isinstance(parent, dict):
        reply_to = (parent.get("user") or {}).get("screen_name")
    if not reply_to:
        reply_to = payload.get("in_reply_to_screen_name")

    fields.update(
        {
            "norm_text": norm_text,
            "norm_lang": payload.get("lang"),
            "norm_media_count": len(media_details),
            "norm_media_types": [m.get("type") for m in media_details if m.get("type")],
            "norm_has_quote": bool(payload.get("quoted_tweet")),
            "norm_reply_to": reply_to,
            "norm_created_at": payload.get("created_at"),
        }
    )

    if norm_text:
        original_text = original_record.get("text") or ""
        fields["text_was_translated"] = (not has_japanese(original_text)) and has_japanese(norm_text)
        fields["text_was_truncated"] = len(norm_text) > len(original_text) + 10

    return fields


def load_existing_output(output_path: Path) -> dict[str, dict[str, Any]]:
    """既存の出力ファイルを tweet_id をキーに読み込む（冪等性の照合用）。"""
    if not output_path.exists():
        return {}
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for rec in data:
        tweet_id = extract_tweet_id(rec.get("url", ""))
        if tweet_id:
            result[tweet_id] = rec
    return result


def compute_file_stats(output_records: list[dict[str, Any]]) -> dict[str, int]:
    """完全性台帳に書く1ファイル分の集計を算出する。"""
    stats = {k: 0 for k in COMPLETENESS_FIELDS}
    stats["total"] = len(output_records)
    for rec in output_records:
        status = rec.get("norm_status")
        if status == "ok":
            stats["ok"] += 1
        elif status == "not_found":
            stats["not_found"] += 1
        elif isinstance(status, str) and status.startswith("error"):
            stats["error"] += 1
        if rec.get("text_was_translated"):
            stats["translated_count"] += 1
        if rec.get("text_was_truncated"):
            stats["truncated_count"] += 1
    return stats


def update_completeness(comp_path: Path, file_key: str, file_stats: dict[str, int]) -> None:
    """完全性台帳（_completeness.json）にファイル別エントリをマージ更新する。"""
    comp: dict[str, Any] = {"files": {}, "summary": {}, "updated_at": None}
    if comp_path.exists():
        try:
            comp = json.loads(comp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    comp.setdefault("files", {})[file_key] = file_stats

    totals = {k: 0 for k in COMPLETENESS_FIELDS}
    for entry in comp["files"].values():
        for k in COMPLETENESS_FIELDS:
            totals[k] += entry.get(k, 0)
    comp["summary"] = totals
    comp["updated_at"] = datetime.now(timezone.utc).isoformat()

    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comp_path.write_text(json.dumps(comp, ensure_ascii=False, indent=2), encoding="utf-8")


def process_file(
    input_path: Path, output_dir: Path, limit: Optional[int], dry_run: bool
) -> dict[str, Any]:
    """1入力ファイルを処理する。dry_run 時は出力・台帳を書かず件数のみ返す。

    limit 指定時は先頭 limit 件のみを「今回の処理対象」とする（テスト用）。対象外の
    レコードは、既存出力があればそれをそのまま引き継ぎ、無ければ norm_* を付けずに
    元レコードのまま出力する（次回の非limit実行で自然に処理対象になる）。
    """
    with open(input_path, encoding="utf-8") as f:
        records: list[dict[str, Any]] = json.load(f)

    output_path = output_dir / input_path.name
    existing = load_existing_output(output_path)
    active_ids = {id(r) for r in (records[:limit] if limit is not None else records)}

    run_counts = {"fetched": 0, "reused_ok": 0, "would_fetch": 0, "no_tweet_id": 0}
    output_records: list[dict[str, Any]] = []

    for record in records:
        tweet_id = extract_tweet_id(record.get("url", ""))
        prior = existing.get(tweet_id) if tweet_id else None

        if id(record) not in active_ids:
            output_records.append(prior if prior is not None else record)
            continue

        if prior is not None and prior.get("norm_status") == "ok":
            run_counts["reused_ok"] += 1
            output_records.append(prior)
            continue

        if tweet_id is None:
            run_counts["no_tweet_id"] += 1
            merged = {**record, **build_norm_fields(record, "error:no_tweet_id", None)}
            output_records.append(merged)
            continue

        if dry_run:
            run_counts["would_fetch"] += 1
            output_records.append(prior if prior is not None else record)
            continue

        status, payload = fetch_syndication(tweet_id)
        merged = {**record, **build_norm_fields(record, status, payload)}
        output_records.append(merged)
        run_counts["fetched"] += 1
        time.sleep(REQUEST_INTERVAL_SECONDS)

    file_stats = compute_file_stats(output_records)

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(output_records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        update_completeness(COMPLETENESS_PATH, input_path.name, file_stats)

    return {"output_path": str(output_path), "file_stats": file_stats, "run_counts": run_counts}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="X syndication API で研究マスター投稿の原文・media情報を後追い補完する"
    )
    parser.add_argument("--files", default=DEFAULT_INPUT_GLOB, help="入力ファイルのglobパターン")
    parser.add_argument("--limit", type=int, default=None, help="テスト用: 各ファイル先頭N件のみ処理対象にする")
    parser.add_argument("--dry-run", action="store_true", help="取得せず対象件数のみ表示する")
    args = parser.parse_args()

    input_paths = sorted(Path(p) for p in glob.glob(args.files))
    if not input_paths:
        print(f"対象ファイルなし: {args.files}", file=sys.stderr)
        return 1

    grand_total = {"total": 0, "fetched": 0, "reused_ok": 0, "would_fetch": 0, "no_tweet_id": 0}

    for input_path in input_paths:
        result = process_file(input_path, OUTPUT_DIR, args.limit, args.dry_run)
        rc = result["run_counts"]
        fs = result["file_stats"]
        grand_total["total"] += fs["total"]
        for k in ("fetched", "reused_ok", "would_fetch", "no_tweet_id"):
            grand_total[k] += rc[k]

        if args.dry_run:
            print(
                f"[DRY-RUN] {input_path.name}: total={fs['total']} "
                f"reused_ok={rc['reused_ok']} would_fetch={rc['would_fetch']} "
                f"no_tweet_id={rc['no_tweet_id']}"
            )
        else:
            print(
                f"{input_path.name} -> {result['output_path']}: "
                f"total={fs['total']} ok={fs['ok']} not_found={fs['not_found']} "
                f"error={fs['error']} (fetched={rc['fetched']} reused={rc['reused_ok']}) "
                f"translated={fs['translated_count']} truncated={fs['truncated_count']}"
            )

    if args.dry_run:
        print(
            f"[DRY-RUN] 合計: total={grand_total['total']} "
            f"reused_ok={grand_total['reused_ok']} would_fetch={grand_total['would_fetch']} "
            f"no_tweet_id={grand_total['no_tweet_id']}"
        )
    else:
        print(f"完全性台帳を更新: {COMPLETENESS_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
