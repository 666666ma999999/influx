#!/usr/bin/env python3
"""TOB/MBO「宿題3点」監査 — 案件(deal)単位の去重・破談損失・翌朝実流動性（記述測定・台帳不算入）。

第19R敵対レビュー（Fable別文脈＋Codex）の一致結論を実装:
  宿題1: 表題ヒット≠独立ディール → code×時間窓(90日)でdeal_idに束ね、1ディール=最先行の適格開示1回のみで全再計算
  宿題2: 破談（撤回・中止・不成立）の率と条件付き損失を実測（右スキューの裏側の左裾）
  宿題3: 翌朝(T+1)の実流動性 — 売買代金Va・参加率10%で張れた金額・寄付ギャップ（買えない値段疑惑の代理測定）

設計上の正直な注記:
- deal_id鍵はインデックス層（銘柄×時間窓）。買付者×買付価格は歴史本文403のため使わない（クロス裁定どおり）。
- 自己株式の公開買付（ディスカウント通例）は BUY シグナルから除外し独立カテゴリで記録。
- exit(20営業日後終値)のbarが無い案件は黙って落とさず、[entry, exit]内の最終取引終値で清算し censored として計数
  （B6の選択バイアス指摘への対応。§6手順2の「廃止=最終取引日終値決済」に整合。買付価格決済は本文403のため不可）。
- 板・特別気配は日足に無い → 「買えたか」はT+1出来高/売買代金/寄付ギャップの代理測定（限界を明記）。
- 評価量の定義（B15二重控除バグ回避）: gross = AdjC(+20bd)/AdjO(T+1)−1。net = gross − 0.3%。判定に使うのは net のCI。

実行: python3 scripts/tob_deal_audit.py
出力: output/tdnet/deal_audit.md / deals.csv
"""
from __future__ import annotations

import csv
import glob
import gzip
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_base_rate as mbr  # noqa: E402  Canonical

INDEX_GLOB = str(ROOT / "data/tdnet/index/*/*.json.gz")
OUT_DIR = ROOT / "output/tdnet"
HORIZON = 20
COST = 0.003
LATE_HOUR = 15
DEAL_GAP_DAYS = 90          # 同一銘柄でこの日数以上あいたら別ディール
SEED = 20260727

TOB_ANY = re.compile(r"公開買付|ＭＢＯ|マネジメント・バイアウト|MBO")
SELF_TENDER = re.compile(r"自己株")
WITHDRAW = re.compile(r"撤回|中止|不成立|買付.*行わない|見送り")
NON_SIGNAL = re.compile(r"結果|終了|状況|応募|訂正|変更|延長|経過")   # 開始/意見/MBO以外の経過系
QUALIFY = re.compile(r"開始|意見表明|賛同|ＭＢＯ|マネジメント・バイアウト|MBO|実施")


def norm_code(raw) -> str | None:
    if not raw:
        return None
    c = str(raw).strip().upper()
    if len(c) == 5:
        return c
    if len(c) == 4:
        return c + "0"
    return None


