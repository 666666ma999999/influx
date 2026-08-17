#!/usr/bin/env python3
"""海外上場カードの前向き記録・評価（§16w・2026-08-17 P-08c 裁定）。

日本株の前向き検定（`price_watch_forward.py`・対TOPIX超過・事前登録 n>=100）とは
**別台帳・別ベンチマーク**で扱う。基準指数も営業日も違うものを同じ分母に入れると
検定が壊れるため（ピークアウト §16j・浸透 §16v と同じレーン分離の裁定）。

台帳は append-only（`data/price_watch/foreign_forward_log.jsonl`）。行の型は3つ:

- `firing`     … 発火の記録。1日1銘柄1行にまとめ、鳴った系列は `series_ids` に全部入れる
                 （同日に複数系列が同じ銘柄へ鳴っても帰属を失わない）
- `backfill`   … firing 時に価格が取れなかった行を、後日埋め直した記録（過去行は書き換えない）
- `evaluation` … 8/15 取引日後の超過リターン（銘柄リターン − ベンチマークリターン）

価格は yfinance（Docker イメージ同梱）。**発火日以前の直近終値**を使い、実際に採用した日付を
`entry_date_used` に残す（発火後の値を掴む look-ahead を避ける）。取得失敗は fail-soft。

実行:
    python3 scripts/foreign_forward.py --selftest   # ネットワーク不要の固定テスト
    python3 scripts/foreign_forward.py --evaluate   # 期日が来た行を評価
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "data" / "price_watch" / "foreign_forward_log.jsonl"
SPEC_VERSION = "foreign-v2"
# 日本株レーンと同じ評価窓（8週=40取引日 / 15週=75取引日）。数えるのは当該銘柄の取引日
WINDOWS_BD = {"w8": 40, "w15": 75}

# (日付, 終値) の列を返す関数。テストでは差し替える
HistoryFn = Callable[[str], List[Tuple[str, float]]]


def foreign_cards(series: Dict[str, Any]) -> List[Dict[str, Any]]:
    """§16w の要件を満たす海外カードだけを返す。

    要件: market が JP 以外 / sign='+' / tier が confirmed|provisional /
    ticker と benchmark の両方を持つ（欠けたものは価格も評価もできないので除外）。

    Args:
        series: configs/price_universe_sources.json の系列 dict。

    Returns:
        条件を満たすカードのリスト。
    """
    out: List[Dict[str, Any]] = []
    for card in series.get("beneficiaries") or []:
        if not isinstance(card, dict):
            continue
        if (card.get("market") or "JP") == "JP":
            continue
        if card.get("sign") != "+" or card.get("tier") not in ("confirmed", "provisional"):
            continue
        if not card.get("ticker") or not card.get("benchmark"):
            continue
        out.append(card)
    return out


def read_log(path: Path = LEDGER_PATH) -> List[Dict[str, Any]]:
    """台帳を読む（無ければ空）。"""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def append_row(row: Dict[str, Any], path: Path = LEDGER_PATH) -> None:
    """台帳へ1行追記する（過去行は書き換えない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def latest_prices(rows: List[Dict[str, Any]], fire_date: str, ticker: str) -> Dict[str, Any]:
    """firing + backfill を畳んで、その発火の最新の価格状態を返す。

    Returns:
        {"exists": bool, "entry_close": float|None, "benchmark_entry": float|None,
         "entry_date_used": str|None}
    """
    state: Dict[str, Any] = {"exists": False, "entry_close": None, "benchmark_entry": None,
                             "entry_date_used": None, "benchmark": None, "card": None}
    rel = [r for r in rows
           if r.get("fire_date") == fire_date and r.get("ticker") == ticker
           and r.get("type") in ("firing", "backfill")]
    if not rel:
        return state
    # カードの benchmark が後から変わった場合、**指数が違う行の価格は混ぜない**
    # （旧指数のエントリー値と新指数の評価値を突き合わせる事故を防ぐ・Codex 5審 #3）
    bench = rel[-1].get("benchmark")
    state["exists"] = True
    state["benchmark"] = bench
    state["card"] = rel[-1]
    for r in rel:
        if r.get("benchmark") != bench:
            continue
        for k in ("entry_close", "benchmark_entry", "entry_date_used"):
            if r.get(k) is not None:
                state[k] = r[k]
    return state


