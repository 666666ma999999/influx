"""過去データの 9→7 カテゴリ一括移行スクリプト。

plan.md M1 T1.0 の一部。`output/classified_*.json` などの既存データに含まれる
旧カテゴリ (`sold_assets`, `winning_trades`) を新カテゴリ (`purchased_assets`)
へ変換し、`llm_categories` / `categories` フィールドを書き換える。

Usage:
    python scripts/migrate_categories.py --dry-run                    # 影響範囲のみ確認
    python scripts/migrate_categories.py                              # output/ 配下を変換
    python scripts/migrate_categories.py --path output/classified.json # 単一ファイル
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# プロジェクトルート import 用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector.config import LEGACY_CATEGORY_MAP, CLASSIFICATION_RULES

ALLOWED_CATEGORIES = set(CLASSIFICATION_RULES.keys())
TARGET_FIELDS = ("llm_categories", "categories")


def migrate_categories(cats: List[str], dropped: List[str]) -> tuple[List[str], int]:
    """旧カテゴリを新カテゴリに置換し、重複を排除する。未知カテゴリは dropped に記録。"""
    new_cats: List[str] = []
    seen = set()
    converted = 0
    for c in cats:
        new_c = LEGACY_CATEGORY_MAP.get(c, c)
        if new_c != c:
            converted += 1
        if new_c not in ALLOWED_CATEGORIES:
            dropped.append(c)
            continue
        if new_c not in seen:
            seen.add(new_c)
            new_cats.append(new_c)
    return new_cats, converted


def process_file(path: Path, dry_run: bool, backup: bool) -> Dict[str, int]:
    """1 ファイルを処理。tweets list を含む JSON 形式を想定。"""
    stats = {"file": 0, "tweets": 0, "modified_tweets": 0, "converted_labels": 0, "dropped_labels": 0}

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  SKIP (JSON error): {path}: {e}", file=sys.stderr)
        return stats

    # 形式判定: dict with 'tweets' key, or list of tweet dicts
    if isinstance(data, dict) and "tweets" in data:
        tweets = data["tweets"]
    elif isinstance(data, list):
        tweets = data
    else:
        return stats

    if not isinstance(tweets, list):
        return stats

    stats["file"] = 1
    stats["tweets"] = len(tweets)
    file_modified = False
    dropped: List[str] = []

    for tweet in tweets:
        if not isinstance(tweet, dict):
            continue
        tweet_modified = False
        for field in TARGET_FIELDS:
            cats = tweet.get(field)
            if not isinstance(cats, list):
                continue
            new_cats, converted = migrate_categories(cats, dropped)
            if converted > 0 or new_cats != cats:
                if not dry_run:
                    tweet[field] = new_cats
                tweet_modified = True
                stats["converted_labels"] += converted
        if tweet_modified:
            stats["modified_tweets"] += 1
            file_modified = True

    stats["dropped_labels"] = len(dropped)
    if dropped:
        unique = sorted(set(dropped))
        print(
            f"  WARNING: {path.name}: dropped {len(dropped)} unknown category labels "
            f"(distinct={unique})",
            file=sys.stderr,
        )

    if file_modified and not dry_run:
        # アトミック書き込み + 任意バックアップ
        if backup:
            import shutil
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="9→7 カテゴリ一括移行")
    parser.add_argument("--dry-run", action="store_true", help="変更せず影響範囲のみ表示")
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="単一ファイル指定（省略時は output/*.json を探索）",
    )
    parser.add_argument(
        "--root",
        type=str,
        default="output",
        help="探索ルート（デフォルト: output）",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="書き込み前に .bak ファイルを作成する",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent

    if args.path:
        targets = [Path(args.path).resolve()]
    else:
        root = (project_root / args.root).resolve()
        if not root.exists():
            print(f"ERROR: ルートが存在しません: {root}", file=sys.stderr)
            return 1
        targets = sorted(root.rglob("*.json"))

    print(f"対象ファイル数: {len(targets)} (dry_run={args.dry_run}, backup={args.backup})")
    total = {"file": 0, "tweets": 0, "modified_tweets": 0, "converted_labels": 0, "dropped_labels": 0}
    for path in targets:
        s = process_file(path, dry_run=args.dry_run, backup=args.backup)
        if s["modified_tweets"] > 0:
            print(
                f"  {'[DRY] ' if args.dry_run else ''}"
                f"{path.relative_to(project_root)}: "
                f"tweets={s['tweets']}, modified={s['modified_tweets']}, "
                f"converted_labels={s['converted_labels']}, dropped={s['dropped_labels']}"
            )
        for k, v in s.items():
            total[k] += v

    print(
        f"\n合計: 処理ファイル={total['file']}, ツイート={total['tweets']}, "
        f"変更ツイート={total['modified_tweets']}, 変換ラベル={total['converted_labels']}, "
        f"未知ラベル drop={total['dropped_labels']}"
    )
    print(f"マッピング: {LEGACY_CATEGORY_MAP}")
    if args.dry_run:
        print("(dry-run のため書き込みは行っていません)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
