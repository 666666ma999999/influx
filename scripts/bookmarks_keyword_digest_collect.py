#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプライン: digest収集CLI（Docker実行・xai_sdk依存）。

設計正本: ~/.claude/docs/x-keywords-plan.md 末尾セクション
「Plan: x-keywords v2 — AI 検索代行ダイジェスト」§詳細設計v2/§アーキテクチャ（C1バッチ）。

`<inbox>/latest_snapshot.json`（呼び出し側がkeywords_latest.jsonをコピーして置く）の
active_clustersごとに1回、xai_sdkのx_searchツールでX投稿を検索し、
`<inbox>/digest_raw-<run_id>.jsonl` + `<inbox>/digest_status-<run_id>.json` を
原子的に書き出す。inbox外のファイル（台帳・bookmarks.jsonl等）には一切触れない契約。

クラスタ単位でtry/exceptするfail-soft方式（grok_collect_twittora.pyの先例踏襲）。
xai_sdk（Client/x_search/system/user）のimportはmain()実行時のみ発生させる
（hostにxai_sdkが無くてもこのモジュール自体のimportは常に成功する）。

Docker実行:
    docker compose run --rm xstock python scripts/bookmarks_keyword_digest_collect.py
    docker compose run --rm xstock python scripts/bookmarks_keyword_digest_collect.py \\
        --inbox output/bookmarks/digest_inbox --days 7 --max-per-cluster 10

exit code: 0=完走（クラスタ単位のfail-softエラーはerrorsに記録の上0）/
    1=XAI_API_KEY未設定 or latest_snapshot.json不在（致命的セットアップ不備）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bookmarks_keyword_common import extract_min_faves  # noqa: E402 (common定義を再利用)

MODEL = "grok-4-1-fast-non-reasoning"
DEFAULT_INBOX = "output/bookmarks/digest_inbox"
DEFAULT_DAYS = 7
DEFAULT_MAX_PER_CLUSTER = 10
DEFAULT_MIN_LIKES_FALLBACK = 80


class DigestCandidate(BaseModel):
    id: str = Field(description="X post ID (URL の status/ の後の数字)")
    url: str = Field(description="完全な X 投稿 URL https://x.com/<author>/status/<id>")
    author: str = Field(description="X handle、@ は付けない")
    content: str = Field(description="投稿本文 (改行は \\n)")
    likes: int = Field(default=0, ge=0)
    impressions: int = Field(default=0, ge=0)
    posted_at: str = Field(default="", description="YYYY-MM-DD")


class DigestCollectionResult(BaseModel):
    candidates: list[DigestCandidate] = Field(default_factory=list)


# --- pure ヘルパー（xai_sdk非依存・host単体テスト可能） ------------------------------
# extract_min_favesはbookmarks_keyword_common.pyへ移動済み（上でimportして再利用）。

def select_min_likes_and_seed(queries: list[dict]) -> tuple[int, str | None]:
    """クラスタのqueriesからmin_faves中央値と、その値を持つ代表クエリのquery_idを返す。

    min_favesを持つクエリが1件も無ければ(DEFAULT_MIN_LIKES_FALLBACK, None)を返す。
    偶数件の場合は下位中央値（ソート後 (n-1)//2 番目）を採用し、常に単一の
    代表query_idを一意に決定できるようにする。
    """
    candidates = []
    for q in queries:
        mf = extract_min_faves(q.get("q", ""))
        if mf is not None:
            candidates.append((mf, q.get("query_id")))
    if not candidates:
        return DEFAULT_MIN_LIKES_FALLBACK, None
    candidates.sort(key=lambda t: (t[0], t[1] or ""))
    idx = (len(candidates) - 1) // 2
    return candidates[idx]


def build_digest_prompt(cluster: dict, min_likes: int, days: int, max_per_cluster: int) -> str:
    keywords = [kw.get("keyword", "") for kw in cluster.get("keywords", []) if kw.get("keyword")]
    return (
        f"以下のテーマに関心があります: {cluster.get('name', '')}\n"
        f"理由: {cluster.get('why', '')}\n"
        f"関連キーワード: {', '.join(keywords)}\n\n"
        f"過去 {days} 日以内に X (Twitter) に投稿された、このテーマに関連する投稿を"
        f"最大 {max_per_cluster} 件探してください。\n"
        f"- いいね {min_likes} 件以上\n"
        "- 日本語の投稿を主体とする\n"
        "- 除外: 広告/宣伝/bot/懸賞・プレゼント企画\n\n"
        "各投稿についてDigestCandidateスキーマ通りに構造化して返してください。\n"
        "id は URL の status/ の後の数字、url は https://x.com/<author>/status/<id> 形式。"
        "実在する投稿のみを返し、URL/IDを捏造しないこと。"
    )


