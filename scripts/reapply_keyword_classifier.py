"""既存 output/ データに新 keyword classifier を再適用する（LLM API Key 不要）。

対象: `output/*/tweets.json` と `output/*/classified_llm*.json`
挙動:
- 各ツイートに `collector.classifier.TweetClassifier` を適用し `categories` を上書き
- `llm_categories` は温存（別経路の判定なので保持）
- 新キーワード（突っ込んだ/ロング/ショート/ガチホ等）+ 逆神オーバーライドで警戒カバー強化
- アトミック書き込み + `.bak` バックアップ

Usage:
    python scripts/reapply_keyword_classifier.py --dry-run
    python scripts/reapply_keyword_classifier.py --backup
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List


def main() -> int:
    parser = argparse.ArgumentParser(description="既存 output へ keyword classifier 再適用")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--dry-run", action="store_true", help="影響範囲のみ表示")
    parser.add_argument("--backup", action="store_true", help="書き込み前に .bak 作成")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from collector.classifier import TweetClassifier
    from collector.config import apply_contrarian_override

    tc = TweetClassifier()

    root = Path(args.output_dir).resolve()
    targets = sorted(root.glob("*/tweets.json")) + sorted(root.glob("*/classified_llm*.json"))

    total_files = 0
    total_tweets = 0
    total_changed = 0
    total_warn_added = 0

    for fp in targets:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  SKIP (JSON error): {fp}: {e}", file=sys.stderr)
            continue

        tweets = data.get("tweets") if isinstance(data, dict) and "tweets" in data else data
        if not isinstance(tweets, list):
            continue

        total_files += 1
        file_changes = 0
        file_warn_added = 0

        for t in tweets:
            if not isinstance(t, dict):
                continue
            total_tweets += 1
            old_cats = list(t.get("categories") or [])
            reclass = tc.classify(t)
            new_cats = reclass.get("categories", [])
            if set(old_cats) != set(new_cats):
                file_changes += 1
                total_changed += 1
                if "warning_signals" in new_cats and "warning_signals" not in old_cats:
                    file_warn_added += 1
                    total_warn_added += 1
                if not args.dry_run:
                    t["categories"] = new_cats
                    t["category_details"] = reclass.get("category_details", {})
                    t["category_count"] = reclass.get("category_count", len(new_cats))

            # LLM 出力側も逆神オーバーライドを後適用（LLM 再実行せずに警戒カバー上昇）
            is_contrarian = bool(t.get("is_contrarian", False))
            old_llm = list(t.get("llm_categories") or [])
            if is_contrarian and old_llm:
                new_llm = apply_contrarian_override(is_contrarian, old_llm)
                if set(new_llm) != set(old_llm):
                    if "warning_signals" in new_llm and "warning_signals" not in old_llm:
                        file_warn_added += 1
                        total_warn_added += 1
                    if not args.dry_run:
                        t["llm_categories"] = new_llm

        if file_changes > 0:
            rel = fp.relative_to(root.parent) if root.parent in fp.parents else fp.name
            print(f"  {'[DRY] ' if args.dry_run else ''}{rel}: tweets={len(tweets)} changed={file_changes} warn_added={file_warn_added}")
            if not args.dry_run:
                if args.backup:
                    shutil.copy2(fp, fp.with_suffix(fp.suffix + ".bak"))
                tmp = fp.with_suffix(fp.suffix + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(fp)

    print(f"\n合計: ファイル {total_files} / ツイート {total_tweets} / 変更 {total_changed} / 新規 warning_signals 付与 {total_warn_added}")
    if args.dry_run:
        print("(dry-run のため書き込み未実施)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
