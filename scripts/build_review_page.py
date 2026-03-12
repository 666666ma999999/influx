#!/usr/bin/env python3
"""PostStoreのデータをreview.htmlに埋め込むスクリプト。"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions.tier3_posting.x_poster.post_store import PostStore

EMBED_PATTERN = re.compile(r"const EMBEDDED_DATA = \[.*?\];", re.DOTALL)


def main():
    parser = argparse.ArgumentParser(description="PostStoreデータをreview.htmlに埋め込み")
    parser.add_argument(
        "--html", default="output/posting/review.html",
        help="テンプレートHTMLパス (default: output/posting/review.html)"
    )
    parser.add_argument(
        "--output", default=None,
        help="出力HTMLパス (デフォルト: --htmlと同じ)"
    )
    parser.add_argument(
        "--data-dir", default="output/posting",
        help="PostStoreのbase_dir (default: output/posting)"
    )
    args = parser.parse_args()

    output_path = args.output or args.html

    # PostStore からデータ読み込み
    store = PostStore(base_dir=args.data_dir)
    drafts = store.load_drafts()
    history = store.load_history()

    # 履歴をnews_idでインデックス化
    history_map = {}
    for rec in history:
        nid = rec.get("news_id")
        if nid:
            history_map[nid] = rec

    # ドラフトに履歴情報をマージ
    for draft in drafts:
        nid = draft.get("news_id")
        if nid and nid in history_map:
            h = history_map[nid]
            if h.get("posted_url"):
                draft["posted_url"] = h["posted_url"]
            if h.get("posted_at"):
                draft["posted_at"] = h["posted_at"]
            if h.get("error"):
                draft["error"] = h["error"]

    print(f"ドラフト: {len(drafts)}件, 履歴: {len(history)}件")

    # HTML読み込み
    if not os.path.exists(args.html):
        print(f"ERROR: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    # EMBEDDED_DATA置換
    data_json = json.dumps(drafts, ensure_ascii=False, indent=None)
    # 制御文字エスケープ
    data_json = data_json.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')

    match = EMBED_PATTERN.search(html)
    if not match:
        print("ERROR: EMBEDDED_DATA placeholder not found in HTML")
        sys.exit(1)

    new_snippet = f"const EMBEDDED_DATA = {data_json};"
    new_html = html[:match.start()] + new_snippet + html[match.end():]

    # 書き出し
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"埋め込み完了: {len(drafts)}件 → {output_path}")


if __name__ == "__main__":
    main()
