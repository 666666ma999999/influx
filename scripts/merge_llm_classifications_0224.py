#!/usr/bin/env python3
"""Merge LLM classification results for 2026-02-24 tweets and regenerate viewer."""

import json
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector.classifier import TweetClassifier

# All 147 LLM classifications: (categories, reasoning)
LLM_RESULTS = {
    0: ([], "Event announcement"),
    1: (["purchased_assets", "bullish_assets"], "Long-term portfolio +billions"),
    2: (["market_trend"], "Tariff ruling market skeptical"),
    3: (["bullish_assets", "market_trend"], "Oil up, Iran war risk"),
    4: (["bullish_assets", "bearish_assets"], "Defense S-high, others crashed"),
    5: ([], "Merchandise sold out"),
    6: ([], "Merchandise"),
    7: (["market_trend"], "NVIDIA OpenAI investment reduced"),
    8: (["market_trend", "bullish_assets"], "Tariff ruling positive for stocks"),
    9: (["bullish_assets"], "Stocks up overnight"),
    10: (["bullish_assets", "purchased_assets"], "Main PF surging"),
    11: (["market_trend"], "Nikkei PER exceeding Nasdaq"),
    12: (["market_trend"], "Trump 5 tariff alternatives"),
    13: (["market_trend"], "Trump 5 options post ruling"),
    14: (["market_trend"], "Tariff impact on Japan-US deals"),
    15: ([], "Empty"),
    16: (["purchased_assets"], "Regret not buying more"),
    17: (["purchased_assets", "bullish_assets"], "Top holding surging"),
    18: (["market_trend", "bullish_assets"], "Tariff removal = tax cut"),
    19: (["bullish_assets", "purchased_assets"], "Holdings incredibly strong"),
    20: (["bullish_assets", "purchased_assets"], "Top holding doubled from 325"),
    21: ([], "Political humor"),
    22: (["market_trend"], "SCOTUS invalidated tariffs"),
    23: ([], "HF earnings"),
    24: (["bearish_assets"], "BTC 4yr low"),
    25: (["market_trend"], "SCOTUS tariff ruling"),
    26: ([], "Historical anecdote"),
    27: (["market_trend"], "US GDP slowdown"),
    28: ([], "Insider trading news"),
    29: ([], "SK Hynix fake news"),
    30: ([], "Book commentary"),
    31: (["recommended_assets"], "Japan semi factory merit"),
    32: (["market_trend", "warning_signals"], "FRB liquidity signal"),
    33: (["market_trend", "bullish_assets"], "HF money flowing Japan Korea"),
    34: (["bullish_assets"], "US futures slightly up"),
    35: (["market_trend"], "Anthropic no public exposure"),
    36: (["recommended_assets"], "Semi factory beneficiary order"),
    37: (["purchased_assets", "bullish_assets"], "Bought Asics, strong in decline"),
    38: ([], "Short selling praise"),
    39: ([], "ADHD AI compatibility"),
    40: ([], "Alpha diminishes when verbalized"),
    41: ([], "SK Hynix Japan factory"),
    42: (["market_trend"], "Nikkei PER exceeding Nasdaq"),
    43: ([], "Personal fitness"),
    44: (["warning_signals"], "Contrarian bullish precious metals"),
    45: (["market_trend"], "SCOTUS ruling FX impact"),
    46: ([], "Stream"),
    47: ([], "Personal"),
    48: (["purchased_assets"], "Paying daily swap"),
    49: ([], "Personal fitness"),
    50: ([], "Stream"),
    51: (["warning_signals"], "Contrarian gains chemical/defense"),
    52: ([], "Personal"),
    53: ([], "Personal"),
    54: ([], "Stream"),
    55: (["warning_signals", "market_trend"], "Contrarian US stocks gradual decline"),
    56: ([], "Personal fitness"),
    57: ([], "Personal"),
    58: ([], "Crime opinion"),
    59: ([], "Stream"),
    60: ([], "Health check"),
    61: ([], "Sponsorship"),
    62: ([], "Gambling"),
    63: ([], "Newsletter"),
    64: (["market_trend"], "SCOTUS tariff slide"),
    65: ([], "Event"),
    66: (["purchased_assets", "bullish_assets"], "Holdings +44M, Meek strong"),
    67: ([], "Empty"),
    68: ([], "Tax filing"),
    69: ([], "No metaplanet, own selection"),
    70: ([], "Magazine feature"),
    71: ([], "FX service intro"),
    72: ([], "Empty"),
    73: (["market_trend"], "Redistribution wasted on debt"),
    74: (["bearish_assets"], "TSE Growth market crashed"),
    75: (["bearish_assets"], "TSE Growth market failing"),
    76: (["purchased_assets", "recommended_assets"], "700M+, buy when low"),
    77: ([], "Real estate"),
    78: ([], "Crime news"),
    79: (["recommended_assets"], "Name policy beneficiary stocks"),
    80: ([], "Identity verification"),
    81: (["bullish_assets"], "Meek IoT SIM adopted"),
    82: ([], "Museum commentary"),
    83: ([], "Empty"),
    84: ([], "Investment education"),
    85: (["recommended_assets"], "Buy dips buy rips"),
    86: ([], "Location"),
    87: (["recommended_assets", "bullish_assets"], "AI semi memory strong"),
    88: (["bullish_assets", "recommended_assets"], "SanDisk +10%, memory bullish"),
    89: ([], "General comment"),
    90: (["recommended_assets"], "Optical interconnect top picks"),
    91: (["market_trend", "bullish_assets"], "Memory price surge phenomena"),
    92: ([], "Empty"),
    93: (["market_trend"], "Taiwan surpasses China AI chip"),
    94: ([], "AI industry competition"),
    95: (["market_trend", "warning_signals", "bullish_assets"], "Gold up, uncertainty record"),
    96: (["recommended_assets"], "100M yen investment options"),
    97: (["market_trend"], "Trump backup plan, market complacent"),
    98: ([], "Buy order comment"),
    99: (["market_trend"], "SCOTUS tariff 3 key points"),
    100: (["bullish_assets"], "Memory firms price discipline"),
    101: (["market_trend"], "Tariff unconstitutional, refund?"),
    102: (["recommended_assets"], "Megaetch most profitable"),
    103: (["bullish_assets", "purchased_assets"], "Explosive gains locked in"),
    104: (["market_trend", "bullish_assets"], "Post-ruling market up"),
    105: (["market_trend"], "Trump alternative tariff powers"),
    106: (["market_trend"], "Multiple tariff tools"),
    107: (["market_trend"], "Rate vs deflation tug of war"),
    108: (["market_trend"], "Global tariff invalidated"),
    109: ([], "Inflation debate"),
    110: (["market_trend"], "Fiscal policy inflation tax"),
    111: (["market_trend", "warning_signals"], "Potential lending restriction"),
    112: ([], "Political"),
    113: ([], "Metaphor"),
    114: (["market_trend"], "Bond issuance inflation tax"),
    115: ([], "Earnings reference"),
    116: ([], "Earnings reference"),
    117: ([], "Shopping promotion"),
    118: ([], "Political"),
    119: ([], "AI funding news"),
    120: (["market_trend"], "Trump 10% tariff market shrug"),
    121: (["market_trend"], "Europe better than US"),
    122: (["market_trend", "warning_signals"], "Nikkei PER exceeding Nasdaq"),
    123: (["market_trend", "warning_signals"], "Fed may raise if inflation up"),
    124: (["market_trend"], "SCOTUS tariff ruling"),
    125: (["market_trend"], "Trump backup plan"),
    126: (["market_trend"], "Tariff refund 66% probability"),
    127: (["market_trend"], "Hundreds of tariff lawsuits"),
    128: (["market_trend"], "Rates up, tariff fiscal impact"),
    129: (["market_trend"], "SCOTUS tariff unconstitutional"),
    130: (["market_trend"], "No refund statement"),
    131: (["market_trend"], "SCOTUS tariff illegal"),
    132: (["market_trend", "warning_signals"], "47% tapping 401k for inflation"),
    133: ([], "Work culture comparison"),
    134: (["market_trend", "bearish_assets"], "Q4 GDP weaker than expected"),
    135: ([], "Personal"),
    136: (["market_trend"], "Nikkei PE vs Nasdaq"),
    137: (["market_trend"], "NVDA $30B OpenAI investment"),
    138: (["market_trend"], "SCOTUS scenario S&P impact"),
    139: (["market_trend", "bullish_assets"], "Iran risk but M7 resilient"),
    140: ([], "Surprise reaction"),
    141: (["warning_signals"], "Contrarian bullish defense"),
    142: ([], "Anti-DCA humor"),
    143: (["warning_signals"], "Contrarian bullish semi inflow"),
    144: ([], "Social media commentary"),
    145: (["purchased_assets"], "Contrarian bought TOPIX stocks"),
    146: ([], "Misinformation warning"),
}


def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_path = os.path.join(base_dir, 'output/2026-02-24/tweets.json')
    output_path = os.path.join(base_dir, 'output/2026-02-24/classified_llm.json')
    viewer_path = os.path.join(base_dir, 'output/viewer.html')

    # 1. Load tweets
    with open(input_path, 'r', encoding='utf-8') as f:
        tweets = json.load(f)
    print(f"Loaded {len(tweets)} tweets")
    assert len(tweets) == 147, f"Expected 147 tweets, got {len(tweets)}"

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
