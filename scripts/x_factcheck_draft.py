"""X 裏取りレーン（P-INF-26・オーナー裁定 2026-10-04 a）: 初出レーンの事象に「①中身の裏取り」の列を機械が下書きし、人が埋める台帳.

何をするか（4 モデル議論 2026-10-03 の一致 K1〜K5・単独 S1〜S4 を実装に写した）:
  draft : 事象（= discovery_queue の候補で first_seen=true・first_posted_at あり・受益表が買い候補を返す語）ごとに
          ①投稿の取り直し（K4・初出の JST 日を 1 日窓で再取得・最古順）②同一記事の束ね（K5・URL か見出し）
          ③元報道の公開時刻（S4・URL の og:/article:published_time・JSON-LD）④遅れラベル（速報 <6h／当日 ≤24h／週内 ≤7d／再流通 >7d）
          を台帳 data/x_price_watch/factcheck_ledger.jsonl へ status=draft で 1 行追記する。同じ語は二重に書かない。
  list  : 事象数（母集団）と台帳の状態別件数・各行の 1 行要約を印字する。
  fill  : 人の判定〈一次ソース到達 ○/△/×・出所ラベル 一次/二次/未確認・範囲 一致/範囲拡大/未確認・メモ〉を同じ語の行として
          status=filled で追記する（append-only・最後の行が現在値）。

やらないこと（既決・非ゴール）: 因果の判定（②は追記9c の `_tmp_x_leadlag_now.py` のまま）／生活系（受益表に主題が無い語）／有料壁の突破。

実行（取り直しは VNC コンテナ・maaaki Cookie・既存の検索実績経路）:
    docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_factcheck_draft.py draft            # 取り直しあり
    docker exec xstock-vnc python3 /app/scripts/x_factcheck_draft.py draft --no-fetch               # 台帳の骨だけ（取り直し・URL なし）
    python3 scripts/x_factcheck_draft.py list
    python3 scripts/x_factcheck_draft.py fill --token マイクロン --primary ○ --label 一次 --scope 一致 --note "Micron 決算リリース 10-01"
回帰: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_x_factcheck_draft
"""
from __future__ import annotations

import argparse
import json
import random
import re
import socket
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))
import x_shortage_map as xsm  # noqa: E402

JST = timezone(timedelta(hours=9))
XD = APP / "data" / "x_price_watch"
QUEUE = XD / "discovery_queue.jsonl"
LEDGER = XD / "factcheck_ledger.jsonl"
PROFILE = APP / "x_profiles/maaaki"
MIN_FAVES = 20          # 発見器と同じ下限（事象の元投稿はこの下限で拾われている）
PER_QUERY = 40
MAX_SCROLLS = 3
REFETCH_KEEP = 30       # 台帳に残す投稿数の上限（最古順）
HEADLINE_KEY_LEN = 10   # URL 無し投稿の見出し束ねの鍵（NFKC・記号除去後の先頭文字数）
BUY_TIERS = {"confirmed", "provisional"}
PRIMARY_CHOICES = ("○", "△", "×")
LABEL_CHOICES = ("一次", "二次", "未確認")
SCOPE_CHOICES = ("一致", "範囲拡大", "未確認")

URL_RE = re.compile(r"https?://[^\s<>\"']+|(?<![\w.])[a-z0-9.-]+\.(?:com|jp|net|org|co\.jp|io|gov)/[^\s<>\"']+", re.I)


# ---------------------------------------------------------------- 事象の選別（純関数）
def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def buy_beneficiaries(subject: dict) -> list[dict]:
    """買い候補（sign + ・confirmed/provisional）だけ。追記9c の事象定義と同じ絞り。"""
    return [{"code": b.get("code"), "name": b.get("name"), "tier": b.get("tier")}
            for b in subject.get("beneficiaries", [])
            if b.get("sign") == "+" and b.get("tier") in BUY_TIERS]


