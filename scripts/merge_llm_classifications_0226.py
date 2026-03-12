#!/usr/bin/env python3
"""Merge LLM classification results for 2026-02-26 tweets and regenerate viewer."""

import json
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector.classifier import TweetClassifier

# All 155 LLM classifications: (categories, reasoning)
LLM_RESULTS = {
    0: ([], "AI alpha source discussion"),
    1: ([], "Research site recommendation"),
    2: ([], "HF manager earnings news"),
    3: ([], "HF earnings commentary"),
    4: ([], "SK Hynix fake news report"),
    5: ([], "Non-financial book comment"),
    6: (["market_trend"], "Semiconductor fab cost Japan advantage"),
    7: (["market_trend", "warning_signals"], "FRB overnight repo liquidity concern"),
    8: (["market_trend", "bullish_assets"], "HF money flowing into Japan Korea"),
    9: (["market_trend"], "Semiconductor supply chain order"),
    10: ([], "Personal trading skill comment"),
    11: ([], "Alpha decay philosophy"),
    12: (["market_trend"], "SK Hynix Japan DRAM factory"),
    13: (["market_trend", "warning_signals"], "Nikkei PER exceeding Nasdaq"),
    14: ([], "EDINET data cleanup struggle"),
    15: (["market_trend", "warning_signals"], "S&P500 slowing VIX rising"),
    16: ([], "Claude Code personal project"),
    17: ([], "Anonymous consultation service ad"),
    18: (["market_trend"], "Foreign investors buying Japan stocks"),
    19: (["market_trend"], "Hikari Tsushin Rheos dividend fund"),
    20: ([], "BTC dollar price clarification"),
    21: ([], "Personal life reflection"),
    22: (["bearish_assets"], "Bitcoin price back to 4yr low"),
    23: (["market_trend"], "Trump tariff unconstitutional ruling"),
    24: (["market_trend"], "US GDP slowed to 1.4%"),
    25: ([], "Insider trading arrest news"),
    26: (["bullish_assets"], "US stock futures slightly up"),
    27: (["market_trend"], "Anthropic no public exposure IPO"),
    28: (["purchased_assets", "bullish_assets"], "Bought Asics strong in decline"),
    29: ([], "ADHD and AI compatibility"),
    30: ([], "Risk management philosophy"),
    31: ([], "ADHD prompt for Gemini"),
    32: ([], "Non-financial sports comment"),
    33: (["bullish_assets"], "US stock futures rising"),
    34: ([], "Gemini pronunciation discussion"),
    35: ([], "GE Vernova after-hours flat"),
    36: (["recommended_assets", "purchased_assets"], "NTT buy at 150 sell at 160"),
    37: ([], "SBI fee structure commentary"),
    38: ([], "Historical anecdote Ikkyu"),
    39: (["purchased_assets"], "Bought Mitsubishi Electric 100 shares"),
    40: ([], "Typo correction"),
    41: ([], "Personal lifestyle comment"),
    42: (["purchased_assets"], "Sold J-Material 1640 to 2075 profit"),
    43: ([], "Personal housing comment"),
    44: (["market_trend"], "Japanese risk aversion investment needed"),
    45: (["market_trend"], "Inflation hurts inactive people"),
    46: ([], "Political Chinese bot commentary"),
    47: ([], "Non-financial pet discussion"),
    48: (["market_trend"], "Trump global tariff 15% hike"),
    49: ([], "Rent increase social topic"),
    50: ([], "Non-financial social media humor"),
    51: (["recommended_assets", "purchased_assets"], "Micron NISA growth slot invested"),
    52: ([], "Non-financial sticker hobby"),
    53: ([], "Non-financial personal"),
    54: ([], "Non-financial human rights"),
    55: (["market_trend"], "SME wages stagnant inflation continues"),
    56: ([], "Non-financial bookmark post"),
    57: ([], "Non-financial chess commentary"),
    58: (["market_trend", "recommended_assets"], "Yen devaluing buy stocks now"),
    59: ([], "Investment blog promotion"),
    60: (["recommended_assets"], "NASDAQ100 monthly DCA simulation"),
    61: ([], "Rent dispute personal story"),
    62: ([], "Rent negotiation success story"),
    63: ([], "Non-financial personal"),
    64: ([], "Non-financial humor"),
    65: (["recommended_assets", "warning_signals"], "Buy AI semi MU SNDK warn SaaS crypto"),
    66: (["recommended_assets"], "Micron SanDisk Nvidia broker ad"),
    67: ([], "Non-financial food comment"),
    68: (["recommended_assets", "bullish_assets"], "AI skepticism Nvidia undervalued"),
    69: ([], "Non-financial tokusatsu"),
    70: ([], "Blog promotion trading tips"),
    71: (["warning_signals"], "Institutions sell while recommending buy"),
    72: (["bearish_assets"], "SaaS overvalued PER 100x"),
    73: (["bullish_assets", "market_trend"], "S&P500 10% Nasdaq 20% Gold 100%"),
    74: ([], "Non-financial life complaint"),
    75: ([], "Non-financial rent discussion"),
    76: ([], "Non-financial rent discussion"),
    77: ([], "Non-financial AI regulation"),
    78: ([], "Rent negotiation success story"),
    79: ([], "Rent increase refusal advice"),
    80: (["recommended_assets"], "AI semi over BTC recommendation"),
    81: ([], "AI replacing humans commentary"),
    82: (["recommended_assets"], "Invest in AI semi and FIRE"),
    83: ([], "Non-financial social complaint"),
    84: ([], "Non-financial personal"),
    85: ([], "Non-financial social commentary"),
    86: ([], "Non-financial sports news"),
    87: (["market_trend"], "Capital shifting from US to global"),
    88: ([], "Buffett philosophy avoid mistakes"),
    89: (["market_trend"], "S&P500 sector dispersion patience"),
    90: (["market_trend"], "OpenAI vs Anthropic AI growth"),
    91: (["market_trend", "warning_signals", "bullish_assets"], "Tariff ruling uncertainty gold rising"),
    92: (["market_trend"], "K-shaped economy wealth gap"),
    93: ([], "Corporate governance statement"),
    94: (["bullish_assets"], "SBI high-dividend funds all increased"),
    95: (["market_trend"], "Risk return all assets overview"),
    96: (["bullish_assets"], "SBIGAM 14yr revenue 17yr profit growth"),
    97: (["market_trend"], "Yen weakness inward thinking"),
    98: (["recommended_assets", "market_trend"], "Commodities historically cheap energy upside"),
    99: (["market_trend", "warning_signals"], "White collar AI job displacement"),
    100: ([], "Buffett philosophy self-reliance"),
    101: (["market_trend"], "Tax deduction from capital gains"),
    102: (["market_trend"], "Bucket strategy retirement planning"),
    103: ([], "Brokerage campaign promotion"),
    104: ([], "NISA portfolio personal planning"),
    105: (["recommended_assets"], "Long-term index investing for kids"),
    106: ([], "Child investment fund question"),
    107: (["market_trend"], "Pension system sustainability"),
    108: ([], "Post-wealth work motivation"),
    109: (["market_trend"], "Japan wages not rising systemic"),
    110: ([], "Gemini FP consultation article"),
    111: ([], "Semi-retirement Gemini simulation"),
    112: (["recommended_assets"], "iFreeETF FANG+ Gold new product"),
    113: ([], "Elderly housing purchase question"),
    114: (["market_trend", "bullish_assets"], "China semiconductor mass production"),
    115: ([], "Copper vs optical tech discussion"),
    116: ([], "PCB alternative makers discussion"),
    117: ([], "PCB material deep tech specs"),
    118: (["market_trend"], "Rubin optical vs copper balance"),
    119: ([], "Non-financial AI knowledge criticism"),
    120: ([], "Nitto Boseki substitute makers"),
    121: ([], "Glass fiber material specifics"),
    122: ([], "Mitsubishi Gas Chemical BT resin"),
    123: (["bullish_assets"], "Nitto Boseki overseas buzz growing"),
    124: (["market_trend"], "Intel vs AMD 2nm CPU competition"),
    125: ([], "Membership promotion ad"),
    126: ([], "Empty tweet"),
    127: (["warning_signals"], "Broadcom AVGO Mediatek share risk"),
    128: (["market_trend", "bullish_assets"], "Memory prices surging NAND SSD shortage"),
    129: ([], "Gartner criticism shallow analysis"),
    130: ([], "AI boosting top performers"),
    131: ([], "Gartner criticism garbage code"),
    132: ([], "Lunar new year rush comment"),
    133: (["bullish_assets"], "HBM4 arrived exciting"),
    134: (["bullish_assets"], "SSD surging NAND shortage"),
    135: (["bullish_assets"], "Price hikes driving profits"),
    136: ([], "Dousan reference comment"),
    137: ([], "Optical connection article roundup"),
    138: (["market_trend", "bullish_assets"], "Memory price surge phenomena list"),
    139: ([], "Empty tweet"),
    140: (["market_trend"], "Taiwan beats China AI chip exports"),
    141: (["market_trend", "bullish_assets"], "Double orders drive price increase"),
    142: (["market_trend"], "SK Hynix NAND resource inefficiency"),
    143: (["market_trend"], "Samsung Micron SK Hynix fab capacity"),
    144: (["bullish_assets"], "STX growing significantly"),
    145: (["market_trend"], "SK Hynix Japan fab subsidy likely"),
    146: ([], "Non-financial journalism comment"),
    147: (["market_trend"], "Samsung memory production analysis"),
    148: ([], "Non-financial vague excitement"),
    149: ([], "Non-financial journalism criticism"),
    150: ([], "Non-financial personal defense"),
    151: ([], "Non-financial naming comment"),
    152: (["market_trend"], "SK Hynix HBM leader Miyagi return"),
    153: ([], "Non-financial price promotion"),
    154: ([], "Rent negotiation legal strategy"),
}


def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_path = os.path.join(base_dir, 'output/2026-02-26/tweets.json')
    output_path = os.path.join(base_dir, 'output/2026-02-26/classified_llm.json')
    viewer_path = os.path.join(base_dir, 'output/viewer.html')

    # 1. Load tweets
    with open(input_path, 'r', encoding='utf-8') as f:
        tweets = json.load(f)
    print(f"Loaded {len(tweets)} tweets")
    assert len(tweets) == 155, f"Expected 155 tweets, got {len(tweets)}"

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