def needs_backfill(state: Dict[str, Any]) -> bool:
    """記録済みだが価格が欠けている（＝埋め直す対象）か。"""
    return bool(state["exists"]) and (
        state["entry_close"] is None or state["benchmark_entry"] is None
    )


def yf_history(ticker: str, start: str, end: str) -> List[Tuple[str, float]]:
    """yfinance から (日付, 終値) の列を取る。失敗時は空（fail-soft）。"""
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
        if hist is None or hist.empty:
            return []
        return [(idx.strftime("%Y-%m-%d"), float(row["Close"]))
                for idx, row in hist.iterrows()]
    except Exception:  # noqa: BLE001  価格取得の失敗で本線を止めない
        return []


def close_on_or_before(hist: List[Tuple[str, float]], day: str) -> Tuple[Optional[float], Optional[str]]:
    """`day` 以前の直近終値と、その日付を返す（look-ahead を作らない）。"""
    cands = [(d, v) for d, v in hist if d <= day]
    if not cands:
        return None, None
    d, v = max(cands, key=lambda x: x[0])
    return v, d


def close_after_bdays(hist: List[Tuple[str, float]], entry_day: str, n: int
                      ) -> Tuple[Optional[float], Optional[str]]:
    """entry_day の n 取引日後の終値と日付を返す（その銘柄の取引日で数える）。"""
    days = sorted({d for d, _ in hist})
    if entry_day not in days:
        return None, None
    idx = days.index(entry_day) + n
    if idx >= len(days):
        return None, None
    target = days[idx]
    price = dict(hist).get(target)
    return (price, target) if price is not None else (None, None)


def record_firings(alerts: List[Tuple[Dict, Dict, List[str]]], fire_date: str,
                   path: Path = LEDGER_PATH,
                   history_fn: Optional[Callable[[str, str, str], List[Tuple[str, float]]]] = None
                   ) -> int:
    """発火した系列の海外カードを別台帳へ記録する（1日1銘柄1行・欠けた価格は後日 backfill）。

    Args:
        alerts: (series, row, triggers) のリスト（price_universe_check と同じ形）。
        fire_date: 発火日 "YYYY-MM-DD"。
        path: 台帳パス。
        history_fn: 価格取得関数（テスト差し替え用）。

    Returns:
        追記した行数（firing + backfill）。
    """
    fetch_hist = history_fn or yf_history
    rows = read_log(path)
    n = 0

    # 同日に複数系列が同じ銘柄へ鳴っても帰属を失わないよう、銘柄単位に畳んでから書く
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for series, row, triggers in alerts:
        for card in foreign_cards(series):
            t = str(card["ticker"])
            slot = by_ticker.setdefault(t, {"card": card, "series_ids": [], "triggers": [],
                                            "commodity": {}})
            slot["series_ids"].append(series.get("id"))
            slot["triggers"].extend(f"{series.get('id')}:{x}" for x in triggers)
            slot["commodity"][series.get("id")] = {
                "value": row.get("value"), "weekly_pct": row.get("weekly_pct")}

    start = (datetime.strptime(fire_date, "%Y-%m-%d")).strftime("%Y-%m-%d")
    for ticker, slot in by_ticker.items():
        card = slot["card"]
        state = latest_prices(rows, fire_date, ticker)
        if state["exists"] and not needs_backfill(state):
            continue  # 価格まで揃っている＝二重計上しない

        # 発火日以前の直近終値を使う（休場日に発火後の値を掴まない）
        px, px_day, bench_px = _entry_prices(fetch_hist, ticker, str(card["benchmark"]), start)
        if state["exists"]:
            # 既存行がある＝価格の穴埋め目的。情報が増えない再実行では追記しない（Codex 6審 #1）
            adds = (px is not None and state["entry_close"] is None) or \
                   (bench_px is not None and state["benchmark_entry"] is None)
            if not adds:
                continue

        kind = "backfill" if state["exists"] else "firing"
        rec = {
            "type": kind, "spec_version": SPEC_VERSION,
            "fire_date": fire_date,
            "series_ids": sorted(set(slot["series_ids"])),
            "ticker": ticker, "market": card.get("market"),
            "name": card.get("name"), "tier": card.get("tier"),
            "benchmark": card.get("benchmark"),
            "entry_close": px, "benchmark_entry": bench_px, "entry_date_used": px_day,
            "triggers": slot["triggers"], "commodity": slot["commodity"],
            "windows_bd": WINDOWS_BD,
            "note": ("§16w 別レーン。対TOPIX検定には含めない。"
                     "価格が null の行は後日 backfill 行で埋める"),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        append_row(rec, path)
        rows.append(rec)
        n += 1
        shown = px if px is not None else "価格未取得"
        print(f"[foreign] {kind}: [{card.get('market')}]{card.get('name') or ticker} "
              f"基準={fire_date}(採用{px_day}) 終値={shown}")
    return n


def _shift_days(day: str, delta: int) -> str:
    from datetime import timedelta
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=delta)).strftime("%Y-%m-%d")


