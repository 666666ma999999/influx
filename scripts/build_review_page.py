#!/usr/bin/env python3
"""PostStoreのデータをreview.htmlに埋め込むスクリプト。"""

import argparse
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions.tier3_posting.x_poster.post_store import PostStore

EMBED_PATTERN = re.compile(r"const EMBEDDED_DATA = \[.*?\];", re.DOTALL)


def encode_images_base64(drafts: list) -> list:
    """ドラフトの画像ファイルをBase64エンコードしてインライン化"""
    for draft in drafts:
        images = draft.get("images", [])
        images_base64 = []
        for img in images:
            img_path = img.get("path", "")
            if os.path.exists(img_path):
                try:
                    with open(img_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    ext = os.path.splitext(img_path)[1].lower()
                    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext.lstrip("."), "image/png")
                    images_base64.append({
                        "data": f"data:{mime};base64,{b64}",
                        "type": img.get("type", ""),
                        "description": img.get("description", ""),
                    })
                except Exception as e:
                    print(f"  ⚠️ 画像エンコードエラー ({img_path}): {e}")
        if images_base64:
            draft["images_base64"] = images_base64
    return drafts


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

    # インプレッションデータのマージ
    latest_impressions = store.get_latest_impressions()
    for draft in drafts:
        news_id = draft.get("news_id")
        if news_id and news_id in latest_impressions:
            draft["impressions"] = latest_impressions[news_id]

    # 画像データのBase64エンコード
    drafts = encode_images_base64(drafts)

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
