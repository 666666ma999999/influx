"""週次インフルエンサー勝率リサーチ（凍結34アカウント・回収案R3）。

data/influencer_list_frozen_2026-07-05.json の34アカウントを対象に、
scripts/research_influencers.py の phase_collect / phase_evaluate をそのまま
再利用して「直近8日分の収集 → シグナル抽出・評価」を行い、
output/research/weekly_log.md に簡易サマリを1行追記する（累積の実体は
ResearchStore の signals.jsonl / evaluations.jsonl・重複はsignal_idで自動排除）。

凍結リストは research_influencers.py の discovery_*.json とスキーマが異なる
（collection_groups入れ子）ため、本スクリプトは phase_collect() が要求する
candidates 形式（{"candidates": [{"username": ...}, ...]}）への変換アダプタ
のみを担う。収集・評価ロジック自体は research_influencers.py の既存実装を
呼び出すだけで、重複実装は行わない（Canonical Module原則）。

phase_evaluate() は今回収集した tweet_files のみに限定して呼び出す
（デフォルトのNone挙動＝output/research/tweets_*.json全件だと、過去に
収集済みの全ツイートを毎週再抽出してAPIコストが際限なく増える）。

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
    phase_evaluate,
)

FROZEN_LIST_PATH = "data/influencer_list_frozen_2026-07-05.json"
WEEKLY_LOG_PATH = os.path.join(RESEARCH_DIR, "weekly_log.md")
COLLECT_LOOKBACK_DAYS = 8  # 週次(7日)+1日のオーバーラップ猶予


def _load_frozen_list(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _to_candidates(frozen: dict) -> list:
    """凍結リスト(collection_groups入れ子)を phase_collect() 用の candidates 形式へ変換する。"""
    candidates = []
    for group in frozen.get("collection_groups", {}).values():
        for acc in group.get("accounts", []):
            username = acc.get("username", "").strip()
            if username:
                candidates.append({"username": username, "score": 0})
    return candidates


def _contrarian_usernames(frozen: dict) -> set:
    """group.is_contrarian=true のグループに属するアカウント名集合を返す。

    phase_collect() はGrok discovery候補用に作られておりis_contrarianを
    知らないため、collector/config.py の INFLUENCER_GROUPS と同じ
    グループ単位のフラグを凍結リストから読み直して後段で補完する。
    """
    usernames = set()
    for group in frozen.get("collection_groups", {}).values():
        if group.get("is_contrarian"):
            for acc in group.get("accounts", []):
                username = acc.get("username", "").strip()
                if username:
                    usernames.add(username)
    return usernames


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
    for fp in collected_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                total_tweets += len(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            print(f"警告: {fp} の読み込みに失敗: {e}")

    if collected_files:
        eval_count = phase_evaluate(tweet_files=collected_files)
    else:
        print("収集ファイルが0件のためシグナル抽出をスキップ")
        eval_count = 0

    summary_line = (
        f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} JST: "
        f"対象{len(candidates)}アカウント中{len(collected_files)}件収集成功・"
        f"ツイート{total_tweets}件・評価{eval_count}件"
    )
    with open(WEEKLY_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(summary_line + "\n")

    print(f"\n週次リサーチ完了: {summary_line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
