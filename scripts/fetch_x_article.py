#!/usr/bin/env python3
"""fetch_x_article.py — X Articles（長文記事）の本文を Cookie セッションで個別取得する。

2026-08-04 新設（攻めネタ棚・敵対レビューA/B一致結論「長文は本文を取ってから判定」）。
コンテナ内（xstock-vnc）で実行する前提。ホスト側からは:
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/fetch_x_article.py <url> [<url>...]
出力: 1 URL 1行の JSON（{"url","status":"full|failed","title","text"}）を stdout へ。
fail-closed: 取れなければ status=failed（呼び出し側は本文なしとして扱い、カード化しない）。
"""
import json
import sys

sys.path.insert(0, "/app/scripts")
sys.path.insert(0, "/app")

from playwright.sync_api import sync_playwright  # noqa: E402
import fetch_bookmarks  # noqa: E402  Cookie loader（canonical）
from bookmarks_keyword_digest_collect_browser import build_context_kwargs  # noqa: E402

LOGIN_MARKERS = ("いま何が起こっているかチェック", "電話番号で続ける")


def main() -> int:
    urls = [u for u in sys.argv[1:] if u.startswith("http")]
    if not urls:
        print(json.dumps({"error": "no urls"}))
        return 2
    import os
    os.chdir("/app")
    cookies = fetch_bookmarks.load_cookies("x_profiles/maaaki")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(**build_context_kwargs())
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        for u in urls:
            row = {"url": u, "status": "failed", "title": "", "text": ""}
            try:
                page.goto(u, timeout=30000)
                page.wait_for_timeout(5000)
                txt = page.evaluate("()=>document.body.innerText") or ""
                if any(m in txt[:400] for m in LOGIN_MARKERS):
                    row["status"] = "failed_login_wall"
                elif len(txt) > 400:
                    lines = [l for l in txt.splitlines() if l.strip()]
                    # X画面のUI雑音（メニュー等）を除去: 記事本文はUI行を落とした残りから始まる
                    UI = ("キーボードショートカット", "ホーム", "話題を検索", "通知", "チャット",
                          "Grok", "プレミアム", "ブックマーク", "クリエイタースタジオ", "記事",
                          "プロフィール", "もっと見る", "ポスト", "フォロー", "フォローする")
                    body_lines = [l for l in lines if l.strip() not in UI
                                  and not l.startswith("キーボードショートカット")]
                    row["title"] = body_lines[0][:80] if body_lines else ""
                    row["text"] = "\n".join(body_lines)[:12000]
                    row["status"] = "full"
            except Exception as exc:  # noqa: BLE001
                row["status"] = f"failed_{type(exc).__name__}"
            print(json.dumps(row, ensure_ascii=False))
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
