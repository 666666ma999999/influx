#!/usr/bin/env python3
"""アンサンブル予測スクリプト

メタ分類器 + KW / ML 分類器を組み合わせて最終予測を出力する。
LLM 分類結果はツイートの既存フィールドから取得する（なければ空）。

使い方:
    docker compose run --rm xstock python3 scripts/predict_ensemble.py \
        --input output/tweets_20260228.json \
        --output output/ensemble_predicted.json
"""

import argparse
import json
import sys

from collector.ensemble_classifier import EnsembleClassifier


def main():
    parser = argparse.ArgumentParser(description="アンサンブル予測")
    parser.add_argument("--input", required=True, help="入力ツイート JSON パス")
    parser.add_argument("--output", default=None, help="出力 JSON パス（省略時は stdout）")
    parser.add_argument("--model-dir", default="models", help="モデルディレクトリ")
    args = parser.parse_args()

    # モデル読み込み
    ensemble = EnsembleClassifier(model_dir=args.model_dir)
    if not ensemble.load():
        print("ERROR: モデルの読み込みに失敗しました", file=sys.stderr)
        sys.exit(1)

    # ツイート読み込み
    with open(args.input, "r", encoding="utf-8") as f:
        tweets = json.load(f)

    print(f"入力ツイート数: {len(tweets)}")

    # バッチ分類
    tweets = ensemble.classify_batch(tweets)

    # 結果サマリー
    categorized = sum(1 for tw in tweets if tw.get("ensemble_categories"))
    print(f"分類済み: {categorized} / {len(tweets)}")

    # 出力
    output_text = json.dumps(tweets, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_text)
        print(f"出力: {args.output}")
    else:
        print(output_text)


if __name__ == "__main__":
    main()
