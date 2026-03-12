#!/usr/bin/env python3
"""Merge LLM classification results for 2026-02-22 tweets and regenerate viewer."""

import json
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector.classifier import TweetClassifier

# All 40 LLM classifications: (categories, reasoning)
LLM_RESULTS = {
    0: (["purchased_assets"], "Personal asset growth from stocks"),
    1: ([], "Empty"),
    2: (["recommended_assets", "bullish_assets"], "Shikiho positive sector keywords"),
    3: (["recommended_assets", "purchased_assets"], "NTT buy timing strategy"),
    4: ([], "AI investment fusion"),
    5: ([], "Research tool intro"),
    6: ([], "HF manager ranking"),
    7: ([], "Price denomination clarification"),
    8: ([], "Personal reflection"),
    9: ([], "Brokerage fee commentary"),
    10: ([], "HF earnings commentary"),
    11: (["bearish_assets"], "BTC back to 4yr low"),
    12: (["market_trend"], "SCOTUS tariff ruling"),
    13: ([], "Historical anecdote"),
    14: (["market_trend"], "US GDP slowdown"),
    15: ([], "Insider trading news"),
    16: ([], "SK Hynix Japan factory fake"),
    17: ([], "Book commentary"),
    18: (["recommended_assets"], "Japan semiconductor factory merit"),
    19: (["market_trend", "warning_signals"], "FRB liquidity crisis signal"),
    20: ([], "Membership milestone"),
    21: ([], "Tax discussion"),
    22: (["recommended_assets", "bullish_assets"], "KOA shunt resistor AI demand"),
    23: (["bullish_assets", "market_trend"], "AI server 4.5M explosive growth"),
    24: ([], "Personal housing"),
    25: (["recommended_assets", "market_trend"], "AI market still early, US stocks"),
    26: ([], "Vague exclamation"),
    27: ([], "Humorous speculation"),
    28: ([], "Political"),
    29: (["market_trend", "warning_signals"], "Dollar decline, yen weakness"),
    30: (["market_trend"], "Advanced process too expensive"),
    31: ([], "Tourism statistics"),
    32: (["recommended_assets", "bullish_assets"], "AI semi bottleneck = upside"),
    33: (["purchased_assets"], "Portfolio allocation gold/cash"),
    34: (["bullish_assets"], "Power bottleneck not bubble"),
    35: (["market_trend"], "Oil topped, tariff uncertainty"),
    36: ([], "Military commentary"),
    37: ([], "Membership promotion"),
    38: ([], "Housing discussion"),
    39: ([], "Personal housing"),
}


def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_path = os.path.join(base_dir, 'output/2026-02-22/tweets.json')
    output_path = os.path.join(base_dir, 'output/2026-02-22/classified_llm.json')
    viewer_path = os.path.join(base_dir, 'output/viewer.html')

    # 1. Load tweets
    with open(input_path, 'r', encoding='utf-8') as f:
        tweets = json.load(f)
    print(f"Loaded {len(tweets)} tweets")
    assert len(tweets) == 40, f"Expected 40 tweets, got {len(tweets)}"

    # 2. Apply LLM classifications
    for i, tweet in enumerate(tweets):
        cats, reasoning = LLM_RESULTS[i]
        tweet['llm_categories'] = cats
        tweet['llm_reasoning'] = reasoning
        tweet['llm_confidence'] = 0.85 if cats else 0.0

    # 3. Run keyword classification
    classifier = TweetClassifier()
    tweets = classifier.classify_all(tweets)
    print("Keyword classification complete")

    # 4. Save classified JSON
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)
    print(f"Saved to {output_path}")

    # 5. Regenerate viewer.html
    with open(viewer_path, 'r', encoding='utf-8') as f:
        content = f.read()

    tweets_json = json.dumps(tweets, ensure_ascii=False, separators=(',', ':'))
    pattern = r'const EMBEDDED_DATA = \[.*?\];'

    if not re.search(pattern, content, re.DOTALL):
        print("ERROR: Could not find EMBEDDED_DATA in viewer.html")
        sys.exit(1)

    replacement = f'const EMBEDDED_DATA = {tweets_json};'
    new_content = re.sub(pattern, lambda m: replacement, content, flags=re.DOTALL)
    with open(viewer_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print(f"Regenerated {viewer_path}")

    # 6. Print comparison stats
    from collector.config import CLASSIFICATION_RULES
    print("\n" + "=" * 60)
    print("Category comparison: Keyword vs LLM")
    print(f"{'Category':30s} {'Keyword':>8s} {'LLM':>8s}")
    print("-" * 50)
    for cat in CLASSIFICATION_RULES:
        kw_count = sum(1 for t in tweets if cat in t.get("categories", []))
        llm_count = sum(1 for t in tweets if cat in t.get("llm_categories", []))
        name = CLASSIFICATION_RULES[cat]["name"]
        print(f"  {name:28s} {kw_count:>8d} {llm_count:>8d}")
    kw_none = sum(1 for t in tweets if not t.get("categories"))
    llm_none = sum(1 for t in tweets if not t.get("llm_categories"))
    print(f"  {'Uncategorized':28s} {kw_none:>8d} {llm_none:>8d}")
    print("=" * 60)
    print("Done!")


if __name__ == '__main__':
    main()
