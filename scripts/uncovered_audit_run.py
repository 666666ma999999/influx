#!/usr/bin/env python3
"""Batch2「本当にX外か」監査の実行（層化24銘柄をX検索して存在有無と件数を記録）。

surge_uncovered_audit_sample.py が作ったワークシートを読み、各銘柄の「急騰前の窓」で
X検索（社名優先・無ければコード）を実行し、ヒット件数と本文サンプルを記録する。
分類（none / unreachable / unidentifiable / found）は本スクリプトの出力を見て後段で確定する
（自動判定はしない＝誤分類を避ける。件数0=none候補、件数>0=found/unidentifiable候補）。

実行（xstock-vnc・DISPLAY=:99 必須）:
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/uncovered_audit_run.py [--limit N]

出力: output/reverse_lookup/uncovered_audit_results.csv（追記・再開可＝既処理はスキップ）
記述分析・台帳不算入。
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector.x_collector import SafeXCollector  # noqa: E402  Canonical収集器

WORKSHEET = ROOT / "output/reverse_lookup/uncovered_audit_worksheet.csv"
RESULTS = ROOT / "output/reverse_lookup/uncovered_audit_results.csv"
PACING = 30          # 検索間の待機（アカウント保護・秒）
MAX_SCROLLS = 4      # 存在確認が目的なので浅く


def build_url(query: str, since: str, until: str) -> str:
    q = (f"{query} since:{since[:4]}-{since[4:6]}-{since[6:]} "
         f"until:{until[:4]}-{until[4:6]}-{until[6:]}")
    return f"https://x.com/search?q={quote(q)}&src=typed_query&f=live"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ（0=全件）")
    ap.add_argument("--profile", default="x_profiles/maaaki")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(WORKSHEET, encoding="utf-8-sig")))
    if a.limit:
        rows = rows[:a.limit]

    done = set()
    if RESULTS.exists():
        for r in csv.DictReader(open(RESULTS, encoding="utf-8")):
            done.add(r["code4"])

    new = not RESULTS.exists()
    out = open(RESULTS, "a", newline="", encoding="utf-8")
    w = csv.writer(out)
    if new:
        w.writerow(["code4", "銘柄名", "検索語", "窓from", "窓to", "status",
                    "hit件数", "サンプル本文1", "サンプル本文2", "サンプル本文3"])
        out.flush()

    collector = SafeXCollector(profile_path=a.profile)
    for i, r in enumerate(rows, 1):
        code4, name = r["code4"], r["銘柄名"]
        if code4 in done:
            print(f"[{i}/{len(rows)}] skip {code4}（既処理）")
            continue
        query = name if name and name != "?" else code4
        url = build_url(query, r["検索窓_from"], r["検索窓_to"])
        print(f"[{i}/{len(rows)}] {code4} {query} …")
        try:
            res = collector.collect(url, max_scrolls=MAX_SCROLLS,
                                    group_name=f"audit:{code4}", stop_after_empty=2)
            texts = [(t.get("text") or "").replace("\n", " ")[:120] for t in res.tweets[:3]]
            texts += [""] * (3 - len(texts))
            w.writerow([code4, name, query, r["検索窓_from"], r["検索窓_to"],
                        res.status, len(res.tweets), *texts])
        except Exception as e:  # 収集失敗も記録（unreachable候補）
            w.writerow([code4, name, query, r["検索窓_from"], r["検索窓_to"],
                        f"error:{type(e).__name__}", -1, str(e)[:120], "", ""])
        out.flush()
        time.sleep(PACING)

    out.close()
    print(f"-> {RESULTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
