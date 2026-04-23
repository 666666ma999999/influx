"""リライト学習データストア。

承認されたリライトペア（修正前/修正後 + 指示）をアカウント別JSONLに保存する。
"""
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
REWRITES_DIR = PROJECT_ROOT / "data" / "writing_style" / "rewrites"

JST = timezone(timedelta(hours=9))


def _ensure_dir() -> None:
    os.makedirs(REWRITES_DIR, exist_ok=True)


def _make_rewrite_id(news_id: str, instruction: str, rewritten_body: str) -> str:
    """冪等性のためのユニークID（同一内容の重複保存防止）。"""
    raw = f"{news_id}:{instruction}:{rewritten_body}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _append_with_lock(filepath: str, line: str) -> None:
    """ファイルロック付きでJSONL行を追記する。"""
    _ensure_dir()
    with open(filepath, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def record_rewrite(
    account_id: str,
    news_id: str,
    instruction: str,
    original_body: str,
    rewritten_body: str,
    draft_metadata: dict = None,
) -> str:
    """承認されたリライトペアを保存する。

    Returns:
        rewrite_id: 生成されたID（重複時はスキップしてそのIDを返す）
    """
    rewrite_id = _make_rewrite_id(news_id, instruction, rewritten_body)

    # 重複チェック
    existing = load_rewrites(account_id)
    for r in existing:
        if r.get("rewrite_id") == rewrite_id:
            return rewrite_id

    record = {
        "rewrite_id": rewrite_id,
        "news_id": news_id,
        "account_id": account_id,
        "instruction": instruction,
        "original_body": original_body,
        "rewritten_body": rewritten_body,
        "accepted_at": datetime.now(JST).isoformat(),
        "draft_metadata": draft_metadata or {},
    }

    filepath = str(REWRITES_DIR / f"{account_id}.jsonl")
    line = json.dumps(record, ensure_ascii=False)
    _append_with_lock(filepath, line)
    return rewrite_id


def load_rewrites(account_id: str) -> list:
    """アカウント別リライト履歴を読み込む。"""
    from .style_prompt_builder import _read_jsonl
    return _read_jsonl(REWRITES_DIR / f"{account_id}.jsonl")


def load_all_rewrites() -> dict:
    """全アカウントのリライト履歴を読み込む。"""
    _ensure_dir()
    result = {}
    for path in REWRITES_DIR.glob("*.jsonl"):
        if path.name.endswith("_learned.jsonl"):
            continue
        account_id = path.stem
        result[account_id] = load_rewrites(account_id)
    return result
