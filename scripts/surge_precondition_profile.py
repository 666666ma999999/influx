#!/usr/bin/env python3
"""426急騰エピソードの事前条件プロファイル（ケース・コントロール記述分析）。

オーナーの問い（2026-07-23）「426件で上がる前の共通のKPIがありますか？」への実測回答。

位置づけ:
- 記述的ケース・コントロール分析。台帳(data/kpi_trials/*)不算入・選抜なし。
- ここで見つかる相関から閾値を作って同一期間で検定するのは §8-6 の curve-fit 禁止に該当。
  使い道は探索prior更新と新規事前登録の設計材料のみ。
- winner autopsy は裁定⑤b(2026-07-19)で四半期背景フィードに格下げ済み。本分析はユーザー
  明示指示(2026-07-23)による単発実行。

設計:
- ケース: output/reverse_lookup/surge_episodes_1y.csv の426エピソード。
  特徴量測定日 t = episode_start の前営業日（PIT: 急騰起点日以降の情報を使わない）。
- コントロール: 同じ暦月の前月TOP500ユニバースから「その月に+30%/20bdエピソードを持たない」
  銘柄を各ケース3本・seed固定で無作為抽出。同じ t で測定（月効果を部分統制）。
- 特徴量: 既存Canonicalのみ（新しい閾値を発明しない）。
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import measure_base_rate as mbr  # noqa: E402 (Canonical: カレンダー/bars)
import kpi_sue_champion_signals as sue_mod  # noqa: E402 (compute_dev200)
import kpi_volshock_v2_amplifiers as v2_amp  # noqa: E402 (compute_quiet_ratio)
import kpi_volshock_v3_ul_earnings as v3_ul  # noqa: E402 (compute_ul_count_10bd)
import kpi_pead_signals as pead_mod  # noqa: E402 (compute_dev25/compute_max20)

SEED = 20260723
EPISODES_CSV = Path("output/reverse_lookup/surge_episodes_1y.csv")
OUT_DIR = Path("output/reverse_lookup")
FINS_DIR = Path("data/jquants/fins")

def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    return f"{y-1}12" if m == 1 else f"{y}{m-1:02d}"

def load_universe():
    u = defaultdict(set)      # month -> codes
    mem = {}                  # (month, code) -> membership
    with gzip.open("output/base_rate/universes_w21.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            u[r["month"]].add(r["code"])
            mem[(r["month"], r["code"])] = r.get("membership", "")
    return u, mem

_fins_cache: dict[str, set] = {}
def fins_codes_on(date: str) -> set:
    """dateに開示があった銘柄コード集合（fins日次ファイル・無ければ空）。"""
    if date in _fins_cache:
        return _fins_cache[date]
    p = FINS_DIR / f"{date}.json.gz"
    codes = set()
    if p.exists():
        try:
            obj = json.load(gzip.open(p))
            recs = obj.get("data") if isinstance(obj, dict) else obj
            for rec in recs or []:
                c = rec.get("LocalCode") or rec.get("Code")
                if c:
                    codes.add(c)
        except Exception:
            pass
    _fins_cache[date] = codes
    return codes

def compute_features(code: str, t: str, bidx: dict, bdays: list) -> dict:
    i = bidx[t]
    bars_t = mbr.load_bars_day(t).get(code, {})
    adjc = bars_t.get("AdjC")
    va_t = bars_t.get("Va")
    # 出来高倍率: Va(t) / mean(Va[t-20..t-1])
    vas = []
    for j in range(max(0, i - 20), i):
        v = mbr.load_bars_day(bdays[j]).get(code, {}).get("Va")
        if v:
            vas.append(v)
    vol_ratio = (va_t / (sum(vas) / len(vas))) if (va_t and len(vas) >= 10) else None
    # 60営業日リターン
    ret60 = None
    if i >= 60 and adjc:
        c60 = mbr.load_bars_day(bdays[i - 60]).get(code, {}).get("AdjC")
        if c60:
            ret60 = adjc / c60 - 1
    # dev25/max20（kpi_screen_batch と同じ呼び出し規約）
    adjc_prev = mbr.load_bars_day(bdays[i - 1]).get(code, {}).get("AdjC") if i >= 1 else None
    dev25 = pead_mod.compute_dev25(code, i, bdays, adjc_prev) if adjc_prev is not None else None
    max20 = pead_mod.compute_max20(code, i, bdays)
    # fins開示接近（t含む過去20営業日）
    fins_recent = any(code in fins_codes_on(bdays[j]) for j in range(max(0, i - 19), i + 1))
    return {
        "dev200": sue_mod.compute_dev200(code, t, bidx, bdays),
        "quiet_ratio": v2_amp.compute_quiet_ratio(code, t, bidx, bdays),
        "ul_count_10bd": v3_ul.compute_ul_count_10bd(code, t, bidx, bdays),
        "dev25": dev25,
        "max20": max20,
        "vol_ratio_20d": vol_ratio,
        "ret_60bd": ret60,
        "fins_within_20bd": fins_recent,
    }

def main() -> int:
    cal = mbr.load_calendar_days()
    bdays = mbr.all_business_days(cal)
    bidx = {d: i for i, d in enumerate(bdays)}
    uni, mem = load_universe()

    episodes = list(csv.DictReader(open(EPISODES_CSV)))
    surge_by_month = defaultdict(set)
    for e in episodes:
        surge_by_month[e["episode_start"][:6]].add(e["code"])

    rng = random.Random(SEED)
    rows = []
    skipped = 0
    for e in episodes:
        D = e["episode_start"]
        i = bidx.get(D)
        if not i or i < 61:
            skipped += 1
            continue
        t = bdays[i - 1]
        month = D[:6]
        pm = prev_month(month)
        u = uni.get(pm, set())
        if e["code"] not in u:
            skipped += 1
            continue
        feats = compute_features(e["code"], t, bidx, bdays)
        feats.update({"role": "case", "code": e["code"], "t": t, "month": month,
                      "membership_new": mem.get((pm, e["code"])) == "new"})
        rows.append(feats)
        # 対照3本: 同月に急騰エピソードを持たない同ユニバース銘柄
        pool = sorted(u - surge_by_month[month] - {e["code"]})
        for c in rng.sample(pool, min(3, len(pool))):
            cf = compute_features(c, t, bidx, bdays)
            cf.update({"role": "control", "code": c, "t": t, "month": month,
                       "membership_new": mem.get((pm, c)) == "new"})
            rows.append(cf)

    # 集計
    num_feats = ["dev200", "quiet_ratio", "ul_count_10bd", "dev25", "max20",
                 "vol_ratio_20d", "ret_60bd"]
    bool_feats = ["fins_within_20bd", "membership_new"]
    cases = [r for r in rows if r["role"] == "case"]
    ctrls = [r for r in rows if r["role"] == "control"]

    def stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None, None, None, 0
        n = len(vals)
        mean = sum(vals) / n
        med = sorted(vals)[n // 2]
        var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
        return mean, med, math.sqrt(var), n

    lines = [
        "# 426急騰エピソードの事前条件プロファイル（ケース・コントロール記述分析）", "",
        "> **記述的ケース・コントロール分析。ここで見つかる相関から閾値を作って同一期間で検定するのは"
        "§8-6のcurve-fit禁止に該当。使い道は探索prior更新と新規事前登録の設計材料のみ。"
        "winner autopsyは裁定⑤b(2026-07-19)で四半期背景フィードに格下げ済みであり、"
        "本分析はユーザー明示指示(2026-07-23)による単発実行。相関≠レシピ。**", "",
        f"- ケース n={len(cases)}（426エピソード中・測定不能スキップ{skipped}）"
        f" / コントロール n={len(ctrls)}（同月TOP500・非急騰・各3本・seed={SEED}）",
        "- 測定日 t = 急騰起点の前営業日（PIT）。月内マッチングで月効果を部分統制。", "",
        "## 数値特徴量（ケース vs 対照）", "",
        "| 特徴量 | case平均 | ctrl平均 | case中央値 | ctrl中央値 | 標準化差(d) | 欠損率(case) |",
        "|---|---|---|---|---|---|---|",
    ]
    for ft in num_feats:
        cm, cmed, csd, cn = stats([r[ft] for r in cases])
        tm, tmed, tsd, tn = stats([r[ft] for r in ctrls])
        if cm is None or tm is None:
            continue
        pooled = math.sqrt(((csd or 0) ** 2 + (tsd or 0) ** 2) / 2) or 1e-9
        d = (cm - tm) / pooled
        miss = 1 - cn / max(1, len(cases))
        lines.append(f"| {ft} | {cm:.4f} | {tm:.4f} | {cmed:.4f} | {tmed:.4f} | {d:+.2f} | {miss:.0%} |")

    lines += ["", "## 比率特徴量", "", "| 特徴量 | case率 | ctrl率 | odds比 |", "|---|---|---|---|"]
    for ft in bool_feats:
        cr = sum(1 for r in cases if r[ft]) / max(1, len(cases))
        tr = sum(1 for r in ctrls if r[ft]) / max(1, len(ctrls))
        odds = ((cr / (1 - cr)) / (tr / (1 - tr))) if 0 < cr < 1 and 0 < tr < 1 else float("nan")
        lines.append(f"| {ft} | {cr:.1%} | {tr:.1%} | {odds:.2f} |")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "precondition_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(OUT_DIR / "precondition_rows.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"done cases={len(cases)} controls={len(ctrls)} skipped={skipped}")
    print(f"-> {OUT_DIR/'precondition_report.md'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
