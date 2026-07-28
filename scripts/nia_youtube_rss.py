#!/usr/bin/env python3
"""fxnia(ニア) YouTube 新チャンネルの前向き動画台帳（第23R裁定③(b)・2026-07-28）.

背景: 旧チャンネル(3.1万人)はコミュニティガイドライン違反で削除され、採点期間
(2026-03〜07)の動画は回復不能。新チャンネルは動画タイトル自体に銘柄リストが
載る構造のため、公式RSSフィード（規約上正規の配信手段・低頻度ポーリング）で
タイトル・日時だけを前向きに追記保存する。X capture (jp/us_forward) と同じ
「改変不能スナップショット」思想の YouTube 版。

実行: python3 scripts/nia_youtube_rss.py   # launchd 週次(us-watchlist wrapper)から呼ばれる
出力: data/influencer_candidates/jp_forward/nia_youtube.jsonl（追記専用・videoId去重）
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data/influencer_candidates/jp_forward/nia_youtube.jsonl"
FEEDS = {
    # channel_id は 2026-07-28 にチャンネルページの externalId から実測
    "nia_jp_rebooted": "UCtotFQ6gkijmrrStqYYXLlQ",   # ニア【株・投資】元 ニアの株チャート研究所
    "nia_us": "UC1BHWwCKXzZlFHCWkpfDsTw",             # ニアの米国株チャート研究所
}


def fetch_feed(channel_id: str) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    xml = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    out = []
    for e in re.findall(r"<entry>.*?</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>([^<]*)</yt:videoId>", e)
        title = re.search(r"<title>([^<]*)</title>", e)
        pub = re.search(r"<published>([^<]*)</published>", e)
        if vid and title:
            out.append({"video_id": vid.group(1), "title": title.group(1),
                        "published": pub.group(1) if pub else None})
    return out


def main() -> int:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["video_id"])
            except (ValueError, KeyError):
                continue
    added = 0
    with LEDGER.open("a", encoding="utf-8") as f:
        for label, cid in FEEDS.items():
            try:
                entries = fetch_feed(cid)
            except Exception as exc:  # ネットワーク断でも他チャンネルは続行
                print(f"[nia-rss] WARN {label}: {exc}", file=sys.stderr)
                continue
            for e in entries:
                if e["video_id"] in seen:
                    continue
                rec = {**e, "channel": label,
                       "fetched_ts": datetime.now(timezone.utc).isoformat()}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                seen.add(e["video_id"])
                added += 1
    print(f"[nia-rss] added={added} total={len(seen)} -> {LEDGER.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
