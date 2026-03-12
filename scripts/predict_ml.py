#!/usr/bin/env python3
"""訓練済みML分類器でツイートを予測する"""

import argparse
import json
import pickle
import sys


def load_model(model_dir="models"):
    """保存済みモデルを読み込み"""
    with open(f"{model_dir}/char_ngram_tfidf.pkl", "rb") as f:
        vectorizer = pickle.load(f)
    with open(f"{model_dir}/multi_label_clf.pkl", "rb") as f:
        clf = pickle.load(f)
    with open(f"{model_dir}/mlb.pkl", "rb") as f:
        mlb = pickle.load(f)
    return vectorizer, clf, mlb


def predict(texts, vectorizer, clf, mlb):
    """テキストリストに対して予測を返す（確率付き）"""
    X = vectorizer.transform(texts)
    Y_pred = clf.predict(X)

    # 確率取得（predict_proba対応モデルのみ）
    proba = None
    if hasattr(clf, "predict_proba"):
        try:
            proba = clf.predict_proba(X)
        except Exception:
            proba = None

    results = []
    for i, row in enumerate(Y_pred):
        cats = mlb.classes_[row == 1].tolist()
        if proba is not None:
            max_conf = float(proba[i].max()) if proba[i].size > 0 else 0.0
        else:
            max_conf = 1.0 if cats else 0.0
        results.append({"categories": cats, "confidence": max_conf})
    return results


def main():
    parser = argparse.ArgumentParser(description="ML分類器でツイートを予測")
    parser.add_argument("--input", required=True, help="入力ツイートJSONパス")
    parser.add_argument("--output", default=None, help="出力JSONパス（省略時stdout）")
    parser.add_argument("--model-dir", default="models", help="モデルディレクトリ")
    args = parser.parse_args()

    # モデル読み込み
    vectorizer, clf, mlb = load_model(args.model_dir)

    # ツイート読み込み
    with open(args.input, "r", encoding="utf-8") as f:
        tweets = json.load(f)

    texts = [tw.get("text", "") for tw in tweets]
    predictions = predict(texts, vectorizer, clf, mlb)

    # 結果をツイートに追加
    for tw, pred in zip(tweets, predictions):
        tw["ml_categories"] = pred["categories"]
        tw["ml_confidence"] = pred["confidence"]

    # 出力
    output_json = json.dumps(tweets, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"予測完了: {len(tweets)}件 → {args.output}")
    else:
        print(output_json)


if __name__ == "__main__":
    main()
