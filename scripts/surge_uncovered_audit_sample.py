#!/usr/bin/env python3
"""Batch2「本当にX外か」監査の層化抽出（第17R事前登録・第18Rでゲート格上げ）。

目的: 急騰271銘柄のうち手元コーパスで事前言及が見つからなかった244銘柄について、
「Xに投稿が無い / 存在するが取得できない / 取得したが銘柄同定できない」の構成比を、
時期×業種×規模で層化した20〜30銘柄の人手監査で推定する。この結果が発掘レーン継続の
ゲート（X外優勢→打ち止め / 収集漏れ優勢→既存収集の穴埋め）になる。

出力: output/reverse_lookup/uncovered_audit_worksheet.csv（人が埋める判定列つき）
記述分析・台帳不算入。外部通信なし（抽出のみ）。
"""
from __future__ import annotations

import ast
import csv
import gzip
import json
import random
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
EPISODES = ROOT / "output/reverse_lookup/surge_episodes_1y.csv"
MENTIONS = ROOT / "output/influencer_candidates/corpus_all/mentions.csv"
MASTER = ROOT / "data/jquants/master/20260630.json.gz"
UNIVERSES = ROOT / "output/base_rate/universes_w21.csv.gz"
OUT = ROOT / "output/reverse_lookup/uncovered_audit_worksheet.csv"

SEED = 20260725
TARGET_N = 24          # 20〜30の中央付近（層化の割り切れも考慮）


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    return f"{y-1}12" if m == 1 else f"{y}{m-1:02d}"


def main() -> int:
    # master: コード -> (社名, 業種33, 規模カテゴリ)
    recs = json.load(gzip.open(MASTER))
    recs = recs.get("data") if isinstance(recs, dict) else recs
    meta = {r["Code"]: (r.get("CoName") or "?", r.get("S33Nm") or "?", r.get("S17Nm") or "?")
            for r in recs}

    # universes: (month, code) -> rank（売買代金順位＝規模/流動性のproxy）
    rank = {}
    with gzip.open(UNIVERSES, "rt") as f:
        for r in csv.DictReader(f):
            try:
                rank[(r["month"], r["code"])] = int(r["rank"])
            except (ValueError, KeyError):
                pass

    # episodes: 銘柄 -> 最初のエピソード（監査は1銘柄1件で足りる）
    episodes = list(csv.DictReader(open(EPISODES)))
    first_ep = {}
    for e in sorted(episodes, key=lambda x: x["episode_start"]):
        first_ep.setdefault(e["code"], e)

    # covered: 手元コーパスで事前言及が確認できた銘柄
    covered = set()
    for r in csv.DictReader(open(MENTIONS)):
        if r.get("surge_hit") in ("True", "true", "1"):
            try:
                if ast.literal_eval(r.get("episode_starts") or "[]"):
                    covered.add(r["code"])
            except Exception:
                covered.add(r["code"])

    uncovered = {c: e for c, e in first_ep.items() if c not in covered}

    # 層化キー: 四半期 × 業種17 × 規模3分位（rankの3分位・前月ユニバース）
    rows = []
    for code, e in uncovered.items():
        name, s33, s17 = meta.get(code, ("?", "?", "?"))
        month = e["episode_start"][:6]
        rk = rank.get((prev_month(month), code))
        rows.append({"code": code, "name": name, "s33": s33, "s17": s17,
                     "rank": rk, "month": month, "ep": e})
    ranked = sorted([r["rank"] for r in rows if r["rank"]])
    if ranked:
        q1, q2 = ranked[len(ranked) // 3], ranked[2 * len(ranked) // 3]
    else:
        q1 = q2 = 0
    for r in rows:
        rk = r["rank"]
        r["size"] = "unknown" if rk is None else ("large" if rk <= q1 else ("mid" if rk <= q2 else "small"))
        m = int(r["month"][4:6])
        r["quarter"] = f"{r['month'][:4]}Q{(m - 1) // 3 + 1}"

    strata = defaultdict(list)
    for r in rows:
        strata[(r["quarter"], r["s17"], r["size"])] = strata[(r["quarter"], r["s17"], r["size"])] + [r]

    # 比例配分（各層から最低1・端数はseed固定でランダムに配る）
    rng = random.Random(SEED)
    keys = sorted(strata.keys())
    picked = []
    # 大きい層から比例で確保
    total = len(rows)
    quota = {k: max(1, round(TARGET_N * len(strata[k]) / total)) for k in keys}
    for k in keys:
        pool = sorted(strata[k], key=lambda r: r["code"])
        picked += rng.sample(pool, min(quota[k], len(pool)))
    rng.shuffle(picked)
    picked = picked[:TARGET_N] if len(picked) >= TARGET_N else picked

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code4", "銘柄名", "業種33", "規模", "急騰開始", "上昇率%",
                    "検索窓_from", "検索窓_to", "X検索URL(社名)", "X検索URL(コード)",
                    "判定[none/unreachable/unidentifiable/found]", "見つけた発信者", "メモ"])
        for r in sorted(picked, key=lambda r: (r["quarter"], r["s17"], r["code"])):
            e = r["ep"]
            c4 = r["code"][:-1]
            since, until = e["search_window_from"], e["search_window_to"]
            def u(q):
                dq = f"{q} since:{since[:4]}-{since[4:6]}-{since[6:]} until:{until[:4]}-{until[4:6]}-{until[6:]}"
                return f"https://x.com/search?q={quote(dq)}&f=live"
            w.writerow([c4, r["name"], r["s33"], r["size"], e["episode_start"],
                        e["max_gain_pct"], since, until, u(r["name"]), u(c4), "", "", ""])

    print(f"急騰銘柄 {len(first_ep)} / 事前言及あり {len(covered)} / 未捕捉 {len(uncovered)}")
    print(f"層数 {len(keys)} → 抽出 {len(picked)}銘柄（seed={SEED}）")
    print(f"-> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
