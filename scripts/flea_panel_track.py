#!/usr/bin/env python3
"""Yahoo!フリマ 固定パネルの日次追跡（出品ID単位・P-INF-19 オーナー裁定 a・2026-09-22）。

目的（オーナー原文）: 「Aという商品の ①値段が上がった ②売れない商品が増えた ③売買成立がすごく増えた」を
指数の無い消費財について測る。板は Yahoo!フリマ 1 枚。

流れ:
  configs/flea_panel.json のクエリごとに
    出品中（既定URL・先頭100件）と 売却済（?sold=1・先頭100件）の埋め込み JSON（__NEXT_DATA__）を取る
  → data/flea_panel/snapshots/<date>.jsonl に生の観測を残す（同日再実行は上書き・run 名=日付）
  → data/flea_panel/state.json（出品ID ごとの first_seen / last_seen_open / sold_seen / price）を更新
  → data/flea_panel/daily.jsonl に 1クエリ1日1行の要約を追記（同日行は置換）
      open_n / new_open / stale_n（stale_days 以上前に初見でまだ出品中）/ sold_new（今日初めて売却済で観測）
      / open_median / sold_median / sold_median_prev7（7日前の売却中央値・比較用）

読み方の限界（Codex レビュー 2026-09-22 P1-3）:
  各面は先頭 100 件だけ＝1日の成約が 100 件を超える商品では sold_new が頭打ち（sold_new_capped=true）。
  初回は全出品が new_open・全売却済が sold_new（基準日）。並び順の変化で過去の売却済が現れると sold_new に数わる。
  ＝ sold_new / stale_n は「観測窓内の初見売却済／継続出品」であり、市場全体の成約数・在庫数ではない。

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
def fetch_html(url: str, user_agent: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


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


def build_url(cfg: Dict[str, Any], query: str, sold: bool) -> str:
    url = cfg["search_url"].format(query=urllib.parse.quote(query))
    return f"{url}?{cfg['sold_param']}" if sold else url


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

    for q in cfg["queries"]:
        qid, query = q["id"], q["query"]
        row: Dict[str, Any] = {"date": today, "query_id": qid, "query": query, "group": q.get("group"),
                               "board": cfg["board"], "status": "ok"}
        try:
            open_raw = parse_items(fetch(build_url(cfg, query, sold=False), cfg["user_agent"]))
            time.sleep(cfg.get("request_interval_sec", 2.0))
            sold_raw = parse_items(fetch(build_url(cfg, query, sold=True), cfg["user_agent"]))
            time.sleep(cfg.get("request_interval_sec", 2.0))
        except Exception as e:  # noqa: BLE001 — 通信失敗は error 行として残す
            open_raw, sold_raw, row["error"] = None, None, f"fetch: {e}"

        open_items = [slim(i) for i in (open_raw or []) if i.get("itemStatus") == "OPEN"]
        sold_items = [slim(i) for i in (sold_raw or []) if i.get("itemStatus") == "SOLD"]
        if open_raw is None or sold_raw is None:
            row["status"] = "error"
            row.setdefault("error", "no __NEXT_DATA__ (page shape changed or blocked)")
        elif not open_items and not sold_items:
            # 生の件数ではなく OPEN/SOLD に絞った後で判定する（状態名の変更を「該当なし」として通さない）
            row["status"] = "error"
            row["error"] = (f"0 OPEN/SOLD items after status filter (raw open={len(open_raw)} sold={len(sold_raw)}; "
                            "treat as failure, not as 'no listings')")
        if row["status"] == "error":
            errors += 1
            daily.append(row)
            print(f"[ERROR] {qid}: {row['error']}", file=sys.stderr)
            continue

        for it in open_items + sold_items:
            snaps.append({"date": today, "query_id": qid, **it})
        row.update(update_state(state, qid, today, open_items, sold_items))
        row["stale_n"] = count_stale(state, qid, today, int(cfg.get("stale_days", 7)), open_items)
        row["sold_median_prev7"] = prev_value(daily, qid, today, 7, "sold_median")
        row["sold_new_prev7"] = prev_value(daily, qid, today, 7, "sold_new")
        daily.append(row)
        processed += 1
        print(f"[ok] {qid}: open={row['open_n']} new_open={row['new_open']} stale={row['stale_n']} "
              f"sold_new={row['sold_new']} open_med={row['open_median']} sold_med={row['sold_median']}")

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
