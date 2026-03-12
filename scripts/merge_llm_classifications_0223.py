#!/usr/bin/env python3
"""Merge LLM classification results for 2026-02-23 tweets."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector.classifier import TweetClassifier

LLM_RESULTS = {
    0: ([], "Trading psychology"),
    1: (["purchased_assets"], "Long-term Nintendo hold"),
    2: ([], "Stream archive"),
    3: ([], "Supply demand reading"),
    4: ([], "Joke response"),
    5: ([], "General comment"),
    6: (["market_trend", "bullish_assets"], "Nikkei 300K inflation era"),
    7: ([], "Merchandise"),
    8: (["bullish_assets"], "Tuna price surge"),
    9: ([], "Weekly schedule"),
    10: (["market_trend"], "Trump tariff 15% escalation"),
    11: ([], "Tool usage"),
    12: ([], "Newsletter"),
    13: (["market_trend"], "SCOTUS tariff ruling summary"),
    14: ([], "Multi-timeframe trading"),
    15: ([], "Inspirational quote"),
}

def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_path = os.path.join(base_dir, 'output/2026-02-23/tweets.json')
    output_path = os.path.join(base_dir, 'output/2026-02-23/classified_llm.json')

    with open(input_path, 'r', encoding='utf-8') as f:
        tweets = json.load(f)
    print(f"Loaded {len(tweets)} tweets")
    assert len(tweets) == 16

    for i, tweet in enumerate(tweets):
        cats, reasoning = LLM_RESULTS[i]
        tweet['llm_categories'] = cats
        tweet['llm_reasoning'] = reasoning
        tweet['llm_confidence'] = 0.85 if cats else 0.0

    classifier = TweetClassifier()
    tweets = classifier.classify_all(tweets)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)
    print(f"Saved to {output_path}")
    print(f"LLM classified: {sum(1 for t in tweets if t.get('llm_categories'))} / {len(tweets)}")

if __name__ == '__main__':
    main()
