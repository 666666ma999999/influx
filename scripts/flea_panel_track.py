#!/usr/bin/env python3
"""Yahoo!フリマ 固定パネルの日次追跡（出品ID単位・P-INF-19 オーナー裁定 a・2026-09-22）。

目的（オーナー原文）: 「Aという商品の ①値段が上がった ②売れない商品が増えた ③売買成立がすごく増えた」を
指数の無い消費財について測る。板は Yahoo!フリマ 1 枚。

流れ（2026-09-23 改訂・敵対レビュー wf_f9df2ee3-706 A1: 既定の並び＝関連度順では売却済 100 件が毎日同じで成約が数えられなかった）:
  configs/flea_panel.json のクエリごとに、Yahoo!フリマ検索の埋め込み JSON（__NEXT_DATA__）から
    a) 出品中・既定順 1 ページ         → open_total（totalResultsAvailable＝在庫の総数）・open_median・出品 ID
    b) 出品中・新しい順（sort=openTime&order=desc）を前日 0:00 JST まで頁送り → new_open_d1（前日の新規出品数）
    c) 売却済・売却日時の新しい順（sold=1&sort=endTime&order=desc）を前日 0:00 まで頁送り
                                       → sold_d1（前日の成約数）・sold_d1_median（前日成約の中央値）・sold_total
  → data/flea_panel/snapshots/<date>.jsonl に生の観測（同日再実行は上書き・run 名=日付）
  → data/flea_panel/state.json（出品ID ごとの first_seen / last_seen_open / sold_seen / price / relisted）
  → data/flea_panel/daily.jsonl に 1クエリ1日1行（同日行は置換）
      3 指標: ①値上がり= sold_d1_median（＋open_median）／②売れ残り= open_total の推移と sold_d1_over_open_total（= 絞った前日成約 / 絞り無しの総在庫・母集団が違う目安値）
             ／③成約急増= sold_d1（前日比は prev7 列）
  「前日」= --date の前の暦日（JST）。21:40 の定期実行は前日 1 日分を確定値として記録する。

読み方の限界:
  頁送りは max_pages（既定 5 頁= 500 件）まで＝前日の成約が 500 件超の商品では sold_d1 が頭打ち（sold_d1_capped=true）。
  出品中の openTime 順は関連度と混ざる（厳密な降順でない）＝ new_open_d1 は max_pages 内に見えた前日出品の数（下限値）。
  売れ残りは open_total（総在庫）と sold_d1_over_open_total で読む。sold_d1_capped=true の行の sold_d1 は下限値（prev7 側にも capped 列あり）。stale_n（既定順 100 件中の 7 日以上前初見）は補助。
  検索語の同定はタイトルの必須語/除外語（config の must / exclude・省略可）で絞る。無い語はノイズ混入あり（RTX 5090 に PC 本体等）。

fail-closed:
  埋め込み JSON が無い・OPEN/SOLD に絞った後で両面 0 件 → そのクエリは status=error で記録し exit 2
  （2026-09-20/22 実測: 既定URLは出品中だけを返す。売却済 0 件を「該当なし」として静かに通した pokeca 取得器の穴を塞ぐ）

実行（Docker）: docker compose run --rm xstock python scripts/flea_panel_track.py [--date YYYY-MM-DD] [--config PATH]
selftest:      python3 -m unittest tests.test_flea_panel_track
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "configs" / "flea_panel.json"
DATA_DIR = ROOT / "data" / "flea_panel"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


# ---------------------------------------------------------------- 取得・解析
RETRY_WAITS = (60, 180, 420)   # 429（Too Many Requests）時の待ち秒・2026-09-23 実走で 3 クエリ目から 429 が出た


def fetch_html(url: str, user_agent: str, timeout: int = 30) -> str:
    """HTTP GET。429 は RETRY_WAITS の回数だけ待って再試行し、それでも 429 なら例外（error 行として記録される）。"""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Language": "ja"})
    for attempt, wait in enumerate((*RETRY_WAITS, None)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait is None:
                raise
            print(f"[429] rate limited; waiting {wait}s (attempt {attempt + 1}/{len(RETRY_WAITS)})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def parse_items(html: str) -> Optional[List[Dict[str, Any]]]:
    """__NEXT_DATA__ から商品配列を取り出す。埋め込みが無ければ None（0件とは区別する）。"""
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    found: List[Dict[str, Any]] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "itemStatus" in o and "price" in o and "id" in o:
                found.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return found


def slim(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "price": item.get("price"),
        "status": item.get("itemStatus"),
        "open_time": item.get("openTime"),
        "end_time": item.get("endTime"),
        "condition": item.get("condition"),
        "is_price_down": bool(item.get("isPriceDown")),
    }


def build_url(cfg: Dict[str, Any], query: str, sold: bool, sort: Optional[str] = None,
              order: Optional[str] = None, page: int = 1) -> str:
    url = cfg["search_url"].format(query=urllib.parse.quote(query))
    params: List[str] = []
    if sold:
        params.append(cfg["sold_param"])
    if sort:
        params.append(f"sort={sort}&order={order or 'desc'}")
    if page > 1:
        params.append(f"page={page}")
    return f"{url}?{'&'.join(params)}" if params else url


def parse_total(html: str) -> Optional[int]:
    """検索結果の総件数（search.result.totalResultsAvailable）。無ければ None。

    正規表現の先頭一致は別モジュール（auctionItemsModule 等）の 0 を拾う実害があったので JSON の経路で取る。
    """
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        v = d["props"]["initialState"]["searchState"]["search"]["result"].get("totalResultsAvailable")
        return int(v) if v is not None else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None   # 経路が変わったら取得不能として扱う（先頭一致に戻すと別モジュールの 0 を拾う）


def title_ok(title: Optional[str], q: Dict[str, Any]) -> bool:
    """config の must（全部含む）/ exclude（どれも含まない）でタイトルを絞る。両方無ければ常に True。"""
    t = (title or "").lower()
    must = [w.lower() for w in q.get("must", [])]
    excl = [w.lower() for w in q.get("exclude", [])]
    return all(w in t for w in must) and not any(w in t for w in excl)


def parse_ts(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def fetch_window(cfg: Dict[str, Any], q: Dict[str, Any], sold: bool, sort_key: str, start: datetime, end: datetime,
                 fetch, monotonic: bool = True) -> Dict[str, Any]:
    """新しい順に頁送りし、時刻 sort_key（openTime / endTime）が [start, end) の商品を集める。

    monotonic=True（売却済の endTime 順・2026-09-23 実測で厳密に降順）: start より古い商品が出た頁で止める。
    monotonic=False（出品中の openTime 順・実測では関連度と混ざり降順でない＝2025-10 の出品が 1 頁目に混在）:
      古い商品で止めず max_pages まで読み、窓内の件数を数える。
    end 以降（今日分）は読み飛ばす。max_pages で頭打ち（capped＝最終頁にも窓内があった）。
    返り値: items（窓内・タイトル絞り込み後）・total・capped・pages。
    """
    max_pages = int(cfg.get("max_pages", 5))
    interval = float(cfg.get("request_interval_sec", 2.0))
    items: List[Dict[str, Any]] = []
    seen_ids: set = set()
    total: Optional[int] = None
    capped = False
    pages = 0
    for page in range(1, max_pages + 1):
        html = fetch(build_url(cfg, q["query"], sold=sold, sort=sort_key, order="desc", page=page), cfg["user_agent"])
        time.sleep(interval)
        raw = parse_items(html)
        pages += 1
        if raw is None:
            raise RuntimeError(f"no __NEXT_DATA__ on page {page} ({'sold' if sold else 'open'} by {sort_key})")
        if total is None:
            total = parse_total(html)
        reached_start = False
        in_window_this_page = 0
        for it in raw:
            ts = parse_ts(it.get(sort_key))
            if ts is None:
                continue
            if ts >= end:
                continue
            if ts < start:
                if monotonic:
                    reached_start = True
                    break
                continue
            in_window_this_page += 1
            if it.get("id") in seen_ids:      # 頁送り中の新着挿入で同じ ID が次頁に流れる＝重複計上を防ぐ
                continue
            seen_ids.add(it.get("id"))
            if title_ok(it.get("title"), q):
                items.append(slim(it))
        if reached_start or len(raw) < 100:
            break
        if page == max_pages:
            # 開始境界にも結果末尾にも届かず頁上限で終了＝窓内の件数は下限値（窓内 0 件でも capped）
            capped = True
    return {"items": items, "total": total, "capped": capped, "pages": pages}


# ---------------------------------------------------------------- 状態更新・要約
def median(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return float(statistics.median(xs)) if xs else None


def update_state(state: Dict[str, Dict[str, Any]], qid: str, today: str,
                 open_items: List[Dict[str, Any]], sold_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """出品ID ごとの状態を更新し、その日の要約（母集団= 各面の先頭100件）を返す。

    件数は state の日付から数える（first_seen == today / sold_seen == today）＝同日に何度再実行しても同じ値。
    SOLD 済みの ID が再び OPEN で観測されたら再出品として sold_seen を解除し relisted を +1（初見日も今日に更新）。
    同じ ID が両面に出た日は SOLD を優先する（OPEN を先に処理し、SOLD が上書き）。
    """
    for it in open_items:
        key = f"{qid}:{it['id']}"
        rec = state.get(key)
        if rec is None:
            state[key] = {"query_id": qid, "id": it["id"], "title": it["title"], "first_seen": today,
                          "last_seen_open": today, "sold_seen": None, "price": it["price"],
                          "first_price": it["price"], "relisted": 0}
        else:
            if rec.get("sold_seen") is not None and rec["sold_seen"] != today:
                rec["relisted"] = rec.get("relisted", 0) + 1     # 再出品＝新しい世代として数え直す
                rec["sold_seen"] = None
                rec["first_seen"] = today
                rec["first_price"] = it["price"]
            rec["last_seen_open"] = today
            rec["price"] = it["price"]
    for it in sold_items:
        key = f"{qid}:{it['id']}"
        rec = state.get(key)
        if rec is None:
            state[key] = {"query_id": qid, "id": it["id"], "title": it["title"], "first_seen": today,
                          "last_seen_open": None, "sold_seen": today, "price": it["price"],
                          "first_price": it["price"], "relisted": 0}
        elif rec.get("sold_seen") is None:
            rec["sold_seen"] = today
            rec["price"] = it["price"]
    open_ids = {f"{qid}:{it['id']}" for it in open_items}
    sold_ids = {f"{qid}:{it['id']}" for it in sold_items}
    new_open = sum(1 for k in open_ids if state[k]["first_seen"] == today and state[k].get("sold_seen") is None)
    sold_new_keys = [k for k in sold_ids if state[k].get("sold_seen") == today]
    return {
        "open_n": len(open_items),
        "new_open": new_open,
        "sold_n_top100": len(sold_items),
        "sold_new": len(sold_new_keys),
        "sold_new_capped": len(sold_new_keys) >= 100,
        "open_median": median([it["price"] for it in open_items]),
        "sold_median": median([it["price"] for it in sold_items]),
        "sold_new_median": median([state[k]["price"] for k in sold_new_keys]),
    }


def count_stale(state: Dict[str, Dict[str, Any]], qid: str, today: str, stale_days: int,
                open_items: List[Dict[str, Any]]) -> int:
    """stale_days 以上前に初見で、**今回の観測**で出品中にあり、まだ売れていない出品の数（= 売れ残り）。

    state の last_seen_open ではなく今回の OPEN 集合で絞る（同日再実行で表示が入れ替わっても最新の観測だけを数える）。
    """
    cutoff = (date.fromisoformat(today) - timedelta(days=stale_days)).isoformat()
    n = 0
    for it in open_items:
        r = state.get(f"{qid}:{it['id']}")
        if r and r.get("sold_seen") is None and r["first_seen"] <= cutoff:
            n += 1
    return n


def prev_value(daily_rows: List[Dict[str, Any]], qid: str, today: str, days_back: int, field: str) -> Optional[float]:
    target = (date.fromisoformat(today) - timedelta(days=days_back)).isoformat()
    for r in daily_rows:
        if r.get("query_id") == qid and r.get("date") == target:
            return r.get(field)
    return None


# ---------------------------------------------------------------- 入出力
def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def atomic_write_text(path: Path, text: str) -> None:
    """一時ファイルに書いて os.replace（途中失敗で正本が壊れない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


