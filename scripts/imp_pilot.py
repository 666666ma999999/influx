"""imp_pilot: ジャンル別に X の投稿がどれだけ見られているかを測るパイロット（P-INF-21・2026-09-27）.

裁定（vault decisions 2026-09-27）:
- 母集団= 「伸びた投稿」でなく X 検索で拾えた全投稿（いいね下限なし）。自分では投稿しない。
- 指標= 閲覧数 ÷ 投稿者のフォロワー数（生の閲覧数はフォロワー規模を映すため）。
- 既存の `configs/x_price_watch.json`（55 クエリ・相対比較専用）と `data/x_price_watch/` は触らない。

3 段:
    collect  検索 → 各ジャンル先頭 N 件の投稿（URL・投稿者・本文）を data/imp_pilot/posts.jsonl へ
    enrich   投稿ページの閲覧数（autopost の ImpressionScraper を再利用）＋プロフィールのフォロワー数
             → data/imp_pilot/metrics.jsonl へ
    report   ジャンル別の中央値・上位 10% を output/imp_pilot_report.md へ

実行（VNC コンテナ・検索系の既存経路と同じ）:
    docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/imp_pilot.py collect
    docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/imp_pilot.py enrich
    python3 scripts/imp_pilot.py report

設計上の約束:
- 0 件と取得失敗を混ぜない（status を必ず残す）。閲覧数・フォロワー数が取れなければ null（0 を書かない）。
- login wall を見たら即中断（アカウント保護）。1 日 1 回まで（fetch-engagement の目安に従う）。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

DEFAULT_CONFIG = APP / "configs/imp_pilot.json"
DEFAULT_DIR = APP / "data/imp_pilot"
DEFAULT_PROFILE = APP / "x_profiles/maaaki"
DEFAULT_REPORT = APP / "output/imp_pilot_report.md"

_STATUS_RE = re.compile(r"^/([A-Za-z0-9_]+)/status/(\d+)")


# ---------------------------------------------------------------- pure helpers（テスト対象）

def parse_count(text: str | None) -> int | None:
    """X の丸め表示（"1,234" / "1.2万" / "12.3K" / "3,456 フォロワー"）を整数に。取れなければ None。"""
    if not text:
        return None
    t = str(text).replace(",", "").replace("，", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|億|K|k|M|m)?", t)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or ""
    mult = {"万": 10_000, "億": 100_000_000, "K": 1_000, "k": 1_000, "M": 1_000_000, "m": 1_000_000}.get(unit, 1)
    return int(round(num * mult))


_VIEWS_JA_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?[万億KkMm]?)\s*件の表示")
_VIEWS_EN_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?[KMkm万億]?)\s*views?", re.IGNORECASE)


def views_from_label(label: str | None) -> int | None:
    """まとめ aria-label（「1 件の返信、…、6366 件の表示」/ "… 6366 views"）から閲覧数だけを取る。

    autopost の `_extract_number_from_label` は先頭の数（返信数）を返してしまう（2026-09-27 実測: 返信 1・閲覧 6366 の投稿で views=1）。
    """
    if not label:
        return None
    m = _VIEWS_JA_RE.search(label) or _VIEWS_EN_RE.search(label)
    return parse_count(m.group(1)) if m else None


def search_window(now: datetime, days_from: int, days_to: int) -> tuple[str, str]:
    """(since, until) を YYYY-MM-DD で返す。since= now-days_from・until= now-days_to（until は排他）。"""
    since = (now - timedelta(days=days_from)).strftime("%Y-%m-%d")
    until = (now - timedelta(days=days_to)).strftime("%Y-%m-%d")
    return since, until


def compose_query(q: str, since: str, until: str) -> str:
    return f"{q} lang:ja -filter:nativeretweets -filter:replies since:{since} until:{until}"


def search_url(query: str) -> str:
    return f"https://x.com/search?q={quote(query)}&f=live"


def parse_card(href: str | None, text: str | None, posted_at: str | None) -> dict | None:
    """カードの permalink から (username, status_id) を取り、本文が無いカードは捨てる。"""
    if not href or not text:
        return None
    m = _STATUS_RE.match(href)
    if not m:
        return None
    return {
        "username": m.group(1),
        "status_id": m.group(2),
        "url": f"https://x.com/{m.group(1)}/status/{m.group(2)}",
        "text": " ".join(str(text).split()),
        "posted_at": posted_at,
    }


def dedupe_latest(rows: list[dict]) -> list[dict]:
    """resume で同じ投稿が複数行ある時、ok 行を優先し、無ければ最後の行を採る（1 投稿 1 行）。"""
    best: dict[tuple, dict] = {}
    for r in rows:
        sid = (r.get("genre"), r.get("status_id"))
        prev = best.get(sid)
        if prev is None or r.get("status") == "ok" or prev.get("status") != "ok":
            best[sid] = r
    return list(best.values())


def summarize(rows: list[dict], posts: list[dict] | None = None) -> dict:
    """metrics 行をジャンル別に集計。ratio= views/followers（両方あり・followers>0 の行だけ）。

    posts（collect の出力）を渡すと「拾った」= 収集件数になり、metrics に無い投稿も欠測として母数に入る。
    """
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r.get("genre", "?"), []).append(r)
    if posts:
        for po in posts:
            by.setdefault(po.get("genre", "?"), [])
    out: dict[str, dict] = {}
    for g, rs in sorted(by.items()):
        n_collected = sum(1 for po in posts if po.get("genre") == g) if posts else len(rs)
        views = [r["views"] for r in rs if isinstance(r.get("views"), int)]
        fol = [r["followers"] for r in rs if isinstance(r.get("followers"), int)]
        ratios = [r["views"] / r["followers"] for r in rs
                  if isinstance(r.get("views"), int) and isinstance(r.get("followers"), int) and r["followers"] > 0]
        ratios.sort()
        out[g] = {
            "n": n_collected,
            "n_views": len(views),
            "n_followers": len(fol),
            "n_ratio": len(ratios),
            "views_median": statistics.median(views) if views else None,
            "ratio_median": statistics.median(ratios) if ratios else None,
            "ratio_p90": ratios[max(0, math.ceil(len(ratios) * 0.9) - 1)] if ratios else None,  # nearest-rank
            "followers_median": statistics.median(fol) if fol else None,
        }
    return out


def render_report(summary: dict, genres: dict[str, dict], meta: dict) -> str:
    def f(v, nd=2):
        if v is None:
            return "—"
        return f"{v:,.{nd}f}" if isinstance(v, float) else f"{v:,}"
    lines = [
        "# ジャンル別 imp パイロット（P-INF-21）",
        "",
        f"実行: {meta.get('run_at', '?')}／収集窓: {meta.get('window', '?')}／母集団= 各ジャンルの検索で拾えた先頭 N 件"
        "（いいね下限なし・RT/リプライ除外）。指標= 閲覧数÷フォロワー数。",
        "",
        "| ジャンル | 意図 | 拾った | 閲覧数あり | フォロワーあり | 比あり | 閲覧数 中央値 | フォロワー 中央値 | 比 中央値 | 比 上位10% |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for g, s in summary.items():
        ge = genres.get(g, {})
        lines.append(
            f"| {ge.get('label', g)} | {ge.get('intent', '')} | {s['n']} | {s['n_views']} | {s['n_followers']} | {s['n_ratio']} "
            f"| {f(s['views_median'], 0)} | {f(s['followers_median'], 0)} | {f(s['ratio_median'])} | {f(s['ratio_p90'])} |"
        )
    lines += [
        "",
        "読み方: 「比」が高いジャンルほど、投稿者の規模に対して見られている（＝ジャンルの引きが強い）。"
        "「拾った」= collect が保存した件数（未処理・欠測も母数に入る）。「拾った」と「比あり」の差は取得できなかった投稿（欠測・0 ではない）。",
        "",
        f"出所: `data/imp_pilot/metrics.jsonl`（run_id={meta.get('run_id', '?')}）。生成= scripts/imp_pilot.py report",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- browser stages

def _load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _extract_cards(page) -> list[dict]:
    from bookmarks_keyword_digest_collect_browser import TWEET_CARD_SELECTOR
    from playwright.sync_api import Error as PlaywrightError
    try:
        triples = page.eval_on_selector_all(
            TWEET_CARD_SELECTOR,
            """els => els.map(e => {
                const a = e.querySelector('a:has(time)');
                const tm = e.querySelector('time');
                const t = e.querySelector('[data-testid="tweetText"]');
                return [a ? a.getAttribute('href') : null,
                        t ? t.innerText : null,
                        tm ? tm.getAttribute('datetime') : null];
            })""",
        )
    except PlaywrightError:
        return []
    out = []
    for href, text, posted_at in triples or []:
        card = parse_card(href, text, posted_at)
        if card:
            out.append(card)
    return out


def stage_collect(args) -> int:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    import fetch_bookmarks
    from bookmarks_keyword_digest_collect_browser import (
        TWEET_CARD_SELECTOR, build_context_kwargs, is_login_wall_url, _wait_for_new_cards,
    )
    from price_watch_collect import log_identity

    cfg = _load_config(args.config)
    ccfg = cfg["collect"]
    now = datetime.now(timezone.utc)
    since, until = search_window(now, ccfg["window_days_ago_from"], ccfg["window_days_ago_to"])
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    run_at = now.isoformat(timespec="seconds")
    per = ccfg["per_genre"]
    handle = log_identity(args.profile)
    cookies = fetch_bookmarks.load_cookies(str(args.profile))
    print(f"[run] run_id={run_id} window={since}..{until} genres={len(cfg['genres'])} per_genre={per}")

    posts_path = args.out_dir / "posts.jsonl"
    log_path = args.out_dir / "collect_log.jsonl"
    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            for ge in cfg["genres"]:
                started = time.time()
                q = compose_query(ge["q"], since, until)
                url = search_url(q)
                base = {"run_id": run_id, "genre": ge["id"], "query": q, "run_at": run_at,
                        "profile": args.profile.name, "expected_handle": handle}
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    time.sleep(random.uniform(*ccfg.get("goto_wait_sec", [3.0, 5.0])))
                    if is_login_wall_url(page.url):
                        print(f"[wall] {ge['id']}: login wall {page.url} — アカウント保護のため中断")
                        _append_jsonl(log_path, [{**base, "status": "login_wall", "count": None}])
                        return 3
                    found: dict[str, dict] = {}
                    scrolls = 0
                    stagnant = 0
                    while len(found) < per and scrolls < ccfg["max_scrolls"]:
                        prev_cards = page.locator(TWEET_CARD_SELECTOR).count()
                        before = len(found)
                        for c in _extract_cards(page):
                            found.setdefault(c["status_id"], c)
                        if len(found) >= per:
                            break
                        page.mouse.wheel(0, random.randint(1600, 2400))
                        scrolls += 1
                        _wait_for_new_cards(page, prev_cards, timeout_sec=6.0)
                        for c in _extract_cards(page):
                            found.setdefault(c["status_id"], c)
                        # 停滞判定は DOM のカード数でなく「新しい投稿 ID が増えたか」で見る（仮想化で枚数が一定でも中身は入れ替わる）
                        stagnant = stagnant + 1 if len(found) == before else 0
                        if stagnant >= 2:
                            break
                    for c in _extract_cards(page):  # 最終スクロール後の取りこぼし防止
                        found.setdefault(c["status_id"], c)
                    rows = [{"run_id": run_id, "genre": ge["id"], "intent": ge.get("intent", ""),
                             **c, "collected_at": run_at} for c in list(found.values())[:per]]
                    if not rows:
                        empty = page.locator('[data-testid="empty_state_header_text"]').count()
                        status = "ok_zero" if empty else "suspect_zero"
                    else:
                        status = "ok"
                    _append_jsonl(posts_path, rows)
                    _append_jsonl(log_path, [{**base, "status": status, "count": len(rows),
                                              "found": len(found), "scrolls": scrolls,
                                              "elapsed_sec": round(time.time() - started, 1)}])
                    total += len(rows)
                    print(f"[{ge['id']}] status={status} count={len(rows)} found={len(found)} scrolls={scrolls}")
                    time.sleep(random.uniform(8.0, 15.0))
                except PlaywrightError as exc:
                    print(f"[crash] {ge['id']}: {str(exc)[:160]}")
                    _append_jsonl(log_path, [{**base, "status": "crash", "count": None, "error": str(exc)[:200]}])
                    time.sleep(random.uniform(5.0, 10.0))
        finally:
            browser.close()
    print(f"[done] run_id={run_id} posts={total} (渡した {per}×{len(cfg['genres'])}={per * len(cfg['genres'])}／処理 {total})")
    return 0


def _scrape_views(page, status_id: str) -> int | None:
    """対象投稿（status_id）の閲覧数。①その投稿の article 内のまとめ aria-label「件の表示」
    ②その投稿の analytics リンク（/status/<id>/analytics）の文字（丸め表示）の順。
    ページ全体から最初の数を取ると親投稿・返信の値を拾うため、必ず status_id で絞る。"""
    article = f'article:has(a[href$="/status/{status_id}/analytics"]), article:has(a[href$="/status/{status_id}"])'
    try:
        for label in page.eval_on_selector_all(
            f'{article} [role="group"][aria-label]', "els => els.map(e => e.getAttribute('aria-label'))"
        ):
            n = views_from_label(label)
            if n is not None:
                return n
    except Exception:
        pass
    try:
        for text in page.eval_on_selector_all(
            f'a[href$="/status/{status_id}/analytics"]', "els => els.map(e => e.textContent)"
        ):
            n = views_from_label(text) or parse_count(text)
            if n is not None:
                return n
    except Exception:
        pass
    return None


def _scrape_followers(page, username: str, wait: tuple[float, float]) -> int | None:
    """プロフィールのフォロワー数。取れなければ None。"""
    from bookmarks_keyword_digest_collect_browser import LoginWallError, is_login_wall_url
    page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_selector('a[href$="/followers"], a[href$="/verified_followers"]', timeout=12_000)
    except Exception:
        pass
    if is_login_wall_url(page.url):
        raise LoginWallError(f"login wall at profile {username}: {page.url}")
    time.sleep(random.uniform(*wait))
    for sel in (f'a[href="/{username}/verified_followers"]', f'a[href="/{username}/followers"]',
                'a[href$="/verified_followers"]', 'a[href$="/followers"]'):
        try:
            el = page.query_selector(sel)
            if el:
                # 例: "1,234 フォロワー" / "1.2万 フォロワー"（aria-label に生数値が入ることもある）
                for src in (el.get_attribute("aria-label"), el.inner_text()):
                    n = parse_count(src)
                    if n is not None:
                        return n
        except Exception:
            continue
    return None


def stage_enrich(args) -> int:
    from playwright.sync_api import sync_playwright
    import fetch_bookmarks
    from bookmarks_keyword_digest_collect_browser import LoginWallError, build_context_kwargs, is_login_wall_url
    from tier3_posting.impression_tracker.scraper import ImpressionScraper

    cfg = _load_config(args.config)
    ecfg = cfg["enrich"]
    posts = _read_jsonl(args.out_dir / "posts.jsonl")
    if args.run_id:
        posts = [p for p in posts if p.get("run_id") == args.run_id]
    elif posts:
        latest = max(p["run_id"] for p in posts)
        posts = [p for p in posts if p["run_id"] == latest]
    if not posts:
        print("FATAL: posts.jsonl に対象行がありません（先に collect）")
        return 1
    run_id = posts[0]["run_id"]
    metrics_path = args.out_dir / "metrics.jsonl"
    # 「ok」= 閲覧数とフォロワー数の両方が取れた行だけ。片方でも欠けた投稿は resume で取り直す
    done = {(m["genre"], m["status_id"]) for m in _read_jsonl(metrics_path)
            if m.get("run_id") == run_id and m.get("status") == "ok"}
    todo = [p for p in posts if (p["genre"], p["status_id"]) not in done]
    print(f"[run] run_id={run_id} posts={len(posts)} todo={len(todo)} (resume: 済 {len(done)})")

    cookies = fetch_bookmarks.load_cookies(str(args.profile))
    scraper = ImpressionScraper(profile_path=str(args.profile))
    followers_cache: dict[str, int | None] = {}
    n_ok = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            for i, po in enumerate(todo, 1):
                rec = {"run_id": run_id, "genre": po["genre"], "status_id": po["status_id"], "url": po["url"],
                       "username": po["username"], "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                # 2026-09-27 実測: 6 件目以降が net::ERR_CONNECTION_REFUSED で連続失敗（数分後は同 URL が開けた）
                # ＝一時的な接続拒否。間を空けて最大 3 回やり直し、それでも拒否なら中断（アカウント保護）
                for attempt in range(4):
                    r = scraper._run_on_page(page, po["url"])  # noqa: SLF001  既存の閲覧数取得をそのまま使う
                    if "ERR_CONNECTION" not in str(r.get("error_detail", "")):
                        break
                    if attempt == 3:
                        print(f"[refused] {po['url']} 3 回やり直しても接続拒否 — 中断（後で resume）")
                        _append_jsonl(metrics_path, [{**rec, "status": "connection_refused"}])
                        return 5
                    backoff = 45 * (attempt + 1)
                    print(f"[refused] {po['url']} attempt={attempt + 1} → {backoff}s 待って再試行")
                    time.sleep(backoff)
                if is_login_wall_url(page.url) or r.get("status") == "login_required":
                    print(f"[wall] {po['url']} — アカウント保護のため中断")
                    _append_jsonl(metrics_path, [{**rec, "status": "login_wall"}])
                    return 3
                if r.get("status") == "rate_limited":
                    print("[rate_limited] 中断（翌日以降に resume）")
                    _append_jsonl(metrics_path, [{**rec, "status": "rate_limited", "autopost_status": "rate_limited"}])
                    return 4
                views = _scrape_views(page, po["status_id"]) if r.get("status") == "ok" else None
                rec.update({"autopost_status": r.get("status"), "views": views, "views_autopost": r.get("views"),
                            "likes": r.get("likes"), "retweets": r.get("retweets"), "error_detail": r.get("error_detail")})
                u = po["username"]
                if u not in followers_cache:  # 取れた値（整数）だけキャッシュ。None はキャッシュせず次の投稿で取り直す
                    time.sleep(random.uniform(*ecfg.get("per_url_wait_sec", [2.0, 4.0])))
                    fol = None
                    for attempt in range(3):
                        try:
                            fol = _scrape_followers(page, u, tuple(ecfg.get("profile_wait_sec", [2.0, 4.0])))
                            break
                        except LoginWallError as exc:
                            print(f"[wall] {exc} — アカウント保護のため中断")
                            _append_jsonl(metrics_path, [{**rec, "status": "login_wall"}])
                            return 3
                        except Exception as exc:  # プロフィールが取れなくても投稿側は残す
                            print(f"[followers] {u}: {str(exc)[:120]}")
                            if "ERR_CONNECTION" not in str(exc) or attempt == 2:
                                break
                            time.sleep(45 * (attempt + 1))
                    if isinstance(fol, int):
                        followers_cache[u] = fol
                    rec["followers"] = fol
                else:
                    rec["followers"] = followers_cache[u]
                if isinstance(rec["views"], int) and isinstance(rec["followers"], int) and rec["followers"] > 0:
                    rec["ratio"] = round(rec["views"] / rec["followers"], 4)
                # 自前の status: 両方取れて初めて ok。片方欠けは partial（resume で取り直す）・投稿側失敗は autopost の分類
                if isinstance(rec["views"], int) and isinstance(rec["followers"], int):
                    rec["status"] = "ok"
                elif r.get("status") == "ok":
                    rec["status"] = "partial"
                else:
                    rec["status"] = r.get("status") or "other"
                _append_jsonl(metrics_path, [rec])
                if rec["status"] == "ok":
                    n_ok += 1
                print(f"[{i}/{len(todo)}] {po['genre']} views={rec['views']} followers={rec['followers']} status={rec['status']}")
                time.sleep(random.uniform(*ecfg.get("per_url_wait_sec", [2.0, 4.0])))
        finally:
            browser.close()
    print(f"[done] run_id={run_id} 渡した {len(todo)}／処理 ok {n_ok}")
    return 0


def stage_report(args) -> int:
    cfg = _load_config(args.config)
    rows = _read_jsonl(args.out_dir / "metrics.jsonl")
    if args.run_id:
        rows = [r for r in rows if r.get("run_id") == args.run_id]
    elif rows:
        latest = max(r["run_id"] for r in rows)
        rows = [r for r in rows if r["run_id"] == latest]
    if not rows:
        print("FATAL: metrics.jsonl に対象行がありません（先に enrich）")
        return 1
    rows = dedupe_latest(rows)
    posts = [p for p in _read_jsonl(args.out_dir / "posts.jsonl") if p.get("run_id") == rows[0]["run_id"]]
    logs = [l for l in _read_jsonl(args.out_dir / "collect_log.jsonl") if l.get("run_id") == rows[0]["run_id"]]
    window = "?"
    if logs:
        m = re.search(r"since:(\S+) until:(\S+)", logs[0].get("query", ""))
        if m:
            window = f"{m.group(1)}..{m.group(2)}"
    summary = summarize(rows, posts)
    genres = {g["id"]: g for g in cfg["genres"]}
    text = render_report(summary, genres, {"run_id": rows[0]["run_id"], "run_at": rows[0].get("scraped_at"), "window": window})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text, encoding="utf-8")
    print(text)
    print(f"[written] {args.report}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=["collect", "enrich", "report"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--run-id", help="対象 run_id（省略時= 最新）")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    return {"collect": stage_collect, "enrich": stage_enrich, "report": stage_report}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
