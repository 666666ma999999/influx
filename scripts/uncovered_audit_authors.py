#!/usr/bin/env python3
"""Batch2監査の「穴埋め」実装: 未捕捉急騰銘柄の事前窓に投稿していた**発信者**を抽出する。

Batch2監査（2026-07-25）で「Xに投稿は在る／主因は収集網の狭さ」が判明した。その穴埋めとして、
**全域クロールを新規構築せず**、監査で株関連投稿が確認できた銘柄の窓だけを再検索して
**投稿者アカウント**を回収し、既知57アカウントに無い**新規候補**を洗い出す（第18Rの配分方針）。

出力:
  output/reverse_lookup/uncovered_audit_authors.jsonl  … 生データ（1行=1投稿）
  output/reverse_lookup/uncovered_audit_candidates.tsv … 新規候補（複数銘柄に出た順）

注意: 出力は「事前窓に投稿していた」だけであり**収益性の証明ではない**（存在証明≠エッジ）。
候補は既存 `recollect_account.py` → `influencer_candidate_score.py` の中立採点にかけて初めて評価する。
記述分析・台帳不算入。

実行（xstock-vnc・DISPLAY=:99）:
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/uncovered_audit_authors.py
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector.x_collector import SafeXCollector  # noqa: E402

WORKSHEET = ROOT / "output/reverse_lookup/uncovered_audit_worksheet.csv"
RAW = ROOT / "output/reverse_lookup/uncovered_audit_authors.jsonl"
OUT = ROOT / "output/reverse_lookup/uncovered_audit_candidates.tsv"
KNOWN_SCORECARD = ROOT / "output/influencer_candidates/corpus_all/scorecard.json"

# Batch2監査で株関連の投稿が確認できた銘柄（A=事前コール / B=株関連言及）
TARGET_CODES = ["4586", "7746", "3498", "5838", "9509", "1662", "6269",
                "4082", "5713", "6976", "3086", "4043", "6965", "9064"]
PACING = 40
MAX_SCROLLS = 6


def known_accounts() -> set[str]:
    try:
        scs = json.load(open(KNOWN_SCORECARD))["scorecards"]
        return {s["account"].lower() for s in scs}
    except Exception:
        return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="x_profiles/maaaki")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    rows = {r["code4"]: r for r in csv.DictReader(open(WORKSHEET, encoding="utf-8-sig"))}
    targets = [rows[c] for c in TARGET_CODES if c in rows]
    if a.limit:
        targets = targets[:a.limit]

    done = set()
    if RAW.exists():
        for line in open(RAW, encoding="utf-8"):
            try:
                done.add(json.loads(line)["code4"])
            except Exception:
                pass

    collector = SafeXCollector(profile_path=a.profile)
    fout = open(RAW, "a", encoding="utf-8")
    for i, r in enumerate(targets, 1):
        code4, name = r["code4"], r["銘柄名"]
        if code4 in done:
            print(f"[{i}/{len(targets)}] skip {code4}")
            continue
        since, until = r["検索窓_from"], r["検索窓_to"]
        q = (f"{name} since:{since[:4]}-{since[4:6]}-{since[6:]} "
             f"until:{until[:4]}-{until[4:6]}-{until[6:]}")
        url = f"https://x.com/search?q={quote(q)}&src=typed_query&f=live"
        print(f"[{i}/{len(targets)}] {code4} {name} …")
        try:
            res = collector.collect(url, max_scrolls=MAX_SCROLLS,
                                    group_name=f"authors:{code4}", stop_after_empty=2)
            for t in res.tweets:
                fout.write(json.dumps({
                    "code4": code4, "name": name,
                    "username": (t.get("username") or "").lstrip("@").lower(),
                    "posted_at": t.get("posted_at"), "url": t.get("url"),
                    "text": (t.get("text") or "").replace("\n", " ")[:300],
                }, ensure_ascii=False) + "\n")
            fout.flush()
            print(f"    status={res.status} tweets={len(res.tweets)}")
        except Exception as e:
            print(f"    ERROR {type(e).__name__}: {e}")
        time.sleep(PACING)
    fout.close()

    # 集計: 新規アカウント × 何銘柄に出たか
    known = known_accounts()
    by_acc: dict[str, set] = defaultdict(set)
    texts: dict[str, str] = {}
    for line in open(RAW, encoding="utf-8"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        u = o.get("username") or ""
        if not u:
            continue
        by_acc[u].add(o["code4"])
        texts.setdefault(u, o.get("text", "")[:100])
    new = {u: c for u, c in by_acc.items() if u not in known}
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("account\tn_codes\tcodes\tsample_text\tknown\n")
        for u, codes in sorted(by_acc.items(), key=lambda x: (-len(x[1]), x[0])):
            f.write(f"{u}\t{len(codes)}\t{','.join(sorted(codes))}\t{texts.get(u,'')}\t"
                    f"{'known' if u in known else 'NEW'}\n")
    print(f"投稿者ユニーク {len(by_acc)} / 新規候補 {len(new)}")
    print(f"-> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
