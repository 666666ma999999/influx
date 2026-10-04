#!/usr/bin/env python3
"""TDnet 開示インデックス（`data/tdnet/index/<year>/<a>_<b>.json.gz`）の読み方を 1 箇所化した部品。

2026-10-04 P-INF-29 で `driver_discover_boj.py`（発見器②）から切り出した（stdlib のみ＝ホストの素の
python3 からも読める。driver_discover_boj は monthly_sources 経由で requests を要するため、供給側レーン
`tdnet_supply_lane.py` が直接 import できなかった）。呼び手: driver_discover_boj.scan_tdnet／tdnet_supply_lane。

数え方の要点: 週次チャンクと日次チャンクが重なり同じ開示が最大 3 回出る（2026-07 以降 167 ファイル
103,930 行で同じ表題が 3 回・実測）＝ `iter_tdnet_records` が **Tdnet.id で重複除去**する。
"""
from __future__ import annotations

import datetime as dt
import glob
import gzip
import json
import os
import re
from typing import Any, Dict, Iterator, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TDNET_INDEX_DIR = os.path.join(ROOT, "data", "tdnet", "index")


def normalize_code(code: str) -> str:
    """TDnet の会社コード（5桁・末尾0）を center_pin の4桁に揃える。"""
    code = (code or "").strip()
    if len(code) == 5 and code.endswith("0"):
        return code[:-1]
    return code


def _index_files_since(index_dir: str, since: dt.date) -> List[str]:
    """`<year>/<YYYYMMDD>_<YYYYMMDD>.json.gz` のうち終了日が since 以降のファイル。"""
    out: List[str] = []
    for path in glob.glob(os.path.join(index_dir, "*", "*.json.gz")):
        m = re.search(r"(\d{8})_(\d{8})\.json\.gz$", path)
        if not m:
            continue
        end = dt.datetime.strptime(m.group(2), "%Y%m%d").date()
        if end >= since:
            out.append(path)
    return sorted(out)


def iter_tdnet_records(index_dir: str, since: dt.date) -> Iterator[Dict[str, Any]]:
    """終了日が since 以降の index ファイルを読み、Tdnet 行を **id で重複除去して** 1 回ずつ返す。

    id の無い行はそのまま返す（呼び手側の鍵で畳む）。pubdate での絞り込みは呼び手が行う。
    """
    seen_ids: set = set()
    for path in _index_files_since(index_dir, since):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        for wrapper in data.get("items", []):
            rec = wrapper.get("Tdnet") if isinstance(wrapper, dict) else None
            if not rec:
                continue
            tid = str(rec.get("id") or "")
            if tid:
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
            yield rec
