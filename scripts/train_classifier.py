#!/usr/bin/env python3
"""手動アノテーションデータからML分類器を訓練する"""

import argparse
import json
import pickle
import os
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.multiclass import OneVsRestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import MultiLabelBinarizer
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, f1_score

CATEGORIES = [
    "recommended_assets", "purchased_assets", "sold_assets", "winning_trades", "ipo",
    "market_trend", "bullish_assets", "bearish_assets", "warning_signals"
]

def load_data(annotations_path, tweets_path):
    """アノテーションとツイートデータを結合してX, yを作成"""
    with open(annotations_path, "r", encoding="utf-8") as f:
        ann_data = json.load(f)
    with open(tweets_path, "r", encoding="utf-8") as f:
        tweets = json.load(f)

    # URL → text のマップ
    url_to_text = {}
    for tw in tweets:
        url_to_text[tw["url"]] = tw.get("text", "")

    texts = []
    labels = []
    for a in ann_data.get("annotations", []):
        url = a["url"]
        cats = a.get("human_categories", [])
        if url not in url_to_text:
            continue
        text = url_to_text[url]
        if not text:
            continue
        # ゴミ箱は除外（human_categories が空でも該当なしの場合はある）
        texts.append(text)
        labels.append(cats if cats else [])

    return texts, labels


def main():
    parser = argparse.ArgumentParser(description="手動アノテーションからML分類器を訓練")
    parser.add_argument("--gold", required=True, help="手動アノテーションJSONパス")
    parser.add_argument("--tweets", default="output/merged_all.json", help="全ツイートJSONパス")
    parser.add_argument("--output-dir", default="models", help="モデル出力ディレクトリ")
    parser.add_argument("--test-size", type=float, default=0.2, help="テストデータ割合")
    parser.add_argument("--grid", action="store_true", help="グリッドサーチでハイパーパラメータ最適化")
    args = parser.parse_args()

    print("=== ML分類器 訓練 ===")
    print(f"教師データ: {args.gold}")
    print(f"ツイートデータ: {args.tweets}")

    # データ読み込み
    texts, labels = load_data(args.gold, args.tweets)
    print(f"データ件数: {len(texts)}")

    if len(texts) < 10:
        print("ERROR: データが少なすぎます（10件以上必要）")
        raise SystemExit(1)

    # MultiLabelBinarizer
    mlb = MultiLabelBinarizer(classes=CATEGORIES)
    Y = mlb.fit_transform(labels)
    print(f"カテゴリ: {mlb.classes_.tolist()}")
    print(f"カテゴリ別件数: {dict(zip(mlb.classes_, Y.sum(axis=0)))}")

    if args.grid:
        # グリッドサーチ
        print("\n=== グリッドサーチ開始 ===")
        best_score = -1
        best_params = {}
        param_grid = {
            "analyzer": ["char", "word"],
            "ngram_range": [(2, 4), (1, 3), (2, 5)],
            "C": [0.5, 1.0, 4.0, 10.0],
        }
        for analyzer in param_grid["analyzer"]:
            for ngram_range in param_grid["ngram_range"]:
                for C in param_grid["C"]:
                    vec = TfidfVectorizer(
                        analyzer=analyzer,
                        ngram_range=ngram_range,
                        min_df=2, max_df=0.95,
                        max_features=50000, sublinear_tf=True,
                    )
                    X_all = vec.fit_transform(texts)
                    X_tr, X_te, Y_tr, Y_te = train_test_split(
                        X_all, Y, test_size=args.test_size, random_state=42
                    )
                    c = OneVsRestClassifier(
                        LogisticRegression(max_iter=10000, C=C, class_weight="balanced", solver="lbfgs")
                    )
                    c.fit(X_tr, Y_tr)
                    score = f1_score(Y_te, c.predict(X_te), average="micro", zero_division=0)
                    print(f"  analyzer={analyzer}, ngram={ngram_range}, C={C} → F1={score:.4f}")
                    if score > best_score:
                        best_score = score
                        best_params = {"analyzer": analyzer, "ngram_range": ngram_range, "C": C}
        print(f"\nベストパラメータ: {best_params} (F1={best_score:.4f})")
        chosen_analyzer = best_params["analyzer"]
        chosen_ngram = best_params["ngram_range"]
        chosen_C = best_params["C"]
    else:
        chosen_analyzer = "char"
        chosen_ngram = (2, 4)
        chosen_C = 4.0

    # TF-IDF — MeCab不要で日本語対応
    vectorizer = TfidfVectorizer(
        analyzer=chosen_analyzer,
        ngram_range=chosen_ngram,
        min_df=2,
        max_df=0.95,
        max_features=50000,
        sublinear_tf=True
    )
    X = vectorizer.fit_transform(texts)
    print(f"特徴量数: {X.shape[1]}")

    # Train/Test split
    X_train, X_test, Y_train, Y_test = train_test_split(
        X, Y, test_size=args.test_size, random_state=42
    )
    print(f"訓練: {X_train.shape[0]}件, テスト: {X_test.shape[0]}件")

    # 学習
    clf = OneVsRestClassifier(
        LogisticRegression(max_iter=10000, C=chosen_C, class_weight="balanced", solver="lbfgs")
    )
    clf.fit(X_train, Y_train)

    # 評価
    Y_pred = clf.predict(X_test)
    print("\n=== テストデータ評価 ===")
    print(classification_report(
        Y_test, Y_pred,
        target_names=CATEGORIES,
        zero_division=0
    ))

    # 全データで再訓練（本番用）
    print("全データで再訓練中...")
    clf_full = OneVsRestClassifier(
        LogisticRegression(max_iter=10000, C=chosen_C, class_weight="balanced", solver="lbfgs")
    )
    clf_full.fit(X, Y)

    # モデル保存
    os.makedirs(args.output_dir, exist_ok=True)
    vec_path = os.path.join(args.output_dir, "char_ngram_tfidf.pkl")
    clf_path = os.path.join(args.output_dir, "multi_label_clf.pkl")
    mlb_path = os.path.join(args.output_dir, "mlb.pkl")

    with open(vec_path, "wb") as f:
        pickle.dump(vectorizer, f)
    with open(clf_path, "wb") as f:
        pickle.dump(clf_full, f)
    with open(mlb_path, "wb") as f:
        pickle.dump(mlb, f)

    print(f"\nモデル保存完了:")
    print(f"  TF-IDF: {vec_path}")
    print(f"  分類器: {clf_path}")
    print(f"  ラベル: {mlb_path}")


if __name__ == "__main__":
    main()
