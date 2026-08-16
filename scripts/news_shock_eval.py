"""news_shock 発火の固定窓評価（+5/+20営業日・冪等）。仕様の正本= tasks/news_shock_preregister.md §3.

規則（凍結・スクリプトは写像）:
- 基準日: 発火 run_at(UTC) を JST に直し、09:00 より前なら当日・以後なら翌日を起点に、
  営業日（data/jquants/bars のファイル実在日）へ繰り上げた日。エントリー相当値=その日の始値 AdjO
- AdjO が欠損（売買停止等）なら翌営業日へ1日だけ繰延。それも欠損なら evaluation_skipped 行
- 測定: 基準始値 → +5/+20営業日の終値 AdjC。TOPIX は基準日終値 → 評価日終値（始値系列が
  無いため・凍結）。超過pt = 銘柄% − TOPIX%
- 冪等キー=(link, code, window)。false_positive 行が付いた link は評価しない
- 台帳は append-only（evaluation / evaluation_skipped 行を追記するのみ）

実行:
    python3 scripts/news_shock_eval.py            # 期日到来分を評価して追記
    python3 scripts/news_shock_eval.py --selftest # 模擬データで規則の固定テスト
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
LEDGER = APP / "data/news_shock/news_log.jsonl"
BARS_DIR = APP / "data/jquants/bars"
TOPIX_PATH = APP / "data/jquants/topix.json.gz"
JST = timezone(timedelta(hours=9))
WINDOWS_BD = {"w5": 5, "w20": 20}


def base_trading_day(run_at_utc: str, bdays: list[str]) -> str | None:
    """発火時刻→エントリー基準の営業日（8桁）。09:00 JST が境界（プレレジ§3）。"""
    dt = datetime.fromisoformat(run_at_utc.replace("Z", "+00:00")).astimezone(JST)
    d0 = dt.date() if (dt.hour, dt.minute) < (9, 0) else dt.date() + timedelta(days=1)
    d8 = d0.strftime("%Y%m%d")
    later = [d for d in bdays if d >= d8]
    return later[0] if later else None


def _bar(day8: str, code5: str, bars_dir: Path) -> dict | None:
    f = bars_dir / f"{day8}.json.gz"
    try:
        with gzip.open(f, "rt") as fh:
            for r in json.load(fh)["data"]:
                if r["Code"] == code5:
                    return r
    except OSError:
        return None
    return None


def evaluate(ledger: Path = LEDGER, bars_dir: Path = BARS_DIR,
             topix_path: Path = TOPIX_PATH) -> int:
    if not ledger.exists():
        print("[eval] 台帳がまだありません")
        return 0
    # ロックは台帳読込より前に取る（読込→ロックの順だと、古い done を持ったプロセスが
    # 先行プロセスの追記後にロックを取り二重追記できる・Codex第3審指摘）
    import fcntl
    lock = (ledger.parent / ".eval.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[eval] 別の評価が実行中のためスキップ")
        return 0
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    fp_links = {r.get("link") for r in rows if r.get("type") == "false_positive"}
    # 分析単位=事象: 各 event_id の**最初の非 false_positive 行**だけを代表として評価する
    # （プレレジ§6の代表行規則。先頭行が後日FP化されたら次の非FP行へ繰り上がる＝Codex再審の注文。
    #  v1行は event_id が無いので従来どおり行単位・FP除外のみ＝後方互換）
    hits, seen_ev = [], set()
    for r in rows:
        if r.get("status") != "hit" or r.get("link") in fp_links:
            continue
        eid = r.get("event_id")
        if eid:
            if eid in seen_ev:
                continue
            seen_ev.add(eid)
        hits.append(r)
    done = {(r.get("link"), r.get("code"), r.get("window")) for r in rows
            if r.get("type") in ("evaluation", "evaluation_skipped")}
    bdays = sorted(Path(f).name[:8] for f in glob.glob(str(bars_dir / "*.json.gz")))
    if not hits or not bdays:
        print(f"[eval] 評価対象なし（発火 {len(hits)}件）")
        return 0
    with gzip.open(topix_path, "rt") as fh:
        tp = {r["Date"].replace("-", ""): r["C"] for r in json.load(fh)["data"]}

    n_new = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ledger.open("a", encoding="utf-8") as fh:
        for h in hits:
            base = base_trading_day(h["run_at"], bdays)
            if base is None:
                continue
            bidx = bdays.index(base)
            for b in h.get("beneficiaries", []):
                code = b["code"]
                # エントリー始値: 基準日 AdjO・欠損なら翌営業日へ1日だけ繰延（凍結）。
                # 繰延先の営業日データが**まだ存在しない**うちは結論を書かず保留する
                # （Codex指摘: 期日前に skipped を確定させると冪等キーで永久に再評価されない）
                entry_day, entry_idx, o = base, bidx, None
                for idx in (bidx, bidx + 1):
                    if idx >= len(bdays):
                        break
                    bar = _bar(bdays[idx], code + "0", bars_dir)
                    if bar and bar.get("AdjO"):
                        entry_day, entry_idx, o = bdays[idx], idx, bar["AdjO"]
                        break
                conclusive = bidx + 1 < len(bdays)     # 繰延先データが実在して初めて欠損が確定
                for win, n_bd in WINDOWS_BD.items():
                    key = (h["link"], code, win)
                    if key in done:
                        continue
                    if o is None:
                        if not conclusive:
                            continue                   # 保留（次回 run で再評価）
                        fh.write(json.dumps({"type": "evaluation_skipped", "link": h["link"],
                                             "code": code, "window": win,
                                             "reason": "始値欠損（基準日+繰延1日とも）",
                                             "run_at": now}, ensure_ascii=False) + "\n")
                        done.add(key)
                        n_new += 1
                        continue
                    if entry_idx + n_bd >= len(bdays):
                        continue                        # 期日未到来
                    ev_day = bdays[entry_idx + n_bd]
                    bar = _bar(ev_day, code + "0", bars_dir)
                    if not bar or not bar.get("AdjC") or entry_day not in tp or ev_day not in tp:
                        continue
                    stock = (bar["AdjC"] / o - 1) * 100
                    topix = (tp[ev_day] / tp[entry_day] - 1) * 100
                    fh.write(json.dumps({"type": "evaluation", "link": h["link"],
                                         "code": code, "tier": b.get("tier"),
                                         "type_label": b.get("type_label"),
                                         "window": win, "entry_day": entry_day,
                                         "eval_day": ev_day, "entry_open": o,
                                         "stock_pct": round(stock, 2),
                                         "topix_pct": round(topix, 2),
                                         "excess_pt": round(stock - topix, 2),
                                         "run_at": now}, ensure_ascii=False) + "\n")
                    done.add(key)
                    n_new += 1
                    print(f"[eval] {code} {win}: 始値{o}→{stock:+.1f}% "
                          f"TOPIX{topix:+.1f}% 超過{stock - topix:+.1f}pt")
    print(f"[eval] 新規 {n_new} 行")
    return 0


def _selftest() -> int:
    import tempfile
    ng = 0
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bars = td / "bars"; bars.mkdir()
        days = [f"2026010{i}" for i in range(1, 10)]     # 1/1〜1/9 を営業日とする
        for i, d in enumerate(days):
            with gzip.open(bars / f"{d}.json.gz", "wt") as fh:
                json.dump({"data": [{"Code": "99990", "AdjO": 100 + i, "AdjC": 102 + i}]}, fh)
        with gzip.open(td / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": f"2026-01-0{i+1}", "C": 100 + i} for i in range(9)]}, fh)
        # 繰延テスト用: 7777 は基準日(1/2)に存在せず 1/3 から出現（→ entry は 1/3 へ繰延）
        with gzip.open(bars / "20260103.json.gz", "wt") as fh:
            json.dump({"data": [{"Code": "99990", "AdjO": 102, "AdjC": 104},
                                {"Code": "77770", "AdjO": 200, "AdjC": 202}]}, fh)
        for i in range(3, 9):   # 1/4以降にも 7777 の終値を足す
            with gzip.open(bars / f"2026010{i+1}.json.gz", "wt") as fh:
                json.dump({"data": [{"Code": "99990", "AdjO": 100 + i, "AdjC": 102 + i},
                                    {"Code": "77770", "AdjO": 200 + i, "AdjC": 202 + i}]}, fh)
        led = td / "log.jsonl"
        mk = lambda run_at, link, code="9999": {"run_at": run_at, "link": link, "status": "hit",
                                   "beneficiaries": [{"code": code, "tier": "confirmed",
                                                      "type_label": "価格直結型"}]}
        rows = [mk("2026-01-01T21:20:00+00:00", "http://a"),   # JST 1/2 06:20 <9時 → 基準1/2
                mk("2026-01-02T10:00:00+00:00", "http://b"),   # JST 1/2 19:00 ≥9時 → 基準1/3
                mk("2026-01-01T21:20:00+00:00", "http://c", "7777"),  # 基準1/2でAdjO欠損→1/3へ繰延
                mk("2026-01-08T22:20:00+00:00", "http://d", "8888"),  # 基準1/9=最終日でAdjO欠損・繰延先データ未到来→保留
                {"run_at": "2026-01-01T21:20:00+00:00", "link": "http://fp", "status": "hit",
                 "beneficiaries": [{"code": "9999", "tier": "confirmed", "type_label": ""}]},
                {"type": "false_positive", "link": "http://fp"},
                # 代表行の繰り上げ: 同一 event_id の先頭行がFP → 2件目が代表として評価される
                {**mk("2026-01-01T21:20:00+00:00", "http://e1"), "event_id": "ev1",
                 "dup_event": False},
                {**mk("2026-01-01T21:20:00+00:00", "http://e2"), "event_id": "ev1",
                 "dup_event": True},
                {"type": "false_positive", "link": "http://e1"}]
        led.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")
        evaluate(led, bars, td / "topix.json.gz")
        out = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines()]
        evs = [r for r in out if r.get("type") == "evaluation"]
        a5 = [r for r in evs if r["link"] == "http://a" and r["window"] == "w5"]
        b5 = [r for r in evs if r["link"] == "http://b" and r["window"] == "w5"]
        c5 = [r for r in evs if r["link"] == "http://c" and r["window"] == "w5"]
        d_rows = [r for r in out if r.get("link") == "http://d"
                  and r.get("type") in ("evaluation", "evaluation_skipped")]
        checks = [
            (a5 and a5[0]["entry_day"] == "20260102", "09:00前発火→当日寄りが基準"),
            (b5 and b5[0]["entry_day"] == "20260103", "09:00以後発火→翌営業日寄りが基準"),
            (a5 and abs(a5[0]["stock_pct"] - ((108 / 101 - 1) * 100)) < 0.01,
             "式=基準始値→+5営業日終値"),
            (not any(r["link"] == "http://fp" for r in evs), "false_positive は評価しない"),
            (any(r["link"] == "http://e2" for r in evs)
             and not any(r["link"] == "http://e1" for r in evs),
             "代表行がFP化されたら同一event_idの次の非FP行へ繰り上げ"),
            (c5 and c5[0]["entry_day"] == "20260103", "基準日にAdjO欠損→翌営業日へ1日繰延"),
            (not d_rows, "繰延先の営業日データ未到来なら結論を書かず保留（skipped を確定させない）"),
        ]
        n_before = len(out)
        evaluate(led, bars, td / "topix.json.gz")       # 再実行
        n_after = len(led.read_text(encoding="utf-8").splitlines())
        checks.append((n_before == n_after, "再実行で増えない（冪等）"))
        for ok, why in checks:
            print(f"  {'OK ' if ok else 'NG '} {why}")
            ng += not ok
    return ng


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        ng = _selftest()
        print("OK: 全通過" if not ng else f"NG: {ng}件失敗")
        return 1 if ng else 0
    return evaluate()


if __name__ == "__main__":
    sys.exit(main())
