#!/usr/bin/env python3
"""@fxnia_kabu 前向き監視の評価＋台帳追記（Batch3事前登録の合否計算）。

forward scorecard の mentions.csv を読み、独立コール数・EV・外れ値除外EV・終値+20%率を出し、
合格3条件（EV>0 ∧ 外れ値1銘柄除外で符号維持 ∧ 終値+20%>基準9.5%）と停止則（独立60 or 4ヶ月）を
判定して、1行を forward_ledger.tsv へ追記する。数値は記述採点・台帳不算入（3条件成立で初めて起案）。

使い方: python3 fxnia_forward_eval.py --mentions <csv> --ledger <tsv> --asof YYYYMMDD --start YYYYMMDD
"""
from __future__ import annotations
import argparse, csv, datetime
from collections import defaultdict
from pathlib import Path

BASELINE = 0.095   # 市場ベースレート（終値+20%到達）
STOP_CALLS = 60
STOP_MONTHS = 4


def isoweek(d: str) -> str:
    dt = datetime.datetime.strptime(d, "%Y%m%d")
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mentions", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--asof", required=True)   # 実行日 YYYYMMDD
    ap.add_argument("--start", default="20260724")  # 監視開始（out-of-sample境界）
    a = ap.parse_args()

    p = Path(a.mentions)
    rows = []
    if p.exists():
        rows = [r for r in csv.DictReader(open(p)) if r["status"] == "scored"]
    # 監視開始日以降のコールのみ（out-of-sample厳守）
    rows = [r for r in rows if r["post_date"] >= a.start]

    line_cols = [a.asof]
    if not rows:
        line_cols += ["0", "0", "-", "-", "-", "PENDING(no matured calls yet)"]
    else:
        f = lambda r: float(r["net_return"])
        n = len(rows)
        indep = len({(r["code"], isoweek(r["post_date"])) for r in rows})
        ev = sum(f(r) for r in rows) / n
        close = sum(1 for r in rows if r["close_20pct"] in ("True", "true", "1"))
        byc = defaultdict(list)
        for r in rows:
            byc[r["code"]].append(f(r))
        contrib = {k: sum(v) for k, v in byc.items()}
        worst = max(contrib, key=lambda k: contrib[k])
        ex = [f(r) for r in rows if r["code"] != worst]
        exev = sum(ex) / len(ex)
        close_rate = close / n
        # 停止則
        months = (datetime.datetime.strptime(a.asof, "%Y%m%d")
                  - datetime.datetime.strptime(a.start, "%Y%m%d")).days / 30.4
        stopped = indep >= STOP_CALLS or months >= STOP_MONTHS
        # 合格3条件
        c1 = ev > 0
        c2 = exev > 0
        c3 = close_rate > BASELINE
        verdict = ("STOP:" if stopped else "ACCRUING:") + \
                  ("PASS" if (stopped and c1 and c2 and c3) else
                   ("FAIL" if stopped else "-")) + \
                  f"[EV{'>' if c1 else '<='}0,ex{'>' if c2 else '<='}0,close{'>' if c3 else '<='}base]"
        line_cols += [str(indep), str(n), f"{ev*100:+.1f}%", f"{exev*100:+.1f}%",
                      f"{close}/{n}={close_rate*100:.0f}%", verdict]

    led = Path(a.ledger)
    if not led.exists():
        led.write_text("asof\tindep_calls\tn_scored\tEV\tex_outlier_EV\tclose20\tverdict\n", encoding="utf-8")
    with open(led, "a", encoding="utf-8") as f:
        f.write("\t".join(line_cols) + "\n")
    print("[fxnia-forward] " + "\t".join(line_cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
