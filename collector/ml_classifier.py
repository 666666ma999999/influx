"""
MLベースのツイート分類器
手動アノテーションで訓練したTF-IDF + SVM分類器
"""

import os
import pickle
from typing import List, Dict, Any, Optional

from collector.logger import get_logger

logger = get_logger(__name__)


class MLClassifier:
    """訓練済みML分類器によるツイート分類"""

    def __init__(self, model_dir: str = "models"):
        """
        初期化

        Args:
            model_dir: モデルファイルのディレクトリパス
        """
        self.model_dir = model_dir
        self.vectorizer = None
        self.clf = None
        self.mlb = None
        self.loaded = False

    def load(self) -> bool:
        """モデルファイルを読み込み"""
        vec_path = os.path.join(self.model_dir, "char_ngram_tfidf.pkl")
        clf_path = os.path.join(self.model_dir, "multi_label_clf.pkl")
        mlb_path = os.path.join(self.model_dir, "mlb.pkl")

        if not all(os.path.exists(p) for p in [vec_path, clf_path, mlb_path]):
            logger.warning("MLモデルファイルが見つかりません: %s", self.model_dir)
            return False

        try:
            with open(vec_path, "rb") as f:
                self.vectorizer = pickle.load(f)
            with open(clf_path, "rb") as f:
                self.clf = pickle.load(f)
            with open(mlb_path, "rb") as f:
                self.mlb = pickle.load(f)
            self.loaded = True
            logger.info("MLモデル読み込み完了")
            return True
        except Exception as e:
            logger.error("MLモデル読み込みエラー: %s", e)
            return False

    def classify(self, tweet: Dict[str, Any]) -> Dict[str, Any]:
        """
        単一ツイートを分類

        Args:
            tweet: ツイートデータ辞書（textフィールド必須）

        Returns:
            ml_categories, ml_confidence を追加したツイート辞書
        """
        if not self.loaded:
            if not self.load():
                tweet["ml_categories"] = []
                tweet["ml_confidence"] = 0.0
                return tweet

        text = tweet.get("text", "")
        if not text:
            tweet["ml_categories"] = []
            tweet["ml_confidence"] = 0.0
            return tweet

        X = self.vectorizer.transform([text])
        Y_pred = self.clf.predict(X)
        cats = self.mlb.classes_[Y_pred[0] == 1].tolist()

        tweet["ml_categories"] = cats
        if hasattr(self.clf, "predict_proba"):
            try:
                proba = self.clf.predict_proba(X)
                tweet["ml_confidence"] = float(proba[0].max()) if proba[0].size > 0 else 0.0
            except Exception:
                tweet["ml_confidence"] = 1.0 if cats else 0.0
        else:
            tweet["ml_confidence"] = 1.0 if cats else 0.0
        return tweet

    def classify_batch(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        複数ツイートをバッチ分類

        Args:
            tweets: ツイートデータのリスト

        Returns:
            ml_categoriesを追加したツイートリスト
        """
        if not self.loaded:
            if not self.load():
                for tw in tweets:
                    tw["ml_categories"] = []
                    tw["ml_confidence"] = 0.0
                return tweets

        texts = [tw.get("text", "") for tw in tweets]
        X = self.vectorizer.transform(texts)
        Y_pred = self.clf.predict(X)

        proba = None
        if hasattr(self.clf, "predict_proba"):
            try:
                proba = self.clf.predict_proba(X)
            except Exception:
                proba = None

        for i, (tw, row) in enumerate(zip(tweets, Y_pred)):
            cats = self.mlb.classes_[row == 1].tolist()
            tw["ml_categories"] = cats
            if proba is not None:
                tw["ml_confidence"] = float(proba[i].max()) if proba[i].size > 0 else 0.0
            else:
                tw["ml_confidence"] = 1.0 if cats else 0.0

        return tweets
