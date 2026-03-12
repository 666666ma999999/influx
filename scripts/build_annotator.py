#!/usr/bin/env python3
"""merged_all.json → annotator.html へのデータ埋め込みスクリプト"""

import argparse
import json
import re
from pathlib import Path

EXTRACT_FIELDS = [
    "username", "display_name", "text", "url", "posted_at",
    "like_count", "retweet_count", "reply_count",
    "is_contrarian", "group", "group_name",
    "categories", "category_details",
    "llm_categories", "llm_reasoning", "llm_confidence"
]


def main():
    parser = argparse.ArgumentParser(description="ツイートデータをannotator.htmlに埋め込み")
    parser.add_argument("--input", default="output/merged_all.json", help="入力JSONパス")
    parser.add_argument("--html", default="output/annotator.html", help="HTMLファイルパス")
    parser.add_argument("--output", default=None, help="出力HTMLパス（デフォルト: --htmlと同じ）")
    args = parser.parse_args()

    output_path = args.output or args.html

    # JSON読み込み
    with open(args.input, "r", encoding="utf-8") as f:
        tweets = json.load(f)

    # フィールド抽出
    extracted = []
    for t in tweets:
        item = {k: t.get(k) for k in EXTRACT_FIELDS}
        extracted.append(item)

    # HTML読み込み
    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    # EMBEDDED_DATA置換
    data_json = json.dumps(extracted, ensure_ascii=False, indent=None)
    # json.dumps は文字列内の改行を既に \n (2文字) にエスケープ済み
    # ただし念のため生の制御文字もエスケープ
    data_json = data_json.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')

    # re.subn の replacement 引数はバックスラッシュシーケンスを解釈するため
    # \n が生改行に戻されてしまう。代わりに re.search + 文字列スライスで置換する。
    pattern = re.compile(r"const EMBEDDED_DATA = \[.*?\];", re.DOTALL)
    match = pattern.search(html)

    if not match:
        print("ERROR: EMBEDDED_DATA placeholder not found in HTML")
        raise SystemExit(1)

    new_snippet = f"const EMBEDDED_DATA = {data_json};"
    new_html = html[:match.start()] + new_snippet + html[match.end():]

    # 書き出し
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"埋め込み完了: {len(extracted)}件のツイート → {output_path}")


if __name__ == "__main__":
    main()
