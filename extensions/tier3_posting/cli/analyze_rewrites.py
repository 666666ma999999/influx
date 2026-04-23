#!/usr/bin/env python3
"""リライト学習データ分析CLI。

蓄積されたリライトペアから指示パターンを抽出し、
few-shot用の学習済み教師データ（bookmark互換スキーマ）を生成する。

Usage:
    python -m extensions.tier3_posting.cli.analyze_rewrites
    python -m extensions.tier3_posting.cli.analyze_rewrites --account maaaki
    python -m extensions.tier3_posting.cli.analyze_rewrites --min-count 3
"""
import argparse
import json
import sys
from pathlib import Path

from ..services.rewrite_store import load_rewrites, load_all_rewrites, REWRITES_DIR
from .build_style_dataset import (
    extract_features,
    detect_topic_domains,
    detect_style_format,
    detect_hook_pattern,
    detect_tone,
    write_jsonl,
)

# 指示の同義語マッピング（正規化用）
INSTRUCTION_SYNONYMS = {
    "カジュアル": ["カジュアル", "くだけた", "フランク", "ゆるく", "ゆるい"],
    "短く": ["短く", "短縮", "コンパクト", "簡潔に", "短めに"],
    "数字強調": ["数字", "具体的な数値", "データ", "数値を強調"],
    "感情的": ["感情的", "熱く", "エモく", "情熱的"],
    "フォーマル": ["フォーマル", "丁寧", "堅く", "ビジネス調"],
    "インパクト": ["インパクト", "目を引く", "キャッチー", "フック"],
}


def normalize_instruction(instruction: str) -> str:
    """指示テキストを同義語クラスタの代表形に正規化する。"""
    lower = instruction.lower().strip()
    for canonical, synonyms in INSTRUCTION_SYNONYMS.items():
        for syn in synonyms:
            if syn.lower() in lower:
                return canonical
    return instruction.strip()


def cluster_instructions(records: list) -> dict:
    """リライト記録を指示パターンでクラスタリングする。"""
    clusters = {}
    for record in records:
        key = normalize_instruction(record.get("instruction", ""))
        if key not in clusters:
            clusters[key] = []
        clusters[key].append(record)
    return clusters


def build_learned_example(record: dict) -> dict:
    """リライト記録をbookmark互換スキーマに変換する。"""
    text = record.get("rewritten_body", "")
    features = extract_features(text)
    topic_domains = detect_topic_domains(text)
    style_format = detect_style_format(features)
    hook_pattern = detect_hook_pattern(text)
    tone = detect_tone(text, features)

    return {
        "bookmark_id": f"rewrite:{record['rewrite_id']}",
        "text": text,
        "author": f"@{record.get('account_id', '')}",
        "labels": {
            "target_account": record.get("account_id", ""),
            "topic_domain": topic_domains,
            "style_format": style_format,
            "hook_pattern": hook_pattern,
            "tone": tone,
            "instruction_trigger": record.get("instruction", ""),
            "source": "rewrite",
        },
        "features": features,
        "style_notes": [],
        "rewrite_context": {
            "original_body": record.get("original_body", ""),
            "instruction": record.get("instruction", ""),
            "rewrite_id": record.get("rewrite_id", ""),
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description="リライト学習データを分析し、few-shot用教師データを生成する"
    )
    parser.add_argument(
        "--account", "-a",
        type=str,
        default=None,
        help="対象アカウントID（省略時は全アカウント）",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="学習データ化する最低リライト回数（デフォルト: 1）",
    )
    args = parser.parse_args()

    # データ読み込み
    if args.account:
        all_data = {args.account: load_rewrites(args.account)}
    else:
        all_data = load_all_rewrites()

    if not any(all_data.values()):
        print("リライト履歴がありません。", file=sys.stderr)
        sys.exit(0)

    total_input = 0
    total_output = 0

    for account_id, records in all_data.items():
        if not records:
            continue

        total_input += len(records)
        print(f"\n=== {account_id} ({len(records)}件) ===")

        # クラスタリング
        clusters = cluster_instructions(records)
        print(f"  指示パターン: {len(clusters)}種類")
        for key, items in sorted(clusters.items(), key=lambda x: -len(x[1])):
            print(f"    「{key}」: {len(items)}件")

        # 学習データ生成（min-countフィルタ）
        learned = []
        for key, items in clusters.items():
            if len(items) < args.min_count:
                continue
            # 各クラスタから最新のリライトを教師データ化
            latest = max(items, key=lambda x: x.get("accepted_at", ""))
            example = build_learned_example(latest)
            learned.append(example)

        if not learned:
            print(f"  min-count={args.min_count} を満たすパターンがありません。スキップ。")
            continue

        # 出力
        output_path = REWRITES_DIR / f"{account_id}_learned.jsonl"
        write_jsonl(output_path, learned)
        total_output += len(learned)
        print(f"  → {output_path} ({len(learned)}件)")

    print(f"\n完了: 入力 {total_input}件 → 学習データ {total_output}件")


if __name__ == "__main__":
    main()
