#!/usr/bin/env python3
"""TDnet 表題マッチによるイベント別リターン・プロファイル（記述測定・台帳不算入）。

目的: 112万件の適時開示インデックス（`data/tdnet/index/`）から表題で機械分類したイベントについて、
「開示の翌営業日始値で買い、20営業日後の終値で売る」場合の実績を **§0 の定義そのままで** 測る。

位置づけ:
- **記述測定のみ。台帳（data/kpi_trials/）不算入・α非消費**（§8-6 curve-fit禁止）。
  ここで良い数字が出ても、それは「事前登録の設計材料」であって合格ではない。
- 閾値の探索・フィルタ探しは行わない（表題マッチの素の姿だけを見る）。
- PIT: 開示時刻が 15:00 以降なら翌営業日をシグナル日 T とし、エントリーは T の翌営業日始値
  （＝場中開示の当日織り込みを避ける保守側）。§0 と同じく調整済み株価。

再利用（新規実装しない）: `measure_base_rate.py` の load_calendar_days / all_business_days /
load_bars_day、ユニバースは `output/base_rate/universes_w21.csv.gz`（月次TOP500）。

実行: python3 scripts/tdnet_event_profile.py [--events 自社株買い決議,業績予想の修正] [--universe-only]
出力: output/tdnet/event_profile.md / event_rows.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_base_rate as mbr  # noqa: E402  Canonical

INDEX_GLOB = str(ROOT / "data/tdnet/index/*/*.json.gz")
UNIVERSES = ROOT / "output/base_rate/universes_w21.csv.gz"
OUT_DIR = ROOT / "output/tdnet"

HORIZON_BD = 20
COST = 0.003          # 往復コスト（§0）
LATE_HOUR = 15        # これ以降の開示は翌営業日をシグナル日にする（保守側）

PATTERNS = {
    "自社株買い決議": r"自己株式の取得",
    "業績予想の修正": r"業績予想.*修正|通期.*予想.*修正",
    "配当予想の修正": r"配当予想.*修正|増配|復配",
    "資本業務提携": r"資本業務提携|業務提携",
    "株式分割": r"株式分割",
    "公開買付(TOB)": r"公開買付",
    "第三者割当増資": r"第三者割当|募集株式|新株予約権.*発行",
    "MSワラント": r"行使価額修正条項",
    "立会外分売": r"立会外分売",
    "希望退職": r"希望退職",
}


def norm_code(raw: str) -> str | None:
    """yanoshin の company_code は 5桁(末尾0付き)が多い。J-Quants の 5桁へ正規化。"""
    if not raw:
        return None
    c = str(raw).strip().upper()
    if len(c) == 5 and (c[:4].isdigit() or c[3].isalpha()):
        return c
    if len(c) == 4:
        return c + "0"
    return None


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    return f"{y-1}12" if m == 1 else f"{y}{m-1:02d}"


def load_universe() -> dict:
    uni = defaultdict(set)
    with gzip.open(UNIVERSES, "rt") as f:
        for r in csv.DictReader(f):
            uni[r["month"]].add(r["code"])
    return uni


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default=None, help="カンマ区切り（既定=全部）")
    ap.add_argument("--min-n", type=int, default=30)
    a = ap.parse_args()
    events = a.events.split(",") if a.events else list(PATTERNS)
    pats = {k: re.compile(v) for k, v in PATTERNS.items() if k in events}

    cal = mbr.load_calendar_days()
    bdays = mbr.all_business_days(cal)
    # bars キャッシュが実在する日までに限定（カレンダーは未取得日も含むため FATAL を避ける）
    have = {p.name[:8] for p in (ROOT / "data/jquants/bars").glob("*.json.gz")}
    if have:
        bdays = [d for d in bdays if d <= max(have)]
    bidx = {d: i for i, d in enumerate(bdays)}
    bset = set(bdays)
    uni = load_universe()
    first_bd, last_bd = bdays[0], bdays[-1]
    print(f"[tdnet-profile] 価格データ範囲 {first_bd}〜{last_bd}（bars依存）")

    # 1) 開示 → (event, code, signal_date)
    sig = []
    scanned = 0
    for f in sorted(glob.glob(INDEX_GLOB)):
        for it in json.loads(gzip.open(f, "rb").read().decode()).get("items", []):
            t = it.get("Tdnet", it)
            scanned += 1
            title = t.get("title") or ""
            code = norm_code(t.get("company_code"))
            pub = t.get("pubdate") or ""
            if not code or len(pub) < 16:
                continue
            d, hh = pub[:10].replace("-", ""), int(pub[11:13])
            if d < first_bd or d > last_bd:
                continue
            for name, rx in pats.items():
                if not rx.search(title):
                    continue
                # 15時以降＝翌営業日をシグナル日（保守）。その日が非営業日なら次の営業日へ送る
                base = d
                if hh >= LATE_HOUR or base not in bset:
                    nxt = [x for x in bdays if x > base]
                    if not nxt:
                        continue
                    base = nxt[0]
                sig.append((name, code, base))
    print(f"[tdnet-profile] 走査 {scanned:,}件 → シグナル {len(sig):,}件")

    # 2) リターン評価（entry=T+1始値 / exit=+20bd終値）
    rows = []
    for name, code, T in sig:
        i = bidx.get(T)
        if i is None or i + 1 + HORIZON_BD >= len(bdays):
            continue
        e_day, x_day = bdays[i + 1], bdays[i + 1 + HORIZON_BD]
        b_e = mbr.load_bars_day(e_day).get(code, {})
        b_x = mbr.load_bars_day(x_day).get(code, {})
        o, c = b_e.get("AdjO"), b_x.get("AdjC")
        if not o or not c or o <= 0:
            continue
        gross = c / o - 1
        month = T[:6]
        rows.append({"event": name, "code": code, "signal_date": T, "entry_date": e_day,
                     "exit_date": x_day, "gross": gross, "net": gross - COST,
                     "hit20": int(gross >= 0.20),
                     "in_universe": int(code in uni.get(prev_month(month), set()))})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "event_rows.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    # 3) 集計（全銘柄 / TOP500内）
    def agg(rs):
        n = len(rs)
        if n == 0:
            return None
        ev = sum(r["net"] for r in rs) / n
        hit = sum(r["hit20"] for r in rs) / n
        med = sorted(r["net"] for r in rs)[n // 2]
        return n, ev, hit, med

    lines = ["# TDnet 表題マッチ イベント別プロファイル（記述測定）", "",
             "> **記述測定のみ・台帳不算入・α非消費**（§8-6 curve-fit禁止）。閾値探索やフィルタ探しは行わず、"
             "表題マッチの素の姿だけを測っている。ここで良い数字が出ても合格ではなく、事前登録の設計材料。", "",
             f"- 対象: `data/tdnet/index/` {scanned:,}件の開示表題",
             f"- 価格データ範囲: **{first_bd}〜{last_bd}**（bars依存＝TDnetの2009-10より短い）",
             f"- 定義(§0): 開示15時以降は翌営業日をシグナル日T → **T+1営業日の始値で買い・20営業日後の終値で売る**"
             f"（調整済み株価・往復コスト{COST:.1%}控除）", "",
             "## 全銘柄", "", "| イベント | n | EV(コスト後) | +20%到達率 | 中央値 |", "|---|---:|---:|---:|---:|"]
    by_ev = defaultdict(list)
    for r in rows:
        by_ev[r["event"]].append(r)
    for name in sorted(by_ev, key=lambda k: -len(by_ev[k])):
        s = agg(by_ev[name])
        if s and s[0] >= a.min_n:
            n, ev, hit, med = s
            lines.append(f"| {name} | {n:,} | {ev*100:+.2f}% | {hit*100:.1f}% | {med*100:+.2f}% |")
    lines += ["", "## TOP500ユニバース内のみ（既存KPIと同じ土俵）", "",
              "| イベント | n | EV(コスト後) | +20%到達率 | 中央値 |", "|---|---:|---:|---:|---:|"]
    for name in sorted(by_ev, key=lambda k: -len(by_ev[k])):
        s = agg([r for r in by_ev[name] if r["in_universe"]])
        if s and s[0] >= a.min_n:
            n, ev, hit, med = s
            lines.append(f"| {name} | {n:,} | {ev*100:+.2f}% | {hit*100:.1f}% | {med*100:+.2f}% |")
    lines += ["", "## 読み方（重要）",
              "- **+20%到達率を市場ベースレート（既存実測 9.5%）と比べる**のが第一。下回るなら候補にならない。",
              "- EVがプラスでも、単一銘柄・単一時期に偏っていれば宝くじ型（§8-6 墓場②）。次段で頑健性を見る。",
              "- ここは**探索**。合格判定は前向き＋事前登録でのみ行う。"]
    (OUT_DIR / "event_profile.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[tdnet-profile] 評価 {len(rows):,}件 -> {(OUT_DIR/'event_profile.md').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