def events_from_queue(batches: list[dict], m: dict) -> list[dict]:
    """candidate_batch 群 → 事象の一覧（語ごとに最初の便だけ・便は date 昇順で見る）。

    事象= first_seen=true かつ first_posted_at が空でなく、かつ subjects_for_token が買い候補を返す語（追記9c と同一）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for b in sorted((x for x in batches if x.get("type") == "candidate_batch"), key=lambda x: x.get("date", "")):
        for c in b.get("candidates", []):
            tok = c.get("token", "")
            if not tok or tok in seen or not c.get("first_seen") or not c.get("first_posted_at"):
                continue
            subs = []
            for s in xsm.subjects_for_token(m, tok):
                bene = buy_beneficiaries(s)
                if bene:
                    subs.append({"id": s["id"], "label": s.get("label", ""), "beneficiaries": bene})
            if not subs:
                continue
            seen.add(tok)
            out.append({"token": tok, "batch_date": b.get("date", ""), "first_posted_at": c["first_posted_at"],
                        "sample": c.get("sample", ""), "subjects": subs})
    return out


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def ledger_state(rows: list[dict]) -> dict[str, dict]:
    """append-only 台帳 → 語ごとの現在値（最後の行が勝つ）。"""
    cur: dict[str, dict] = {}
    for r in rows:
        if r.get("type") == "factcheck" and r.get("token"):
            cur[r["token"]] = r
    return cur


# ---------------------------------------------------------------- 束ね・遅れラベル（純関数）
def extract_urls(text: str) -> list[str]:
    return [u.rstrip(").,、。") for u in URL_RE.findall(text or "")]


def _headline_key(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = URL_RE.sub("", t)
    t = re.sub(r"[\s#＃@＠「」『』【】\[\]（）()|｜:：・…]+", "", t)
    return t[:HEADLINE_KEY_LEN]


def _url_key(u: str) -> str:
    """scheme・www.・末尾の / を落とし、ホストだけ小文字化（同じ記事の表記違いを 1 束に）。"""
    k = re.sub(r"^https?://", "", u.strip(), flags=re.I)
    k = re.sub(r"^www\.", "", k, flags=re.I)
    host, sep, path = k.partition("/")
    return host.lower() + sep + path.rstrip("/")   # 小文字化はホストだけ（パスは大文字小文字を区別する配信元がある・Codex #4）


def bundle_posts(posts: list[dict]) -> list[dict]:
    """同一記事の束ね（K5）。鍵= 本文中の最初の URL（t.co 以外・scheme/www 無視）・無ければ本文先頭 HEADLINE_KEY_LEN 字。
    見出し束ねは荒い（同じ見出しの転載を拾う用・人が bundles を見て判断する前提）。

    posts は {status_id, author, posted_at_full, text, ...}。返り値は束ごとに
    {key, kind(url|headline), url, n_posts, authors, earliest_posted_at, status_ids}。
    """
    groups: dict[str, dict] = {}
    for p in posts:
        urls = [u for u in extract_urls(p.get("text", "")) if "t.co/" not in u]
        if urls:
            key, kind, url = _url_key(urls[0]), "url", urls[0]
        else:
            key, kind, url = "h:" + _headline_key(p.get("text", "")), "headline", ""
        g = groups.setdefault(key, {"key": key, "kind": kind, "url": url, "n_posts": 0, "authors": [],
                                    "earliest_posted_at": "", "status_ids": []})
        g["n_posts"] += 1
        if p.get("author") and p["author"] not in g["authors"]:
            g["authors"].append(p["author"])
        g["status_ids"].append(p.get("status_id"))
        pa = p.get("posted_at_full") or ""
        if pa and (not g["earliest_posted_at"] or pa < g["earliest_posted_at"]):
            g["earliest_posted_at"] = pa
    return sorted(groups.values(), key=lambda g: (-g["n_posts"], g["earliest_posted_at"]))


def delay_label(first_posted_at: str, source_published_at: str) -> str:
    """投稿初出 − 元報道公開 の差でラベル。元報道が取れていなければ「未確認」。

    速報 <6h／当日 ≤24h／週内 ≤7d／再流通 >7d／投稿が先（報道より前に投稿）。
    """
    a, b = parse_iso(first_posted_at), parse_iso(source_published_at)
    if a is None or b is None:
        return "未確認"
    d = a - b
    if d < timedelta(0):
        return "投稿が先"
    if d < timedelta(hours=6):
        return "速報"
    if d <= timedelta(hours=24):
        return "当日"
    if d <= timedelta(days=7):
        return "週内"
    return "再流通"


# ---------------------------------------------------------------- 元報道の公開時刻（HTML から・ネットは draft 時のみ）
_META_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:article:published_time|datePublished|pubdate|publish-date)["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:article:published_time|og:article:published_time|datePublished|pubdate|publish-date)["\']', re.I),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
]
# `<time datetime>` は公開時刻とは限らない（更新日・関連記事の日付）ので使わない（Codex #3）


def parse_published_time(html: str) -> str:
    """HTML 文字列から公開時刻（ISO・UTC 化）を 1 つ返す。取れなければ空文字。

    採るのは「時刻とタイムゾーンを両方持つ」値だけ。日付だけ・オフセット無しの値は補わず空＝遅れは「未確認」のまま
    （00:00 UTC と置くと 速報／当日／投稿が先 が入れ替わる・Codex #3）。
    """
    for rx in _META_RES:
        for raw in rx.findall(html or ""):
            raw = raw.strip()
            if "T" not in raw.upper() and " " not in raw:
                continue  # 日付だけ
            dt = parse_iso(raw)
            if dt is None or dt.tzinfo is None:
                continue  # 解釈不能・タイムゾーン無し
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return ""


def fetch_published_time(url: str, timeout: float = 15.0) -> str:
    import urllib.request
    if not url.lower().startswith("http"):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (influx factcheck draft)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  公開 URL の HEAD 相当
            return parse_published_time(resp.read(300_000).decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001  取れなければ空＝人が埋める
        return ""


# ---------------------------------------------------------------- 取り直し（ブラウザ・コンテナのみ）
def refetch_posts(token: str, first_posted_at: str) -> tuple[list[dict], str]:
    """初出の JST 日を 1 日窓で取り直し、最古順 REFETCH_KEEP 件を返す。(rows, status)。"""
    import fetch_bookmarks  # noqa: E402
    import x_search_collect_twittora as xs  # noqa: E402
    from bookmarks_keyword_digest_collect_browser import LoginWallError, build_context_kwargs  # noqa: E402
    from playwright.sync_api import sync_playwright

    dt = parse_iso(first_posted_at)
    if dt is None:
        return [], "no_time"
    day = dt.astimezone(JST).strftime("%Y-%m-%d")
    xs.MAX_SCROLLS = MAX_SCROLLS
    cookies = fetch_bookmarks.load_cookies(str(PROFILE))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()
        try:
            rows = xs.collect_query(page, token, day, day, MIN_FAVES, PER_QUERY, lang="ja", tab="live")
        except LoginWallError as exc:
            print(f"✗ ログイン壁で中断: {exc}", file=sys.stderr)
            return [], "login_wall"
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {token}: {type(exc).__name__}", file=sys.stderr)
            return [], "error"
        finally:
            browser.close()
    return normalize_refetched(rows), "ok"


def normalize_refetched(rows: list[dict]) -> list[dict]:
    out = [{"status_id": r.get("id"), "author": r.get("author"), "likes": r.get("likes"),
            "posted_at_full": r.get("posted_at_full") or "", "text": r.get("content") or "",
            "urls": [u for u in extract_urls(r.get("content") or "") if "t.co/" not in u]} for r in rows]
    out.sort(key=lambda r: r["posted_at_full"] or "9999")
    return out[:REFETCH_KEEP]


# ---------------------------------------------------------------- 台帳の行
def empty_human() -> dict:
    return {"primary_source": "", "source_label": "", "scope_judgement": "", "note": "", "filled_at": ""}


def source_url_for_first_post(ev: dict, refetched: list[dict]) -> str:
    """初出の裏取りに使える URL= 取り直しが初出時刻まで届いていて、かつ **最古の投稿** が URL を持つ時だけ。

    投稿数の多い束の URL を採ると「後発の別件記事」で初出の遅れを測ってしまう（Codex #1）。該当が無ければ空＝人が bundles を見て埋める。
    """
    if not refetched:
        return ""
    oldest = min(refetched, key=lambda r: r.get("posted_at_full") or "9999")
    if not oldest.get("posted_at_full") or oldest["posted_at_full"] > ev["first_posted_at"]:
        return ""
    return (oldest.get("urls") or [""])[0]


def draft_row(ev: dict, refetched: list[dict], refetch_status: str, source_published_at: str, source_url: str) -> dict:
    t = parse_iso(ev["first_posted_at"])
    return {
        "type": "factcheck", "status": "draft", "token": ev["token"], "batch_date": ev["batch_date"],
        "first_posted_at": ev["first_posted_at"],
        "first_posted_at_jst": t.astimezone(JST).strftime("%Y-%m-%d %H:%M") if t else "",
        "subjects": ev["subjects"], "sample": ev.get("sample", ""),
        "refetch_status": refetch_status, "refetched": refetched, "bundles": bundle_posts(refetched),
        # 取り直しが初出投稿の時刻まで届いたか（live タブは新しい順＝スクロール上限で当日の早い側が欠けることがある・2026-10-04 実走で実測）
        "covers_first_post": any(p.get("posted_at_full") and p["posted_at_full"] <= ev["first_posted_at"] for p in refetched),
        "source_url": source_url, "source_published_at": source_published_at,
        "delay_label": delay_label(ev["first_posted_at"], source_published_at),
        "human": empty_human(),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": socket.gethostname(),
    }


def needs_draft(cur: dict | None, fetch: bool) -> bool:
    """下書きが要るか。無ければ要る。draft のまま取り直しが ok でない行は、取り直しありの実行なら再試行する（Codex #2）。

    人が埋めた filled 行は触らない。--no-fetch の実行は骨を二重に書かない。
    """
    if cur is None:
        return True
    return fetch and cur.get("status") == "draft" and cur.get("refetch_status") != "ok"


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def fill_row(prev: dict, primary: str, label: str, scope: str, note: str) -> dict:
    """人の判定を載せた新しい行（元の下書きを複製して human と status を更新）。"""
    if primary not in PRIMARY_CHOICES or label not in LABEL_CHOICES or scope not in SCOPE_CHOICES:
        raise ValueError(f"選択肢外: primary={primary} label={label} scope={scope}")
    row = json.loads(json.dumps(prev, ensure_ascii=False))
    row["status"] = "filled"
    row["human"] = {"primary_source": primary, "source_label": label, "scope_judgement": scope, "note": note,
                    "filled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    row["ts"] = row["human"]["filled_at"]
    return row


# ---------------------------------------------------------------- コマンド
def cmd_draft(args) -> int:
    m = xsm.load()
    events = events_from_queue(read_jsonl(QUEUE), m)
    state = ledger_state(read_jsonl(LEDGER))
    todo = [e for e in events if needs_draft(state.get(e["token"]), fetch=not args.no_fetch)]
    print(f"[draft] 事象 {len(events)} / 台帳済み {len(events) - len(todo)} / 下書き対象 {len(todo)}（取り直し未了の再試行を含む）")
    if args.limit:
        todo = todo[: args.limit]
    rc = 0
    for i, ev in enumerate(todo):
        refetched, st = ([], "skipped") if args.no_fetch else refetch_posts(ev["token"], ev["first_posted_at"])
        if st == "login_wall":
            return 1
        if st == "error":
            rc = 1
        bundles = bundle_posts(refetched)
        src_url = source_url_for_first_post(ev, refetched)
        pub = fetch_published_time(src_url) if (src_url and not args.no_fetch) else ""
        row = draft_row(ev, refetched, st, pub, src_url)
        if args.dry_run:
            print(json.dumps({k: row[k] for k in ("token", "first_posted_at_jst", "refetch_status", "delay_label")}, ensure_ascii=False))
        else:
            append_row(LEDGER, row)
        print(f"[ok] {ev['token']}: 取り直し {len(refetched)} 件（初出まで届いた={row['covers_first_post']}）・束 {len(bundles)}・遅れ {row['delay_label']} ({i + 1}/{len(todo)})")
        if not args.no_fetch and i < len(todo) - 1:
            time.sleep(random.uniform(20.0, 40.0))
    return rc


def cmd_list(_args) -> int:
    m = xsm.load()
    events = events_from_queue(read_jsonl(QUEUE), m)
    state = ledger_state(read_jsonl(LEDGER))
    cnt = Counter(r.get("status", "?") for r in state.values())
    print(f"事象（母集団） {len(events)} 語 / 台帳 {len(state)} 語: {dict(cnt)} / 未下書き {len([e for e in events if e['token'] not in state])}")
    for ev in events:
        r = state.get(ev["token"])
        codes = ",".join(b["code"] for s in ev["subjects"] for b in s["beneficiaries"])[:40]
        if r:
            h = r["human"]
            print(f"  {ev['token']:14s} {r['status']:6s} 初出 {r.get('first_posted_at_jst','')} 遅れ {r.get('delay_label','')} "
                  f"一次 {h.get('primary_source') or '-'} 出所 {h.get('source_label') or '-'} 範囲 {h.get('scope_judgement') or '-'} 受益 {codes}")
        else:
            print(f"  {ev['token']:14s} (未)    初出 {ev['first_posted_at']} 受益 {codes}")
    return 0


def cmd_fill(args) -> int:
    state = ledger_state(read_jsonl(LEDGER))
    prev = state.get(args.token)
    if prev is None:
        print(f"✗ 台帳に無い語: {args.token}（先に draft）", file=sys.stderr)
        return 2
    append_row(LEDGER, fill_row(prev, args.primary, args.label, args.scope, args.note or ""))
    print(f"[filled] {args.token}: 一次 {args.primary} 出所 {args.label} 範囲 {args.scope}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--no-fetch", action="store_true", help="取り直し・URL 取得をしない（骨だけ）")
    d.add_argument("--dry-run", action="store_true", help="台帳に書かない")
    d.add_argument("--limit", type=int, default=0)
    d.set_defaults(fn=cmd_draft)
    l = sub.add_parser("list")
    l.set_defaults(fn=cmd_list)
    f = sub.add_parser("fill")
    f.add_argument("--token", required=True)
    f.add_argument("--primary", required=True, choices=PRIMARY_CHOICES, help="一次ソース到達 ○/△/×")
    f.add_argument("--label", required=True, choices=LABEL_CHOICES, help="出所ラベル")
    f.add_argument("--scope", required=True, choices=SCOPE_CHOICES, help="投稿の主張と元報道の範囲")
    f.add_argument("--note", default="")
    f.set_defaults(fn=cmd_fill)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
