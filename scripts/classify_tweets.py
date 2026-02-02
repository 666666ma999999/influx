#!/usr/bin/env python3
"""
LLM Classification Script for Tweets

This script:
1. Loads tweets from JSON file (latest in output/ by default)
2. Runs LLM classification (when implemented)
3. Runs keyword classification for comparison
4. Prints comparison statistics
5. Saves classified tweets to output/classified_llm_{timestamp}.json
6. Regenerates output/viewer.html with the classified data
"""

import json
import sys
import os
import glob
import argparse
from datetime import datetime
from typing import List, Dict, Optional

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector.classifier import TweetClassifier
from collector.llm_classifier import LLMClassifier


def find_latest_tweets_json(output_dir: str = 'output') -> Optional[str]:
    """
    Find the latest tweets JSON file in the output directory.
    Uses file modification time to determine the latest file.

    Args:
        output_dir: Directory to search for tweets JSON files

    Returns:
        Path to the latest tweets JSON file, or None if not found
    """
    pattern = os.path.join(output_dir, 'tweets_*.json')
    files = glob.glob(pattern)

    if not files:
        return None

    # Sort by modification time (most recent first)
    files_with_mtime = [(f, os.path.getmtime(f)) for f in files]
    files_with_mtime.sort(key=lambda x: x[1], reverse=True)

    return files_with_mtime[0][0]


