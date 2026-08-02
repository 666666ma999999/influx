"""食品14社の決算ブリッジ（値上げ効果 vs 原材料）を四半期ごとに取得・抽出する.

位置づけ（2026-08-02 新設・docs/price-watch-universe.md §16j が正本）:
食品株は値上げでは上がらず、「値上げ効果が原材料高を上回った期」に上がる
（実測: 森永乳業 単価+185億 vs 原料△92億 = 正味+93億の期が増益。§16f）。
その転換は決算ブリッジ（四半期開示）にしか出ないため、開示PDFを機械取得して
ブリッジ候補行を抽出する。**完全自動の数値化はしない**（各社の表形式がバラバラで
誤読リスクが高い）——取得と行抽出まで機械がやり、正味プラス転換の最終判断は人が読む。

原材料ピークアウト（price_universe_check の peakout 系列）が鳴ったら、
次の四半期にこのスクリプトの出力で「値上げが利益に残り始めたか」を確認する、という分業。

取得経路（2026-08-02 v2）: **TDnet の公開日次リスト**（release.tdnet.info・認証不要・保持31日）を
遡り、対象14社の「決算短信・決算説明資料・補足資料」PDFを取得する。
（v1 の EDINET 経路は有報しか取れず、ブリッジ表は有報に無いことが実測で判明したため差し替え。
　TDnet は31日しか遡れないので、**決算シーズンの月内に実行する**こと）

実行（四半期に1回・決算シーズン後）:
    python3 scripts/food_bridge_fetch.py             # 全14社・過去31日
    python3 scripts/food_bridge_fetch.py --codes 2264 2212 --days 14
出力: output/food_bridge/<四半期>/<code>_<会社名>_<n>.txt（抽出行）+ report.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
OUT_BASE = APP / "output/food_bridge"

# ブリッジ（増減要因の金額）を開示することを §16f の実読で確認済みの食品会社。
# note = どの資料のどこに出るか（次回の探し先）
COMPANIES = {
    "2264": ("森永乳業", "決算補足資料に毎期の営業利益増減要因表（単価差/原料乳/原材料エネ）"),
    "2212": ("山崎製パン", "決算説明資料【原材料市況の影響】が品目別（卵・包材・油脂…）"),
    "2593": ("伊藤園", "決算説明資料に連結営業利益増減要因（売上増加 vs 原料資材）"),
    "2206": ("江崎グリコ", "決算短信本文に6項目ブリッジ（売上高/原材料価格変動/…）"),
    "2269": ("明治ホールディングス", "決算記者会見資料に食品セグメントブリッジ（価格改定効果/原材料コスト）"),
    "2897": ("日清食品ホールディングス", "決算補足資料に既存事業コア営業利益の増減要因"),
    "2229": ("カルビー", "決算説明資料にEBITDA/営業利益ブリッジ（単価改善効果/原材料・動力費内訳）"),
    "2587": ("サントリーベバレッジ＆フード", "決算説明会資料の日本事業 利益増減分析"),
    "2801": ("キッコーマン", "決算説明会資料の国内/海外ブリッジ（売上増減/原材料等）"),
    "2579": ("コカ・コーラボトラーズジャパンHD", "決算説明資料の事業利益ブリッジ（価格/ミックス vs 商品市況）"),
    "2810": ("ハウス食品グループ本社", "決算短信のセグメント別増減要因（原価率変動に合算・部分開示）"),
    "2875": ("東洋水産", "決算説明会資料のセグメント別ブリッジ（海外即席麺は百万ドル建て）"),
    "2871": ("ニチレイ", "セグメント/サブセグメント単位の営業利益増減（金額ブリッジは無い）"),
    "2802": ("味の素", "決算説明資料『原材料・原燃料影響（億円）』ブロック"),
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# ブリッジらしい行の検出語（金額を伴うもの・全角数字にも対応）
NUM = r"[0-9０-９,，.．]+"
BRIDGE_RE = re.compile(
    r"(原材料|原料|価格改定|単価|値上げ|価格.?ミックス|資材|エネルギー|燃料|物流費)"
    rf".{{0,40}}([+＋△▲\-−]{NUM}|{NUM}億|{NUM}百万)")
# 取りに行く開示の表題（短信本体＋説明・補足資料。ブリッジはこの層にある）
TITLE_RE = re.compile(r"決算短信|決算説明|補足|決算資料|データブック|説明会資料")
TDNET = "https://www.release.tdnet.info/inbs/"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def tdnet_day(day8: str, want_codes5: set[str]) -> list[dict]:
    """TDnet の1日分のリスト（全ページ）から対象コードの開示を返す。"""
    out = []
    for page in range(1, 40):
        url = f"{TDNET}I_list_{page:03d}_{day8}.html"
        try:
            html = fetch(url).decode("utf-8", errors="replace")
        except Exception:
            break
        rows = re.split(r"<tr", html)
        found_pdf = False
        for row in rows:
            pm = re.search(r'href="([0-9a-z_]+\.pdf)"[^>]*>([^<]{3,80})', row)
            cm = re.search(r">(\d{5})<", row)
            if not pm:
                continue
            found_pdf = True
            if cm and cm.group(1) in want_codes5 and TITLE_RE.search(pm.group(2)):
                out.append({"code5": cm.group(1), "pdf": pm.group(1),
                            "title": " ".join(pm.group(2).split()), "date": day8})
        if not found_pdf:
            break
        time.sleep(0.3)
    return out


def extract_bridge_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if BRIDGE_RE.search(line):
            lines.append(line[:200])
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--codes", nargs="*", help="対象コードを絞る（既定=14社全部）")
    ap.add_argument("--days", type=int, default=31, help="遡る日数（TDnetの保持は31日）")
    args = ap.parse_args()
    targets = {c: COMPANIES[c] for c in (args.codes or COMPANIES) if c in COMPANIES}

    quarter = datetime.now().strftime("%Y-Q") + str((datetime.now().month - 1) // 3 + 1)
    out_dir = OUT_BASE / quarter
    out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    want5 = {c + "0" for c in targets}
    name_of = {c + "0": targets[c][0] for c in targets}

    # TDnet を日次で遡って対象社の開示を集める（決算シーズンの月内に実行する前提）
    disclosures: list[dict] = []
    for i in range(args.days):
        day8 = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            disclosures += tdnet_day(day8, want5)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {day8}: {str(exc)[:60]}")
    print(f"[scan] 過去{args.days}日で対象社の決算系開示 {len(disclosures)} 件")

    per_code: dict[str, list[dict]] = {}
    for d in disclosures:
        per_code.setdefault(d["code5"], []).append(d)

    for code, (name, note) in targets.items():
        docs = per_code.get(code + "0", [])
        if not docs:
            report.append({"code": code, "name": name, "status": "no_docs",
                           "note": "期間内に決算系開示なし（決算シーズン外の実行か）"})
            print(f"[no_docs] {code} {name}")
            continue
        for n, d in enumerate(docs, 1):
            try:
                pdf_path = out_dir / f"{code}_{n}.pdf"
                pdf_path.write_bytes(fetch(TDNET + d["pdf"]))
                txt = subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"],
                                     capture_output=True, text=True, timeout=120).stdout
                lines = extract_bridge_lines(txt)
                (out_dir / f"{code}_{name}_{n}.txt").write_text(
                    f"# {name}（{code}） {d['title']}（{d['date']}）\n# 探し先: {note}\n\n"
                    + "\n".join(lines), encoding="utf-8")
                report.append({"code": code, "name": name, "status": "ok",
                               "title": d["title"], "date": d["date"],
                               "bridge_lines": len(lines)})
                print(f"[ok] {code} {name}: {d['title'][:34]} → ブリッジ候補 {len(lines)} 行")
            except Exception as exc:  # noqa: BLE001  1件の失敗で全体を止めない
                report.append({"code": code, "name": name, "status": "error",
                               "error": str(exc)[:80]})
                print(f"[error] {code} {name}: {str(exc)[:80]}")

    with (out_dir / "report.jsonl").open("w", encoding="utf-8") as fh:
        for r in report:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    ok = sum(1 for r in report if r["status"] == "ok")
    print(f"\n[done] {ok}/{len(report)} 社 → {out_dir}")
    print("次: 各 <code>_<会社名>.txt の候補行を読み、「値上げ効果 > 原材料」へ転換した社を確認する")
    return 0


if __name__ == "__main__":
    sys.exit(main())