def main() -> int:
    cal = mbr.load_calendar_days()
    bdays = mbr.all_business_days(cal)
    have = {p.name[:8] for p in (ROOT / "data/jquants/bars").glob("*.json.gz")}
    if have:
        bdays = [d for d in bdays if d <= max(have)]
    bidx = {d: i for i, d in enumerate(bdays)}
    bset = set(bdays)
    first_bd, last_bd = bdays[0], bdays[-1]

    # 1) TOB関連開示を全部拾う（code別・時系列）
    events = defaultdict(list)     # code -> [(date, hh, title)]
    scanned = 0
    for f in sorted(glob.glob(INDEX_GLOB)):
        for it in json.loads(gzip.open(f, "rb").read().decode()).get("items", []):
            t = it.get("Tdnet", it)
            scanned += 1
            title = t.get("title") or ""
            if not TOB_ANY.search(title):
                continue
            code = norm_code(t.get("company_code"))
            pub = t.get("pubdate") or ""
            if not code or len(pub) < 16:
                continue
            events[code].append((pub[:10].replace("-", ""), int(pub[11:13]), title))

    # 2) code×90日窓で deal に束ねる
    deals = []
    for code, evs in events.items():
        evs.sort()
        cur = None
        for d, hh, title in evs:
            if cur is None or _daydiff(cur["last"], d) > DEAL_GAP_DAYS:
                if cur:
                    deals.append(cur)
                cur = {"code": code, "first": d, "last": d, "events": []}
            cur["last"] = d
            cur["events"].append((d, hh, title))
        if cur:
            deals.append(cur)

    # 3) 各dealの属性: 適格シグナル(最先行)・自己株・破談
    rows = []
    n_self = n_nosig = 0
    for dl in deals:
        titles = [e[2] for e in dl["events"]]
        is_self = any(SELF_TENDER.search(t) and "公開買付" in t for t in titles)
        has_withdraw = any(WITHDRAW.search(t) for t in titles)
        sig = None
        for d, hh, title in dl["events"]:
            if SELF_TENDER.search(title):
                continue
            if WITHDRAW.search(title):
                continue
            if QUALIFY.search(title) and not NON_SIGNAL.search(title):
                sig = (d, hh, title)
                break
        if is_self and sig is None:
            n_self += 1
            continue
        if sig is None:
            n_nosig += 1
            continue
        d, hh, title = sig
        if d < first_bd or d > last_bd:
            continue
        base = d
        if hh >= LATE_HOUR or base not in bset:
            nxt_i = None
            for x in bdays:
                if x > base:
                    base = x
                    break
            else:
                continue
        i = bidx.get(base)
        if i is None or i + 1 >= len(bdays):
            continue
        e_day = bdays[i + 1]
        exit_target = i + 1 + HORIZON
        b_e = mbr.load_bars_day(e_day).get(dl["code"], {})
        o = b_e.get("AdjO")
        va1 = b_e.get("Va")
        if not o or o <= 0:
            continue
        # 前日終値→寄付ギャップ
        prevc = mbr.load_bars_day(bdays[i]).get(dl["code"], {}).get("AdjC")
        gap = (o / prevc - 1) if prevc else None
        # exit: 通常は+20bd終値。無ければ窓内最終取引終値（清算扱い・censored計数）
        status = "normal"
        c = None
        if exit_target < len(bdays):
            c = mbr.load_bars_day(bdays[exit_target]).get(dl["code"], {}).get("AdjC")
        if not c:
            for j in range(min(exit_target, len(bdays) - 1), i, -1):
                cc = mbr.load_bars_day(bdays[j]).get(dl["code"], {}).get("AdjC")
                if cc:
                    c = cc
                    status = "early_end"
                    break
        if not c:
            status = "no_exit"
        gross = (c / o - 1) if c else None
        rows.append({
            "code": dl["code"], "signal_date": base, "entry": e_day,
            "title": title[:60], "n_disclosures": len(dl["events"]),
            "withdraw": int(has_withdraw), "status": status,
            "gap_at_open": f"{gap:.4f}" if gap is not None else "",
            "va_t1": va1 or "",
            "gross": f"{gross:.4f}" if gross is not None else "",
            "net": f"{gross - COST:.4f}" if gross is not None else "",
        })

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "deals.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    scored = [r for r in rows if r["net"]]
    nets = [float(r["net"]) for r in scored]
    n = len(nets)
    ev = sum(nets) / n
    med = sorted(nets)[n // 2]

    # 月次ブロック bootstrap（deal単位・去重後）
    random.seed(SEED)
    bym = defaultdict(list)
    for r in scored:
        bym[r["signal_date"][:6]].append(float(r["net"]))
    months = list(bym)
    boots = []
    for _ in range(2000):
        s = []
        for _ in range(len(months)):
            s += bym[random.choice(months)]
        boots.append(sum(s) / len(s))
    boots.sort()
    lo = boots[100]

    # 上位k除外
    srt = sorted(nets, reverse=True)
    def ex_top(k):
        rest = srt[k:]
        return sum(rest) / len(rest)

    # 破談
    wd = [r for r in scored if r["withdraw"] == 1 or r["withdraw"] == "1"]
    wd_nets = [float(r["net"]) for r in wd]

    # 流動性
    vas = [float(r["va_t1"]) for r in scored if r["va_t1"]]
    vas_sorted = sorted(vas)
    def pct(p):
        return vas_sorted[int(p * len(vas_sorted))]
    gaps = [float(r["gap_at_open"]) for r in scored if r["gap_at_open"]]
    gaps_sorted = sorted(gaps)
    med_gap = gaps_sorted[len(gaps_sorted) // 2]

    censored = sum(1 for r in rows if r["status"] == "early_end")
    yearly = defaultdict(list)
    for r in scored:
        yearly[r["signal_date"][:4]].append(float(r["net"]))
    pos_years = sum(1 for y in yearly if sum(yearly[y]) / len(yearly[y]) > 0)

    L = []
    A = L.append
    A("# TOB/MBO 宿題3点監査（deal単位・記述測定・台帳不算入）")
    A("")
    A(f"- 走査 {scanned:,}件 → TOB関連開示 {sum(len(v) for v in events.values()):,}件 → **deal {len(deals):,}件**")
    A(f"- 自己株TOB除外 {n_self} / 適格シグナルなし {n_nosig} / 評価対象 **{n}ディール**（イベント計測時の3,000超行から去重）")
    A(f"- 価格範囲 {first_bd}〜{last_bd}。評価量: net = AdjC(+20bd)/AdjO(T+1) − 1 − 0.3%（二重控除なし・B15対応）")
    A(f"- exit欠損は窓内最終終値で清算し censored 計数 = **{censored}件**（黙って落とさない・B6対応）")
    A("")
    A("## 宿題1: 去重後の本当の成績")
    A("")
    A(f"| 指標 | 値 |\n|---|---|")
    A(f"| deal数 n | **{n:,}** |")
    A(f"| EV(net) | **{ev*100:+.2f}%** |")
    A(f"| EV 片側95%下限（月次ブロックbootstrap 2000・seed={SEED}） | **{lo*100:+.2f}%** |")
    A(f"| 中央値 | {med*100:+.2f}% |")
    A(f"| 上位1除外 EV | {ex_top(1)*100:+.2f}% |")
    A(f"| 上位5除外 EV | {ex_top(5)*100:+.2f}% |")
    A(f"| 上位10除外 EV | {ex_top(10)*100:+.2f}% |")
    A(f"| 年別プラス | {pos_years}/{len(yearly)}年 |")
    A("")
    A("## 宿題2: 破談の左裾")
    A("")
    if wd_nets:
        wd_ev = sum(wd_nets) / len(wd_nets)
        A(f"- 破談・撤回・中止を含むdeal: **{len(wd)}件 / {n}（{len(wd)/n*100:.1f}%）**")
        A(f"- 破談dealの平均net: **{wd_ev*100:+.2f}%** / 最悪: {min(wd_nets)*100:+.1f}%")
        nwd = [float(r['net']) for r in scored if not (r['withdraw']==1 or r['withdraw']=='1')]
        A(f"- 破談なしdealの平均net: {sum(nwd)/len(nwd)*100:+.2f}%")
        if wd_ev < 0:
            A(f"- **破談1件が勝ちを消す倍率**: |{wd_ev*100:.2f}| ÷ 破談なし平均 = {abs(wd_ev)/max(sum(nwd)/len(nwd),1e-9):.1f}件分")
    else:
        A("- 破談検出 0件（表題マッチの限界の可能性・要注記）")
    A("")
    A("## 宿題3: 翌朝の実流動性（代理測定）")
    A("")
    A(f"| 指標 | 値 |\n|---|---|")
    A(f"| T+1売買代金 中央値 | {pct(0.5)/1e6:,.0f}百万円 |")
    A(f"| 同 25%タイル | {pct(0.25)/1e6:,.0f}百万円 |")
    A(f"| 同 10%タイル | {pct(0.10)/1e6:,.0f}百万円 |")
    A(f"| 参加率10%で張れる額（中央値） | {pct(0.5)*0.1/1e6:,.1f}百万円 |")
    A(f"| 同（25%タイル） | {pct(0.25)*0.1/1e6:,.1f}百万円 |")
    A(f"| 寄付ギャップ（前日終値→T+1寄付）中央値 | **{med_gap*100:+.1f}%** |")
    A(f"| ギャップ+20%以上で寄った率 | {sum(1 for g in gaps if g>=0.20)/len(gaps)*100:.1f}% |")
    A("")
    A("※板・特別気配は日足に存在しないため、「買えたか」は売買代金と寄付ギャップの代理測定。")
    A("")
    A("## 直近20ディール（実在確認用）")
    A("")
    A("| code | シグナル日 | 寄付ギャップ | T+1代金(百万) | net |")
    A("|---|---|---:|---:|---:|")
    for r in sorted(scored, key=lambda r: r["signal_date"])[-20:]:
        g = f"{float(r['gap_at_open'])*100:+.1f}%" if r["gap_at_open"] else "—"
        v = f"{float(r['va_t1'])/1e6:,.0f}" if r["va_t1"] else "—"
        A(f"| {r['code'][:-1]} | {r['signal_date']} | {g} | {v} | {float(r['net'])*100:+.1f}% |")
    (OUT_DIR / "deal_audit.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"deals={len(deals)} scored={n} censored={censored} -> {(OUT_DIR/'deal_audit.md').relative_to(ROOT)}")
    return 0


def _daydiff(a: str, b: str) -> int:
    import datetime
    da = datetime.date(int(a[:4]), int(a[4:6]), int(a[6:]))
    db = datetime.date(int(b[:4]), int(b[4:6]), int(b[6:]))
    return (db - da).days


if __name__ == "__main__":
    sys.exit(main())
