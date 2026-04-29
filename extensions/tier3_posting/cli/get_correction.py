#!/usr/bin/env python3
"""外部プロジェクトから修正指示を取得する CLI。

外部プロジェクトが PostStore を直接 import する代わりに、本 CLI を subprocess 経由で呼び出す。
news_id または make_article_id で対象ドラフトを検索し、correction_instructions が
登録されていれば JSON で返す。

Usage:
    python -m extensions.tier3_posting.cli.get_correction \\
        --identifier art_013

    python -m extensions.tier3_posting.cli.get_correction \\
        --identifier 0123456789abcdef

Output (stdout, 1行 JSON):
    指示あり: {"news_id": "...", "make_article_id": "...", "title": "...", "correction_instructions": "..."}
    指示なし: {"news_id": null, "correction_instructions": null}

Exit codes:
    0: 取得成功（指示の有無は出力 JSON で判定）
    1: 実行時エラー
    2: 引数不足
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from ..x_poster.post_store import PostStore


def _resolve_post_store(base_dir: Optional[str] = None) -> PostStore:
    if base_dir:
        return PostStore(base_dir=base_dir)
    repo_root = Path(__file__).resolve().parents[3]
    return PostStore(base_dir=str(repo_root / "output" / "posting"))


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="news_id / make_article_id から correction_instructions を取得",
    )
    parser.add_argument("--identifier", required=True, help="news_id または make_article_id")
    parser.add_argument("--base-dir", help="PostStore ベースディレクトリ（テスト用）")
    return parser.parse_args(argv)


def find_correction(store: PostStore, identifier: str) -> dict:
    drafts = store.load_drafts()
    for d in drafts:
        nid = d.get("news_id", "")
        mid = (d.get("metadata") or {}).get("make_article_id", "")
        if nid == identifier or mid == identifier:
            ci = d.get("correction_instructions", "")
            if ci:
                return {
                    "news_id": nid,
                    "make_article_id": mid,
                    "title": d.get("title", ""),
                    "correction_instructions": ci,
                }
            return {"news_id": nid, "make_article_id": mid, "correction_instructions": None}
    return {"news_id": None, "correction_instructions": None}


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        store = _resolve_post_store(args.base_dir)
    except Exception as e:
        print(f"ERROR: PostStore 初期化失敗: {e}", file=sys.stderr)
        return 1

    try:
        result = find_correction(store, args.identifier)
    except Exception as e:
        print(f"ERROR: 修正指示取得失敗: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
