#!/usr/bin/env python3
"""超急騰426件の起点帰属分析（第16R凍結設計・ユーザー実施承認 2026-07-23）。

位置づけ: 記述的帰属分析。**本分析からレシピ・閾値・trialを一切生成しない**
（レシピ化は§8-6通常ゲート経由のみ）。目的=データ地図と8月TDnet課金判断への数字の根拠。
台帳(data/kpi_trials/*)不算入。

凍結設計:
- t0 = +30%到達日（peak_signal_dateの20営業日後）から遡る120営業日内の
  終値(AdjC)最安値日（同率は直近側）の翌営業日。
- 先行窓 = t0-10〜t0（因果候補はこれのみ）/ 後追い窓 = t0+1〜+5（別掲）。
  感度 = 先行窓 t0-3〜t0。
- 軸（外生のみ）: fins開示 / EDINET(大量保有350系・TOB240-280・その他) /
  margin_alert制度イベント(新規収載 or 規制区分変化) / 空売り残高報告 /
  同33業種の先行急騰 / TOP500新規入り。
- 「説明できた」= 全体lift(case率/ctrl率)≥2 の軸で先行ヒット。説明率E=その割合。
- 事前宣言分岐: E>=60% → TDnet見送り / E<=30% → TDnet前倒し推奨 / 30-60% → 灰色。
"""
from __future__ import annotations

import csv
import glob
import gzip
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import measure_base_rate as mbr  # noqa: E402

SEED = 20260723
EP_CSV = Path("output/reverse_lookup/surge_episodes_1y.csv")
OUT = Path("output/reverse_lookup")
FINS = Path("data/jquants/fins")
EDINET = Path("data/edinet/documents_all")
MALERT = Path("data/jquants/margin_alert")
SSALE = Path("data/jquants/shortsale")
MASTER = Path("data/jquants/master")

AXES = ["fins", "edinet_lsh", "edinet_tob", "edinet_other", "margin_event", "shortsale", "sector_lead", "member_new"]

def prev_month(ym):
    y, m = int(ym[:4]), int(ym[4:6])
    return f"{y-1}12" if m == 1 else f"{y}{m-1:02d}"

def _gz(path):
    try:
        return json.load(gzip.open(path))
    except Exception:
        return None

# ---- 日次イベントキャッシュ ----
_fins = {}
def fins_codes(d):
    if d not in _fins:
        o = _gz(FINS / f"{d}.json.gz")
        _fins[d] = {r.get("Code") for r in (o or {}).get("data", []) if r.get("Code")}
    return _fins[d]

_ed = {}
def edinet_by_code(d):
    """date(YYYYMMDD) -> {code5: set(docTypeCode)}"""
    if d not in _ed:
        o = _gz(EDINET / f"{d}.json.gz")
        m = defaultdict(set)
        if isinstance(o, dict):
            recs = None
            for v in o.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "docID" in v[0]:
                    recs = v
                    break
            for r in recs or []:
                sc = r.get("secCode")
                if sc:
                    m[str(sc)[:5].ljust(5, "0")].add(str(r.get("docTypeCode")))
        _ed[d] = m
    return _ed[d]

_ma = {}
def malert_state(d):
    """date -> {code: TSEMrgnRegCls}"""
    if d not in _ma:
        o = _gz(MALERT / f"{d}.json.gz")
        _ma[d] = {r.get("Code"): r.get("TSEMrgnRegCls") for r in (o or {}).get("data", []) if r.get("Code")}
    return _ma[d]

_ss = {}
def ssale_codes(d):
    if d not in _ss:
        o = _gz(SSALE / f"{d}.json.gz")
        _ss[d] = {r.get("Code") for r in (o or {}).get("data", []) if r.get("Code")}
    return _ss[d]

_master_cache = {}
_master_files = sorted(MASTER.glob("*.json.gz"))
def sector_map_asof(d):
    """d以前で最新のmasterの code->S33"""
    cand = None
    for p in _master_files:
        if p.name[:8] <= d:
            cand = p
        else:
            break
    if cand is None:
        cand = _master_files[0]
    k = cand.name
    if k not in _master_cache:
        o = _gz(cand)
        _master_cache[k] = {r.get("Code"): r.get("S33") for r in (o or {}).get("data", []) if r.get("Code")}
    return _master_cache[k]

