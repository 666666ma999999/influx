"""週次インフルエンサー収集（凍結34 research_accounts）。

凍結リストの research_accounts を phase_collect() 用 candidates に変換し、
直近8日分を収集する。抽出・取り込み・採点はラッパー側で行う。
stdout の RW_SUMMARY JSON で対象数・成功ファイル数・投稿0件の口座数・
総投稿数を渡し、output/research/weekly_log.md に1行追記する。

Usage（Docker VNCコンテナ内での実行を想定）:
    python scripts/research_weekly.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.research_influencers import (  # noqa: E402
    RESEARCH_DIR,
    ensure_research_dir,
    phase_collect,
)

FROZEN_LIST_PATH = "data/influencer_list_frozen_2026-07-05.json"
WEEKLY_LOG_PATH = os.path.join(RESEARCH_DIR, "weekly_log.md")
COLLECT_LOOKBACK_DAYS = 8  # 週次(7日)+1日のオーバーラップ猶予


def _load_frozen_list(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _to_candidates(frozen: dict) -> list:
    """凍結 research_accounts（34 username文字列）を candidates 形式へ変換する。"""
    return [
        {"username": username.strip(), "score": 0}
        for username in frozen.get("research_accounts", [])
        if username.strip()
    ]


def _contrarian_usernames(frozen: dict) -> set:
    """group.is_contrarian=true のグループに属するアカウント名集合を返す。

    phase_collect() はGrok discovery候補用に作られておりis_contrarianを
    知らないため、collector/config.py の INFLUENCER_GROUPS と同じ
    グループ単位のフラグを凍結リストから読み直して後段で補完する。
    research_accounts と交差しない口座は除外する。
    """
    usernames = set()
    for group in frozen.get("collection_groups", {}).values():
        if group.get("is_contrarian"):
            for acc in group.get("accounts", []):
                username = acc.get("username", "").strip()
                if username:
                    usernames.add(username)
    return usernames & {c["username"] for c in _to_candidates(frozen)}


def _apply_contrarian_flag(collected_files: list, contrarian_usernames: set) -> None:
    """収集済みtweets_<username>.jsonに is_contrarian を付与し直す（該当ファイルのみ）。"""
    for fp in collected_files:
        username = os.path.splitext(os.path.basename(fp))[0]
        if username.startswith("tweets_"):
            username = username[len("tweets_"):]
        if username not in contrarian_usernames:
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                tweets = json.load(f)
            for tweet in tweets:
                tweet["is_contrarian"] = True
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(tweets, f, ensure_ascii=False, indent=2)
            print(f"  is_contrarian=True を付与: {fp} ({len(tweets)}件)")
        except (OSError, json.JSONDecodeError) as e:
            print(f"警告: {fp} への is_contrarian 付与に失敗: {e}")


def main() -> int:
    ensure_research_dir()

    frozen = _load_frozen_list(FROZEN_LIST_PATH)
    candidates = _to_candidates(frozen)
    contrarian_usernames = _contrarian_usernames(frozen)
    print(f"凍結リストから{len(candidates)}アカウントを読み込み: {FROZEN_LIST_PATH}")
    if contrarian_usernames:
        print(f"逆指標アカウント: {sorted(contrarian_usernames)}")

    if not candidates:
        print("エラー: 凍結リストにアカウントがありません")
        return 1

    adapter_path = os.path.join(RESEARCH_DIR, "_frozen34_candidates.json")
    with open(adapter_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "discovered_at": datetime.now().isoformat(),
                "keywords": ["(frozen_list_2026-07-05)"],
                "candidates_count": len(candidates),
                "candidates": candidates,
                "errors": [],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    since = (datetime.now() - timedelta(days=COLLECT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    until = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    collected_files = phase_collect(
        candidates_files=[adapter_path],
        max_collect=len(candidates),
        scrolls=10,
        since=since,
        until=until,
        screening_files=[],  # 凍結34アカウントの実行にGrok discoveryのスクリーニングは無関係
    )

    if contrarian_usernames:
        _apply_contrarian_flag(collected_files, contrarian_usernames)

    total_tweets = 0
    accounts_with_posts = set()
    for fp in collected_files:
        with open(fp, "r", encoding="utf-8") as f:
            tweets = json.load(f)
        if not isinstance(tweets, list):
            raise ValueError(f"収集結果は投稿配列である必要があります: {fp}")
        total_tweets += len(tweets)
        if tweets:
            username = os.path.splitext(os.path.basename(fp))[0].removeprefix("tweets_")
            accounts_with_posts.add(username)

    # phase_collect は投稿0件・収集エラーの口座のファイルを返さない。
    # 今回返された非空ファイルだけを数え、過去のファイルで成功を水増ししない。
    zero_post_accounts = sum(
        c["username"] not in accounts_with_posts for c in candidates
    )
    print("RW_SUMMARY " + json.dumps({
        "accounts": len(candidates),
        "collected_files": len(collected_files),
        "zero_post_accounts": zero_post_accounts,
        "total_tweets": total_tweets,
    }, ensure_ascii=False), flush=True)

    summary_line = (
        f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} JST: "
        f"対象{len(candidates)}アカウント中{len(collected_files)}件収集成功・"
        f"ツイート{total_tweets}件・投稿0件{zero_post_accounts}口座・抽出はラッパー側"
    )
    with open(WEEKLY_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(summary_line + "\n")

    print(f"\n週次リサーチ完了: {summary_line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
