#!/usr/bin/env python3
"""インフルエンサーが選んだ銘柄の「その時点の共通KPI」プロファイル（記述測定・台帳不算入）。

質問（2026-07-27 ユーザー）:「彼らが選んで買っている銘柄に、その時共通するKPIはあるか？」
方法: 中立収集済みの言及（recollect_*/mentions.csv）の (code, post_date) について、
**投稿日の前営業日 t（PIT）** の特徴量を、426急騰プロファイルと同じ Canonical 関数で測定し、
同日の無作為対照（bars に存在する全銘柄から3本・seed固定）と比較する。

- 特徴量・測定器は `surge_precondition_profile.py` の compute_features を再利用（Dual-Path禁止）。
- 対照は「同じ日の全上場銘柄」から抽出（TOP500に限定しない＝小型株ピックとのサイズ交絡を緩和）。
- 記述測定のみ。閾値化・検定への転用は §8-6 curve-fit 禁止に従い行わない。

実行: python3 scripts/influencer_pick_profile.py
出力: output/influencer_candidates/pick_profile.md
"""
from __future__ import annotations

import csv
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_base_rate as mbr                      # noqa: E402

# 性能パッチ（2026-07-27）: lru_cache(600)では横断的な参照で追い出しが起き
# JSON再解析が支配的になる実測（sample出力=_jsonに全時間）。無制限memoに差し替え、
# 必要日を先に一括ロードする。数値・定義は不変（読み込みの器だけの変更）。
_bars_memo: dict = {}
_orig_load = mbr.load_bars_day.__wrapped__
def _load_bars_unbounded(d):
    r = _bars_memo.get(d)
    if r is None:
        r = _bars_memo[d] = _orig_load(d)
    return r
mbr.load_bars_day = _load_bars_unbounded

import surge_precondition_profile as spp             # noqa: E402  compute_features 再利用

SEED = 20260727
ACCOUNTS = {
    # account: (mentions.csv, 中立検証の判定)
    "fxnia_kabu":   ("recollect_fxnia",        "強PASS（唯一）"),
    "u___a___53":   ("recollect_u___a___53",   "弱PASS"),
    "gihuboy":      ("recollect_gihuboy",      "弱PASS"),
    "serikura":     ("recollect_serikura",     "FAIL"),
    "investramza":  ("recollect_investramza",  "FAIL"),
    "kazzn_blog":   ("recollect_kazzn_blog",   "FAIL"),
    "kabuzoon":     ("output_root",            "FAIL（外れ値依存）"),
}
NUM_FEATS = ["dev200", "quiet_ratio", "ul_count_10bd", "dev25", "max20",
             "vol_ratio_20d", "ret_60bd"]


def mentions_path(key: str) -> Path:
    if key == "output_root":
        return ROOT / "output/influencer_candidates/mentions.csv"   # kabuzoon 初回採点
    return ROOT / f"output/influencer_candidates/{key}/mentions.csv"


