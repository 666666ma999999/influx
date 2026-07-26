#!/usr/bin/env python3
"""TDnet 適時開示インデックスの全史アーカイブ収集器（無料・認証不要）。

出自: `docs/tdnet-feasibility-study.md`（2026-07-26 実現性調査）の結論に基づく。
  - 表題＋銘柄コード＋**分単位の開示時刻**は第三者API（yanoshin・非公式）で **2009-10-10〜** 取得でき、
    公式TDnet一覧の「全NNN件」と**4営業日分完全一致**を実測検証済み。
  - **TDnet本体（release.tdnet.info）へは巡回しない**（robots.txt が `User-agent: * / Disallow: /`）。
    本スクリプトは第三者APIのみを叩く（同APIの robots.txt は Allow・`llms.txt` を自ら公開）。
  - 本文PDF/XBRLは取得しない（31日超は403・有料アドオン領域）。**インデックス層のみ**。

Canonical Collector 型（`jq_fetch.py` / `edinet_fetch.py` 準拠）:
  冪等（既存週はスキップ）・raw無加工保存・receipt証跡（時刻/件数/sha256/URL）・
  レートリミット尊重（既定1.0秒）・失敗は記録して続行・アトミック書き込み。

チャンク: **週次**（実現性調査の実測で ~1,000件/週は安定・月次一括 ~8,000件は HTTP 500）。

実行例:
  python3 scripts/tdnet_index_fetch.py --since 2009-10-01 --until 2026-07-25
  python3 scripts/tdnet_index_fetch.py --recent 30          # 直近30日ぶんだけ更新
  python3 scripts/tdnet_index_fetch.py --status             # 取得済み状況

出力:
  data/tdnet/index/<YYYY>/<YYYYMMDD>_<YYYYMMDD>.json.gz    週次 raw（gitignore 想定・再取得可）
  data/tdnet/receipts.jsonl                                受領証跡（追跡対象・append-only）
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data/tdnet/index"
RECEIPTS = ROOT / "data/tdnet/receipts.jsonl"
API = "https://webapi.yanoshin.jp/webapi/tdnet/list/{since}-{until}.json?limit={limit}"
UA = "influx-research/1.0 (personal research; contact via repo owner)"
REQUEST_INTERVAL_SECONDS = 1.0
LIMIT = 3000          # 週次なら実測 ~1,000件。余裕を持たせた上限
MAX_RETRY = 3
EARLIEST = "2009-10-01"   # 実現性調査の実測（2009-10-10 が最古・2009-06 は0件）


def jst_now() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()


def append_receipt(rec: dict) -> None:
    RECEIPTS.parent.mkdir(parents=True, exist_ok=True)
    with open(RECEIPTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def fetch_json(url: str) -> tuple[dict | None, str]:
    """(payload, status)。失敗しても例外を投げず status を返す。"""
    for attempt in range(1, MAX_RETRY + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            return json.loads(body.decode("utf-8")), "ok"
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < MAX_RETRY:
                time.sleep(REQUEST_INTERVAL_SECONDS * (2 ** attempt))
                continue
            return None, f"http_{e.code}"
        except Exception as e:  # noqa: BLE001
            if attempt < MAX_RETRY:
                time.sleep(REQUEST_INTERVAL_SECONDS * (2 ** attempt))
                continue
            return None, f"error_{type(e).__name__}"
    return None, "retry_exhausted"


def week_ranges(since: dt.date, until: dt.date):
    cur = since
    while cur <= until:
        end = min(cur + dt.timedelta(days=6), until)
        yield cur, end
        cur = end + dt.timedelta(days=1)


def chunk_path(a: dt.date, b: dt.date) -> Path:
    return OUT_DIR / f"{a.year}" / f"{a:%Y%m%d}_{b:%Y%m%d}.json.gz"


def count_items(payload: dict) -> int:
    items = payload.get("items") if isinstance(payload, dict) else None
    return len(items) if isinstance(items, list) else 0


def save_atomic(path: Path, payload: dict) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def cmd_status() -> int:
    if not OUT_DIR.exists():
        print("未取得（data/tdnet/index が存在しない）")
        return 0
    files = sorted(OUT_DIR.glob("*/*.json.gz"))
    total = 0
    per_year: dict[str, int] = {}
    for p in files:
        try:
            payload = json.loads(gzip.open(p, "rb").read().decode("utf-8"))
            n = count_items(payload)
        except Exception:
            n = -1
        total += max(n, 0)
        per_year[p.parent.name] = per_year.get(p.parent.name, 0) + max(n, 0)
    print(f"=== TDnet インデックス取得状況（{OUT_DIR}）===")
    for y in sorted(per_year):
        print(f"  {y}: {per_year[y]:>7,} 件")
    print(f"--- 週次ファイル {len(files)} 個 / 合計 {total:,} 件 ---")
    if RECEIPTS.exists():
        print(f"receipts: {sum(1 for _ in open(RECEIPTS))} 行")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=EARLIEST)
    ap.add_argument("--until", default=None, help="既定=今日")
    ap.add_argument("--recent", type=int, default=0, help="直近N日ぶんだけ（--since/--until を上書き）")
    ap.add_argument("--interval", type=float, default=REQUEST_INTERVAL_SECONDS)
    ap.add_argument("--force", action="store_true", help="既存週も再取得")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        return cmd_status()

    today = dt.date.today()
    if a.recent:
        until = today
        since = today - dt.timedelta(days=a.recent)
    else:
        since = dt.date.fromisoformat(a.since)
        until = dt.date.fromisoformat(a.until) if a.until else today

    run_id = hashlib.sha256(f"{jst_now()}{since}{until}".encode()).hexdigest()[:16]
    ranges = list(week_ranges(since, until))
    print(f"[tdnet] {since} 〜 {until} / 週次チャンク {len(ranges)} 個 / interval={a.interval}s")
    append_receipt({"run_id": run_id, "ts": jst_now(), "status": "run_start",
                    "since": str(since), "until": str(until), "chunks": len(ranges)})

    saved = skipped = failed = 0
    total_items = 0
    for i, (x, y) in enumerate(ranges, 1):
        path = chunk_path(x, y)
        if path.exists() and not a.force:
            skipped += 1
            continue
        url = API.format(since=f"{x:%Y%m%d}", until=f"{y:%Y%m%d}", limit=LIMIT)
        payload, status = fetch_json(url)
        if payload is None:
            failed += 1
            append_receipt({"run_id": run_id, "ts": jst_now(), "chunk": path.name,
                            "url": url, "status": status, "items": None})
            print(f"  [{i}/{len(ranges)}] {path.name} FAILED {status}")
        else:
            n = count_items(payload)
            sha, nbytes = save_atomic(path, payload)
            saved += 1
            total_items += n
            append_receipt({"run_id": run_id, "ts": jst_now(), "chunk": path.name, "url": url,
                            "status": "saved", "items": n, "sha256": sha, "bytes": nbytes})
            if i % 50 == 0 or n == 0:
                print(f"  [{i}/{len(ranges)}] {path.name} items={n} 累計={total_items:,}")
        time.sleep(a.interval)

    append_receipt({"run_id": run_id, "ts": jst_now(), "status": "run_end",
                    "saved": saved, "skipped": skipped, "failed": failed, "items": total_items})
    print(f"[tdnet] saved={saved} skipped={skipped} failed={failed} items={total_items:,}")
    print(f"-> {OUT_DIR.relative_to(ROOT)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