def normalize_item(candidate: dict, cluster_id: str, seed_query_id: str | None) -> dict:
    """DigestCandidate.model_dump()相当のdictをdigest_raw.jsonlの1行分へ整形する。

    厳密な検証（URL形式・Snowflake窓・likes下限の再チェック等）はapply側の契約。
    """
    return {
        "url": candidate.get("url", ""),
        "id": candidate.get("id", ""),
        "author": candidate.get("author", ""),
        "content": candidate.get("content", ""),
        "likes": candidate.get("likes", 0),
        "impressions": candidate.get("impressions", 0),
        "posted_at": candidate.get("posted_at", ""),
        "cluster_id": cluster_id,
        "seed_query_id": seed_query_id,
    }


def build_status(
    run_id: str,
    source: dict,
    per_cluster_counts: dict,
    errors: list,
    generated_at: str,
    collect_days: int,
    complete: bool = True,
) -> dict:
    return {
        "run_id": run_id,
        "source": source,
        "generated_at": generated_at,
        "collect_days": collect_days,
        "per_cluster_counts": per_cluster_counts,
        "errors": errors,
        "complete": complete,
    }


def load_latest_snapshot(inbox: Path) -> dict:
    snapshot_path = inbox / "latest_snapshot.json"
    if not snapshot_path.exists():
        raise FileNotFoundError(f"{snapshot_path} が存在しない（呼び出し側がkeywords_latest.jsonを配置する契約）")
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-digest-raw-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-digest-status-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- xai_sdk依存部（この関数の呼び出し時にのみxai_sdkをimportする） -------------------

def call_x_search(client: Any, prompt: str, days: int) -> DigestCollectionResult | None:
    from xai_sdk.chat import system, user
    from xai_sdk.tools import x_search

    to_dt = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=days)
    chat = client.chat.create(model=MODEL, tools=[x_search(from_date=from_dt, to_date=to_dt)])
    chat.append(system(
        "あなたはX (Twitter) 上の投稿を発見する分析アナリストです。"
        "実在する投稿のみを返し、URL/IDを捏造しないこと。"
    ))
    chat.append(user(prompt))
    _, parsed = chat.parse(DigestCollectionResult)
    return parsed


def collect_for_cluster(client: Any, cluster: dict, days: int, max_per_cluster: int) -> list[dict]:
    cid = cluster.get("cluster_id")
    min_likes, seed_query_id = select_min_likes_and_seed(cluster.get("queries", []))
    prompt = build_digest_prompt(cluster, min_likes, days, max_per_cluster)
    parsed = call_x_search(client, prompt, days)
    if not parsed:
        return []
    return [
        normalize_item(c.model_dump(), cid, seed_query_id)
        for c in parsed.candidates[:max_per_cluster]
    ]


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--max-per-cluster", type=int, default=DEFAULT_MAX_PER_CLUSTER)
    ap.add_argument("--run-id", default=None)
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    from xai_sdk import Client  # 遅延import（host非依存を保つため）

    inbox = Path(args.inbox)
    run_id = args.run_id or uuid.uuid4().hex

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("ERROR: XAI_API_KEY 環境変数が未設定", file=sys.stderr)
        return 1

    try:
        snapshot = load_latest_snapshot(inbox)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    client = Client(api_key=api_key)
    active_clusters = snapshot.get("active_clusters", [])

    print(f"=== bookmarks_keyword_digest_collect run_id={run_id} ===")
    print(f"active_clusters: {len(active_clusters)}, days: {args.days}, max_per_cluster: {args.max_per_cluster}")

    rows: list[dict] = []
    per_cluster_counts: dict[str, int] = {}
    errors: list[dict] = []

    for cluster in active_clusters:
        cid = cluster.get("cluster_id")
        print(f"  → collecting: {cluster.get('name', cid)!r}")
        try:
            items = collect_for_cluster(client, cluster, args.days, args.max_per_cluster)
        except Exception as exc:
            print(f"    ✗ 失敗: {exc}", file=sys.stderr)
            errors.append({"cluster_id": cid, "error": str(exc)})
            per_cluster_counts[cid] = 0
            continue
        print(f"    got {len(items)} candidates")
        rows.extend(items)
        per_cluster_counts[cid] = len(items)

    generated_at = datetime.now(timezone.utc).isoformat()
    status = build_status(
        run_id=run_id,
        source={
            "generation": snapshot.get("generation"),
            "revision": snapshot.get("revision"),
            "urls_sha256": (snapshot.get("source_snapshot") or {}).get("urls_sha256"),
        },
        per_cluster_counts=per_cluster_counts,
        errors=errors,
        generated_at=generated_at,
        collect_days=args.days,
        complete=True,
    )

    raw_path = inbox / f"digest_raw-{run_id}.jsonl"
    status_path = inbox / f"digest_status-{run_id}.json"
    atomic_write_jsonl(raw_path, rows)
    atomic_write_json(status_path, status)

    print(f"\n✓ {raw_path} ({len(rows)} entries)")
    print(f"✓ {status_path} (errors={len(errors)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
