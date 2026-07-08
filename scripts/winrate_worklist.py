#!/usr/bin/env python3
"""インフルエンサー勝率アルゴ P1: 未抽出ツイートの洗い出し。

docs/influencer-winrate-spec.md §5 F2 の前段（抽出対象の選定）を担う。
output/research/tweets_*.json 全件から、signals.jsonl に既に抽出済み
（tweet_url が一致）のツイートを除外し、未抽出分を extraction_worklist.json
に整形出力する。株と無関係な明らかな雑談は軽量なキーワードプリフィルタで
除外するが、判断に迷うものは含める（過剰除外しない）。

Usage:
    python3 scripts/winrate_worklist.py
    python3 scripts/winrate_worklist.py --tweets-glob "output/research/tweets_*.json" \
        --output output/research/extraction_worklist.json
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector.ticker_extractor import TickerExtractor  # noqa: E402  (Canonical Module: 既存ティッカー検出を再利用)
from collector.config import CLASSIFICATION_RULES  # noqa: E402  (Canonical Module: 既存キーワード辞書を再利用)
from extensions.tier1_collection.grok_discoverer.research_store import ResearchStore  # noqa: E402

RESEARCH_DIR = "output/research"
DEFAULT_TWEETS_GLOB = os.path.join(RESEARCH_DIR, "tweets_*.json")
DEFAULT_OUTPUT_PATH = os.path.join(RESEARCH_DIR, "extraction_worklist.json")

# 4桁の銘柄コードらしき数字列（前後が数字でない=日付/電話番号等との衝突をある程度回避）
STOCK_CODE_RE = re.compile(r'(?<!\d)\d{4}(?!\d)')
CASHTAG_RE = re.compile(r'\$[A-Z]{1,5}\b')
# この文字数未満かつ金融シグナルが一切ない場合のみ除外（過剰除外防止のための保守的な閾値）
MIN_LEN_WITHOUT_SIGNAL = 20

_FINANCE_KEYWORDS = set()
for _rule in CLASSIFICATION_RULES.values():
    _FINANCE_KEYWORDS.update(_rule.get("keywords", []))


def has_finance_signal(text: str, extractor: TickerExtractor) -> bool:
    """テキストに株式投資関連のシグナルらしき手がかりがあるかを判定する（軽量ヒューリスティック）。"""
    if not text:
        return False
    if STOCK_CODE_RE.search(text):
        return True
    if CASHTAG_RE.search(text):
        return True
    if extractor.extract({"text": text}):
        return True
    return any(kw in text for kw in _FINANCE_KEYWORDS)


def should_include(text: str, extractor: TickerExtractor) -> bool:
    """プリフィルタ判定。

    金融シグナルの手がかりがあれば無条件で採用する。手がかりが無い場合でも、
    ある程度の長さがあるツイートは判断を抽出フェーズ（サブエージェント）に
    委ねるため採用する。明らかに短い雑談のみを除外する。
    """
    stripped = (text or "").strip()
    if has_finance_signal(stripped, extractor):
        return True
    return len(stripped) >= MIN_LEN_WITHOUT_SIGNAL


def load_extracted_tweet_urls(research_dir: str) -> set:
    """signals.jsonl に既に記録済みの tweet_url 集合を返す（重複抽出防止）。"""
    store = ResearchStore(base_dir=research_dir)
    return {s.get("tweet_url") for s in store.load_signals() if s.get("tweet_url")}


def build_worklist(tweets_glob: str, research_dir: str) -> dict:
    """未抽出ツイートのワークリストを構築する。"""
    extractor = TickerExtractor()
    extracted_urls = load_extracted_tweet_urls(research_dir)

    tweet_files = sorted(glob.glob(tweets_glob))
    total_scanned = 0
    already_extracted = 0
    prefiltered_out = 0
    no_url = 0
    worklist = []
    file_errors = []

    for filepath in tweet_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                tweets = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            file_errors.append({"file": filepath, "error": str(e)})
            continue

        for tweet in tweets:
            total_scanned += 1
            tweet_url = tweet.get("url", "")
            if not tweet_url:
                no_url += 1
                continue
            if tweet_url in extracted_urls:
                already_extracted += 1
                continue

            text = tweet.get("text", "")
            if not should_include(text, extractor):
                prefiltered_out += 1
                continue

            worklist.append({
                "tweet_url": tweet_url,
                "username": tweet.get("username", ""),
                "posted_at": tweet.get("posted_at", ""),
                "text": text,
                "is_contrarian": bool(tweet.get("is_contrarian", False)),
            })

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_files_count": len(tweet_files),
        "total_tweets_scanned": total_scanned,
        "no_url": no_url,
        "already_extracted": already_extracted,
        "prefiltered_out": prefiltered_out,
        "worklist_count": len(worklist),
        "file_errors": file_errors,
        "worklist": worklist,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="未抽出ツイートの洗い出し（インフルエンサー勝率アルゴ P1）")
    parser.add_argument("--tweets-glob", default=DEFAULT_TWEETS_GLOB, help="収集済みツイートJSONのglobパターン")
    parser.add_argument("--research-dir", default=RESEARCH_DIR, help="signals.jsonl の探索元ディレクトリ")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="ワークリスト出力先パス")
    args = parser.parse_args()

    result = build_worklist(args.tweets_glob, args.research_dir)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("=== 未抽出ツイート洗い出し ===")
    print(f"対象ファイル数: {result['source_files_count']}")
    print(f"走査ツイート数: {result['total_tweets_scanned']}")
    if result["no_url"]:
        print(f"URL欠落（スキップ）: {result['no_url']}")
    print(f"抽出済み(signals.jsonlに存在): {result['already_extracted']}")
    print(f"プリフィルタ除外: {result['prefiltered_out']}")
    print(f"未抽出ワークリスト件数: {result['worklist_count']} → {args.output}")
    if result["file_errors"]:
        print(f"警告: 読み込み失敗 {len(result['file_errors'])}件")
        for err in result["file_errors"]:
            print(f"  - {err['file']}: {err['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
