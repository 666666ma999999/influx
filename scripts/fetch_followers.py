#!/usr/bin/env python3
"""プロフィールのフォロワー数を1件取得して JSONL に追記する（x-buzz 二次KPI・2026-07-18 新設）。

背景: x-buzz 完成度監査（Fable5+Codex）で「フォロワー数を測る装置が無い」が最重要欠落と判定。
既存 engagement 計測は投稿単位のみのため、プロフィール単位の最小スクレイパーを追加した。

実行場所: influx xstock-vnc コンテナ内（fetch_engagement.py と同じ）。
Cookie 読込は tier3_posting の canonical loader を再利用（二重実装しない）。

Usage (コンテナ内):
  python3 /app/scripts/fetch_followers.py --profile /app/x_profiles/maaaki \
      --handle twittora_ --out /tmp/followers.jsonl
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tier3_posting.shared.cookie_crypto import load_cookies_or_raise  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402


def _parse_count(text: str) -> int | None:
    """'1,234' / '1.2K' / '3.4M' / '1.2万' を整数へ。"""
    t = text.strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KkMm万]?)$", t)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    mult = {"k": 1_000, "m": 1_000_000, "万": 10_000}.get(unit, 1)
    return int(val * mult)


def fetch_followers(profile_dir: str, handle: str) -> dict:
    rec: dict = {
        "profile_handle": handle,
        "kind": "followers",
        "scraped_at": _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(),
    }
    cookies = load_cookies_or_raise(Path(profile_dir) / "cookies.json")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=30_000)
        try:
            el = page.wait_for_selector(
                f'a[href$="/verified_followers"] span span', timeout=15_000
            )
            count = _parse_count(el.inner_text())
        except Exception:
            # フォールバック: ページ全文から "N Followers" を拾う
            count = None
            try:
                body = page.inner_text("body")
                m = re.search(r"([\d.,]+[KkMm万]?)\s*(?:Followers|フォロワー)", body)
                if m:
                    count = _parse_count(m.group(1))
            except Exception:
                pass
        browser.close()
    if count is None:
        rec["status"] = "error"
        rec["error_detail"] = "followers count not found"
    else:
        rec["status"] = "ok"
        rec["followers"] = count
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="cookies.json の親ディレクトリ")
    ap.add_argument("--handle", default="twittora_")
    ap.add_argument("--out", required=True, help="追記先 JSONL")
    args = ap.parse_args()

    rec = fetch_followers(args.profile, args.handle)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"DONE: {rec.get('status')} followers={rec.get('followers')}")
    sys.exit(0 if rec.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
