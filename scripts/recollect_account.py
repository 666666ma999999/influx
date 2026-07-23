#!/usr/bin/env python3
"""単一アカウントの中立全収集ランナー（Batch1 T2・第17R事前登録）。

目的: signals(Grok抽出)由来の候補を、選別バイアスなく全投稿（分母込み）で採り直し、
influencer_candidate_score.py で再採点するための収集器。min_faves フィルタは掛けない
（＝噴かなかった言及も含む完全な発射台帳を作る＝敵対レビューA1/B1の要求）。

Canonical 再利用: collector.x_collector.SafeXCollector（cookieベース・自動ログインなし）。
実行（xstock-vnc・DISPLAY=:99 必須）:
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/recollect_account.py \
      --account fxnia_kabu --since 2025-08-01 --until 2026-07-24 --max-scrolls 200

出力: data/influencer_candidates/recollect/<account>.json（posts[].{account,date,text}・採点器入力形式）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector.x_collector import SafeXCollector  # noqa: E402  Canonical収集器


def iso_to_ymd(s: str) -> str | None:
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
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", required=True, help="収集対象の username（@なし）")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD（窓開始）")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD（窓終了）")
    ap.add_argument("--max-scrolls", type=int, default=200)
    ap.add_argument("--profile", default="x_profiles/maaaki")
    ap.add_argument("--stop-after-empty", type=int, default=5)
    ap.add_argument("--output-dir", default="data/influencer_candidates/recollect")
    args = ap.parse_args()

    acc = args.account.lstrip("@")
    # min_faves なし＝完全な分母。窓は任意。
    parts = [f"from:{acc}"]
    if args.since:
        parts.append(f"since:{args.since}")
    if args.until:
        parts.append(f"until:{args.until}")
    query = " ".join(parts)
    url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"

    print(f"[recollect] account=@{acc} query='{query}'")
    print(f"[recollect] profile={args.profile} max_scrolls={args.max_scrolls}")

    collector = SafeXCollector(profile_path=args.profile)
    res = collector.collect(url, max_scrolls=args.max_scrolls,
                            group_name=f"recollect:{acc}", stop_after_empty=args.stop_after_empty)

    # from:<account> 検索は本人の投稿のみを返すため、username での除外はしない。
    # （表示名に "@" を含むアカウントは collector の username 抽出が誤るため gate しない。
    #  混入した引用元カード等は account=acc とみなす＝from: の著者保証を信頼する。2026-07-24 修正）
    posts = []
    for t in res.tweets:
        date = iso_to_ymd(t.get("posted_at") or t.get("collected_at"))
        text = (t.get("text") or "").replace("\n", " ").strip()
        if date and text:
            posts.append({"account": acc, "date": date, "text": text})

    out_dir = ROOT / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{acc}.json"
    payload = {
        "_note": f"@{acc} 中立全収集（min_faves無し=分母込み・第17R Batch1 T2）。"
                 f"cookie={args.profile}・自動ログインなし。status={res.status}。",
        "_schema": "posts[].{account, date(YYYYMMDD), text}",
        "_collection": {"query": query, "status": res.status,
                        "raw_collected": len(res.tweets), "own_posts": len(posts),
                        "max_scrolls": args.max_scrolls, "error": res.error_message or None},
        "posts": posts,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[recollect] status={res.status} raw={len(res.tweets)} own={len(posts)} -> {out.relative_to(ROOT)}")
    return 0 if res.status == "success" else 2


if __name__ == "__main__":
    sys.exit(main())
