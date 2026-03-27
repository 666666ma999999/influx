"""ドラフト作成・スキーマ管理サービス。

news_id生成、ドラフトスキーマ組立を一元化し、
server.py / compose.py の重複を解消する。
"""
import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def generate_news_id(seed: str) -> str:
    """一意のnews_idを生成する。

    Args:
        seed: ハッシュの元となる文字列
    Returns:
        16文字の16進数文字列
    """
    full_seed = f"{seed}:{os.urandom(8).hex()}"
    return hashlib.sha256(full_seed.encode()).hexdigest()[:16]


def build_draft(
    body: str,
    title: str = "",
    hashtags: List[str] = None,
    template_type: str = "manual",
    scheduled_at: str = None,
    account_id: str = None,
    images: List[Dict] = None,
    source_items: List = None,
    news_id: str = None,
    metadata: Dict = None,
) -> Dict[str, Any]:
    """標準化されたドラフト辞書を構築する。

    server.py と compose.py の両方がこの関数を使い、
    スキーマの一貫性を保証する。
    """
    if not news_id:
        news_id = generate_news_id(f"{title}:{body}")

    draft = {
        "news_id": news_id,
        "title": title or body[:30],
        "body": body,
        "format": "x_post",
        "template_type": template_type,
        "hashtags": hashtags or [],
        "status": "draft",
        "source_items": source_items or [],
    }

    if scheduled_at:
        draft["scheduled_at"] = scheduled_at
    if account_id:
        draft["account_id"] = account_id
    if images:
        draft["images"] = images
    if metadata:
        draft["metadata"] = metadata

    return draft
