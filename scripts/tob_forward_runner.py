#!/usr/bin/env python3
"""tob_drift_v1 前向きペーパートレード・ランナー（毎朝07:15・launchd）。

凍結仕様 `tasks/tob_deal_policy_preregister.md`（v3・Codex GO 019fa25e）の運用実装。
分類・deal束ね・シグナル日・約定状態・netの**正本は `tob_drift_v1_stats.py`**（sha凍結済み）を import。
本ファイルはオーケストレーションのみ（判定ロジックを持たない＝Dual-Path禁止）。

状態機械: observe(開示取込) → signal(deal最先行qualify) → order(entry当日07:15) →
fill(barsが来たら約定判定) → exit(+20営業日成熟で清算) ／ pre_start・operational_missed を区別。

台帳: data/paper_trades/tob_ledger.jsonl（append-only・全イベント）
状態: data/paper_trades/tob_state.json
"""
from __future__ import annotations

import datetime as dt
import glob
import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_base_rate as mbr          # noqa: E402  Canonical bars/calendar
import tob_drift_v1_stats as S           # noqa: E402  凍結済み正本

START = "20260728"                        # prospective_start_date（凍結・trials.jsonl登録値）
S_HORIZON = 20                            # 出口=成立日から20営業日後の終値（bdays[idx(entry)+20]・監査実装と同一）
STATE = ROOT / "data/paper_trades/tob_state.json"
LEDGER = ROOT / "data/paper_trades/tob_ledger.jsonl"
INDEX_GLOB = str(ROOT / "data/tdnet/index/*/*.json.gz")
LOOKBACK_DAYS = 21                        # 取込走査窓（--recent 7 より広め）


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()


def log(event: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = now_iso()
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"disclosures": {}, "handled_signals": {}, "positions": []}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def norm_code(raw) -> str | None:
    if not raw:
        return None
    c = str(raw).strip().upper()
    if len(c) == 5:
        return c
    if len(c) == 4:
        return c + "0"
    return None