def _entry_prices(fetch_hist, ticker: str, benchmark: str, fire_date: str
                  ) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    """エントリー価格の組を返す。**指数は銘柄の採用日に合わせる**（Codex 6審 #2）。

    銘柄と指数をそれぞれ独立に「発火日以前の直近」で取ると、市場の休場差で
    比較期間の起点がずれ、超過リターンが歪む。銘柄の採用日を決めてから指数を揃える。

    Returns:
        (銘柄終値, 採用日, 指数終値)。銘柄が取れなければ指数も採らない。
    """
    win_start, win_end = _shift_days(fire_date, -12), _shift_days(fire_date, 2)
    px, px_day = close_on_or_before(fetch_hist(ticker, win_start, win_end), fire_date)
    if px is None or px_day is None:
        return None, None, None
    bench_px, _ = close_on_or_before(fetch_hist(benchmark, win_start, win_end), px_day)
    return px, px_day, bench_px


def backfill_missing(path: Path = LEDGER_PATH,
                     history_fn: Optional[Callable[[str, str, str], List[Tuple[str, float]]]] = None
                     ) -> int:
    """台帳を走査し、価格が欠けたままの過去の発火を埋める（Codex 5審 #1）。

    `record_firings` は当日の発火しか見ないため、取得に失敗した過去行は
    そのままでは永久に null で残る。本関数が全期間を再訪する。

    Returns:
        追記した backfill 行数。
    """
    fetch_hist = history_fn or yf_history
    rows = read_log(path)
    keys = []
    seen = set()
    for r in rows:
        if r.get("type") not in ("firing", "backfill"):
            continue
        k = (r.get("fire_date"), r.get("ticker"))
        if k not in seen:
            seen.add(k)
            keys.append(k)
    n = 0
    for fire_date, ticker in keys:
        state = latest_prices(rows, fire_date, ticker)
        if not needs_backfill(state):
            continue
        card = state["card"] or {}
        px, px_day, bench_px = _entry_prices(
            fetch_hist, ticker, str(state["benchmark"]), fire_date)
        # 追記して情報が増える時だけ書く。片側だけ永遠に欠測する銘柄で
        # 毎回同じ行を積むと台帳が無制限に膨らむ（Codex 6審 #1）
        adds_entry = px is not None and state["entry_close"] is None
        adds_bench = bench_px is not None and state["benchmark_entry"] is None
        if not adds_entry and not adds_bench:
            continue
        rec = {
            "type": "backfill", "spec_version": SPEC_VERSION,
            "fire_date": fire_date, "series_ids": card.get("series_ids"),
            "ticker": ticker, "market": card.get("market"), "name": card.get("name"),
            "tier": card.get("tier"), "benchmark": state["benchmark"],
            "entry_close": px, "benchmark_entry": bench_px, "entry_date_used": px_day,
            "windows_bd": card.get("windows_bd") or WINDOWS_BD,
            "note": "§16w 価格の後追い取得（過去の発火を再訪して埋めた行）",
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        append_row(rec, path)
        rows.append(rec)
        n += 1
        print(f"[foreign] backfill(再訪): {ticker} 基準={fire_date} 終値={px}")
    return n


def evaluate(today: str, path: Path = LEDGER_PATH,
             history_fn: Optional[Callable[[str, str, str], List[Tuple[str, float]]]] = None
             ) -> int:
    """期日が来た発火を評価し、超過リターンを追記する。

    指標 = 銘柄リターン − ベンチマークリターン（カードの benchmark）。
    まだ取引日が足りない窓は書かず、次回に持ち越す（期日前に結論を焼かない）。

    Returns:
        追記した evaluation 行数。
    """
    fetch_hist = history_fn or yf_history
    rows = read_log(path)
    done = {(r.get("fire_date"), r.get("ticker"), r.get("window"))
            for r in rows if r.get("type") == "evaluation"}
    n = 0
    for r in rows:
        if r.get("type") not in ("firing", "backfill"):
            continue
        fire_date, ticker = r.get("fire_date"), r.get("ticker")
        state = latest_prices(rows, fire_date, ticker)
        entry, bench_entry = state["entry_close"], state["benchmark_entry"]
        entry_day = state["entry_date_used"]
        if entry is None or bench_entry is None or not entry_day:
            continue  # 価格未取得は評価しない（backfill 待ち）
        bench_id = state["benchmark"]  # エントリー値と同じ指数を使う（途中変更を混ぜない）
        hist = fetch_hist(ticker, _shift_days(entry_day, -5), _shift_days(today, 1))
        bhist = fetch_hist(str(bench_id), _shift_days(entry_day, -5), _shift_days(today, 1))
        for win, bd in (r.get("windows_bd") or WINDOWS_BD).items():
            if (fire_date, ticker, win) in done:
                continue
            px, day = close_after_bdays(hist, entry_day, bd)
            if px is None or day is None:
                continue  # 期日未到来 or 取得不能 → 次回へ
            # 指数は**銘柄の評価日時点**を採る。指数側で独立に40/75日を数えると
            # 休場日の違いで別の暦日どうしを引き算してしまう（Codex 5審 #2）
            bpx, _bday = close_on_or_before(bhist, day)
            if bpx is None:
                continue
            ret = (px / entry - 1) * 100
            bret = (bpx / bench_entry - 1) * 100
            append_row({
                "type": "evaluation", "spec_version": SPEC_VERSION,
                "fire_date": fire_date, "ticker": ticker, "window": win,
                "eval_day": day, "close": px, "benchmark_close": bpx,
                "ret_pct": round(ret, 2), "benchmark_ret_pct": round(bret, 2),
                "excess_pct": round(ret - bret, 2),
                "benchmark": bench_id, "benchmark_eval_day": _bday,
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }, path)
            done.add((fire_date, ticker, win))
            n += 1
            print(f"[foreign] 評価 {ticker} {win}: 超過 {ret - bret:+.1f}% "
                  f"（銘柄 {ret:+.1f}% / {r.get('benchmark')} {bret:+.1f}%）")
    return n


def selftest() -> int:
    """ネットワーク不要の固定テスト（カード選別・畳み込み・backfill・評価）。"""
    import tempfile

    cases: List[Tuple[str, bool]] = []
    s = {"id": "dry-whey", "beneficiaries": [
        {"code": "5713", "sign": "+", "tier": "confirmed"},
        {"code": "A", "market": "IE", "ticker": "GL9.IR", "benchmark": "^ISEQ",
         "sign": "+", "tier": "provisional"},
        {"code": "B", "market": "US", "ticker": "X", "sign": "+", "tier": "confirmed"},
        {"code": "C", "market": "US", "benchmark": "^GSPC", "sign": "+", "tier": "confirmed"},
        {"code": "D", "market": "US", "ticker": "Y", "benchmark": "^GSPC",
         "sign": "+", "tier": "rejected"},
        {"code": "E", "market": "US", "ticker": "Z", "benchmark": "^GSPC",
         "sign": "-", "tier": "confirmed"},
    ]}
    picked = [c["code"] for c in foreign_cards(s)]
    cases.append(("JP は対象外", "5713" not in picked))
    cases.append(("要件を満たす海外カードのみ採用", picked == ["A"]))
    cases.append(("ticker/benchmark 欠落は除外", "B" not in picked and "C" not in picked))
    cases.append(("rejected/逆風は除外", "D" not in picked and "E" not in picked))
    cases.append(("beneficiaries 欠落でも落ちない", foreign_cards({}) == []))

    # look-ahead 防止: 発火日が休場でも「以前の直近」を採る
    hist = [("2026-08-13", 10.0), ("2026-08-14", 11.0), ("2026-08-18", 12.0)]
    px, day = close_on_or_before(hist, "2026-08-15")
    cases.append(("休場日は直近の過去終値を採用", (px, day) == (11.0, "2026-08-14")))
    cases.append(("発火日より後の値を掴まない", day is not None and day <= "2026-08-15"))
    cases.append(("履歴が全て未来なら None", close_on_or_before(hist, "2026-08-01") == (None, None)))
    cases.append(("n取引日後を銘柄の営業日で数える",
                  close_after_bdays(hist, "2026-08-13", 2) == (12.0, "2026-08-18")))
    cases.append(("期日未到来は None", close_after_bdays(hist, "2026-08-14", 5) == (None, None)))

    # 台帳まわり（一時ファイル）
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "log.jsonl"
        prices = {
            "GL9.IR": [("2026-08-17", 100.0), ("2026-08-18", 110.0)],
            "^ISEQ": [("2026-08-17", 1000.0), ("2026-08-18", 1050.0)],
        }
        def fake(t: str, a: str, b: str) -> List[Tuple[str, float]]:
            return prices.get(t, [])
        def none_fn(t: str, a: str, b: str) -> List[Tuple[str, float]]:
            return []

        series_a = {"id": "dry-whey", "jp": "ホエイ", "beneficiaries": s["beneficiaries"]}
        series_b = {"id": "milk", "jp": "生乳", "beneficiaries": s["beneficiaries"]}
        alerts = [(series_a, {"value": 1}, ["weekly +6%"]),
                  (series_b, {"value": 2}, ["weekly +7%"])]

        # 価格が取れない状況で記録 → firing 1行・価格 null
        n1 = record_firings(alerts, "2026-08-17", p, none_fn)
        rows = read_log(p)
        fire = [r for r in rows if r["type"] == "firing"]
        cases.append(("同日同銘柄は1行に畳む", n1 == 1 and len(fire) == 1))
        cases.append(("複数系列の帰属を保持", fire[0]["series_ids"] == ["dry-whey", "milk"]))
        cases.append(("価格未取得は null で残す", fire[0]["entry_close"] is None))

        # 価格が取れるようになったら backfill される（前審#1）
        n2 = record_firings(alerts, "2026-08-17", p, fake)
        back = [r for r in read_log(p) if r["type"] == "backfill"]
        cases.append(("欠けた価格は backfill で埋まる",
                      n2 == 1 and len(back) == 1 and back[0]["entry_close"] == 100.0))

        # 価格が揃った後は二重記録しない
        n3 = record_firings(alerts, "2026-08-17", p, fake)
        cases.append(("価格が揃えば再記録しない", n3 == 0))

        # 評価（1取引日後を窓に見立てる）
        rows = read_log(p)
        for r in rows:
            if r["type"] in ("firing", "backfill"):
                r["windows_bd"] = {"w1": 1}
        p2 = Path(td) / "log2.jsonl"
        for r in rows:
            append_row(r, p2)
        n4 = evaluate("2026-08-18", p2, fake)
        ev = [r for r in read_log(p2) if r["type"] == "evaluation"]
        cases.append(("評価行が書かれる", n4 >= 1 and len(ev) >= 1))
        if ev:
            # 銘柄 +10% / ベンチ +5% → 超過 +5%
            cases.append(("超過リターン = 銘柄 − ベンチ", ev[0]["excess_pct"] == 5.0))
            cases.append(("評価は自国指数で測る", ev[0]["benchmark"] == "^ISEQ"))
        n5 = evaluate("2026-08-18", p2, fake)
        cases.append(("同じ窓を二重評価しない", n5 == 0))

        # Codex 5審 #1: 過去の発火を再訪して埋める（当日 alerts が無くても動く）
        p3 = Path(td) / "log3.jsonl"
        append_row({"type": "firing", "fire_date": "2026-08-17", "ticker": "GL9.IR",
                    "benchmark": "^ISEQ", "market": "IE", "name": "Glanbia plc",
                    "tier": "provisional", "series_ids": ["dry-whey"],
                    "entry_close": None, "benchmark_entry": None,
                    "entry_date_used": None, "windows_bd": {"w1": 1}}, p3)
        nb = backfill_missing(p3, fake)
        st = latest_prices(read_log(p3), "2026-08-17", "GL9.IR")
        cases.append(("過去発火の再訪で価格が埋まる",
                      nb == 1 and st["entry_close"] == 100.0))
        cases.append(("埋まった後は再訪しても増えない", backfill_missing(p3, fake) == 0))

        # Codex 6審 #1: 片側だけ永遠に欠測しても台帳が膨らまない
        p6 = Path(td) / "log6.jsonl"
        half = {"S": [("2026-08-17", 100.0)]}   # 指数 "^B" は取れないまま
        def fake_half(t: str, a: str, b: str) -> List[Tuple[str, float]]:
            return half.get(t, [])
        append_row({"type": "firing", "fire_date": "2026-08-17", "ticker": "S",
                    "benchmark": "^B", "entry_close": None, "benchmark_entry": None,
                    "entry_date_used": None, "windows_bd": {"w1": 1}}, p6)
        first = backfill_missing(p6, fake_half)     # 銘柄側だけ埋まる
        again = backfill_missing(p6, fake_half)     # 情報が増えないので追記しない
        third = backfill_missing(p6, fake_half)
        cases.append(("片側欠測でも台帳が無制限に増えない",
                      first == 1 and again == 0 and third == 0))

        # Codex 6審 #2: 指数のエントリーも銘柄の採用日に揃える
        p7 = Path(td) / "log7.jsonl"
        prices3 = {
            "S": [("2026-08-14", 100.0)],                       # 銘柄は17日休場 → 14日採用
            "^B": [("2026-08-14", 1000.0), ("2026-08-17", 1500.0)],  # 指数は17日も開いている
        }
        def fake3(t: str, a: str, b: str) -> List[Tuple[str, float]]:
            return prices3.get(t, [])
        append_row({"type": "firing", "fire_date": "2026-08-17", "ticker": "S",
                    "benchmark": "^B", "entry_close": None, "benchmark_entry": None,
                    "entry_date_used": None, "windows_bd": {"w1": 1}}, p7)
        backfill_missing(p7, fake3)
        st3 = latest_prices(read_log(p7), "2026-08-17", "S")
        cases.append(("指数エントリーは銘柄の採用日に揃える",
                      st3["entry_date_used"] == "2026-08-14" and st3["benchmark_entry"] == 1000.0))

        # Codex 5審 #3: benchmark が変わった行の価格を混ぜない
        p4 = Path(td) / "log4.jsonl"
        append_row({"type": "firing", "fire_date": "2026-08-17", "ticker": "GL9.IR",
                    "benchmark": "^OLD", "entry_close": 1.0, "benchmark_entry": 2.0,
                    "entry_date_used": "2026-08-17"}, p4)
        append_row({"type": "backfill", "fire_date": "2026-08-17", "ticker": "GL9.IR",
                    "benchmark": "^NEW", "entry_close": None, "benchmark_entry": None,
                    "entry_date_used": None}, p4)
        st2 = latest_prices(read_log(p4), "2026-08-17", "GL9.IR")
        cases.append(("指数変更後は旧指数の価格を引き継がない",
                      st2["benchmark"] == "^NEW" and st2["benchmark_entry"] is None))

        # Codex 5審 #2: 指数は銘柄の評価日時点を採る（指数側で独立に数えない）
        p5 = Path(td) / "log5.jsonl"
        prices2 = {
            "S": [("2026-08-17", 100.0), ("2026-08-18", 110.0)],           # 銘柄: 18日が1取引日後
            "^B": [("2026-08-17", 1000.0), ("2026-08-18", 1000.0),
                   ("2026-08-19", 2000.0)],                                 # 指数: 19日も開いている
        }
        def fake2(t: str, a: str, b: str) -> List[Tuple[str, float]]:
            return prices2.get(t, [])
        append_row({"type": "firing", "fire_date": "2026-08-17", "ticker": "S",
                    "benchmark": "^B", "entry_close": 100.0, "benchmark_entry": 1000.0,
                    "entry_date_used": "2026-08-17", "windows_bd": {"w1": 1}}, p5)
        evaluate("2026-08-19", p5, fake2)
        ev2 = [r for r in read_log(p5) if r["type"] == "evaluation"]
        # 指数側で独立に1取引日を数えると 19日(2000)=+100% になり超過が -90% に化ける
        cases.append(("指数は銘柄の評価日で揃える（暦日ズレを作らない）",
                      bool(ev2) and ev2[0]["excess_pct"] == 10.0))

    ok = sum(1 for _, passed in cases if passed)
    for name, passed in cases:
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    print(f"[selftest] {ok}/{len(cases)} PASS")
    return 0 if ok == len(cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="海外カードの前向き記録・評価（§16w）")
    parser.add_argument("--selftest", action="store_true", help="固定テストを実行")
    parser.add_argument("--evaluate", action="store_true", help="期日が来た発火を評価")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.evaluate:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        nb = backfill_missing()          # 価格が欠けた過去の発火を先に埋める
        n = evaluate(today)
        print(f"[foreign] backfill {nb} 件 / 評価 {n} 件")
        return 0
    print("記録は price_universe_check から呼ばれます（単体は --selftest / --evaluate）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
