#!/usr/bin/env python3
"""x_metrics_lib.py — X投稿の数値指標を測る唯一のオンデマンド計測モジュール。

2026-08-19 敵対レビュー一致結論＋オーナー裁定で新設（設計= 共通契約＋用途別アダプタ）。
それまで計器が経路ごとに独自実装され、同じ投稿の数字が経路間で食い違っていた
（17/17件不一致・「万」パース全損で3万いいね→0保存の実証4件）。

契約（この形以外で数値を保存しない）:
  {
    "status_id": str,
    "likes" / "replies" / "views" / "bookmarks" / "retweets" / "quotes": int | None,
    "sources": {field: "syndication" | "fxtwitter"},   # どの経路で取れたか
    "captured_at": "2026-08-19T12:34:56+00:00",        # 測った時刻（必須）
    "errors": [str, ...],                               # 取得失敗の記録（空なら全取得成功）
  }
  - 欠測は null（0 を書かない＝反応ゼロと計測不能を区別する・2026-07-26 T11）
  - likes / replies の正= syndication（2026-07-22 オーナー裁定の正ルート）
  - views / bookmarks / retweets / quotes の正= fxtwitter（2026-08-19 オーナー裁定・
    syndication がこれらを持たないため）
  - fxtwitter の likes は syndication の照合用にのみ使う（sources に出さない）

使い方:
  python3 scripts/x_metrics_lib.py <url-or-status-id> [...]   # 1行1件の JSONL を stdout へ
  from x_metrics_lib import fetch_metrics                     # ライブラリとして

標準ライブラリのみ・読み取り専用（どこにも保存しない。保存は呼び出し側の責務）。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result?id={}&lang=ja&token=a"
FXTWITTER_URL = "https://api.fxtwitter.com/i/status/{}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
STATUS_RE = re.compile(r"/status/(\d+)")
MAX_RETRIES = 3
BACKOFF_BASE = 2
TIMEOUT = 15
REQUEST_INTERVAL = 0.8  # CLI で連続照会する時の礼儀（既存 normalize_master_posts.py と同値）

METRIC_FIELDS = ("likes", "replies", "views", "bookmarks", "retweets", "quotes")


def status_id_of(url_or_id: str) -> Optional[str]:
    """URL または生の status ID から ID を取る。/status/<digits> 以外の末尾要素は拾わない。"""
    s = str(url_or_id or "").strip()
    if s.isdigit():
        return s
    m = STATUS_RE.search(s)
    return m.group(1) if m else None


def _get_json(url: str, errors: list) -> Optional[dict]:
    """再試行・バックオフつき GET。失敗は errors に1行残して None。"""
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                errors.append(f"{url.split('/')[2]}: HTTP404(削除・非公開・不存在)")
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE ** attempt
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and str(retry_after).isdigit():
                    wait = max(wait, int(retry_after))
                time.sleep(wait)
                continue
            errors.append(f"{url.split('/')[2]}: HTTP{e.code}")
            return None
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            errors.append(f"{url.split('/')[2]}: {e.__class__.__name__}")
            return None
    errors.append(f"{url.split('/')[2]}: retry_exhausted")
    return None


def _as_int(v: Any) -> Optional[int]:
    """int にできない値・None は欠測（None）として返す。0 は正当な観測値として通す。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def build_record(status_id: str, syn: Optional[dict], fx: Optional[dict],
                 errors: list, captured_at: str) -> dict:
    """2経路の生payloadから契約形のレコードを組む（純関数・テスト対象）。"""
    rec: dict = {"status_id": status_id, "sources": {}, "captured_at": captured_at,
                 "errors": list(errors)}
    for f in METRIC_FIELDS:
        rec[f] = None

    if syn is not None:
        likes = _as_int(syn.get("favorite_count"))
        replies = _as_int(syn.get("conversation_count"))
        if likes is not None:
            rec["likes"] = likes
            rec["sources"]["likes"] = "syndication"
        if replies is not None:
            rec["replies"] = replies
            rec["sources"]["replies"] = "syndication"

    tweet = (fx or {}).get("tweet") or {}
    for field in ("views", "bookmarks", "retweets", "quotes"):
        v = _as_int(tweet.get(field))
        if v is not None:
            rec[field] = v
            rec["sources"][field] = "fxtwitter"
    # replies の穴埋め（syndication 落ちの時だけ）
    if rec["replies"] is None:
        v = _as_int(tweet.get("replies"))
        if v is not None:
            rec["replies"] = v
            rec["sources"]["replies"] = "fxtwitter"
    # likes は syndication が正。syndication 落ちの時だけ fxtwitter で穴埋め
    if rec["likes"] is None:
        v = _as_int(tweet.get("likes"))
        if v is not None:
            rec["likes"] = v
            rec["sources"]["likes"] = "fxtwitter"
    # 両経路が生きている時は likes を照合し、大きく食い違えば記録する（黙って見過ごさない）
    elif tweet:
        fx_likes = _as_int(tweet.get("likes"))
        if fx_likes is not None and rec["likes"] and abs(fx_likes - rec["likes"]) > max(10, rec["likes"] * 0.05):
            rec["errors"].append(f"likes_mismatch: syndication={rec['likes']} fxtwitter={fx_likes}")
    return rec


def fetch_metrics(url_or_id: str) -> Optional[dict]:
    """1投稿の指標を契約形で返す。ID が取れない入力は None。"""
    sid = status_id_of(url_or_id)
    if not sid:
        return None
    errors: list = []
    syn = _get_json(SYNDICATION_URL.format(sid), errors)
    fx = _get_json(FXTWITTER_URL.format(sid), errors)
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return build_record(sid, syn, fx, errors, captured_at)


def main(argv: list) -> int:
    if not argv:
        print("usage: x_metrics_lib.py <url-or-status-id> [...]", file=sys.stderr)
        return 2
    rc = 0
    for i, arg in enumerate(argv):
        rec = fetch_metrics(arg)
        if rec is None:
            print(json.dumps({"input": arg, "error": "status_id を抽出できない"},
                             ensure_ascii=False))
            rc = 1
            continue
        print(json.dumps(rec, ensure_ascii=False))
        if i < len(argv) - 1:
            time.sleep(REQUEST_INTERVAL)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
