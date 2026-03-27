"""UIビューモデル変換サービス。

PostStoreのドラフトデータをFE表示用に変換する。
FEは render() のみに集中し、計算ロジックを持たない。
"""
from typing import Any, Dict, List

from ..account_routing import resolve_account, get_account_label, ACCOUNTS, DEFAULT_ACCOUNT
from .post_preparation import (
    CHAR_LIMIT, STATUSES, STATUS_ACTIONS,
    compute_full_post_length, compute_char_class,
    is_stale, compute_stale_days,
    to_date_key_jst, normalize_datetime_jst,
    build_final_post_text, DEFAULT_OFFSET_DAYS,
)


def enrich_draft_for_ui(draft: dict) -> dict:
    """ドラフトにUI表示用の派生値を追加する。

    FEが再計算していたロジックをBE側で事前計算:
    - char_remaining, char_class
    - is_stale, stale_days
    - available_actions
    - account_id (未設定時は自動判定)
    - account_label
    - scheduled_date_key (カレンダー用)
    - final_post_preview (ハッシュタグ込み最終テキスト)
    - cli_hint (実行すべきCLIコマンド)
    """
    body = draft.get("body", "")
    hashtags = draft.get("hashtags", [])
    status = draft.get("status", "draft")

    # 文字数
    full_len = compute_full_post_length(body, hashtags)
    remaining = CHAR_LIMIT - full_len
    draft["char_remaining"] = remaining
    draft["char_class"] = compute_char_class(remaining)

    # 鮮度
    created_at = draft.get("created_at", "")
    draft["is_stale"] = is_stale(created_at)
    draft["stale_days"] = compute_stale_days(created_at)

    # アクション
    draft["available_actions"] = STATUS_ACTIONS.get(status, [])

    # アカウント
    if not draft.get("account_id"):
        draft["account_id"] = resolve_account(draft)
    draft["account_label"] = get_account_label(draft.get("account_id", DEFAULT_ACCOUNT))

    # カレンダー用日付キー
    scheduled_at = draft.get("scheduled_at")
    draft["scheduled_date_key"] = to_date_key_jst(scheduled_at)

    # 最終投稿プレビュー
    draft["final_post_preview"] = build_final_post_text(body, hashtags)

    # 日時正規化
    if scheduled_at:
        draft["scheduled_at_display"] = normalize_datetime_jst(scheduled_at)

    return draft


def build_meta() -> dict:
    """FE初期化用のメタデータを返す。

    ステータス一覧、アカウント一覧、定数などをBE側から配信し、
    FEにハードコードさせない。
    """
    return {
        "statuses": STATUSES,
        "status_actions": STATUS_ACTIONS,
        "accounts": [
            {"id": aid, "label": acc["label"], "categories": acc.get("categories", [])}
            for aid, acc in ACCOUNTS.items()
        ],
        "char_limit": CHAR_LIMIT,
        "default_offset_days": DEFAULT_OFFSET_DAYS,
        "default_account": DEFAULT_ACCOUNT,
        "optimal_hours": [7, 8, 12, 18, 20, 21],
    }


def build_cli_commands(drafts: List[dict]) -> List[dict]:
    """approved ドラフトのアカウント別CLIコマンドを生成する。"""
    by_account = {}
    for d in drafts:
        if d.get("status") == "approved":
            acc = d.get("account_id", DEFAULT_ACCOUNT)
            by_account.setdefault(acc, 0)
            by_account[acc] += 1

    commands = []
    for acc_id, count in by_account.items():
        cmd = f"docker exec xstock-vnc python -m extensions.tier3_posting.cli.run --no-dry-run --limit {count}"
        commands.append({
            "account_id": acc_id,
            "account_label": get_account_label(acc_id),
            "count": count,
            "command": cmd,
        })

    return commands
