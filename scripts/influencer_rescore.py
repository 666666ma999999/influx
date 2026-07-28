#!/usr/bin/env python3
"""中立採点済み14人の再採点（第23R裁定①・去重＋PIT matched-control・記述測定）。

第23R敵対レビューで確定した現行採点の上振れ3欠陥を補正して測り直す:
  1) 去重なし → クラスタ=(銘柄コード×ISO週)・クラスタ内は最初の適格コールのみ
     （Batch3補強1の凍結定義を後ろ向き採点にも適用・preregister:61,74-77）
  2) 一律市場基準9.5% → PIT matched-control（同月×業種33×規模×直前20日リターン3分位・
     preregister:86-88）の超過到達率を主評価
  3) 区間なし → cluster bootstrap 1000反復のEV 95%CI

凍結仕様からの逸脱（明記）: 「時価総額3分位」は時価総額データ非保持のため
master の ScaleCat（TOPIX規模区分）で代用する。粗い層化の趣旨（B5）には適合。

位置づけ: 記述測定・台帳不算入。正式合格/不合格の変更はここでは行わず、
信任判断（fxnia前向きの継続可否・②③の設計）への入力とする。

実行: python3 scripts/influencer_rescore.py
出力: output/influencer_candidates/rescore_matched.md
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import influencer_pick_profile as ipp  # noqa: E402  無制限memoパッチ済み mbr を再利用
mbr = ipp.mbr

SEED = 20260728
HORIZON_BD = 20
N_CONTROL = 20
N_BOOT = 1000

ACCOUNTS = {  # account: (mentions.csv 相対パス, 旧判定)
    "fxnia_kabu":    ("output/influencer_candidates/recollect_fxnia/mentions.csv", "強PASS"),
    "u___a___53":    ("output/influencer_candidates/recollect_u___a___53/mentions.csv", "弱PASS"),
    "gihuboy":       ("output/influencer_candidates/recollect_gihuboy/mentions.csv", "弱PASS"),
    "serikura":      ("output/influencer_candidates/recollect_serikura/mentions.csv", "FAIL"),
    "investramza":   ("output/influencer_candidates/recollect_investramza/mentions.csv", "FAIL"),
    "kazzn_blog":    ("output/influencer_candidates/recollect_kazzn_blog/mentions.csv", "FAIL"),
    "noatake1127":   ("output/influencer_candidates/recollect_noatake1127/mentions.csv", "FAIL"),
    "drdebuneko":    ("output/influencer_candidates/recollect_drdebuneko/mentions.csv", "FAIL"),
    "purazumakoi":   ("output/influencer_candidates/recollect_purazumakoi/mentions.csv", "FAIL"),
    "kabudev_gc":    ("output/influencer_candidates/recollect_kabudev_gc/mentions.csv", "FAIL"),
    "bahbi_76":      ("output/influencer_candidates/recollect_bahbi_76/mentions.csv", "FAIL"),
    "kabuzoon":      ("output/influencer_candidates/mentions.csv", "FAIL(外れ値依存)"),
}


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def iso_week(ymd: str) -> str:
    y, w, _ = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])).isocalendar()
    return f"{y}-W{w:02d}"


def load_master_index(master_dir: Path):
    """月次master: 日付昇順の (ymd, {code:(S33, ScaleCat)})。"""
    files = sorted(master_dir.glob("*.json.gz"))
    out = []
    for f in files:
        ymd = f.name[:8]
        rows = json.load(gzip.open(f, "rt"))
        if isinstance(rows, dict):
            rows = rows.get("info") or rows.get("data") or []
        m = {r["Code"]: (r.get("S33", "?"), r.get("ScaleCat", "?")) for r in rows}
        out.append((ymd, m))
    return out


def master_asof(masters, ymd: str):
    """signal日以前の最新master（PIT）。無ければ最古を返す。"""
    prev = None
    for d, m in masters:
        if d <= ymd:
            prev = m
        else:
            break
    return prev or masters[0][1]


def ret20_cross_section(bdays, bidx, t: str) -> dict[str, float]:
    """t時点の全銘柄 直前20営業日リターン（AdjC基準）。"""
    i = bidx[t]
    if i < 20:
        return {}
    day_t, day_p = bdays[i], bdays[i - 20]
    bt, bp = mbr.load_bars_day(day_t), mbr.load_bars_day(day_p)
    out = {}
    for code, row in bt.items():
        c1, c0 = fnum(row.get("AdjC")), fnum(bp.get(code, {}).get("AdjC"))
        if c1 and c0 and c0 > 0:
            out[code] = c1 / c0 - 1.0
    return out


def tercile_bounds(vals):
    s = sorted(vals)
    n = len(s)
    return (s[n // 3], s[2 * n // 3]) if n >= 3 else (0.0, 0.0)


def tercile_of(v, lo, hi):
    return 0 if v <= lo else (1 if v <= hi else 2)


def forward_reach(code: str, bdays, bidx, signal: str, last_bar: str):
    """§0執行（entry=signal翌営業日AdjO・exit=20bd AdjC・touch=AdjH）の到達判定。"""
    si = bidx.get(signal)
    if si is None:
        return None
    ei, xi = si + 1, si + 1 + HORIZON_BD
    if xi >= len(bdays) or bdays[xi] > last_bar:
        return None
    entry = fnum(mbr.load_bars_day(bdays[ei]).get(code, {}).get("AdjO"))
    if not entry or entry <= 0:
        return None
    exit_c = fnum(mbr.load_bars_day(bdays[xi]).get(code, {}).get("AdjC"))
    if not exit_c:
        return None
    touch = False
    for d in bdays[ei:xi + 1]:
        h = fnum(mbr.load_bars_day(d).get(code, {}).get("AdjH"))
        if h is not None and h >= entry * 1.2:
            touch = True
            break
    return {"close20": exit_c / entry - 1.0 >= 0.20, "touch": touch}


def main() -> int:
    cal = mbr.load_calendar_days()
    bdays = mbr.all_business_days(cal)
    have = {p.name[:8] for p in (ROOT / "data/jquants/bars").glob("*.json.gz")}
    bdays = [d for d in bdays if d <= max(have)]
    bidx = {d: i for i, d in enumerate(bdays)}
    last_bar = bdays[-1]
    masters = load_master_index(ROOT / "data/jquants/master")
    rng = random.Random(SEED)

    # 対象クラスタの収集（先に全読みして必要日を prewarm）
    acc_clusters: dict[str, list[dict]] = {}
    for acc, (rel, _old) in ACCOUNTS.items():
        p = ROOT / rel
        if not p.exists():
            continue
        rows = [r for r in csv.DictReader(open(p)) if r.get("status") == "scored"
                and fnum(r.get("net_return")) is not None and r.get("signal_date")]
        if rel.endswith("candidates/mentions.csv"):
            rows = [r for r in rows if (r.get("account") or "").lstrip("@") == "kabuzoon"]
        seen = set()
        clusters = []
        for r in sorted(rows, key=lambda x: x["post_date"]):
            key = (r["code"], iso_week(r["post_date"]))
            if key in seen:
                continue
            seen.add(key)
            clusters.append({"code": r["code"], "signal": r["signal_date"],
                             "net": fnum(r["net_return"]),
                             "close20": r.get("close_20pct") in ("True", "1", "true"),
                             "touch": r.get("touch_20pct") in ("True", "1", "true")})
        acc_clusters[acc] = clusters
        print(f"[rescore] @{acc}: raw={len(rows)} -> clusters={len(clusters)}", flush=True)

    all_signals = sorted({c["signal"] for cs in acc_clusters.values() for c in cs if c["signal"] in bidx})
    if not all_signals:
        print("FATAL: no clusters")
        return 2
    lo = max(0, bidx[all_signals[0]] - 25)
    need = bdays[lo:]
    print(f"[rescore] prewarm {len(need)}営業日", flush=True)
    for k, d in enumerate(need):
        ipp._load_bars_unbounded(d)
        if k % 200 == 0:
            print(f"  warm {k}/{len(need)}", flush=True)

    # signal日ごとの ret20 断面と3分位境界（キャッシュ）
    cs_cache: dict[str, tuple[dict, float, float]] = {}

    def cs_of(t):
        r = cs_cache.get(t)
        if r is None:
            cs = ret20_cross_section(bdays, bidx, t)
            lo_b, hi_b = tercile_bounds(list(cs.values()))
            r = cs_cache[t] = (cs, lo_b, hi_b)
        return r

    L = ["# 14人再採点（去重＋PIT matched-control・第23R裁定①・記述測定）", "",
         "> 記述測定・台帳不算入。去重=(銘柄×ISO週)最初のコールのみ（Batch3補強1の凍結定義を適用）。",
         "> 対照=同月master×同業種33×同規模(ScaleCat=時価総額3分位の代用・逸脱として明記)×",
         f"> 直前20日リターン同3分位から{N_CONTROL}銘柄seed抽出・同一執行（entry=signal+1 AdjO/20bd AdjC/touch=AdjH）。",
         f"> EV 95%CI=cluster bootstrap {N_BOOT}反復（クラスタ単位リサンプル）。seed={SEED}。", ""]
    L.append("| account | 旧判定 | 旧n | 去重n | EV(去重) | EV95%CI | 除外EV | close20% | 対照close20% | **超過pp** | touch% | 対照touch% |")
    L.append("|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|")

    summary = {}
    for acc, clusters in acc_clusters.items():
        old = ACCOUNTS[acc][1]
        if not clusters:
            continue
        nets = [c["net"] for c in clusters]
        n = len(nets)
        ev = sum(nets) / n
        # 頑健性: 寄与最大の1銘柄を除外
        by_code = defaultdict(float)
        for c in clusters:
            by_code[c["code"]] += c["net"]
        top_code = max(by_code, key=by_code.get)
        ex_nets = [c["net"] for c in clusters if c["code"] != top_code]
        ex_ev = sum(ex_nets) / len(ex_nets) if ex_nets else float("nan")
        # cluster bootstrap CI
        boots = []
        for _ in range(N_BOOT):
            s = [nets[rng.randrange(n)] for _ in range(n)]
            boots.append(sum(s) / n)
        boots.sort()
        ci_lo, ci_hi = boots[int(0.025 * N_BOOT)], boots[int(0.975 * N_BOOT) - 1]
        close_rate = sum(1 for c in clusters if c["close20"]) / n
        touch_rate = sum(1 for c in clusters if c["touch"]) / n
        # matched-control
        ctrl_close, ctrl_touch, matched = [], [], 0
        for c in clusters:
            t = c["signal"]
            if t not in bidx or bidx[t] < 21:
                continue
            cs, lo_b, hi_b = cs_of(t)
            if c["code"] not in cs:
                continue
            m = master_asof(masters, t)
            sub = m.get(c["code"])
            ter = tercile_of(cs[c["code"]], lo_b, hi_b)
            pool = [k for k, v in cs.items()
                    if k != c["code"] and tercile_of(v, lo_b, hi_b) == ter
                    and (sub is None or m.get(k) == sub)]
            if not pool:
                pool = [k for k, v in cs.items()
                        if k != c["code"] and tercile_of(v, lo_b, hi_b) == ter]
            picks = rng.sample(pool, min(N_CONTROL, len(pool)))
            res = [forward_reach(k, bdays, bidx, t, last_bar) for k in picks]
            res = [r for r in res if r]
            if not res:
                continue
            matched += 1
            ctrl_close.append(sum(1 for r in res if r["close20"]) / len(res))
            ctrl_touch.append(sum(1 for r in res if r["touch"]) / len(res))
        cc = sum(ctrl_close) / len(ctrl_close) if ctrl_close else float("nan")
        ct = sum(ctrl_touch) / len(ctrl_touch) if ctrl_touch else float("nan")
        excess = (close_rate - cc) * 100 if ctrl_close else float("nan")
        summary[acc] = {"n": n, "ev": ev, "ci": (ci_lo, ci_hi), "ex_ev": ex_ev,
                        "close": close_rate, "ctrl_close": cc, "excess": excess}
        L.append(f"| {acc} | {old} | {len([1 for _ in csv.DictReader(open(ROOT / ACCOUNTS[acc][0]))])} | {n} "
                 f"| {ev:+.1%} | [{ci_lo:+.1%},{ci_hi:+.1%}] | {ex_ev:+.1%} "
                 f"| {close_rate:.0%} | {cc:.0%} | **{excess:+.1f}** | {touch_rate:.0%} | {ct:.0%} |")
        print(f"[rescore] @{acc}: n={n} EV={ev:+.2%} CI=[{ci_lo:+.2%},{ci_hi:+.2%}] "
              f"close={close_rate:.0%} ctrl={cc:.0%} excess={excess:+.1f}pp (matched {matched}/{n})", flush=True)

    L.append("")
    L.append("## 読み方")
    L.append("- **超過pp** = 本人のclose20到達率 − 同条件（業種×規模×モメンタム同3分位）銘柄の到達率。")
    L.append("  0近傍なら「銘柄選好（モメンタム）で説明でき、本人の追加情報はない」。")
    L.append("- 旧判定は一律9.5%比・去重なしの器で得たもの。本表とズレる場合は本表が優先（欠陥補正後）。")
    out = ROOT / "output/influencer_candidates/rescore_matched.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
