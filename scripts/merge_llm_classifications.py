#!/usr/bin/env python3
"""Merge LLM classification results into tweets and regenerate viewer."""

import json
import sys
import os
import re
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector.classifier import TweetClassifier

# All 280 LLM classifications: (categories, reasoning)
LLM_RESULTS = {
    0: (["market_trend"], "Buffett philosophy DCA"),
    1: ([], "Personal wealth journey"),
    2: (["bullish_assets","bearish_assets"], "Gold surged then silver crashed"),
    3: ([], "Not financial"),
    4: (["market_trend"], "Market panic metaphor"),
    5: (["warning_signals"], "5-sigma abnormal moves"),
    6: (["bearish_assets"], "Gold silver crash"),
    7: (["market_trend"], "Currency intervention news"),
    8: (["purchased_assets"], "Bought Hulic 1000 shares"),
    9: (["bearish_assets"], "Microsoft -10%"),
    10: ([], "Personal anecdote gold gift"),
    11: (["bullish_assets"], "Gold all-time highs"),
    12: (["recommended_assets"], "Precious metals inflation hedge"),
    13: (["warning_signals"], "Gold bubble concern"),
    14: (["market_trend"], "Suzuki ranking trade analysis"),
    15: (["market_trend"], "EU-India-China trade"),
    16: (["bullish_assets"], "Gold 30k/gram"),
    17: (["warning_signals"], "Gold too fast"),
    18: ([], "No content"),
    19: (["purchased_assets"], "Gold DCA from 2004"),
    20: (["bullish_assets","market_trend"], "Gold surging despite hawkish Fed"),
    21: (["market_trend"], "Tech layoffs for AI"),
    22: ([], "Portfolio impact"),
    23: (["market_trend"], "FRB leadership change"),
    24: ([], "Insurance discussion"),
    25: ([], "Risk philosophy"),
    26: (["market_trend"], "Asset strength since 2010"),
    27: (["purchased_assets"], "Bought 2 stocks"),
    28: ([], "Portfolio loss"),
    29: ([], "Tax policy"),
    30: (["purchased_assets"], "Holding post-tariff"),
    31: (["bearish_assets"], "Silver -32%"),
    32: (["recommended_assets"], "YCS FNGS recommended"),
    33: (["market_trend"], "Rate cut expectations"),
    34: (["market_trend"], "Dollar trend reversal"),
    35: (["bearish_assets"], "Gold -10% Silver -24%"),
    36: (["bearish_assets"], "Gold 470T loss"),
    37: (["purchased_assets"], "Position reduced"),
    38: ([], "Entry error"),
    39: (["recommended_assets"], "AI semiconductor recommended"),
    40: (["bearish_assets"], "Precious metals crash"),
    41: (["recommended_assets"], "Gold/silver buying chance"),
    42: (["bearish_assets"], "Silver -19%"),
    43: (["warning_signals"], "Contrarian losses"),
    44: ([], "Unclear"),
    45: ([], "Non-financial"),
    46: ([], "Non-financial"),
    47: ([], "Non-financial"),
    48: ([], "Non-financial"),
    49: ([], "Non-financial"),
    50: ([], "Non-financial"),
    51: (["warning_signals"], "Contrarian major loss"),
    52: ([], "Stream announcement"),
    53: ([], "Personal"),
    54: (["warning_signals"], "Contrarian loss expectation"),
    55: (["bearish_assets"], "Silver -10% in 60min"),
    56: (["warning_signals"], "Contrarian anomalous correlation"),
    57: ([], "Non-financial"),
    58: ([], "Non-financial"),
    59: ([], "Non-financial"),
    60: ([], "Non-financial"),
    61: ([], "Non-financial"),
    62: (["bullish_assets"], "Advantest surging"),
    63: (["market_trend"], "Gold crash analysis"),
    64: (["bearish_assets"], "Silver -17.52%"),
    65: (["market_trend"], "Walsh limits QE"),
    66: (["bearish_assets"], "Software stocks bear market"),
    67: (["warning_signals"], "National debt worsening"),
    68: (["market_trend"], "VIX/sentiment"),
    69: (["market_trend"], "AI productivity"),
    70: (["bullish_assets","bearish_assets"], "Google 3D service but game stocks crashed"),
    71: (["market_trend"], "Flat market unstable"),
    72: ([], "Not investment"),
    73: ([], "Not investment"),
    74: ([], "Not investment"),
    75: ([], "Not investment"),
    76: ([], "Not investment"),
    77: (["bearish_assets","purchased_assets"], "Gold silver crashed, took profits"),
    78: (["market_trend","purchased_assets"], "Yen depreciation, secured dollars"),
    79: (["bullish_assets","bearish_assets","purchased_assets"], "SanDisk +19% but silver offset"),
    80: (["recommended_assets"], "Micron recommended"),
    81: (["market_trend"], "Forex intervention"),
    82: ([], "Educational question"),
    83: (["recommended_assets","bullish_assets"], "MSFT crash is buying opportunity, recommends MU STX SNDK"),
    84: ([], "Trump/Warsh relationship"),
    85: ([], "Not investment"),
    86: (["bullish_assets"], "SoFi record Q4"),
    87: (["purchased_assets"], "Holding Micron"),
    88: ([], "Congratulations"),
    89: ([], "Political"),
    90: ([], "Government spending"),
    91: (["recommended_assets","bullish_assets"], "MU STX early stage uptrend"),
    92: (["bullish_assets"], "Silver recovered 9%"),
    93: ([], "Writing note"),
    94: (["market_trend"], "Warsh appointed FRB chair"),
    95: ([], "Political"),
    96: ([], "Tax burden"),
    97: (["market_trend"], "Warsh appointed"),
    98: (["purchased_assets"], "Long-term gold, normal correction"),
    99: (["recommended_assets"], "MU STX recommended"),
    100: ([], "Political"),
    101: ([], "Political"),
    102: ([], "Reference"),
    103: ([], "Commentary"),
    104: (["market_trend"], "Dollar strength precious metals"),
    105: ([], "Healthcare policy"),
    106: ([], "Payment system"),
    107: (["bearish_assets"], "Gold silver crash"),
    108: (["recommended_assets","purchased_assets"], "Considering adding STX"),
    109: (["market_trend","warning_signals"], "Warsh selling pressure"),
    110: (["bearish_assets","market_trend"], "Market decline"),
    111: (["bearish_assets","purchased_assets"], "Silver ETF gains eroded"),
    112: (["bearish_assets"], "Silver -15%"),
    113: (["market_trend"], "Gold as dollar hedge"),
    114: ([], "Generational wealth gap"),
    115: ([], "Social insurance"),
    116: ([], "Personal announcement"),
    117: (["recommended_assets","bullish_assets","purchased_assets"], "MU strong, AI semi continues"),
    118: (["recommended_assets","bullish_assets"], "Memory sold out, AI demand"),
    119: (["market_trend"], "H200 impact larger than expected"),
    120: (["market_trend"], "Trump creating dips"),
    121: (["recommended_assets","bullish_assets"], "Nvidia coming"),
    122: (["recommended_assets","bullish_assets"], "Storage coming"),
    123: (["recommended_assets","bullish_assets"], "Memory stocks early stage"),
    124: (["market_trend"], "Memory shortage"),
    125: ([], "Political"),
    126: ([], "Healthcare"),
    127: ([], "Healthcare costs"),
    128: (["purchased_assets","bullish_assets"], "Silver position 1.5-2x gains"),
    129: ([], "Government pension"),
    130: (["recommended_assets"], "Japanese companies discounted"),
    131: ([], "Home renovation"),
    132: ([], "Personal"),
    133: ([], "Elderly attitudes"),
    134: ([], "Government bond"),
    135: (["market_trend","warning_signals"], "AI replacing jobs, inflation"),
    136: ([], "Healthcare policy"),
    137: (["bearish_assets","warning_signals"], "Lehman-level volatility, precious metals bubble"),
    138: (["market_trend"], "Fiscal stimulus crowding out"),
    139: ([], "Political"),
    140: (["bearish_assets"], "BTC -6.72%"),
    141: ([], "US pension discussion"),
    142: (["market_trend"], "COMEX gold price divergence"),
    143: ([], "AI tool comparison"),
    144: ([], "US returnee discussion"),
    145: (["warning_signals"], "Cathie Wood gold warning"),
    146: (["warning_signals"], "BlackRock selling BTC"),
    147: (["purchased_assets"], "Precious metals trade profit"),
    148: (["bullish_assets"], "SanDisk strong earnings"),
    149: (["purchased_assets"], "Bought Tesla first US stock"),
    150: ([], "Healthcare policy"),
    151: (["purchased_assets"], "Silver trade 12% profit"),
    152: ([], "Healthcare"),
    153: ([], "Political"),
    154: ([], "Political"),
    155: ([], "Political"),
    156: ([], "Political"),
    157: (["purchased_assets"], "Bought 1542 silver ETF"),
    158: (["market_trend","bullish_assets"], "AI bubble criticism wrong, bonus stage"),
    159: (["market_trend"], "Precious metals ETF NAV deviation"),
    160: ([], "Social policy"),
    161: ([], "Healthcare statistics"),
    162: ([], "US pension discussion"),
    163: ([], "Real estate"),
    164: ([], "School exam"),
    165: (["purchased_assets"], "Bought 1542 silver ETF on -15% dip"),
    166: ([], "US pension simulation"),
    167: (["bullish_assets"], "Apple strong earnings"),
    168: ([], "Political"),
    169: (["recommended_assets"], "AI semi stocks recommended"),
    170: ([], "US returnee"),
    171: ([], "Complaining"),
    172: (["bullish_assets"], "10% profit overnight"),
    173: (["recommended_assets","bullish_assets"], "Micron AI memory surging"),
    174: (["purchased_assets","bullish_assets"], "Micron +40k, MSFT -40k"),
    175: (["market_trend"], "FRB chair change impact"),
    176: (["bullish_assets"], "SanDisk +17%, Micron up"),
    177: (["market_trend"], "Missed bargain opportunity"),
    178: (["market_trend"], "Trump FRB announcement"),
    179: (["market_trend","warning_signals"], "Trump gold manipulation"),
    180: ([], "Political"),
    181: (["recommended_assets"], "Buy and hold"),
    182: ([], "Political"),
    183: (["warning_signals"], "Silver destined to crash"),
    184: (["bullish_assets"], "Seagate surging"),
    185: (["market_trend"], "SpaceX Tesla xAI merger"),
    186: (["bearish_assets"], "Gold -1.05%"),
    187: (["recommended_assets"], "Micron bullish"),
    188: (["recommended_assets"], "MU STX undervalued buy"),
    189: (["purchased_assets"], "Bought Seagate"),
    190: (["bullish_assets"], "SanDisk incredible earnings"),
    191: (["bullish_assets"], "SanDisk beats estimates"),
    192: ([], "Political"),
    193: (["market_trend"], "Cash MMF 3.5% yield"),
    194: (["market_trend","warning_signals"], "US Treasury risk-free 4% discourages business"),
    195: ([], "Political"),
    196: (["bullish_assets"], "Apple beats estimates"),
    197: ([], "Personal gains"),
    198: (["bullish_assets"], "SanDisk beats all estimates"),
    199: ([], "Political"),
    200: ([], "Personal finance"),
    201: (["bearish_assets"], "Toyota crash"),
    202: (["bearish_assets"], "MSFT -12%"),
    203: (["market_trend","warning_signals"], "China gold up, US debt down"),
    204: (["recommended_assets","bearish_assets"], "MSFT at fair PER, buying level"),
    205: (["warning_signals","market_trend"], "10yr yield 5% economy collapse risk"),
    206: (["market_trend"], "VC investment vs rates"),
    207: ([], "Crime news"),
    208: (["bearish_assets"], "MSFT -12% crash"),
    209: (["bearish_assets"], "Gold silver falling"),
    210: (["purchased_assets"], "Bought MU 150 shares, STX 50 shares"),
    211: ([], "Political"),
    212: ([], "Blame commentary"),
    213: (["bearish_assets","warning_signals"], "Precious metals crash, bubble"),
    214: (["bearish_assets"], "BTC -4.33%"),
    215: (["recommended_assets"], "MU STX momentum"),
    216: (["bearish_assets"], "MSFT -11%"),
    217: (["bearish_assets","warning_signals"], "MSFT shock AI bubble collapse"),
    218: (["bullish_assets","market_trend"], "S&P 7000, Gold 5600"),
    219: ([], "Personal"),
    220: ([], "Trading style"),
    221: (["bearish_assets"], "MSFT OpenAI destruction"),
    222: (["purchased_assets","bullish_assets"], "Silver +53% NISA"),
    223: (["market_trend"], "Peter Schiff dollar collapse thesis"),
    224: (["recommended_assets"], "Kioxia patience, market follows"),
    225: (["recommended_assets"], "Megatech will rise, hold"),
    226: ([], "OpenAI concern"),
    227: ([], "Personal"),
    228: (["bearish_assets"], "FANG+ Nasdaq flat"),
    229: (["purchased_assets","bullish_assets"], "Silver bottom buy, stock top sell"),
    230: (["bullish_assets"], "Caterpillar record sales"),
    231: (["bullish_assets"], "Next play after gold/silver surging"),
    232: (["recommended_assets","bearish_assets"], "Oracle bad, Broadcom good"),
    233: ([], "Political"),
    234: (["market_trend"], "Gold rise = inflation, currency devaluation"),
    235: ([], "Political"),
    236: (["recommended_assets"], "Gold coins recommended"),
    237: ([], "Political"),
    238: (["recommended_assets","market_trend"], "30% gold allocation strategy"),
    239: (["recommended_assets"], "Hold through noise"),
    240: (["market_trend"], "2026 market outlook"),
    241: (["recommended_assets"], "Gold coins 15+ years recommending"),
    242: (["purchased_assets","bullish_assets"], "20% gains quick"),
    243: (["purchased_assets","bullish_assets"], "MU revenue confirmed"),
    244: (["purchased_assets","bullish_assets"], "MU memory dominance"),
    245: (["recommended_assets","market_trend"], "Yen strength = bargain"),
    246: (["market_trend","warning_signals"], "Interest rate world, invest or perish"),
    247: (["recommended_assets"], "Tsurumi detailed analysis"),
    248: (["market_trend"], "Currency devaluation"),
    249: (["bullish_assets"], "Keyence strong Q3"),
    250: ([], "Vague encouragement"),
    251: (["bullish_assets"], "Hitachi strong Q3"),
    252: (["warning_signals"], "META AI scams"),
    253: (["purchased_assets"], "3 billion yen precious metals"),
    254: ([], "Political"),
    255: (["purchased_assets","bullish_assets"], "1.5 billion precious metals return"),
    256: (["purchased_assets","bullish_assets"], "Silver profits"),
    257: ([], "Personal safety concern"),
    258: ([], "Personal safety"),
    259: ([], "Personal safety"),
    260: (["recommended_assets"], "SBI iShares Gold recommended"),
    261: (["recommended_assets","bullish_assets"], "Gold 30k breakthrough"),
    262: ([], "Political"),
    263: ([], "Political immigration"),
    264: ([], "Congratulations"),
    265: (["recommended_assets"], "AI semiconductor recommended"),
    266: (["market_trend","warning_signals"], "NYC fiscal crisis"),
    267: (["recommended_assets","purchased_assets"], "Kioxia held, stocks hold"),
    268: (["market_trend"], "FX intervention strategy"),
    269: (["bullish_assets"], "MSFT AI business growing"),
    270: (["market_trend","bullish_assets"], "Advantest surging, market analysis"),
    271: (["bearish_assets","recommended_assets"], "Palantir -5% but buy MU"),
    272: ([], "Japan doomed"),
    273: (["bullish_assets","purchased_assets"], "Gold +1400万 gains"),
    274: (["bearish_assets"], "BTC -1.60%"),
    275: (["recommended_assets","bullish_assets"], "Gold +5.89% recommended"),
    276: (["purchased_assets","bullish_assets"], "Gold price record"),
    277: (["bullish_assets"], "META guidance beat"),
    278: (["bearish_assets","market_trend"], "Stocks crashing in gold terms"),
    279: (["market_trend","bullish_assets"], "S&P 7000, Advantest surging"),
}

def main():
    base_dir = '/Users/masaaki/Desktop/prm/xstock'
    input_path = f'{base_dir}/output/tweets_0129-0130_merged.json'
    output_path = f'{base_dir}/output/classified_llm_final.json'
    viewer_path = f'{base_dir}/output/viewer.html'

    # 1. Load tweets
    with open(input_path, 'r', encoding='utf-8') as f:
        tweets = json.load(f)
    print(f"Loaded {len(tweets)} tweets")

    assert len(tweets) == 280, f"Expected 280 tweets, got {len(tweets)}"

    # 2. Apply LLM classifications
    for i, tweet in enumerate(tweets):
        cats, reasoning = LLM_RESULTS[i]
        tweet['llm_categories'] = cats
        tweet['llm_reasoning'] = reasoning

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

    new_content = re.sub(pattern, f'const EMBEDDED_DATA = {tweets_json};', content, flags=re.DOTALL)
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
