#!/usr/bin/env python3
"""外部プロジェクト（make_article 等）からドラフトを登録する CLI。

外部プロジェクトが PostStore を直接 import する代わりに、本 CLI を subprocess 経由で
呼び出すことで責務分離を保つ。news_id 既存時は upsert する。

Usage:
    # 引数指定
    python -m extensions.tier3_posting.cli.register_external \\
        --news-id <hex16> \\
        --title "..." \\
        --promo-text "..." \\
        --article-body-file path/to/body.md \\
        --image-paths output/posting/images/a.png,output/posting/images/b.png \\
        --make-article-id art_013 \\
        --source-file output/drafts/art_013_xxx.md \\
        --category tech_tips \\
        --score 8.5

    # stdin JSON 入力
    echo '{"news_id":"...","title":"...","promo_text":"...","article_body":"...","image_paths":[],"metadata":{...}}' | \\
        python -m extensions.tier3_posting.cli.register_external --json -

Output (stdout, 1行 JSON):
    {"news_id": "<hex16>", "action": "added"|"updated", "ok": true}

Exit codes:
    0: 登録/更新成功
    1: 実行時エラー
    2: 引数不足・JSON パース失敗
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


def _read_article_body(args: argparse.Namespace) -> str:
    if args.article_body is not None:
        return args.article_body
    if args.article_body_file:
        return Path(args.article_body_file).read_text(encoding="utf-8")
    return ""


def _parse_image_paths(raw: Optional[str]) -> list:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _normalize_images(image_paths: list) -> list:
    """外部 I/F の `image_paths: [str]` を内部 canonical な `images: [{path,type,description}]` に変換。

    UI 表示（server.py）・X 投稿（run.py）・オフライン HTML（build_html.py）は
    すべて draft トップレベルの `images` を読むため、ここで正規化して dual-path を防ぐ。
    """
    return [{"path": p, "type": "", "description": ""} for p in image_paths]


def _build_payload_from_args(args: argparse.Namespace) -> dict:
    article_body = _read_article_body(args)
    image_paths = _parse_image_paths(args.image_paths)
    images_present = args.image_paths is not None

    metadata = {
        "make_article_id": args.make_article_id or "",
        "category": args.category or "",
        "score": args.score or "",
        "source_file": args.source_file or "",
        "article_body": article_body,
    }
    if image_paths:
        metadata["image_paths"] = image_paths

    payload = {
        "news_id": args.news_id,
        "title": args.title,
        "body": args.promo_text,
        "format": args.format,
        "template_type": args.template_type,
        "hashtags": [],
        "status": "draft",
        "metadata": metadata,
    }
    if images_present:
        payload["images"] = _normalize_images(image_paths)
    return payload


def _build_payload_from_stdin() -> dict:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: stdin JSON パース失敗: {e}", file=sys.stderr)
        sys.exit(2)

    news_id = data.get("news_id")
    title = data.get("title")
    promo_text = data.get("promo_text") or data.get("body")
    if not (news_id and title and promo_text):
        print("ERROR: news_id / title / promo_text(body) が必要", file=sys.stderr)
        sys.exit(2)

    metadata = data.get("metadata") or {}
    if "article_body" in data and "article_body" not in metadata:
        metadata["article_body"] = data["article_body"]
    images_present = "image_paths" in data
    image_paths = data.get("image_paths") or []
    if image_paths and "image_paths" not in metadata:
        metadata["image_paths"] = image_paths

    payload = {
        "news_id": news_id,
        "title": title,
        "body": promo_text,
        "format": data.get("format", "x_article"),
        "template_type": data.get("template_type", "make_article"),
        "hashtags": data.get("hashtags", []),
        "status": "draft",
        "metadata": metadata,
    }
    if images_present:
        payload["images"] = _normalize_images(image_paths)
    return payload


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="外部プロジェクトのドラフトを influx PostStore に登録/更新（upsert）",
    )
    parser.add_argument("--json", dest="json_input", help="'-' を指定すると stdin から JSON を読む")
    parser.add_argument("--base-dir", help="PostStore のベースディレクトリ（テスト用）")
    parser.add_argument("--news-id", help="決定的 news_id（16 文字 hex 推奨）")
    parser.add_argument("--title")
    parser.add_argument("--promo-text", help="X 投稿文（Xタイムライン本文）")
    parser.add_argument("--article-body", help="記事本文（直接渡す）")
    parser.add_argument("--article-body-file", help="記事本文ファイルパス（--article-body と排他）")
    parser.add_argument("--image-paths", help="influx 相対パスをカンマ区切りで指定")
    parser.add_argument("--source-file", default="", help="元 MD ファイルパス（参照用）")
    parser.add_argument("--make-article-id", default="", help="例: art_013")
    parser.add_argument("--category", default="", help="例: tech_tips")
    parser.add_argument("--score", default="", help="記事スコア")
    parser.add_argument("--format", default="x_article")
    parser.add_argument("--template-type", default="make_article")
    return parser.parse_args(argv)


def register(payload: dict, store: PostStore) -> dict:
    """add_draft を試行し、既存なら update_draft_status で upsert する。"""
    news_id = payload["news_id"]
    added = store.add_draft(payload)
    if added:
        return {"news_id": news_id, "action": "added", "ok": True}

    extra = {
        "title": payload.get("title", ""),
        "body": payload.get("body", ""),
        "metadata": payload.get("metadata", {}),
    }
    if "images" in payload:
        extra["images"] = payload["images"]
    ok = store.update_draft_status(news_id, "draft", **extra)
    if ok:
        return {"news_id": news_id, "action": "updated", "ok": True}
    return {"news_id": news_id, "action": "noop", "ok": False, "error": "update failed"}


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.json_input == "-":
        payload = _build_payload_from_stdin()
    else:
        if not (args.news_id and args.title and args.promo_text):
            print("ERROR: --news-id / --title / --promo-text が必要", file=sys.stderr)
            return 2
        payload = _build_payload_from_args(args)

    try:
        store = _resolve_post_store(args.base_dir)
    except Exception as e:
        print(f"ERROR: PostStore 初期化失敗: {e}", file=sys.stderr)
        return 1

    try:
        result = register(payload, store)
    except Exception as e:
        print(f"ERROR: 登録失敗: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
