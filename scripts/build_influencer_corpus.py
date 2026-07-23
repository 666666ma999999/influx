#!/usr/bin/env python3
"""手元のX投稿コーパス3ソースを influencer_candidate_score.py の入力形式
(posts[].{account,date,text}) に統合する整形アダプタ。

目的: @kabuzoon 単体で回した「本採点」を、426急騰エピソード全体に対して
「誰が噴く前に当てたか」を洗い出すために、全アカウント横断で同じ採点器へ流す。

3ソース（influencer_leaderboard.py が採用している正本と同一）:
- output/research/signals.jsonl        : Grokリサーチ抽出（694・structured ticker + matched_text）
- output/merged_all.json               : Playwright収集445ツイート（本文あり）
- data/masters_harvest_20260717/candidates_slim.json : 名人5人の1年分（558・claim/sketch）

注意:
- 記述的採点の入力整形のみ。銘柄抽出・価格評価・急騰突合は採点器側の正本ロジックに委ねる。
- signals は ticker が構造化済みなので text 末尾に「（<4桁>）」を合成し、採点器の
  DIRECT_RE が確実に拾えるようにする（元 matched_text も社名解決用に残す）。
- 外部通信なし。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "output/research/signals.jsonl"
MERGED = ROOT / "output/merged_all.json"
MASTERS = ROOT / "data/masters_harvest_20260717/candidates_slim.json"
OUT = ROOT / "data/influencer_candidates/corpus_all.json"

TICKER_RE = re.compile(r"^\s*([0-9]{4}|[0-9]{3}[A-Z])(?:\.[A-Z]+)?\s*$", re.IGNORECASE)


def iso_to_ymd(s: str) -> str | None:
    """ISO8601 または YYYY-MM-DD を YYYYMMDD へ。失敗時 None。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
            return dt.strftime("%Y%m%d")
        except ValueError:
            continue
    # フォールバック: 先頭10文字が YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else None


def norm_account(u: str) -> str:
    return (u or "").lstrip("@").strip().lower()


def load_signals() -> list[dict]:
    posts = []
    with open(SIGNALS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            acc = norm_account(r.get("username"))
            date = iso_to_ymd(r.get("posted_at"))
            tk = r.get("ticker") or ""
            m = TICKER_RE.match(tk)
            if not (acc and date and m):
                continue
            code4 = m.group(1).upper()
            body = (r.get("matched_text") or "").replace("\n", " ")
            # 構造化コードを合成付与（社名解決用に元本文も残す）
            posts.append({"account": acc, "date": date, "text": f"{body} （{code4}）"})
    return posts


def load_merged() -> list[dict]:
    posts = []
    for r in json.load(open(MERGED, encoding="utf-8")):
        acc = norm_account(r.get("username"))
        date = iso_to_ymd(r.get("posted_at"))
        text = (r.get("text") or "").replace("\n", " ")
        if acc and date and text:
            posts.append({"account": acc, "date": date, "text": text})
    return posts


def load_masters() -> list[dict]:
    posts = []
    for r in json.load(open(MASTERS, encoding="utf-8")):
        acc = norm_account(r.get("user"))
        date = iso_to_ymd(r.get("date"))
        text = " ".join(str(r.get(k) or "") for k in ("claim", "sketch", "text_head")).replace("\n", " ")
        if acc and date and text.strip():
            posts.append({"account": acc, "date": date, "text": text})
    return posts


def main() -> int:
    parts = {"signals": load_signals(), "merged": load_merged(), "masters": load_masters()}
    posts = [p for lst in parts.values() for p in lst]
    accounts = sorted({p["account"] for p in posts})
    payload = {
        "_note": "手元Xコーパス3ソース統合（influencer_candidate_score.py 入力用）。"
                 "signals/merged/masters を account/date(YYYYMMDD)/text へ正規化。"
                 "偏り: Grok抽出(2026-03〜04)・収集済み445・名人5人に限られる=網羅ではない。",
        "_schema": "posts[].{account, date(YYYYMMDD), text}",
        "_provenance": {k: len(v) for k, v in parts.items()},
        "_accounts": len(accounts),
        "posts": posts,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"sources: {payload['_provenance']}")
    print(f"total posts={len(posts)}  accounts={len(accounts)}")
    print(f"-> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