def load_tweets(file_path: str) -> List[Dict]:
    """
    Load tweets from JSON file.

    Args:
        file_path: Path to tweets JSON file

    Returns:
        List of tweet dictionaries
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        tweets = json.load(f)

    return tweets


def save_classified_tweets(tweets: List[Dict], output_path: str):
    """
    Save classified tweets to JSON file.

    Args:
        tweets: List of classified tweets
        output_path: Path to save the output JSON
    """
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)

    print(f"✓ Classified tweets saved to: {output_path}")


def compare_classifications(tweets: List[Dict]):
    """
    Compare keyword-based and LLM classifications and print statistics.

    Args:
        tweets: Tweets with both keyword (categories) and LLM (llm_categories) fields
    """
    total = len(tweets)
    if total == 0:
        print("ツイートがありません")
        return
    match = 0
    differ = 0

    print("\n" + "=" * 70)
    print("分類比較: キーワード vs LLM")
    print("=" * 70)
    print(f"総ツイート数: {total}")

    for t in tweets:
        kw = set(t.get("categories", []))
        llm = set(t.get("llm_categories", []))
        if kw == llm:
            match += 1
        else:
            differ += 1

    print(f"一致: {match}件 ({match*100//total}%)")
    print(f"不一致: {differ}件 ({differ*100//total}%)")

    # Category-level comparison
    from collector.config import CLASSIFICATION_RULES
    print("\nカテゴリ別:")
    print(f"  {'カテゴリ':30s} {'キーワード':>8s} {'LLM':>8s}")
    print("-" * 50)
    for cat in CLASSIFICATION_RULES:
        kw_count = sum(1 for t in tweets if cat in t.get("categories", []))
        llm_count = sum(1 for t in tweets if cat in t.get("llm_categories", []))
        name = CLASSIFICATION_RULES[cat]["name"]
        print(f"  {name:30s} {kw_count:>8d} {llm_count:>8d}")

    kw_none = sum(1 for t in tweets if not t.get("categories"))
    llm_none = sum(1 for t in tweets if not t.get("llm_categories"))
    print(f"  {'該当なし':30s} {kw_none:>8d} {llm_none:>8d}")
    print("=" * 70)


def regenerate_viewer_html(tweets: List[Dict], output_path: str = 'output/viewer.html'):
    """
    Regenerate viewer.html with new classified tweet data.

    Args:
        tweets: List of classified tweets
        output_path: Path to viewer.html
    """
    # Read current viewer.html
    with open(output_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Convert tweets to JSON string (compact format)
    tweets_json = json.dumps(tweets, ensure_ascii=False, separators=(',', ':'))

    # Find and replace the EMBEDDED_DATA line
    # The pattern is: const EMBEDDED_DATA = [...];
    import re
    pattern = r'const EMBEDDED_DATA = \[.*?\];'
    replacement = f'const EMBEDDED_DATA = {tweets_json};'

    # Check if pattern exists
    if not re.search(pattern, content, re.DOTALL):
        print("Error: Could not find EMBEDDED_DATA in viewer.html")
        return False

    # Replace the data
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

    # Write back to file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print(f"✓ viewer.html regenerated: {output_path}")
    return True


def print_summary(tweets: List[Dict], classifier: TweetClassifier):
    """
    Print classification summary.

    Args:
        tweets: Classified tweets
        classifier: TweetClassifier instance
    """
    print("\n" + "=" * 70)
    print("Classification Summary")
    print("=" * 70)

    summary = classifier.get_summary(tweets)

    print(f"Total tweets: {summary['total_tweets']}")
    print("\nCategories:")
    print("-" * 70)

    for category, info in summary['categories'].items():
        print(f"  {info['name']:40s} {info['count']:4d} tweets")

    print("-" * 70)
    print(f"  Uncategorized: {summary['uncategorized']:4d} tweets")
    print("=" * 70)


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(
        description='Classify tweets using LLM and regenerate viewer.html'
    )
    parser.add_argument(
        '--input',
        type=str,
        help='Path to input tweets JSON file (default: latest in output/)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='output/viewer.html',
        help='Path to output viewer.html (default: output/viewer.html)'
    )
    parser.add_argument(
        '--no-viewer',
        action='store_true',
        help='Skip viewer.html regeneration'
    )

    args = parser.parse_args()

    # Find input file
    if args.input:
        input_file = args.input
    else:
        input_file = find_latest_tweets_json()
        if not input_file:
            print("Error: No tweets JSON files found in output/ directory")
            print("Please specify --input path/to/tweets.json")
            sys.exit(1)

    print(f"Loading tweets from: {input_file}")

    # Load tweets
    try:
        tweets = load_tweets(input_file)
        print(f"✓ Loaded {len(tweets)} tweets")
    except Exception as e:
        print(f"Error loading tweets: {e}")
        sys.exit(1)

    # Initialize classifiers
    print("\nInitializing classifiers...")
    keyword_classifier = TweetClassifier()
    try:
        llm_classifier = LLMClassifier()
        llm_available = True
    except ValueError as e:
        print(f"Warning: LLM classifier unavailable ({e})")
        print("Falling back to keyword-only classification.")
        llm_available = False
    print("Classifiers initialized")

    # Run keyword classification
    print("\nRunning keyword classification...")
    keyword_classified = keyword_classifier.classify_all(tweets)
    print("Keyword classification complete")

    # Print keyword classification summary
    print_summary(keyword_classified, keyword_classifier)

    # Run LLM classification
    if llm_available:
        print("\nRunning LLM classification...")
        llm_classifier.classify_all(keyword_classified)
        print("LLM classification complete")

        # Compare classifications
        compare_classifications(keyword_classified)

    final_classified = keyword_classified

    # Save classified tweets
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_json = f'output/classified_llm_{timestamp}.json'
    save_classified_tweets(final_classified, output_json)

    # Regenerate viewer.html
    if not args.no_viewer:
        print("\nRegenerating viewer.html...")
        if regenerate_viewer_html(final_classified, args.output):
            print(f"✓ Complete! Viewer available at: {args.output}")
        else:
            print("✗ Failed to regenerate viewer.html")
            sys.exit(1)
    else:
        print("\nSkipping viewer.html regeneration (--no-viewer specified)")

    print("\n" + "=" * 70)
    print("Classification complete!")
    print("=" * 70)


if __name__ == '__main__':
    main()