def axis_hits(code, days, sector, ep_starts_by_sector, member_new_flag):
    """指定日集合における各軸ヒット(bool)"""
    h = dict.fromkeys(AXES, False)
    for d in days:
        if code in fins_codes(d):
            h["fins"] = True
        ed = edinet_by_code(d).get(code)
        if ed:
            if any(t.startswith("35") for t in ed):
                h["edinet_lsh"] = True
            if any(t in ("240", "250", "260", "270", "280") for t in ed):
                h["edinet_tob"] = True
            if any(not (t.startswith("35") or t in ("240", "250", "260", "270", "280")) for t in ed):
                h["edinet_other"] = True
        # margin_alert: 新規収載 or 区分変化（前営業日比）
        st_today = malert_state(d).get(code)
        if st_today is not None:
            # 前営業日
            pi = _bidx.get(d, 0) - 1
            st_prev = malert_state(_bdays[pi]).get(code) if pi >= 0 else None
            if st_prev is None or st_prev != st_today:
                h["margin_event"] = True
        if code in ssale_codes(d):
            h["shortsale"] = True
        if sector and any(s != code for s in ep_starts_by_sector.get((sector, d), ())):
            h["sector_lead"] = True
    h["member_new"] = member_new_flag
    return h

def main():
    global _bdays, _bidx
    cal = mbr.load_calendar_days()
    _bdays = mbr.all_business_days(cal)
    _bidx = {d: i for i, d in enumerate(_bdays)}

    eps = list(csv.DictReader(open(EP_CSV)))
    # 同月急騰コード（対照除外用）と 業種先行索引
    surge_by_month = defaultdict(set)
    for e in eps:
        surge_by_month[e["episode_start"][:6]].add(e["code"])

    uni = defaultdict(set); mem = {}
    with gzip.open("output/base_rate/universes_w21.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            uni[r["month"]].add(r["code"]); mem[(r["month"], r["code"])] = r.get("membership", "")

    # まず全ケースの t0 を確定（業種先行索引に使う）
    cases = []
    for e in eps:
        peak = e["peak_signal_date"]
        pi = _bidx.get(peak)
        if pi is None or pi + 20 >= len(_bdays):
            continue
        attain = _bdays[pi + 20]
        ai = _bidx[attain]
        lo, best = None, None
        for j in range(max(0, ai - 120), ai + 1):
            c = mbr.load_bars_day(_bdays[j]).get(e["code"], {}).get("AdjC")
            if c is not None and (best is None or c <= best):
                best, lo = c, j
        if lo is None or lo + 1 >= len(_bdays) or lo < 15:
            continue
        t0 = _bdays[lo + 1]
        cases.append({"code": e["code"], "ep_start": e["episode_start"], "gain": e["max_gain_pct"], "t0": t0})

    # 業種先行索引: (sector, date)->set(code)  ※ケースのepisode_startベース
    ep_starts_by_sector = defaultdict(set)
    for e in eps:
        sec = sector_map_asof(e["episode_start"]).get(e["code"])
        if sec:
            ep_starts_by_sector[(sec, e["episode_start"])].add(e["code"])

    rng = random.Random(SEED)
    rows = []
    sector_matched = 0
    for c in cases:
        t0 = c["t0"]; i = _bidx[t0]
        lead = _bdays[max(0, i - 10):i + 1]
        lead3 = _bdays[max(0, i - 3):i + 1]
        lag = _bdays[i + 1:min(len(_bdays), i + 6)]
        pm = prev_month(t0[:6])
        sec = sector_map_asof(t0).get(c["code"])
        mnew = mem.get((pm, c["code"])) == "new"
        for role, code, s, mn in [("case", c["code"], sec, mnew)]:
            r = {"role": role, "code": code, "t0": t0, "gain": c["gain"], "sector": s}
            r.update({f"lead_{k}": v for k, v in axis_hits(code, lead, s, ep_starts_by_sector, mn).items()})
            r.update({f"lead3_{k}": v for k, v in axis_hits(code, lead3, s, ep_starts_by_sector, mn).items()})
            r.update({f"lag_{k}": v for k, v in axis_hits(code, lag, s, ep_starts_by_sector, False).items()})
            rows.append(r)
        # 対照3: 同月非急騰・同業種優先
        pool = sorted(uni.get(pm, set()) - surge_by_month[t0[:6]] - {c["code"]})
        smap = sector_map_asof(t0)
        same = [p for p in pool if smap.get(p) == sec] if sec else []
        picks = rng.sample(same, min(3, len(same)))
        sector_matched += len(picks)
        if len(picks) < 3:
            rest = [p for p in pool if p not in picks]
            picks += rng.sample(rest, min(3 - len(picks), len(rest)))
        for p in picks:
            ps = smap.get(p)
            pmn = mem.get((pm, p)) == "new"
            r = {"role": "control", "code": p, "t0": t0, "gain": "", "sector": ps}
            r.update({f"lead_{k}": v for k, v in axis_hits(p, lead, ps, ep_starts_by_sector, pmn).items()})
            r.update({f"lead3_{k}": v for k, v in axis_hits(p, lead3, ps, ep_starts_by_sector, pmn).items()})
            r.update({f"lag_{k}": v for k, v in axis_hits(p, lag, ps, ep_starts_by_sector, False).items()})
            rows.append(r)

    cs = [r for r in rows if r["role"] == "case"]
    ct = [r for r in rows if r["role"] == "control"]

    def rate(rs, key):
        return sum(1 for r in rs if r[key]) / max(1, len(rs))

    lines = [
        "# 超急騰426件の起点帰属分析（第16R凍結設計）", "",
        "> **記述的帰属分析。本分析からレシピ・閾値・trialを一切生成しない（レシピ化は§8-6通常ゲート経由のみ）。"
        "目的=データ地図と8月TDnet課金判断への数字の根拠。台帳不算入。**", "",
        f"- ケース n={len(cs)} / 対照 n={len(ct)}（同月TOP500非急騰×3・同業種優先マッチ{sector_matched}本・seed={SEED}）",
        "- t0=+30%到達日から遡る120営業日の最安値翌営業日 / 先行窓=t0-10〜t0（因果候補）/ 後追い=+1〜+5（別掲）", "",
        "## 軸別帰属（先行窓）", "",
        "| 軸 | case率 | ctrl率 | lift | 超過率 | 説明軸(lift>=2) |",
        "|---|---|---|---|---|---|",
    ]
    qualifying = []
    for a in AXES:
        cr, tr = rate(cs, f"lead_{a}"), rate(ct, f"lead_{a}")
        lift = (cr / tr) if tr > 0 else float("inf") if cr > 0 else 0.0
        q = lift >= 2 and cr > 0
        if q:
            qualifying.append(a)
        lines.append(f"| {a} | {cr:.1%} | {tr:.1%} | {lift:.2f} | {cr-tr:+.1%} | {'YES' if q else ''} |")

    def explained(rs, prefix):
        return sum(1 for r in rs if any(r[f"{prefix}_{a}"] for a in qualifying)) / max(1, len(rs))

    E = explained(cs, "lead")
    E3 = explained(cs, "lead3")
    Ec = explained(ct, "lead")
    lines += [
        "", "## 後追い窓（+1〜+5・因果に数えない・参考）", "",
        "| 軸 | case率 | ctrl率 |", "|---|---|---|",
    ]
    for a in AXES:
        lines.append(f"| {a} | {rate(cs, f'lag_{a}'):.1%} | {rate(ct, f'lag_{a}'):.1%} |")

    branch = ("E>=60%: 源は手元データ内 → TDnet見送り推奨・§8-6 priorへ1行追記のみ" if E >= 0.60 else
              "E<=30%: 残差の大半は未収載材料 → TDnet前倒し推奨 + X発掘パイロット優先度上げ" if E <= 0.30 else
              "30%<E<60%: 灰色 → 深掘り禁止・TDnet判断は予定どおり8月")
    lines += [
        "", "## 説明率（事前宣言分岐の機械適用）", "",
        f"- 説明軸（lift>=2で先行）: {qualifying if qualifying else 'なし'}",
        f"- **説明率 E = {E:.1%}**（感度: 先行窓-3〜0では {E3:.1%}）",
        f"- 対照の擬似説明率 = {Ec:.1%}（Eがこれに近いほど『説明』の中身は薄い）",
        f"- **事前宣言分岐の該当: {branch}**",
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "origin_attribution_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(OUT / "origin_attribution_rows.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    resid = [r for r in cs if not any(r[f"lead_{a}"] for a in qualifying)]
    with open(OUT / "origin_residual_cases.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["code", "t0", "gain", "manual_label"])
        for r in resid:
            w.writerow([r["code"], r["t0"], r["gain"], ""])
    print(f"done cases={len(cs)} controls={len(ct)} E={E:.3f} E3={E3:.3f} Ec={Ec:.3f} residual={len(resid)}")
    print(f"-> {OUT/'origin_attribution_report.md'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
