#!/usr/bin/env python3
"""メタ分類器の訓練スクリプト

KW / LLM / ML 3分類器の出力を特徴量として
GradientBoostingClassifier (OneVsRest) を訓練する。

使い方:
    docker compose run --rm xstock python3 scripts/train_meta.py \
        --gold output/human_annotations.json \
        --tweets output/merged_all.json
"""

import argparse
import json
import os
import pickle
import sys

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.multiclass import OneVsRestClassifier
from sklearn.preprocessing import MultiLabelBinarizer
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

CATEGORIES = [
    "recommended_assets", "purchased_assets", "sold_assets", "winning_trades", "ipo",
    "market_trend", "bullish_assets", "bearish_assets", "warning_signals",
]

CAT_TO_IDX = {c: i for i, c in enumerate(CATEGORIES)}
N_CATS = len(CATEGORIES)


def _cats_to_vec(cats: list) -> list:
    """カテゴリリストを N_CATS 次元の 0/1 ベクトルに変換"""
    vec = [0] * N_CATS
    for c in cats:
        idx = CAT_TO_IDX.get(c)
        if idx is not None:
            vec[idx] = 1
    return vec


def build_features(tweet: dict) -> list:
    """ツイートから 29 次元の特徴ベクトルを構築

    内訳:
        [0:9]   KW  categories の 1/0
        [9:18]  LLM llm_categories の 1/0
        [18]    LLM llm_confidence
        [19:28] ML  ml_categories の 1/0
        [28]    disagreement_count (3 分類器が一致しないカテゴリの数)
    """
    kw_vec = _cats_to_vec(tweet.get("categories", []))
    llm_vec = _cats_to_vec(tweet.get("llm_categories", []))
    llm_conf = [float(tweet.get("llm_confidence", 0.0))]
    ml_vec = _cats_to_vec(tweet.get("ml_categories", []))

    # disagreement: 各カテゴリについて 3 分類器の多数決と一致しない数を合計
    disagree = 0
    for i in range(N_CATS):
        votes = kw_vec[i] + llm_vec[i] + ml_vec[i]
        if votes == 1 or votes == 2:
            disagree += 1
    disagree_vec = [disagree]

    return kw_vec + llm_vec + llm_conf + ml_vec + disagree_vec


def load_data(gold_path: str, tweets_path: str):
    """Gold standard とツイートデータを読み込み、特徴量・ラベルを返す"""
    with open(gold_path, "r", encoding="utf-8") as f:
        gold_data = json.load(f)
    with open(tweets_path, "r", encoding="utf-8") as f:
        tweets = json.load(f)

    url_to_tweet = {tw["url"]: tw for tw in tweets}

    features = []
    labels = []
    skipped = 0

    for ann in gold_data.get("annotations", []):
        url = ann["url"]
        human_cats = ann.get("human_categories", [])
        tw = url_to_tweet.get(url)
        if tw is None:
            skipped += 1
            continue
        features.append(build_features(tw))
        labels.append(human_cats if human_cats else [])

    if skipped:
        print(f"  スキップ（ツイート未発見）: {skipped} 件")

    return np.array(features, dtype=np.float64), labels


def main():
    parser = argparse.ArgumentParser(description="メタ分類器（アンサンブル）の訓練")
    parser.add_argument("--gold", required=True, help="手動アノテーション JSON パス")
    parser.add_argument("--tweets", default="output/merged_all.json", help="全ツイート JSON パス")
    parser.add_argument("--output-dir", default="models", help="モデル出力ディレクトリ")
    args = parser.parse_args()

    print("=== メタ分類器 訓練 ===")
    print(f"教師データ: {args.gold}")
    print(f"ツイートデータ: {args.tweets}")

    X, raw_labels = load_data(args.gold, args.tweets)
    print(f"データ件数: {len(X)}")

    if len(X) < 10:
        print("ERROR: データが少なすぎます（10 件以上必要）")
        sys.exit(1)

    # ラベルバイナライズ
    mlb = MultiLabelBinarizer(classes=CATEGORIES)
    Y = mlb.fit_transform(raw_labels)
    print(f"カテゴリ: {mlb.classes_.tolist()}")
    print(f"カテゴリ別件数: {dict(zip(mlb.classes_, Y.sum(axis=0)))}")
    print(f"特徴量次元: {X.shape[1]}")

    # 70/30 split
    X_train, X_val, Y_train, Y_val = train_test_split(
        X, Y, test_size=0.3, random_state=42,
    )
    print(f"訓練: {X_train.shape[0]} 件, 検証: {X_val.shape[0]} 件")

    # 訓練（validation 評価用）
    clf = OneVsRestClassifier(
        GradientBoostingClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.1,
            random_state=42,
        )
    )
    clf.fit(X_train, Y_train)

    Y_pred = clf.predict(X_val)
    print("\n=== 検証データ評価 ===")
    print(classification_report(
        Y_val, Y_pred,
        target_names=CATEGORIES,
        zero_division=0,
    ))

    # 全データで再訓練（本番用）
    print("全データで再訓練中...")
    clf_full = OneVsRestClassifier(
        GradientBoostingClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.1,
            random_state=42,
        )
    )
    clf_full.fit(X, Y)

    # 保存
    os.makedirs(args.output_dir, exist_ok=True)
    meta_path = os.path.join(args.output_dir, "meta_clf.pkl")
    with open(meta_path, "wb") as f:
        pickle.dump(clf_full, f)

    print(f"\nメタ分類器を保存しました: {meta_path}")


if __name__ == "__main__":
    main()
