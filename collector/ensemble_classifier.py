"""
アンサンブル分類器
KW / LLM / ML 3分類器の出力をメタ分類器で統合する
"""

import os
import pickle
from typing import List, Dict, Any

import numpy as np

from collector.classifier import TweetClassifier
from collector.ml_classifier import MLClassifier
from collector.logger import get_logger

logger = get_logger(__name__)

CATEGORIES = [
    "recommended_assets", "purchased_assets", "sold_assets", "winning_trades", "ipo",
    "market_trend", "bullish_assets", "bearish_assets", "warning_signals",
]

CAT_TO_IDX = {c: i for i, c in enumerate(CATEGORIES)}
N_CATS = len(CATEGORIES)


class EnsembleClassifier:
    """KW + LLM + ML のメタ分類器によるアンサンブル分類"""

    def __init__(self, model_dir: str = "models"):
        self.model_dir = model_dir
        self.kw_classifier = None   # TweetClassifier
        self.ml_classifier = None   # MLClassifier
        self.meta_clf = None        # GradientBoosting meta
        self.loaded = False

    def load(self) -> bool:
        """TweetClassifier, MLClassifier, meta_clf を読み込み"""
        meta_path = os.path.join(self.model_dir, "meta_clf.pkl")
        if not os.path.exists(meta_path):
            logger.warning("メタ分類器が見つかりません: %s", meta_path)
            return False

        try:
            with open(meta_path, "rb") as f:
                self.meta_clf = pickle.load(f)
        except Exception as e:
            logger.error("メタ分類器読み込みエラー: %s", e)
            return False

        self.kw_classifier = TweetClassifier()

        self.ml_classifier = MLClassifier(model_dir=self.model_dir)
        if not self.ml_classifier.load():
            logger.warning("ML分類器の読み込みに失敗しました（ML特徴量はゼロベクトルになります）")

        self.loaded = True
        logger.info("EnsembleClassifier 読み込み完了")
        return True

    @staticmethod
    def _cats_to_vec(cats: list) -> list:
        """カテゴリリストを N_CATS 次元の 0/1 ベクトルに変換"""
        vec = [0] * N_CATS
        for c in cats:
            idx = CAT_TO_IDX.get(c)
            if idx is not None:
                vec[idx] = 1
        return vec

    def _build_features(self, tweet: dict) -> list:
        """ツイートから 29 次元の特徴ベクトルを構築

        内訳:
            [0:9]   KW  categories の 1/0
            [9:18]  LLM llm_categories の 1/0
            [18]    LLM llm_confidence
            [19:28] ML  ml_categories の 1/0
            [28]    disagreement_count
        """
        kw_vec = self._cats_to_vec(tweet.get("categories", []))
        llm_vec = self._cats_to_vec(tweet.get("llm_categories", []))
        llm_conf = [float(tweet.get("llm_confidence", 0.0))]
        ml_vec = self._cats_to_vec(tweet.get("ml_categories", []))

        disagree = 0
        for i in range(N_CATS):
            votes = kw_vec[i] + llm_vec[i] + ml_vec[i]
            if votes == 1 or votes == 2:
                disagree += 1

        return kw_vec + llm_vec + llm_conf + ml_vec + [disagree]

    def classify(self, tweet: dict) -> dict:
        """単一ツイートを分類

        Args:
            tweet: ツイートデータ辞書

        Returns:
            ensemble_categories, ensemble_confidence を追加したツイート辞書
        """
        if not self.loaded:
            if not self.load():
                tweet["ensemble_categories"] = []
                tweet["ensemble_confidence"] = 0.0
                return tweet

        # KW 分類（categories フィールドがなければ実行）
        if "categories" not in tweet:
            self.kw_classifier.classify(tweet)

        # ML 分類（ml_categories フィールドがなければ実行）
        if "ml_categories" not in tweet:
            self.ml_classifier.classify(tweet)

        # 特徴ベクトル構築・予測
        feat = self._build_features(tweet)
        X = np.array([feat], dtype=np.float64)
        pred = self.meta_clf.predict(X)[0]

        cats = [CATEGORIES[i] for i in range(N_CATS) if pred[i] == 1]

        # confidence: predict_proba があれば使用
        conf = 0.0
        if hasattr(self.meta_clf, "predict_proba"):
            try:
                proba = self.meta_clf.predict_proba(X)
                # OneVsRest の場合 proba はリストになりうる
                if isinstance(proba, list):
                    conf = float(max(p[0].max() for p in proba)) if proba else 0.0
                else:
                    conf = float(proba[0].max()) if proba[0].size > 0 else 0.0
            except Exception:
                conf = 1.0 if cats else 0.0
        else:
            conf = 1.0 if cats else 0.0

        tweet["ensemble_categories"] = cats
        tweet["ensemble_confidence"] = conf
        return tweet

    def classify_batch(self, tweets: list) -> list:
        """複数ツイートをバッチ分類

        Args:
            tweets: ツイートデータのリスト

        Returns:
            ensemble_categories を追加したツイートリスト
        """
        if not self.loaded:
            if not self.load():
                for tw in tweets:
                    tw["ensemble_categories"] = []
                    tw["ensemble_confidence"] = 0.0
                return tweets

        # KW / ML を一括適用
        for tw in tweets:
            if "categories" not in tw:
                self.kw_classifier.classify(tw)
            if "ml_categories" not in tw:
                self.ml_classifier.classify(tw)

        # 特徴ベクトル構築
        feats = np.array(
            [self._build_features(tw) for tw in tweets],
            dtype=np.float64,
        )
        preds = self.meta_clf.predict(feats)

        # confidence
        proba = None
        if hasattr(self.meta_clf, "predict_proba"):
            try:
                proba = self.meta_clf.predict_proba(feats)
            except Exception:
                proba = None

        for i, tw in enumerate(tweets):
            cats = [CATEGORIES[j] for j in range(N_CATS) if preds[i][j] == 1]
            tw["ensemble_categories"] = cats

            if proba is not None:
                if isinstance(proba, list):
                    tw["ensemble_confidence"] = float(
                        max(p[i].max() for p in proba)
                    ) if proba else 0.0
                else:
                    tw["ensemble_confidence"] = (
                        float(proba[i].max()) if proba[i].size > 0 else 0.0
                    )
            else:
                tw["ensemble_confidence"] = 1.0 if cats else 0.0

        return tweets
