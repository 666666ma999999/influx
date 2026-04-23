"""post_history.jsonl の旧レコードに account_id を後方補完するスクリプト。

drafts.jsonl の news_id → account_id マッピングを構築し、history 側で
account_id が欠落しているレコードに値を埋める。本文キーワード判定への
フォールバックは使わず、drafts に存在しない news_id はそのまま据え置く
（誤った補完を避けるため）。

Usage:
    python scripts/backfill_history_account.py --dry-run
    python scripts/backfill_history_account.py
    python scripts/backfill_history_account.py --base-dir output/posting
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict


def _load_drafts_account_map(drafts_path: Path) -> Dict[str, str]:
    """drafts.jsonl から news_id → account_id のマップを構築。"""
    mapping: Dict[str, str] = {}
    if not drafts_path.exists():
        return mapping
    with drafts_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            nid = rec.get("news_id")
            acc = rec.get("account_id")
            if nid and acc:
                mapping[nid] = acc
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description="post_history への account_id 補完")
    parser.add_argument("--base-dir", default="output/posting", help="PostStore ベース")
    parser.add_argument("--dry-run", action="store_true", help="変更せず影響範囲のみ表示")
    parser.add_argument(
        "--default-account",
        default=None,
        help="drafts に news_id が存在しない場合のフォールバック account_id。"
        " 指定時は account_id_source=backfill_default で記録",
    )
    args = parser.parse_args()

    base = Path(args.base_dir).resolve()
    drafts = base / "drafts.jsonl"
    history = base / "post_history.jsonl"

    if not history.exists():
        print(f"ERROR: post_history.jsonl が存在しません: {history}", file=sys.stderr)
        return 1

    account_map = _load_drafts_account_map(drafts)
    print(f"drafts.jsonl から {len(account_map)} 件の news_id → account_id を取得")

    records = []
    total = 0
    with_account = 0
    backfilled = 0
    backfilled_default = 0
    skipped_no_match = 0

    with history.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            rec = json.loads(line)
            if rec.get("account_id"):
                with_account += 1
            else:
                nid = rec.get("news_id")
                acc = account_map.get(nid) if nid else None
                if acc:
                    rec["account_id"] = acc
                    rec["account_id_source"] = "backfill_from_drafts"
                    backfilled += 1
                elif args.default_account:
                    rec["account_id"] = args.default_account
                    rec["account_id_source"] = "backfill_default"
                    backfilled_default += 1
                else:
                    skipped_no_match += 1
            records.append(rec)

    print(f"\n履歴レコード: {total} 件")
    print(f"  既に account_id あり: {with_account}")
    print(f"  drafts 経由補完: {backfilled}")
    print(f"  デフォルト補完 (--default-account): {backfilled_default}")
    print(f"  スキップ（補完不能）: {skipped_no_match}")
    backfilled += backfilled_default

    if args.dry_run:
        print("\n(dry-run のため書き込みは行いません)")
        return 0

    if backfilled == 0:
        print("\n補完対象なし。書き込みスキップ。")
        return 0

    # アトミック書き込み + .bak バックアップ
    backup = history.with_suffix(history.suffix + ".bak")
    shutil.copy2(history, backup)
    print(f"バックアップ: {backup}")

    tmp = history.with_suffix(history.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(history)
    print(f"書き込み完了: {history}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
