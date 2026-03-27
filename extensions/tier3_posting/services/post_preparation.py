"""投稿準備サービス。

文字数計算、ハッシュタグ結合、日時正規化をBE側に一元化。
FE (review.html) はこのサービスの計算結果を受け取るだけにする。
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

CHAR_LIMIT = 280
JST = timezone(timedelta(hours=9))
OPTIMAL_HOURS = [7, 8, 12, 18, 20, 21]

# ステータス定義（Single Source of Truth）
STATUSES = ["draft", "approved", "scheduled", "posted", "rejected", "failed", "archived"]

# ステータスごとの利用可能アクション
STATUS_ACTIONS = {
    "draft": ["approve", "edit", "archive"],
    "approved": ["edit", "reject", "archive"],
    "scheduled": ["edit", "reject"],
    "posted": [],
    "rejected": ["edit", "approve", "archive"],
    "failed": ["retry", "archive"],
    "archived": ["restore"],
}

DEFAULT_OFFSET_DAYS = 30


def compute_full_post_length(body: str, hashtags: List[str] = None) -> int:
    """ハッシュタグ込みの最終投稿文字数を計算する。

    投稿スクリプトと同じロジック:
    body + "\\n\\n" + " ".join(hashtags) が280以内なら結合。
    """
    length = len(body) if body else 0
    if hashtags:
        tags_str = " ".join(hashtags)
        if length + len(tags_str) + 2 <= CHAR_LIMIT:
            length += 2 + len(tags_str)
    return length


def build_final_post_text(body: str, hashtags: List[str] = None) -> str:
    """最終投稿テキストを組み立てる（ハッシュタグ結合）。

    schedule.py / post.py が使う正式な本文組立。
    """
    if not body:
        return ""
    if hashtags:
        tags_str = " ".join(hashtags)
        if len(body) + len(tags_str) + 2 <= CHAR_LIMIT:
            return f"{body}\n\n{tags_str}"
    return body


def normalize_datetime_jst(dt_str: str) -> Optional[str]:
    """日時文字列をJST ISO 8601に正規化する。

    入力: 任意のISO 8601文字列（Z, +00:00, +09:00等）
    出力: YYYY-MM-DDTHH:MM:SS+09:00
    """
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        jst_dt = dt.astimezone(JST)
        return jst_dt.isoformat()
    except (ValueError, TypeError):
        return dt_str  # パース失敗時はそのまま返す


def to_date_key_jst(dt_str: str) -> Optional[str]:
    """日時文字列からJST基準の日付キー(YYYY-MM-DD)を返す。"""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        jst_dt = dt.astimezone(JST)
        return jst_dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def compute_char_class(remaining: int) -> str:
    """残文字数からCSS classを返す。"""
    if remaining < 0:
        return "over"
    if remaining <= 40:
        return "warn"
    return "ok"


def is_stale(created_at: str, days: int = 7) -> bool:
    """ドラフトが指定日数以上古いか判定する。"""
    if not created_at:
        return False
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - dt
        return age.days >= days
    except (ValueError, TypeError):
        return False


def compute_stale_days(created_at: str) -> int:
    """作成からの経過日数を返す。"""
    if not created_at:
        return 0
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - dt
        return age.days
    except (ValueError, TypeError):
        return 0
