"""JSONL追記型の推奨レコードストア。

推奨レコードをJSONL形式で追記保存し、rec_idベースの重複排除を行う。
"""

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RecommendationStore:
    """JSONL追記型の推奨レコードストア。

    Attributes:
        base_dir: ストアのベースディレクトリ
        jsonl_path: recommendations.jsonl のパス
        index_path: index.json のパス（rec_id一覧のキャッシュ）
    """

    def __init__(self, base_dir: str = "output/performance") -> None:
        """初期化。

        Args:
            base_dir: ストアのベースディレクトリ
        """
        self.base_dir = base_dir
        self.jsonl_path = os.path.join(base_dir, "recommendations.jsonl")
        self.index_path = os.path.join(base_dir, "index.json")
        self._index: Optional[set] = None

    def _ensure_dir(self) -> None:
        """ベースディレクトリを作成する。"""
        os.makedirs(self.base_dir, exist_ok=True)

    def _load_index(self) -> set:
        """rec_idインデックスを読み込む。

        Returns:
            既存のrec_idのセット
        """
        if self._index is not None:
            return self._index

        self._index = set()

        # index.json が存在すればそこから読む
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._index = set(data.get("rec_ids", []))
                return self._index
            except (json.JSONDecodeError, OSError):
                pass

        # index.json がない場合、JSONL から再構築
        if os.path.exists(self.jsonl_path):
            try:
                with open(self.jsonl_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rec = json.loads(line)
                                rec_id = rec.get("rec_id")
                                if rec_id:
                                    self._index.add(rec_id)
                            except json.JSONDecodeError:
                                continue
            except OSError:
                pass
            # 再構築したインデックスを保存
            self._save_index()

        return self._index

    def _save_index(self) -> None:
        """rec_idインデックスをindex.jsonに保存する。"""
        self._ensure_dir()
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(
                {"rec_ids": sorted(self._load_index() if self._index is None else self._index)},
                f,
                ensure_ascii=False,
                indent=2,
            )

    @staticmethod
    def get_rec_id(tweet_url: str, ticker: str) -> str:
        """推奨レコードの一意IDを生成する。

        Args:
            tweet_url: ツイートURL
            ticker: ティッカーシンボル

        Returns:
            sha256(tweet_url + ':' + ticker) の先頭16文字
        """
        key = f"{tweet_url}:{ticker}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def add_if_new(self, rec: Dict[str, Any]) -> bool:
        """推奨レコードを追加する（重複時はスキップ）。

        Args:
            rec: 推奨レコード辞書（rec_id キー必須）

        Returns:
            True: 新規追加された, False: 重複のためスキップ
        """
        rec_id = rec.get("rec_id")
        if not rec_id:
            logger.warning("rec_id が無いレコードはスキップ: %s", rec)
            return False

        index = self._load_index()
        if rec_id in index:
            return False

        # JSONL追記
        self._ensure_dir()
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # インデックス更新
        index.add(rec_id)
        self._save_index()

        logger.debug("推奨レコード追加: %s (%s)", rec_id, rec.get("ticker", "?"))
        return True

    def load_all(self) -> List[Dict[str, Any]]:
        """全推奨レコードを読み込む。

        Returns:
            推奨レコードのリスト
        """
        records = []
        if not os.path.exists(self.jsonl_path):
            return records

        with open(self.jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("不正なJSONL行をスキップ")
                        continue

        return records

    def load_by_influencer(self, username: str) -> List[Dict[str, Any]]:
        """インフルエンサー別の推奨レコードを読み込む。

        Args:
            username: インフルエンサーのユーザー名

        Returns:
            該当インフルエンサーの推奨レコードリスト
        """
        return [r for r in self.load_all() if r.get("influencer") == username]

    def count(self) -> int:
        """推奨レコード数を返す。

        Returns:
            レコード数
        """
        return len(self._load_index())
