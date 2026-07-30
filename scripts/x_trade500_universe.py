"""取引高TOP500ユニバースの生成（銘柄言及レーンの検知対象拡張・2026-07-30）.

ユーザー定義の完了条件（2026-07-30）:
    「今リストの中に入っていない取引高TOP500に入る株の中から、品薄や値上がりをX投稿で拾い、
     株価が連動して上がっていることが確認できれば終わり」

このスクリプトはその「取引高TOP500」を J-Quants の実データから機械的に定義する:
- 直近60営業日の平均売買代金(Va)上位500・普通株式のみ（ProdCat=011。ETF/REIT等は除外）
- 各行に「既存リストとの関係」を付与:
    in_center_pin     … 時価総額TOP1000台帳（言及辞書の既存供給源）に居るか
    in_beneficiaries  … 品薄対応表の受益カードを持つか（=「今リスト」）
  **狙いの主対象は in_beneficiaries=false の銘柄**（リスト外での検知が完了条件のため）
- x_mention_dict がこのファイルを第2の辞書供給源として読む（center_pin に居ない約60銘柄が
  新たに検知可能になる。例: ニデック・フェローテック・アストロスケール）

実行（月1回程度の更新で十分。売買代金ランキングは急には入れ替わらないため）:
    python3 scripts/x_trade500_universe.py
出力: data/x_price_watch/universe_trade500.jsonl（tracked・1行1銘柄）
"""
from __future__ import annotations

import glob
import gzip
import json
from collections import defaultdict
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent

BARS_GLOB = str(APP / "data/jquants/bars/*.json.gz")
MASTER_GLOB = str(APP / "data/jquants/master/*.json.gz")
CENTER_PIN = APP / "data/center_pin/center_pin.jsonl"
SHORTAGE_MAP = APP / "configs/x_shortage_map.json"
OUT = APP / "data/x_price_watch/universe_trade500.jsonl"

WINDOW_DAYS = 60      # 平均売買代金の観測窓（営業日）
MIN_TRADED_DAYS = 40  # 窓内でこれ未満しか取引が無い銘柄は除外（新規上場・整理銘柄のノイズ）
TOP_N = 500
COMMON_STOCK = "011"  # ProdCat: 普通株式（ETF=014 等を除外）


def to4(code5: str) -> str:
    """J-Quants の5桁コード（末尾0埋め）→ 台帳の4桁表記。英字入り（285A0→285A）も同じ規則。"""
    return code5[:4]


def main() -> int:
    files = sorted(glob.glob(BARS_GLOB))[-WINDOW_DAYS:]
    va: dict[str, list[float]] = defaultdict(list)
    for f in files:
        with gzip.open(f, "rt") as fh:
            for r in json.load(fh)["data"]:
                if r.get("Va"):
                    va[r["Code"]].append(r["Va"])
    avg = {c: sum(v) / len(v) for c, v in va.items() if len(v) >= MIN_TRADED_DAYS}

    with gzip.open(sorted(glob.glob(MASTER_GLOB))[-1], "rt") as fh:
        d = json.load(fh)
    master = {r["Code"]: r for r in (d["info"] if "info" in d else d["data"])}

    stocks = [c for c in sorted(avg, key=avg.get, reverse=True)
              if master.get(c, {}).get("ProdCat") == COMMON_STOCK]
    top = stocks[:TOP_N]

    cp = {json.loads(l)["code"] for l in CENTER_PIN.read_text().splitlines() if l.strip()}
    m = json.loads(SHORTAGE_MAP.read_text())
    bene = {b["code"] for s in m["subjects"] for b in s.get("beneficiaries", [])}

    period = f"{files[0].split('/')[-1][:8]}-{files[-1].split('/')[-1][:8]}"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    n_new = 0
    with OUT.open("w", encoding="utf-8") as fh:
        for rank, c in enumerate(top, 1):
            code = to4(c)
            row = {
                "code": code,
                "name": master[c]["CoName"],
                "rank": rank,
                "avg_va_okuyen": round(avg[c] / 1e8, 1),
                "in_center_pin": code in cp,
                "in_beneficiaries": code in bene,
                "window": period,
            }
            if not row["in_center_pin"]:
                n_new += 1
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"取引高TOP{TOP_N}（普通株のみ・窓 {period}）→ {OUT}")
    print(f"  既存辞書(center_pin)に居ない: {n_new} 銘柄（新たに言及検知の対象になる）")
    print(f"  受益カード保持（今リスト内）: {sum(1 for c in top if to4(c) in bene)} 銘柄")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