# ---------------------------------------------------------------- main
def run(cfg: Dict[str, Any], today: str, data_dir: Path, fetch=fetch_html) -> int:
    state_path = data_dir / "state.json"
    daily_path = data_dir / "daily.jsonl"
    snap_path = data_dir / "snapshots" / f"{today}.jsonl"
    state: Dict[str, Dict[str, Any]] = load_json(state_path, {})
    daily = [r for r in load_jsonl(daily_path) if r.get("date") != today]  # 同日再実行は置換
    snaps: List[Dict[str, Any]] = []
    errors = 0
    planned = len(cfg["queries"])
    processed = 0

    jst = timezone(timedelta(hours=9))
    day = datetime.fromisoformat(today).replace(tzinfo=jst)
    win_start, win_end = day - timedelta(days=1), day          # 「前日」= [D-1 00:00, D 00:00) JST
    interval = float(cfg.get("request_interval_sec", 2.0))

    for q in cfg["queries"]:
        qid, query = q["id"], q["query"]
        row: Dict[str, Any] = {"date": today, "query_id": qid, "query": query, "group": q.get("group"),
                               "board": cfg["board"], "window": f"{win_start.date()}", "status": "ok"}
        try:
            open_html = fetch(build_url(cfg, query, sold=False), cfg["user_agent"])
            time.sleep(interval)
            open_raw = parse_items(open_html)
            if open_raw is None:
                raise RuntimeError("no __NEXT_DATA__ (open, default order)")
            open_total = parse_total(open_html)
            new_w = fetch_window(cfg, q, sold=False, sort_key="openTime", start=win_start, end=win_end, fetch=fetch,
                                 monotonic=False)
            sold_w = fetch_window(cfg, q, sold=True, sort_key="endTime", start=win_start, end=win_end, fetch=fetch)
        except Exception as e:  # noqa: BLE001 — 通信・構造の失敗は error 行として残す
            row["status"], row["error"] = "error", f"{e}"
            errors += 1
            daily.append(row)
            print(f"[ERROR] {qid}: {row['error']}", file=sys.stderr)
            continue

        open_items = [slim(i) for i in open_raw if i.get("itemStatus") == "OPEN" and title_ok(i.get("title"), q)]
        sold_items = [i for i in sold_w["items"] if i.get("status") == "SOLD"]
        if not open_items and not sold_items:
            # 生の件数ではなく絞った後で判定する（状態名の変更・遮断・きつすぎる必須語を「該当なし」として通さない）
            row["status"] = "error"
            row["error"] = (f"0 OPEN/SOLD items after filter (open_total={open_total}, "
                            f"(raw open={len(open_raw)}; treat as failure, not as 'no listings')")
            errors += 1
            daily.append(row)
            print(f"[ERROR] {qid}: {row['error']}", file=sys.stderr)
            continue

        for it in open_items + sold_items:
            snaps.append({"date": today, "query_id": qid, **it})
        row.update(update_state(state, qid, today, open_items, sold_items))
        row["stale_n"] = count_stale(state, qid, today, int(cfg.get("stale_days", 7)), open_items)
        # 3 指標の本体（2026-09-23 改訂）
        row["open_total"] = open_total
        row["sold_total"] = sold_w["total"]
        row["new_open_d1"] = len(new_w["items"])
        row["new_open_d1_capped"] = new_w["capped"]
        row["sold_d1"] = len(sold_items)
        row["sold_d1_capped"] = sold_w["capped"]
        row["sold_d1_median"] = median([it["price"] for it in sold_items])
        # 分子= 同定欄で絞った前日成約・分母= 検索結果の総在庫（絞り無し）＝母集団が違う比率。傾向の目安にだけ使う
        row["sold_d1_over_open_total"] = (round(len(sold_items) / open_total, 4) if open_total else None)
        row["pages_fetched"] = 1 + new_w["pages"] + sold_w["pages"]
        row["sold_d1_prev7"] = prev_value(daily, qid, today, 7, "sold_d1")
        row["sold_d1_capped_prev7"] = prev_value(daily, qid, today, 7, "sold_d1_capped")
        row["sold_d1_median_prev7"] = prev_value(daily, qid, today, 7, "sold_d1_median")
        row["open_total_prev7"] = prev_value(daily, qid, today, 7, "open_total")
        daily.append(row)
        processed += 1
        print(f"[ok] {qid}: open_total={open_total} new_open_d1={row['new_open_d1']} sold_d1={row['sold_d1']}"
              f"{'(capped)' if sold_w['capped'] else ''} sold_d1_med={row['sold_d1_median']} "
              f"sold/open_total={row['sold_d1_over_open_total']} open_med={row['open_median']}")

    # 書き順= 生観測 → 日次 → state（全て一時ファイル→置換）。件数は state の日付から導くので途中失敗後の再実行でも同じ値になる
    write_jsonl(snap_path, snaps)
    write_jsonl(daily_path, daily)
    atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, indent=0))
    print(f"--- flea_panel {today}: queries_planned={planned} processed={processed} errors={errors} "
          f"state_ids={len(state)} snapshot_rows={len(snaps)}")
    return 2 if errors else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # 既定は日本時間の今日（Docker コンテナの時計は UTC＝深夜 0〜9 時 JST に走ると前日扱いになる・2026-09-23 launchd 実走で実害）
    jst_today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    ap.add_argument("--date", default=jst_today, help="観測日（既定= 今日・JST）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    a = ap.parse_args(argv)
    cfg = load_json(Path(a.config), None)
    if not cfg:
        print(f"[FATAL] config not found: {a.config}", file=sys.stderr)
        return 2
    return run(cfg, a.date, Path(a.data_dir))


if __name__ == "__main__":
    sys.exit(main())
