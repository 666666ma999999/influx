"""center_pin 台帳（977社）を受益タイプ別に並べた一覧 MD を生成する.

目的: 「◯◯高騰」ニュースが出た時に、関連銘柄を**儲け方の型**で仕分けて
「値上がりがそのまま利益になる銘柄」だけを選べるようにする。
2026-08-15 新設の背景= 8/4 銅検知の実測: 価格直結型（三菱マテ+21.2%・アルコニックス+13.8%）
は上がり、数量型（JX金属−11.2%・三井金属−16.6%）は上がらなかった。

実行:
    python3 scripts/gen_center_pin_types.py   # output/center_pin_types.md を上書き生成
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))

from x_mention_dict import PIN_TYPE_LABELS, SIGN_LABELS  # noqa: E402  # ラベルの正本を共有（二重定義しない）

LEDGER = APP / "data/center_pin/center_pin.jsonl"
OUT = APP / "output/center_pin_types.md"

# 表示順と「高騰ニュースで買ってよいか」の平易な説明（型の定義は center_pin.jsonl が正本）
TYPE_GUIDE = [
    ("commodity", "◎ 買い候補", "商品価格の上昇がそのまま利益になる（鉱山権益・在庫保有など）。"
     "高騰ニュースの筆頭候補。ただし符号−の銘柄は逆に痛む側"),
    ("spread", "△ 判定不能", "儲けは売値と仕入の**差**（加工賃・利ざや）。原料も同時に上がると"
     "差は広がらないため、値上がりニュースだけでは買えない"),
    ("price_set", "△ 別の話", "自社製品の値上げが浸透するかで決まる。市況の高騰とは別物"),
    ("volume", "✕ ほぼ無関係", "利益は販売**数量**が決める。価格高騰の恩恵は薄い"
    "（例: JX金属は銅高騰でも−11%）"),
    ("asset", "△ 資産次第", "保有資産（不動産等）の売却・評価額が動く型"),
    ("event", "△ イベント待ち", "制度改定・承認などの個別イベントで動く型"),
    ("fx", "△ 為替の話", "円安/円高が利益を左右する型（符号−=円安が痛い）"),
    ("rate", "△ 金利の話", "金利水準が利益を左右する型"),
]


def main() -> int:
    rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[r.get("pin_type", "不明")].append(r)

    lines = [
        "# 受益タイプ別 銘柄一覧（center_pin 台帳の型別ビュー）",
        "",
        f"> 生成: {date.today().isoformat()}／正本: `data/center_pin/center_pin.jsonl`"
        f"（{len(rows)}社）／再生成: `python3 scripts/gen_center_pin_types.py`",
        "> 使い方: 「◯◯高騰」のニュースが出たら、関連銘柄をこの表で引き、"
        "**価格直結型（符号+）だけ**を買い候補にする。数量型・利ざや型を同列に買わない。",
        "",
        "| 型 | 高騰ニュースで買ってよいか | 社数 |",
        "|---|---|---|",
    ]
    for t, verdict, _ in TYPE_GUIDE:
        lines.append(f"| {PIN_TYPE_LABELS[t]} | {verdict} | {len(by_type.get(t, []))} |")
    lines.append("")

    def emit(title: str, verdict: str, desc: str, group: list[dict]) -> None:
        lines.extend([f"## {title}（{len(group)}社）— {verdict}", "", desc, ""])
        for r in sorted(group, key=lambda r: r["code"]):
            note = SIGN_LABELS.get(r.get("sign", ""), "")
            mark = f"（{note}）" if note else ""
            lines.append(f"- {r['code']} {r['name']} — {r.get('pin', '')}{mark}")
        lines.append("")

    for t, verdict, desc in TYPE_GUIDE:
        emit(PIN_TYPE_LABELS[t], verdict, desc, by_type.get(t, []))
    # 台帳に新しい pin_type が増えても977社全件一覧の性質を守る（黙って落とさない）
    unknown = sorted(set(by_type) - {t for t, _, _ in TYPE_GUIDE})
    for t in unknown:
        emit(f"未分類: {t}", "？ 型の定義を確認", "台帳に新しい型が追加された（本スクリプトの"
             "TYPE_GUIDE と x_mention_dict.PIN_TYPE_LABELS へ訳語を追加すること）", by_type[t])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    n_listed = sum(1 for l in lines if l.startswith("- "))
    print(f"{OUT} を生成（台帳{len(rows)}社／掲載{n_listed}社"
          + (f"／⚠️未知の型 {unknown} を未分類節へ掲載" if unknown else "") + "）")
    return 0 if n_listed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