def stats(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 5:
        return None
    n = len(vals)
    mean = sum(vals) / n
    med = sorted(vals)[n // 2]
    var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
    return mean, med, math.sqrt(var), n


def main() -> int:
    cal = mbr.load_calendar_days()
    bdays = mbr.all_business_days(cal)
    have = {p.name[:8] for p in (ROOT / "data/jquants/bars").glob("*.json.gz")}
    if have:
        bdays = [d for d in bdays if d <= max(have)]
    bidx = {d: i for i, d in enumerate(bdays)}
    rng = random.Random(SEED)
    # 事前一括ロード: 全pickの必要窓（最古pick-210営業日〜最新）を1回だけ読む
    all_dates = []
    for acc, (key, _v) in ACCOUNTS.items():
        p = mentions_path(key)
        if p.exists():
            all_dates += [r["post_date"] for r in csv.DictReader(open(p)) if r["status"] == "scored"]
    if all_dates:
        lo_i = max(0, bidx.get(min(d for d in all_dates if d in bidx), 210) - 210)
        need = [d for d in bdays[lo_i:]]
        print(f"[pick-profile] 事前ロード {len(need)}営業日", flush=True)
        for k, d in enumerate(need):
            _load_bars_unbounded(d)
            if k % 100 == 0:
                print(f"  warm {k}/{len(need)}", flush=True)

    rows_by_acc: dict[str, list] = {}
    ctrl_rows: list = []
    for acc, (key, verdict) in ACCOUNTS.items():
        p = mentions_path(key)
        if not p.exists():
            continue
        seen = set()
        feats_list = []
        mrows = [r for r in csv.DictReader(open(p)) if r["status"] == "scored"
                 and (r.get("account", "").lstrip("@") == acc or key != "output_root")]
        # 日付順（barsキャッシュ効率）
        for r in sorted(mrows, key=lambda x: x["post_date"]):
            code, d = r["code"], r["post_date"]
            if (code, d) in seen:
                continue
            seen.add((code, d))
            i = bidx.get(d)
            if i is None:
                nxt = [x for x in bdays if x <= d]
                if not nxt:
                    continue
                i = bidx[nxt[-1]]
            if i < 61:
                continue
            t = bdays[i - 1]
            f = spp.compute_features(code, t, bidx, bdays)
            f["account"] = acc
            feats_list.append(f)
            # 対照3本: 同日 t に bars がある全銘柄から無作為（pick除く）
            pool = [c for c in mbr.load_bars_day(t).keys() if c != code]
            for c in rng.sample(pool, min(3, len(pool))):
                cf = spp.compute_features(c, t, bidx, bdays)
                ctrl_rows.append(cf)
        rows_by_acc[acc] = feats_list
        print(f"[pick-profile] @{acc}: {len(feats_list)} picks 測定", flush=True)

    # レポート
    L = ["# インフルエンサーのピック銘柄「その時点」の共通KPIプロファイル（記述測定）", "",
         "> 記述測定のみ・台帳不算入。ここで見つかる相関から閾値を作って検定するのは §8-6 curve-fit 禁止。",
         "> 測定日 t = 投稿日の前営業日（PIT）。対照 = 同日の全上場銘柄から無作為3本/pick（seed=20260727）。",
         "> 特徴量は 426急騰プロファイルと同一の Canonical 関数。", ""]
    ctrl_stats = {ft: stats([r[ft] for r in ctrl_rows]) for ft in NUM_FEATS}
    ctrl_new = sum(1 for r in ctrl_rows if r.get("membership_new")) / max(1, len(ctrl_rows))

    L.append(f"対照（無作為・n={len(ctrl_rows)}）: " +
             " / ".join(f"{ft}={ctrl_stats[ft][0]:.3f}" for ft in NUM_FEATS if ctrl_stats[ft]))
    L.append("")
    for acc, feats in rows_by_acc.items():
        verdict = ACCOUNTS[acc][1]
        if not feats:
            continue
        L.append(f"## @{acc}（{verdict}・pick n={len(feats)}）")
        L.append("")
        L.append("| KPI | pick平均 | 対照平均 | 標準化差 d |")
        L.append("|---|---:|---:|---:|")
        for ft in NUM_FEATS:
            s = stats([r[ft] for r in feats])
            c = ctrl_stats[ft]
            if not s or not c:
                continue
            pooled = math.sqrt((s[2] ** 2 + c[2] ** 2) / 2) or 1e-9
            d = (s[0] - c[0]) / pooled
            L.append(f"| {ft} | {s[0]:.3f} | {c[0]:.3f} | {d:+.2f} |")
        new_rate = sum(1 for r in feats if r.get("membership_new")) / len(feats)
        L.append(f"| membership_new率 | {new_rate:.1%} | {ctrl_new:.1%} | — |")
        L.append("")
    out = ROOT / "output/influencer_candidates/pick_profile.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
