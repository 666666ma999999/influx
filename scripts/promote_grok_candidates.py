#!/usr/bin/env python3
"""Grok リサーチ score 上位候補を INFLUENCER_GROUPS の diff として出力する。

plan.md M0 T0.7: `output/research/research_scorecard.json` を読み、
`score >= threshold` (既定 50 — plan.md 2026-04-24 Decision で 70→50 緩和)
のインフルエンサーのうち `INFLUENCER_GROUPS` 未登録のアカウントを
stdout に YAML 風 diff で出力する。

使い方:
    python scripts/promote_grok_candidates.py
    python scripts/promote_grok_candidates.py --threshold 40 --scorecard output/research/research_scorecard.json
    python scripts/promote_grok_candidates.py --format json
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collector.config import INFLUENCER_GROUPS

# plan.md M0 T0.7: suggest_group の閾値テーブル（score ≥ threshold → group）
# 2026-04-24 時点の構造的上限が 60.0 であることを踏まえた保守的な割当:
#   高 score ほど大型グループに、低 score は小型グループに寄せる
SUGGESTION_RULES = (
    # (min_score, min_trackable, group_key, default_min_faves)
    (55.0, 15, "group1", 300),
    (50.0, 10, "group2", 80),
    (40.0, 5, "group3", 30),
    (30.0, 0, "group4", 20),
    (0.0, 0, "group5", 10),
)

# plan.md 2026-04-24 Decision: Exit #3 閾値 70→50 緩和
DEFAULT_THRESHOLD = 50.0


def registered_usernames() -> set[str]:
    """INFLUENCER_GROUPS に登録済みの username を小文字セットで返す。"""
    names: set[str] = set()
    for group in INFLUENCER_GROUPS.values():
        for acc in group.get("accounts", []):
            u = acc.get("username")
            if u:
                names.add(u.lower())
    return names


def suggest_group(score: float, trackable: int) -> str:
    """score と trackable から推奨グループを決める（SUGGESTION_RULES に従う）。"""
    for min_score, min_trackable, group_key, _ in SUGGESTION_RULES:
        if score >= min_score and trackable >= min_trackable:
            return group_key
    return "group5"


def suggest_min_faves(group: str) -> int:
    """推奨グループから min_faves の下限値を決める。"""
    for _, _, group_key, default_min_faves in SUGGESTION_RULES:
        if group_key == group:
            return default_min_faves
    return 20


def build_diff(scorecard: dict, threshold: float) -> list[dict]:
    """scorecard から未登録かつ score >= threshold の候補を返す。

    tiebreak は plan.md M0 Exit #4 準拠で `avg_return_pct` 20BD。
    """
    registered = registered_usernames()
    candidates: list[dict] = []

    for username, stats in scorecard.get("influencers", {}).items():
        score = stats.get("score", 0.0)
        if score < threshold:
            continue
        if username.lower() in registered:
            continue

        h20 = stats.get("horizon_20bd", {})
        trackable = h20.get("trackable", 0)
        group = suggest_group(score, trackable)
        candidates.append({
            "username": username,
            "display_name": stats.get("display_name", ""),
            "score": score,
            "trackable_20bd": trackable,
            "win_rate_20bd": h20.get("win_rate", 0.0),
            "avg_return_pct_20bd": h20.get("avg_return_pct", 0.0),
            "suggested_group": group,
            "suggested_min_faves": suggest_min_faves(group),
        })

    # sort: score 降順、tiebreak は avg_return_pct_20bd 降順
    candidates.sort(
        key=lambda c: (c["score"], c["avg_return_pct_20bd"]),
        reverse=True,
    )
    return candidates


def format_yaml(candidates: list[dict]) -> str:
    """INFLUENCER_GROUPS に貼り付けやすい YAML 風 diff を生成する。"""
    if not candidates:
        return "# 該当候補なし（score >= threshold の未登録アカウントが見つかりませんでした）"

    lines = [
        "# plan.md M0 T0.7: Grok リサーチ promote diff",
        f"# 候補数: {len(candidates)}",
        "# 貼り付け先: collector/config.py INFLUENCER_GROUPS",
        "",
    ]

    by_group: dict[str, list[dict]] = {}
    for c in candidates:
        by_group.setdefault(c["suggested_group"], []).append(c)

    for group, items in sorted(by_group.items()):
        lines.append(f"# === {group} に追加 ===")
        for c in items:
            lines.append(
                f'    {{"username": "{c["username"]}", "min_faves": {c["suggested_min_faves"]}}},'
                f'  # score={c["score"]} trackable={c["trackable_20bd"]}'
                f' win_rate={c["win_rate_20bd"]}% display="{c["display_name"]}"'
            )
        lines.append("")

    return "\n".join(lines)


EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NOT_FOUND = 2
EXIT_INVALID_JSON = 3
EXIT_INVALID_SCHEMA = 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scorecard",
        default="output/research/research_scorecard.json",
        help="scorecard JSON のパス",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"score 閾値（既定 {DEFAULT_THRESHOLD}、plan.md 2026-04-24 Decision に準拠）",
    )
    parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="出力形式",
    )
    args = parser.parse_args()

    scorecard_path = Path(args.scorecard)
    if not scorecard_path.exists():
        print(f"エラー: scorecard が見つかりません: {scorecard_path}", file=sys.stderr)
        return EXIT_NOT_FOUND

    try:
        with open(scorecard_path, encoding="utf-8") as f:
            scorecard = json.load(f)
    except json.JSONDecodeError as e:
        print(f"エラー: scorecard JSON の parse に失敗: {scorecard_path}: {e}", file=sys.stderr)
        return EXIT_INVALID_JSON
    except OSError as e:
        print(f"エラー: scorecard 読み込みに失敗: {scorecard_path}: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND

    if not isinstance(scorecard, dict) or "influencers" not in scorecard:
        print(
            f"エラー: scorecard schema 不正（'influencers' キーが見つかりません）: {scorecard_path}",
            file=sys.stderr,
        )
        return EXIT_INVALID_SCHEMA

    candidates = build_diff(scorecard, args.threshold)

    if args.format == "json":
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
    else:
        print(format_yaml(candidates))

    print(f"\n# 該当候補: {len(candidates)} 件 (score >= {args.threshold})", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
