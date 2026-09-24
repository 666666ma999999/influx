#!/usr/bin/env python3
"""トレンド株リスト: 株価の条件で絞った会社を、センターピン台帳でトレンド判定して並べる.

手順（2026-09-24 セッションの D 節を定型化）:
  1. 母集団 = 時価総額（生の終値 × 決算の発行済株式数）が下限以上・250 日の株価欠けが少ない会社
  2. 株価の条件 = 250 日高値からの下落率が上限以下・50 日線の上（既定）
  3. トレンド判定 = data/center_pin/center_pin.jsonl の pin が config/trend_map.json の
     トレンドに当たり sign が +（東証33業種では判定しない）
  4. 台帳の pin が狭い会社は overrides（決算の事業別利益で裏取り済み）、
     台帳にない会社は candidates_not_in_ledger（未確認）として別枠で出す
  5. 並び順 = 200 日線の上にいる株が先、下の株は「抜けるのに必要な上昇率」が小さい順
  6. output/trend_list_<日付>.md に件数（N／N 社）つきで保存

使い方:
  python3 scripts/trend_stock_screen.py                  # 既定の条件（config/trend_map.json の defaults）
  python3 scripts/trend_stock_screen.py --max-dd -0.30 --no-ma50
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JQ = os.path.join(ROOT, "data", "jquants")
CENTER_PIN_PATH = os.path.join(ROOT, "data", "center_pin", "center_pin.jsonl")
TREND_MAP_PATH = os.path.join(ROOT, "config", "trend_map.json")
LOAD_DAYS = 320  # 250 日高値と 200 日線に必要な日数＋余裕


def classify(cp: Optional[Dict[str, Any]], code4: str, tmap: Dict[str, Any]) -> Dict[str, Any]:
    """1社のトレンドを判定する.

    Args:
        cp: センターピン台帳の行（無ければ None）
        code4: 4桁コード
        tmap: config/trend_map.json の中身

    Returns:
        {"group": "ledger"|"override"|"candidate"|"out"|"no_ledger", "trend": id or None, "why": str}
    """
    if code4 in tmap.get("overrides", {}):
        o = tmap["overrides"][code4]
        return {"group": "override", "trend": o["trend"], "why": o["reason"], "mark": o.get("mark", "")}
    if cp is None:
        c = tmap.get("candidates_not_in_ledger", {}).get(code4)
        if c:
            v = c.get("verdict")  # 決算で裏取りした結果（確証／△／却下）。無ければ未確認
            if v == "却下":
                return {"group": "out", "trend": None, "why": f"決算で却下: {c.get('reason', '')}", "mark": ""}
            if v in ("確証", "△"):
                return {"group": "override", "trend": c["trend"], "why": c.get("reason", c["note"]),
                        "mark": "△" if v == "△" else ""}
            return {"group": "candidate", "trend": c["trend"], "why": c["note"], "mark": ""}
        return {"group": "no_ledger", "trend": None, "why": "台帳なし", "mark": ""}
    if cp.get("sign") != "+":
        return {"group": "out", "trend": None, "why": f"sign {cp.get('sign')}", "mark": ""}
    pin = cp.get("pin", "")
    for t in tmap["trends"]:
        if t.get("pin_types") and cp.get("pin_type") not in t["pin_types"]:
            continue
        if any(k in pin for k in t.get("pin_exclude", [])):
            continue
        if not t.get("pin_include") or any(k in pin for k in t["pin_include"]):
            return {"group": "ledger", "trend": t["id"], "why": pin, "mark": ""}
    return {"group": "out", "trend": None, "why": f"pin がトレンド外（{cp.get('pin_type')}）", "mark": ""}


def _load_json(path: str) -> Any:
    with gzip.open(path) as f:
        d = json.load(f)
    return d["data"] if isinstance(d, dict) else d


def load_prices(asof: Optional[str], days: int = LOAD_DAYS):
    """直近 days 日の生の終値・分割補正済み終値・時価総額（百万円）を返す."""
    files = sorted(os.listdir(os.path.join(JQ, "bars")))
    if asof:
        files = [f for f in files if f[:8] <= asof.replace("-", "")]
    files = files[-days:]
    recs = []
    for f in files:
        for r in _load_json(os.path.join(JQ, "bars", f)):
            # 終値が欠けた日も AdjFactor は残す（分割日の終値欠けで補正が消えるのを防ぐ）
            recs.append((r["Date"], r["Code"], r.get("C"), r.get("AdjFactor") or 1.0))
    df = pd.DataFrame(recs, columns=["d", "code", "c", "af"])
    C = df.pivot(index="d", columns="code", values="c").sort_index().astype(float)
    AF = df.pivot(index="d", columns="code", values="af").reindex(C.index).fillna(1.0)
    # 分割補正: その日より後の AdjFactor の積を掛ける（窓の外＝asof より後の分割は関係しない）
    P = C * AF[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    # 発行済株式数: 最も新しい決算期（CurPerEn）の値。同じ期の訂正は開示日時の遅い方
    last_sh: Dict[str, tuple] = {}
    end = C.index[-1].replace("-", "")
    for f in sorted(os.listdir(os.path.join(JQ, "fins"))):
        if f[:8] > end:
            break
        for r in _load_json(os.path.join(JQ, "fins", f)):
            try:
                s = float(r.get("ShOutFY") or 0)
            except (TypeError, ValueError):
                s = 0
            if s > 0:
                key = (r.get("CurPerEn") or "", r.get("DiscDate") or "", r.get("DiscTime") or "")
                if r["Code"] not in last_sh or key >= last_sh[r["Code"]][0]:
                    last_sh[r["Code"]] = (key, s)
    SH = pd.Series({k: v[1] for k, v in last_sh.items()}).reindex(C.columns)
    MC = C.iloc[-1] * SH / 1e6
    return C, P, MC


def screen(args, tmap: Dict[str, Any]):
    C, P, MC = load_prices(args.asof, max(LOAD_DAYS, args.high_window + 70))
    master = sorted(os.listdir(os.path.join(JQ, "master")))[-1]
    MM = {r["Code"]: (r["CoName"], r["S33Nm"]) for r in _load_json(os.path.join(JQ, "master", master))}
    CP = {}
    with open(CENTER_PIN_PATH) as f:
        for line in f:
            r = json.loads(line)
            CP[r["code"]] = r
    W = P.iloc[-args.high_window:]
    hi = W.max()
    hid = W.fillna(-np.inf).idxmax()
    MA50 = P.rolling(50).mean()
    MA200 = P.rolling(200, min_periods=200).mean()
    base = 0
    rows: List[Dict[str, Any]] = []
    for c in C.columns:
        if c not in MM or not (MC.get(c, np.nan) >= args.min_mcap) or W[c].isna().sum() > args.max_missing \
                or len(W) < args.high_window:
            continue
        base += 1
        p = P[c].iloc[-1]
        dd = p / hi[c] - 1
        if dd > args.max_dd:
            continue
        if args.ma50 and not (p > MA50[c].iloc[-1]):
            continue
        code4 = c[:4]
        cp = CP.get(code4)
        j = classify(cp, code4, tmap)
        m200 = MA200[c].iloc[-1]
        rows.append({
            "code": code4, "name": MM[c][0], "sect": MM[c][1], "mcap_oku": MC[c] / 100,
            "dd": dd, "hi_date": hid[c], "g50": p / MA50[c].iloc[-1] - 1,
            "s50": MA50[c].iloc[-1] / MA50[c].iloc[-11] - 1, "g200": p / m200 - 1 if m200 == m200 else np.nan,
            "in_ledger": cp is not None, "pin": cp["pin"] if cp else "", **j,
        })
    df = pd.DataFrame(rows)
    k = min(max(args.activist_days, 1), len(C.index))
    act = activist_flags(C.index[-k].replace("-", ""), args.asof.replace("-", "") if args.asof else None)
    args.activist_end = (act or {}).get("_end", "")
    if len(df):
        df["activist"] = [(act or {}).get(c, "—") if act is not None else "取得できず" for c in df.code]
    return C.index[-1], base, df


def activist_flags(start_bd: str, end_bd: Optional[str]) -> Optional[Dict[str, str]]:
    """期間内にアクティビストが新規5%報告（大量保有報告書）を出した会社を {4桁コード: "提出者（提出日）"} で返す.

    判定は kpi_activist_signals.generate_activist_signals（提出者名を data/activist_dictionary.json と照合・
    変更報告と訂正は除く）をそのまま使う。9/14 の計測で先行きにプラスだった唯一の大口情報
    （tasks/bigholder_free3.md S5・120 営業日で中央値 +4.2pt）。EDINET の取得が欠けていれば None。
    """
    import sys
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import kpi_activist_signals as ka
    if end_bd is None:
        files = sorted(f for f in os.listdir(os.path.join(ROOT, "data", "edinet")) if f.endswith(".json.gz"))
        if not files:
            print("[warn] アクティビスト判定を省略: data/edinet に大量保有報告のキャッシュがない")
            return None
        end_bd = files[-1][:8]
    try:
        sig, _ = ka.generate_activist_signals(start_bd, end_bd)
    except SystemExit as e:  # EDINET キャッシュ欠け（edinet_fetch.py で補う）
        print(f"[warn] アクティビスト判定を省略: {str(e).splitlines()[0]}")
        return None
    out: Dict[str, str] = {"_end": end_bd}
    if sig.empty:
        return out
    for r in sig.sort_values("submission_date").itertuples():
        d = r.submission_date
        out[r.code[:4]] = f"{r.filer_name[:24]}（{d[:4]}-{d[4:6]}-{d[6:]}）"
    return out


def order(df: pd.DataFrame) -> pd.DataFrame:
    """200 日線の上の株を先（上にいる幅の小さい順）、下の株は抜けるのに必要な上昇率の小さい順."""
    if df.empty:
        return df
    up = df[df.g200 >= 0].sort_values("g200")
    down = df[df.g200 < 0].assign(need=lambda x: 1 / (1 + x.g200) - 1).sort_values("need")
    return pd.concat([up, down.drop(columns="need"), df[df.g200.isna()]])  # 200日線が出せない株は末尾（消さない）


def fmt_row(r, labels: Dict[str, str]) -> str:
    if r.g200 != r.g200:
        need = "出せない（200日に株価の欠け）"
    elif r.g200 >= 0:
        need = "上にいる（{:+.0f}%）".format(r.g200 * 100)
    else:
        need = "あと+{:.1f}%".format((1 / (1 + r.g200) - 1) * 100)
    s50 = "上向き" if r.s50 > 0 else "下向き"
    return "| {}（{}） | {}{} | {} | {:.0f}% | {:+.0f}%・{} | {} | {} |".format(
        r.name, r.code, labels[r.trend], r.mark, r.why, r.dd * 100, r.g50 * 100, s50, need, r.activist)


def render(date: str, base: int, df: pd.DataFrame, args, tmap: Dict[str, Any]) -> str:
    labels = {t["id"]: t["label"] for t in tmap["trends"]}
    n = len(df)
    nl = int(df.in_ledger.sum()) if n else 0
    cond = "高値から {:.0f}% 以下".format(args.max_dd * 100) + (" かつ 50 日線の上" if args.ma50 else "")
    hdr = "| 社名（コード） | トレンド | 利益を動かすもの（上がると利益＋） | 高値からの下落率 | 50日線との差・向き | 200日線 | アクティビストの新規5%報告 |\n|---|---|---|---|---|---|---|"
    out = [f"# トレンド株リスト（{date} 終値）", "",
           f"- 母集団: 時価総額 {args.min_mcap / 100:,.0f} 億円以上で、{args.high_window} 日の株価の欠けが {args.max_missing} 日以下の {base:,} 社 → {cond} = {n}／{base:,} 社",
           f"- 台帳で照合: {n}／{n} 社（台帳あり {nl}・台帳なし {n - nl}）",
           "- トレンド判定: センターピン台帳の pin が config/trend_map.json のトレンドに当たり sign が +（東証33業種では判定しない）",
           "- 並び順: 200 日線の上にいる株が先、下の株は「抜けるのに必要な上昇率」が小さい順",
           f"- アクティビストの新規5%報告: 直近 {args.activist_days} 営業日に、提出者が data/activist_dictionary.json に載る大量保有報告書（新規のみ・変更と訂正は除く）が出た会社（EDINET は {getattr(args, 'activist_end', '')} 提出分まで読む＝株価の日付より後の報告も含む。最終日の提出分は翌営業日の扱いで入らない。英字入りのコード〈186A 等〉は判定元が未対応で拾えない）。9/14 の計測で先行きにプラスだった唯一の大口情報（tasks/bigholder_free3.md）",
           "- 作成: scripts/trend_stock_screen.py", ""]
    sec = [("ledger", "A. 台帳でトレンドと確認できた会社"),
           ("override", "B. 台帳の pin が狭い・台帳にないが、決算の事業別利益で当たると確かめた会社"),
           ("candidate", "C. 台帳にないが、商売の中身がトレンドに当たる可能性が高い会社（判定は未確認）")]
    for g, title in sec:
        sub = order(df[df.group == g]) if n else df
        out += [f"## {title}（{len(sub)} 社）", ""]
        if len(sub):
            out += [hdr] + [fmt_row(r, labels) for r in sub.itertuples()]
        else:
            out.append("（なし）")
        out.append("")
    if n:
        out_n = int((df.group == "out").sum())
        nol = df[df.group == "no_ledger"]
        out += [f"## 外した会社（台帳でトレンド外 {out_n} 社・台帳なしで候補表にもない {len(nol)} 社）", "",
                "- 台帳でトレンド外: " + "、".join(f"{r.name}（{r.code}・{r.why}）" for r in df[df.group == "out"].itertuples()),
                "- 台帳なし: " + "、".join(f"{r.name}（{r.code}）" for r in nol.itertuples()),
                "- 台帳なしの会社がトレンドに当たると思ったら、決算の事業別利益（30% 以上）を確かめてから config/trend_map.json の candidates_not_in_ledger か overrides に足す", ""]
    return "\n".join(out)


def main() -> None:
    with open(TREND_MAP_PATH) as f:
        tmap = json.load(f)
    d = tmap["defaults"]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--asof", help="この日（YYYY-MM-DD）までのデータで判定。既定=手元の最新")
    ap.add_argument("--min-mcap", type=float, default=d["min_mcap_million_yen"], help="時価総額の下限（百万円）")
    ap.add_argument("--max-dd", type=float, default=d["max_drawdown"], help="高値からの下落率の上限（例 -0.25）")
    ap.add_argument("--high-window", type=int, default=d["high_window_days"])
    ap.add_argument("--max-missing", type=int, default=d["max_missing_days"])
    ap.add_argument("--no-ma50", dest="ma50", action="store_false", default=d["require_above_ma50"])
    ap.add_argument("--activist-days", type=int, default=120, help="アクティビスト報告を見る営業日数（計測の期間=120）")
    ap.add_argument("--out", help="出力先（既定 output/trend_list_<日付>.md）")
    args = ap.parse_args()
    date, base, df = screen(args, tmap)
    md = render(date, base, df, args, tmap)
    path = args.out or os.path.join(ROOT, "output", f"trend_list_{date.replace('-', '')}.md")
    with open(path, "w") as f:
        f.write(md + "\n")
    cnt = df.group.value_counts().to_dict() if len(df) else {}
    print(f"{date}: 母集団 {base} 社 → 株価の条件 {len(df)} 社 → 台帳で確認 {cnt.get('ledger', 0)}・"
          f"事業別利益で追加 {cnt.get('override', 0)}・台帳なし候補 {cnt.get('candidate', 0)}・外した {cnt.get('out', 0) + cnt.get('no_ledger', 0)}")
    print(f"保存: {path}")


if __name__ == "__main__":
    main()
