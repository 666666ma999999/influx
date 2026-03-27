#!/usr/bin/env python3
"""PostStoreのデータをreview.htmlに埋め込むスクリプト。"""

import argparse
import base64
import json
import os
import re
import sys

from ..x_poster.post_store import PostStore

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
        "--html",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "review.html"),
        help="テンプレートHTMLパス (default: extensions/tier3_posting/ui/review.html)"
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

    # PostStore からデータ読み込み（履歴・インプレッション・メタ情報統合済み）
    store = PostStore(base_dir=args.data_dir)
    drafts = store.get_all_with_history()

    # screenshot_paths をファイル名のみに変換
    for draft in drafts:
        paths = draft.get("screenshot_paths", [])
        draft["screenshot_paths"] = [os.path.basename(p) for p in paths]

    # 画像データのBase64エンコード
    drafts = encode_images_base64(drafts)

    print(f"ドラフト: {len(drafts)}件（履歴・インプレッション統合済み）")

    # HTML読み込み
    if not os.path.exists(args.html):
        print(f"ERROR: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    # EMBEDDED_DATA置換（プレースホルダーがあるHTMLのみ。API版review.htmlではスキップ）
    data_json = json.dumps(drafts, ensure_ascii=False, indent=None)
    # 制御文字エスケープ
    data_json = data_json.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')

    match = EMBED_PATTERN.search(html)
    if not match:
        print("INFO: EMBEDDED_DATA placeholder not found (API版review.html)。データJSONのみ出力します。")
        # フォールバック: JSONファイルとして出力
        json_path = output_path.replace(".html", "_data.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(drafts, f, ensure_ascii=False, indent=2)
        print(f"データ出力: {len(drafts)}件 → {json_path}")
        return

    new_snippet = f"const EMBEDDED_DATA = {data_json};"
    new_html = html[:match.start()] + new_snippet + html[match.end():]

    # 書き出し
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"埋め込み完了: {len(drafts)}件 → {output_path}")


if __name__ == "__main__":
    main()
