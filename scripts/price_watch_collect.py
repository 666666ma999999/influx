"""price_watch: X値上がり検出 — 固定クエリbatteryの日次件数収集.

configs/x_price_watch.json の固定クエリ約30本を、毎日同一条件
（f=live・lang:ja・-filter:nativeretweets・直前UTC日の since/until 1日窓）で検索し、
distinct status_id 数を data/x_price_watch/ledger.jsonl へ追記する。

設計上の約束（docs/research-workflow.md / 2026-07-26 検証の教訓）:
- X検索は間引き・打ち切りがあるため **絶対件数は主張しない**。同条件の相対比較専用。
- 「0件(status=ok)」と「取得失敗(login_wall/blocked/crash/suspect_zero)」を絶対に混ぜない。
- スクロール上限/件数capで打ち切った日は censored=true（zスコアのベースラインから除外）。
- クエリ(id/q/min_faves)は凍結。編集せず新idで追加する（時系列比較の前提）。

実行（VNCコンテナ・headless=False。検索系の既存実績経路）:
    docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_collect.py
    docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_collect.py \
        --queries gen1-memori-kotou gen1-ddr5-price
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import fetch_bookmarks  # noqa: E402  Canonical cookie loader
from bookmarks_keyword_digest_collect_browser import (  # noqa: E402  共有部品（Dual-Path禁止）
    TWEET_CARD_SELECTOR,
    LoginWallError,
    build_context_kwargs,
    build_search_url,
    is_login_wall_url,
    _wait_for_new_cards,
)

DEFAULT_CONFIG = APP / "configs/x_price_watch.json"
DEFAULT_LEDGER = APP / "data/x_price_watch/ledger.jsonl"
DEFAULT_TEXTS_DIR = APP / "data/x_price_watch/texts"
DEFAULT_PROFILE = APP / "x_profiles/maaaki"

# x_search_collect_twittora.py:88 と同一パターン（当該モジュールは未コミット変更が
# 進行中のため import 結合せず定数を再掲。変更時は両方を見ること）
_STATUS_ID_RE = re.compile(r"/status/(\d+)")

# collector/config.py BLOCK_ERROR_PATTERNS と同趣旨（ネットワーク遮断/レート制限の兆候）
_BLOCK_MARKERS = ("net::ERR_", "429", "CONNECTION_REFUSED", "CONNECTION_RESET", "TIMED_OUT")


def target_utc_day(now: datetime | None = None) -> str:
    """収集対象日 = 直前の完結したUTC日（YYYY-MM-DD）。

    毎日22:10 JST(=13:10 UTC)実行の時点で前UTC日は完結済み＝毎回まる1日窓になる。
    """
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def compose_query(entry: dict, day: str) -> str:
    """凍結クエリ + min_faves + 共通演算子 + 1日窓 を合成する。"""
    parts = [entry["q"]]
    if entry.get("min_faves"):
        parts.append(f"min_faves:{entry['min_faves']}")
    parts.append("lang:ja -filter:nativeretweets")
    next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    parts.append(f"since:{day} until:{next_day}")
    return " ".join(parts)


def extract_status_ids(page) -> set[str]:
    # カード自身の permalink（<time> を包む <a>）だけを数える。引用ポストが埋め込む
    # 引用元 /status/ リンクを拾うと1カードが2件に化ける（Codexレビュー CONFIRMED-1）
    hrefs = page.eval_on_selector_all(
        f"{TWEET_CARD_SELECTOR} a:has(time)",
        "els => els.map(e => e.getAttribute('href'))",
    )
    ids = set()
    for href in hrefs or []:
        m = _STATUS_ID_RE.search(href or "")
        if m:
            ids.add(m.group(1))
    return ids


def extract_posts(page) -> dict[str, str]:
    """カード単位で {status_id: 本文} を返す（2026-07-29 追加・本文レーン用）。

    **件数(count)には使わない。** count は従来どおり extract_status_ids の distinct 数で、
    凍結クエリの時系列を1件も変えないため。本関数は本文を拾う best-effort の追加経路で、
    取りこぼしても count には影響しない。

    スクロールのたびに呼ぶ必要がある: X は仮想リストで古いカードが DOM から消えるため、
    ループ外で1回だけ拾うと大半の本文を取り逃す（sample_texts が抱える既知の制約）。
    """
    try:
        pairs = page.eval_on_selector_all(
            TWEET_CARD_SELECTOR,
            """els => els.map(e => {
                const a = e.querySelector('a:has(time)');
                const t = e.querySelector('[data-testid="tweetText"]');
                return [a ? a.getAttribute('href') : null, t ? t.innerText : null];
            })""",
        )
    except PlaywrightError:
        return {}
    out: dict[str, str] = {}
    for href, text in pairs or []:
        m = _STATUS_ID_RE.search(href or "")
        if m and text:
            out[m.group(1)] = " ".join(str(text).split())
    return out


def sample_texts(page, limit: int = 3) -> list[str]:
    texts = page.eval_on_selector_all(
        '[data-testid="tweetText"]',
        "els => els.map(e => e.innerText)",
    )
    out = []
    for t in texts or []:
        one_line = " ".join(str(t).split())[:120]
        if one_line:
            out.append(one_line)
        if len(out) >= limit:
            break
    return out


def collect_query(page, url: str, max_scrolls: int, cap_posts: int,
                  goto_wait: tuple[float, float] = (3.0, 5.0)) -> dict:
    """1クエリを収集し {count, censored, scrolls, status, samples} を返す。"""
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    time.sleep(random.uniform(*goto_wait))
    if is_login_wall_url(page.url):
        raise LoginWallError(page.url)

    ids: set[str] = set()
    posts: dict[str, str] = {}   # 本文（count には使わない・best-effort）
    scrolls = 0
    censored = False
    stagnant = 0  # 一時的な読み込み遅延を「自然枯渇」と誤認しないため2連続無増加で確定
    while True:
        prev_cards = page.locator(TWEET_CARD_SELECTOR).count()
        ids |= extract_status_ids(page)
        posts.update(extract_posts(page))
        if len(ids) >= cap_posts:
            censored = True
            break
        if scrolls >= max_scrolls:
            censored = True  # 上限到達＝続きが在るかもしれない打ち切り
            break
        page.mouse.wheel(0, random.randint(1600, 2400))
        scrolls += 1
        new_cards = _wait_for_new_cards(page, prev_cards, timeout_sec=6.0)
        if new_cards <= prev_cards:
            stagnant += 1
            ids |= extract_status_ids(page)
            posts.update(extract_posts(page))
            if stagnant >= 2:
                censored = False  # 2連続で新規カードなし＝自然枯渇と確定
                break
        else:
            stagnant = 0

    if not ids:
        # 0件: X の empty state が出ていれば正当な0、出ていなければ疑わしい0として分離
        empty = page.locator('[data-testid="empty_state_header_text"]').count()
        status = "ok" if empty else "suspect_zero"
        return {"count": 0, "censored": False, "scrolls": scrolls, "status": status,
                "samples": [], "posts": {}}
    return {
        "count": len(ids),
        "censored": censored,
        "scrolls": scrolls,
        "status": "ok",
        "samples": sample_texts(page),
        # 本文は台帳(ledger.jsonl)には入れず別ファイルへ回す。台帳は件数の時系列が正本で、
        # 本文を混ぜると1行が肥大化して既存の読み手を壊すため（2026-07-29）
        "posts": posts,
    }


def log_identity(profile: Path) -> str:
    """identity guard: どのアカウントの合鍵で動くかを実測ログに残す（2026-07-23教訓）。"""
    expected = profile / "expected_account.json"
    handle = "(expected_account.json なし)"
    if expected.exists():
        try:
            handle = json.loads(expected.read_text()).get("handle", handle)
        except (json.JSONDecodeError, OSError):
            handle = "(expected_account.json 読取失敗)"
    print(f"[identity] profile={profile.name} expected_handle={handle}")
    return handle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--texts-dir", type=Path, default=DEFAULT_TEXTS_DIR,
                        help="本文の保存先ディレクトリ（日付ごとに1ファイル）")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--date", help="対象UTC日 YYYY-MM-DD（省略時=直前の完結UTC日）")
    parser.add_argument("--queries", nargs="*", help="query_id で絞り込み（テスト用）")
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    collect_cfg = config["collect"]
    entries = config["queries"]
    if args.queries:
        entries = [e for e in entries if e["id"] in set(args.queries)]
        if not entries:
            print(f"FATAL: --queries に一致する id がありません: {args.queries}")
            return 1
    day = args.date or target_utc_day()
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # クエリ凍結の実効化: クエリ単位の指紋を各行へ記録し、alert側は同一指紋の日だけを
    # ベースラインに使う（config誤編集の混入防止。全体単位でなくクエリ単位にするのは、
    # 新クエリの追加が既存クエリのベースラインをリセットしないため・2026-07-27修正）
    def query_sha(entry: dict) -> str:
        raw = f"{entry['id']}|{entry['q']}|{entry.get('min_faves', 0)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    log_identity(args.profile)
    cookies = fetch_bookmarks.load_cookies(str(args.profile))
    print(f"[run] day={day} queries={len(entries)} config_version={config['version']}")

    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    texts: list[dict] = []   # 本文レーン（2026-07-29 追加・台帳とは別ファイル）
    wall_hit = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()

        for i, entry in enumerate(entries):
            started = time.time()
            base = {
                "date": day,
                "query_id": entry["id"],
                "lane": entry.get("lane", ""),
                "config_version": config["version"],
                "query_sha": query_sha(entry),
                "run_at": run_at,
            }
            try:
                url = build_search_url(compose_query(entry, day), days=1)
                result = collect_query(
                    page, url, collect_cfg["max_scrolls"], collect_cfg["cap_posts"],
                    goto_wait=tuple(collect_cfg.get("goto_wait_sec", [3.0, 5.0])),
                )
            except LoginWallError as exc:
                print(f"[wall] {entry['id']}: login wall {exc} — アカウント保護のため全体中断")
                rows.append({**base, "count": None, "censored": False, "scrolls": 0,
                             "status": "login_wall", "samples": [],
                             "elapsed_sec": round(time.time() - started, 1)})
                wall_hit = True
                break
            except PlaywrightError as exc:
                status = "blocked" if any(m in str(exc) for m in _BLOCK_MARKERS) else "crash"
                print(f"[{status}] {entry['id']}: {str(exc)[:140]}")
                rows.append({**base, "count": None, "censored": False, "scrolls": 0,
                             "status": status, "samples": [],
                             "elapsed_sec": round(time.time() - started, 1)})
                time.sleep(random.uniform(5.0, 10.0))
                continue
            # 本文は台帳から外して別ファイルへ（台帳の1行の形を変えない＝既存の読み手を壊さない）
            posts = result.pop("posts", {})
            for sid, txt in posts.items():
                texts.append({"date": day, "query_id": entry["id"], "status_id": sid,
                              "text": txt, "run_at": run_at})
            rows.append({**base, **result, "elapsed_sec": round(time.time() - started, 1)})
            print(f"[{i + 1}/{len(entries)}] {entry['id']}: count={result['count']} "
                  f"censored={result['censored']} status={result['status']}")
            if i + 1 < len(entries):
                lo, hi = collect_cfg["query_pacing_sec"]
                time.sleep(random.uniform(lo, hi))

        browser.close()

    with args.ledger.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 本文は日付ごとの別ファイルへ。銘柄言及の抽出（scripts/x_mention_extract.py）が読む
    if texts:
        tpath = args.texts_dir / f"{day}.jsonl"
        tpath.parent.mkdir(parents=True, exist_ok=True)
        with tpath.open("a", encoding="utf-8") as fh:
            for t in texts:
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(f"[texts] {len(texts)}件の本文を保存 → {tpath}")

    ok_rows = sum(1 for r in rows if r["status"] == "ok")
    print(f"[done] ledger={args.ledger} appended={len(rows)} ok={ok_rows} wall={wall_hit}")
    # ok率80%未満は失敗扱い（部分的大障害を「成功」で握りつぶさない・Codexレビュー CONFIRMED-6。
    # ランナー側の有界再試行(1回)につながる）
    threshold = max(1, int(0.8 * len(entries)))
    return 0 if ok_rows >= threshold else 1


if __name__ == "__main__":
    sys.exit(main())