def ingest(st: dict, today: str) -> int:
    """直近の週次indexからTOB_ANY開示を取り込み（初観測時刻つき・冪等）。"""
    cutoff = (dt.date(int(today[:4]), int(today[4:6]), int(today[6:]))
              - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    added = 0
    for f in sorted(glob.glob(INDEX_GLOB)):
        if Path(f).name[:8] < cutoff[:6] + "01":     # 月粗フィルタ
            continue
        for it in json.loads(gzip.open(f, "rb").read().decode()).get("items", []):
            t = it.get("Tdnet", it)
            title = t.get("title") or ""
            if S.classify_title(title) == "not_tob":
                continue
            code = norm_code(t.get("company_code"))
            pub = t.get("pubdate") or ""
            if not code or len(pub) < 19:
                continue
            d = pub[:10].replace("-", "")
            if d < cutoff:
                continue
            key = hashlib.sha256(f"{code}|{pub}|{title}".encode()).hexdigest()[:16]
            lst = st["disclosures"].setdefault(code, [])
            if any(x["key"] == key for x in lst):
                continue
            lst.append({"key": key, "date": d, "time": pub[11:19], "title": title,
                        "first_seen": now_iso()})
            lst.sort(key=lambda x: (x["date"], x["time"]))
            added += 1
    return added


def main() -> int:
    cal = mbr.load_calendar_days()
    bdays = mbr.all_business_days(cal)
    bset = set(bdays)
    bidx = {d: i for i, d in enumerate(bdays)}
    today = dt.date.today().strftime("%Y%m%d")
    have_bars = {p.name[:8] for p in (ROOT / "data/jquants/bars").glob("*.json.gz")}
    last_bar = max(have_bars) if have_bars else ""

    st = load_state()
    added = ingest(st, today)

    def is_bday(d: str) -> bool:
        return d in bset

    def next_bday(d: str) -> str:
        for x in bdays:
            if x > d:
                return x
        return d

    n_order = n_missed = n_pre = 0
    # --- signal → order / missed / pre_start ---
    for code, lst in st["disclosures"].items():
        pairs = [(x["date"], x["title"]) for x in lst]
        for deal in S.build_deals(pairs):
            si = S.signal_index(pairs, deal)
            if si is None:
                continue
            sig = lst[si]
            deal_key = f"{code}:{sig['key']}"
            if deal_key in st["handled_signals"]:
                continue
            hh, mm, ss = (int(sig["time"][:2]), int(sig["time"][3:5]), int(sig["time"][6:8]))
            T = S.signal_day(sig["date"], (hh, mm, ss), is_bday, next_bday)
            entry_day = next_bday(T)
            if entry_day < START:
                st["handled_signals"][deal_key] = "pre_start"
                n_pre += 1
                continue
            if entry_day == today:
                st["handled_signals"][deal_key] = "ordered"
                st["positions"].append({"deal_key": deal_key, "code": code,
                                        "signal_date": T, "entry_day": entry_day,
                                        "status": "pending_fill", "title": sig["title"][:80]})
                log({"event": "order", "deal_key": deal_key, "code": code,
                     "entry_day": entry_day, "title": sig["title"][:80]})
                n_order += 1
            elif entry_day < today:
                st["handled_signals"][deal_key] = "operational_missed"
                log({"event": "operational_missed", "deal_key": deal_key, "code": code,
                     "entry_day": entry_day, "first_seen": sig["first_seen"],
                     "title": sig["title"][:80]})
                n_missed += 1
            # entry_day > today → 次回以降のランで order する（何もしない）

    # --- fill ---
    n_fill = n_unfill = 0
    for pos in st["positions"]:
        if pos["status"] != "pending_fill" or pos["entry_day"] > last_bar:
            continue
        bar = mbr.load_bars_day(pos["entry_day"]).get(pos["code"], {})
        state = S.fill_state(bar.get("AdjO"), bar.get("Vo"))
        if state == "filled":
            pos["status"] = "filled"
            pos["entry_px"] = bar["AdjO"]
            i = bidx.get(pos["entry_day"])
            pos["exit_target"] = bdays[i + S_HORIZON] if i is not None and i + S_HORIZON < len(bdays) else None
            log({"event": "fill", "deal_key": pos["deal_key"], "entry_px": bar["AdjO"],
                 "va_t1": bar.get("Va"), "exit_target": pos["exit_target"]})
            n_fill += 1
        else:
            pos["status"] = state
            log({"event": "unfilled", "deal_key": pos["deal_key"], "reason": state})
            n_unfill += 1

    # --- exit ---
    n_exit = 0
    for pos in st["positions"]:
        if pos["status"] != "filled" or not pos.get("exit_target") or pos["exit_target"] > last_bar:
            continue
        c = mbr.load_bars_day(pos["exit_target"]).get(pos["code"], {}).get("AdjC")
        censored = False
        if not c:
            i0, i1 = bidx[pos["entry_day"]], bidx[pos["exit_target"]]
            for j in range(i1, i0, -1):
                cc = mbr.load_bars_day(bdays[j]).get(pos["code"], {}).get("AdjC")
                if cc:
                    c = cc
                    censored = True
                    break
        if not c:
            pos["status"] = "no_exit"
            log({"event": "no_exit", "deal_key": pos["deal_key"]})
            continue
        net = S.net_return(pos["entry_px"], c)
        pos["status"] = "closed"
        pos["net"] = net
        pos["censored"] = censored
        log({"event": "exit", "deal_key": pos["deal_key"], "exit_px": c,
             "net": round(net, 6), "censored": censored})
        n_exit += 1

    save_state(st)
    open_pos = sum(1 for p in st["positions"] if p["status"] in ("pending_fill", "filled"))
    closed = [p for p in st["positions"] if p["status"] == "closed"]
    summary = (f"[tob-forward] {today} ingest+{added} order={n_order} missed={n_missed} "
               f"pre_start+{n_pre} fill={n_fill} unfilled={n_unfill} exit={n_exit} "
               f"open={open_pos} closed_total={len(closed)}")
    if closed:
        ev = sum(p["net"] for p in closed) / len(closed)
        summary += f" 累計EV={ev*100:+.2f}%"
    print(summary)
    log({"event": "run_summary", "summary": summary})
    return 0


if __name__ == "__main__":
    sys.exit(main())
