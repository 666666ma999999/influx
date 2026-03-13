"""リサーチシグナル用JSONL追記型ストア。

シグナルレコードと評価レコードをJSONL形式で追記保存し、
signal_idベースの重複排除を行う。
"""

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ResearchStore:
    """リサーチシグナル・評価レコードのJSONL追記型ストア。

    Attributes:
        base_dir: ストアのベースディレクトリ
        signals_path: signals.jsonl のパス
        evaluations_path: evaluations.jsonl のパス
        index_path: index.json のパス（signal_id一覧のキャッシュ）
    """

    def __init__(self, base_dir: str = "output/research") -> None:
        """初期化。

        Args:
            base_dir: ストアのベースディレクトリ
        """
        self.base_dir = base_dir
        self.signals_path = os.path.join(base_dir, "signals.jsonl")
        self.evaluations_path = os.path.join(base_dir, "evaluations.jsonl")
        self.index_path = os.path.join(base_dir, "index.json")
        self._index: Optional[set] = None

    def _ensure_dir(self) -> None:
        """ベースディレクトリを作成する。"""
        os.makedirs(self.base_dir, exist_ok=True)

    def _load_index(self) -> set:
        """signal_idインデックスを読み込む。

        Returns:
            既存のsignal_idのセット
        """
        if self._index is not None:
            return self._index

        self._index = set()

        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._index = set(data.get("signal_ids", []))
                return self._index
            except (json.JSONDecodeError, OSError):
                pass

        # index.json がない場合、JSONL から再構築
        if os.path.exists(self.signals_path):
            try:
                with open(self.signals_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rec = json.loads(line)
                                signal_id = rec.get("signal_id")
                                if signal_id:
                                    self._index.add(signal_id)
                            except json.JSONDecodeError:
                                continue
            except OSError:
                pass
            self._save_index()

        return self._index

    def _save_index(self) -> None:
        """signal_idインデックスをindex.jsonに保存する。"""
        self._ensure_dir()
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(
                {"signal_ids": sorted(self._load_index() if self._index is None else self._index)},
                f,
                ensure_ascii=False,
                indent=2,
            )

    @staticmethod
    def get_signal_id(tweet_url: str, ticker: str) -> str:
        """シグナルレコードの一意IDを生成する。

        Args:
            tweet_url: ツイートURL
            ticker: ティッカーシンボル

        Returns:
            sha256(tweet_url + ':' + ticker) の先頭16文字
        """
        key = f"{tweet_url}:{ticker}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def add_signal(self, signal: Dict[str, Any]) -> bool:
        """シグナルレコードを追加する（重複時はスキップ）。

        Args:
            signal: シグナルレコード辞書（signal_id キー必須）

        Returns:
            True: 新規追加された, False: 重複のためスキップ
        """
        signal_id = signal.get("signal_id")
        if not signal_id:
            logger.warning("signal_id が無いレコードはスキップ: %s", signal)
            return False

        index = self._load_index()
        if signal_id in index:
            return False

        self._ensure_dir()
        with open(self.signals_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(signal, ensure_ascii=False) + "\n")

        index.add(signal_id)
        self._save_index()

        logger.debug("シグナル追加: %s (%s %s)", signal_id, signal.get("ticker", "?"), signal.get("direction", "?"))
        return True

    def add_evaluation(self, evaluation: Dict[str, Any]) -> None:
        """評価レコードを追加する。

        Args:
            evaluation: 評価レコード辞書
        """
        self._ensure_dir()
        with open(self.evaluations_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(evaluation, ensure_ascii=False) + "\n")

    def load_signals(self) -> List[Dict[str, Any]]:
        """全シグナルレコードを読み込む。

        Returns:
            シグナルレコードのリスト
        """
        return self._load_jsonl(self.signals_path)

    def load_evaluations(self) -> List[Dict[str, Any]]:
        """全評価レコードを読み込む。

        Returns:
            評価レコードのリスト
        """
        return self._load_jsonl(self.evaluations_path)

    def load_signals_by_influencer(self, username: str) -> List[Dict[str, Any]]:
        """インフルエンサー別のシグナルレコードを読み込む。

        Args:
            username: インフルエンサーのユーザー名

        Returns:
            該当インフルエンサーのシグナルレコードリスト
        """
        return [s for s in self.load_signals() if s.get("username") == username]

    def signal_count(self) -> int:
        """シグナルレコード数を返す。

        Returns:
            レコード数
        """
        return len(self._load_index())

    def _load_jsonl(self, path: str) -> List[Dict[str, Any]]:
        """JSONL ファイルを読み込む。"""
        records = []
        if not os.path.exists(path):
            return records

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("不正なJSONL行をスキップ")
                        continue

        return records
