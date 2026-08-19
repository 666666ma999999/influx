#!/usr/bin/env python3
"""check_metric_contract.py — 「0は書かない」契約の見張り（report-only・毎日実行）。

2026-08-19 敵対レビュー S4（不一致資産化）: 計測の裁定をルール文で終わらせず、
機械が毎日検査する。検査は2点:

 1. bookmarks.jsonl の凍結検査
    2026-08-19 に数値計測を廃止した（新規行の指標は全て null）。廃止前の遺物
    （int の指標を持つ行・当日実測 243 行）より「数値入り行」が増えていたら、
    どこかが再び数値を書き始めた＝契約違反として警告する。
 2. 新規行の null 検査
    遺物件数を超えた分の行に int の指標が1つでもあれば、その行を表示する。

exit code: 0=違反なし / 1=違反あり（呼び出し側は warn 表示のみ・ジョブは止めない）
使い方: python3 scripts/check_metric_contract.py [--bookmarks PATH]
"""
import argparse
import json
import sys
from pathlib import Path

# 2026-08-19 の計測廃止時点で存在した「数値入り行」の凍結数（増えたら違反）
LEGACY_NUMERIC_ROWS = 243

METRIC_KEYS = ("like_count", "retweet_count", "reply_count", "bookmark_count", "view_count")
DEFAULT_BOOKMARKS = Path.home() / "Desktop/biz/influx/output/bookmarks.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bookmarks", default=str(DEFAULT_BOOKMARKS))
    args = ap.parse_args()

    p = Path(args.bookmarks)
    if not p.exists():
        # 見張り対象の消失は「正常」ではない（パス変更で見張りが黙って無効化されるのを防ぐ）
        print(f"[metric-contract] ❌ 監視対象が無い: {p}（移動したなら本スクリプトのパスも直すこと）")
        return 1

    numeric_rows = 0
    bad_examples = []
    total = 0
    broken = 0
    for line in p.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        total += 1
        if any(isinstance(d.get(k), int) for k in METRIC_KEYS):
            numeric_rows += 1
            if numeric_rows > LEGACY_NUMERIC_ROWS and len(bad_examples) < 3:
                bad_examples.append(d.get("url", "?"))

    rc = 0
    if broken:
        print(f"[metric-contract] ❌ JSONL 破損行 {broken} 行（凍結行の毀損の可能性・要調査）")
        rc = 1
    if numeric_rows > LEGACY_NUMERIC_ROWS:
        print(f"[metric-contract] ❌ 違反: 数値入り行が {numeric_rows} 行（凍結値 "
              f"{LEGACY_NUMERIC_ROWS} を超過）。計測廃止後に誰かが数値を書いている。"
              f" 例: {bad_examples}")
        print("[metric-contract]    正= 新規行の指標は null 固定"
              "（fetch_bookmarks.py ヘッダの 2026-08-19 履歴・x_metrics_lib.py が唯一の計測口）")
        rc = 1
    elif numeric_rows < LEGACY_NUMERIC_ROWS:
        # 凍結は「不変」＝減るのも違反（行削除・null 化・差し替えを検知する）
        print(f"[metric-contract] ❌ 違反: 数値入り行が {numeric_rows} 行（凍結値 "
              f"{LEGACY_NUMERIC_ROWS} を下回った）。凍結済みの過去行が削除・書き換えられている。")
        rc = 1

    if rc == 0:
        print(f"[metric-contract] OK: 全{total}行・数値入り行 {numeric_rows}"
              f"（凍結値 {LEGACY_NUMERIC_ROWS} と一致）・破損行 0")
    return rc


if __name__ == "__main__":
    sys.exit(main())
