#!/usr/bin/env python3
"""ライブ・バズトレーサー — ウォッチリストの X アカウントを巡回し、投稿の伸びを時系列で追う。

背景（2026-07-21 新設・x-buzz 役割監査の裁定1）:
  週次収集（x_search_collect_twittora.py）は「上がりきったバズの写真」しか撮れない。
  本装置は注目アカウント10-15を高頻度で見回り、同一投稿を巡回毎に再計測して
  「今まさに伸び始めている投稿」を速度（likes/時）で検知する＝バズの"動画"を撮る。

役割分担:
  - 収集器（週次・面・コーパス用）は残す。本装置は別レーン（ライブ・ウォッチリスト）
  - official レーンの新投稿検知は速報通知（裁定7）の入力になる（通知の配線は launchd 側）

出力:
  - output/x_tracer/tracer-YYYY-MM-DD.jsonl  … スナップショット行（1投稿×1巡回=1行）
  - output/x_tracer/alerts-YYYY-MM-DD.jsonl  … 検知行（rising / official_new）

実行場所: influx xstock-vnc コンテナ内（fetch_followers.py と同じ・headless）。
  docker exec xstock-vnc python3 /app/scripts/x_watchlist_tracer.py
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tier3_posting.shared.cookie_crypto import load_cookies_or_raise  # noqa: E402
from x_search_collect_twittora import parse_aria_metrics  # noqa: E402  # aria正確指標を再利用

from playwright.sync_api import sync_playwright  # noqa: E402

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
CONFIG = APP / "configs" / "x_watchlist.json"
OUT_DIR = APP / "output" / "x_tracer"
PROFILE = "/app/x_profiles/maaaki"

_STATUS_RE = re.compile(r"/([A-Za-z0-9_]+)/status/(\d+)")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def load_recent_snapshots(days: int = 3) -> dict:
    """直近 days 日のスナップショットを tweet_id → [rows(時刻昇順)] で返す。"""
    by_id: dict[str, list[dict]] = {}
    today = _dt.date.today()
    for i in range(days, -1, -1):
        f = OUT_DIR / f"tracer-{today - _dt.timedelta(days=i)}.jsonl"
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # dict 以外・必須キー欠落は履歴に採用しない (2026-07-21 Codex R1 W1)
            if not isinstance(row, dict) or not row.get("tweet_id") or not row.get("scraped_at"):
                continue
            by_id.setdefault(row["tweet_id"], []).append(row)
    return by_id


def scrape_profile(page, handle: str, max_posts: int) -> list[dict]:
    """プロフィールタイムライン先頭の投稿を最大 max_posts 件取得。fail-soft。"""
    rows: list[dict] = []
    page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_selector("article", timeout=25_000)
    page.wait_for_timeout(1500)
    for art in page.locator("article").all()[: max_posts + 4]:  # pinned/RT ぶん余分に見る
        try:
            link = art.locator('a[href*="/status/"]').first
            m = _STATUS_RE.search(link.get_attribute("href") or "")
            if not m:
                continue
            author, tweet_id = m.group(1), m.group(2)
            # socialContext = 「リポスト」「固定」等のバッジ。他人の投稿の再掲は追跡対象外
            ctx = ""
            sc = art.locator('[data-testid="socialContext"]')
            if sc.count():
                ctx = (sc.first.inner_text() or "").strip()
            is_repost = author.lower() != handle.lower()
            posted_at = None
            t = art.locator("time").first
            if t.count():
                posted_at = t.get_attribute("datetime")
            text = ""
            tt = art.locator('[data-testid="tweetText"]')
            if tt.count():
                # U+2028/U+2029 は json.dumps が素通しし splitlines() を割る → 無害化
                text = tt.first.inner_text().replace("\u2028", " ").replace("\u2029", " ")
            aria = None
            grp = art.locator('[role="group"]').last
            if grp.count():
                aria = grp.get_attribute("aria-label")
            met = parse_aria_metrics(aria)
            rows.append({
                "tweet_id": tweet_id,
                "handle": handle,
                "author": author,
                "url": f"https://x.com/{author}/status/{tweet_id}",
                "posted_at": posted_at,
                "text": (text or "")[:280],
                "is_repost": is_repost,
                "social_context": ctx[:40],
                **met,
                "scraped_at": now_iso(),
            })
            if len([r for r in rows if not r["is_repost"]]) >= max_posts:
                break
        except Exception:
            continue
    return rows


def detect(rows: list[dict], history: dict, cfg: dict, lane: str) -> list[dict]:
    """rising（速度超過）と official_new（公式の新投稿）を検知する。"""
    alerts: list[dict] = []
    thr = cfg.get("rising_likes_per_hour_ja", 15) if lane == "ja" else cfg.get("rising_likes_per_hour", 30)
    new_win_h = cfg.get("official_new_within_hours", 24)
    now = _dt.datetime.now(_dt.timezone.utc)
    for r in rows:
        if r["is_repost"]:
            continue
        prev = history.get(r["tweet_id"]) or []
        # official_new: 履歴に無い＋投稿が新しい
        if lane == "official" and not prev and r.get("posted_at"):
            try:
                age_h = (now - _dt.datetime.fromisoformat(r["posted_at"].replace("Z", "+00:00"))).total_seconds() / 3600
            except ValueError:
                age_h = None
            if age_h is not None and 0 <= age_h <= new_win_h:  # 未来時刻は時計ずれ (Codex R1 W2)
                alerts.append({"reason": "official_new", "age_hours": round(age_h, 1), **_alert_base(r)})
                continue
        # rising: 前回スナップショットとの差分速度
        if prev:
            last = prev[-1]
            try:
                dt_h = (
                    _dt.datetime.fromisoformat(r["scraped_at"]) - _dt.datetime.fromisoformat(last["scraped_at"])
                ).total_seconds() / 3600
            except (ValueError, TypeError):
                continue
            if dt_h < 0.25:  # 15分未満の再計測は速度ノイズ
                continue
            try:
                dl = (r.get("likes") or 0) - (last.get("likes") or 0)
            except TypeError:
                continue  # 数値でない likes は速度計算に使わない (Codex R1 W1)
            lph = dl / dt_h
            if lph >= thr:
                alerts.append({"reason": "rising", "likes_per_hour": round(lph, 1), **_alert_base(r)})
    return alerts


def _alert_base(r: dict) -> dict:
    return {
        "tweet_id": r["tweet_id"], "handle": r["handle"], "url": r["url"],
        "likes": r.get("likes"), "views": r.get("impressions"),
        "text_head": (r.get("text") or "")[:80], "detected_at": now_iso(),
    }


def main() -> int:
    cfg = load_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = _dt.date.today()
    snap_f = OUT_DIR / f"tracer-{today}.jsonl"
    alert_f = OUT_DIR / f"alerts-{today}.jsonl"

    history = load_recent_snapshots()
    cookies = load_cookies_or_raise(Path(PROFILE) / "cookies.json")

    all_rows: list[dict] = []
    all_alerts: list[dict] = []
    failed: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.add_cookies(cookies)
        page = context.new_page()
        for h in cfg["handles"]:
            handle, lane = h["handle"], h.get("lane", "en")
            try:
                rows = scrape_profile(page, handle, cfg.get("max_posts_per_handle", 10))
                own = [r for r in rows if not r["is_repost"]]
                all_alerts.extend(detect(rows, history, cfg, lane))
                all_rows.extend(rows)
                print(f"[{handle}] {len(own)}件（+repost {len(rows)-len(own)}）")
            except Exception as e:  # per-handle fail-soft
                failed.append(handle)
                print(f"[{handle}] FAIL: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
        browser.close()

    with open(snap_f, "a", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if all_alerts:
        with open(alert_f, "a", encoding="utf-8") as f:
            for a in all_alerts:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")

    n_series = sum(1 for r in all_rows if not r["is_repost"] and history.get(r["tweet_id"]))
    print(f"--- self-check: rows={len(all_rows)} 時系列2点目以降={n_series} "
          f"alerts={len(all_alerts)} failed_handles={failed or 'なし'}")
    # 全滅のみ失敗扱い（0件偽装の再発防止・grok収集の教訓）
    if not all_rows:
        print("ERROR: 全ハンドル取得0件", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
