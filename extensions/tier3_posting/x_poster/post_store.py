"""JSONL追記型の投稿ドラフト・履歴ストア。

ドラフトと投稿履歴をJSONL形式で追記保存し、news_idベースの重複排除を行う。
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

IMPRESSIONS_FILE = "impressions.jsonl"


class PostStore:
    """JSONL追記型の投稿ドラフト・履歴ストア。

    Attributes:
        base_dir: ストアのベースディレクトリ
        drafts_path: drafts.jsonl のパス
        history_path: post_history.jsonl のパス
        index_path: index.json のパス（news_id一覧のキャッシュ）
        impressions_path: impressions.jsonl のパス
    """

    def __init__(self, base_dir: str = "output/posting") -> None:
        self.base_dir = base_dir
        self.drafts_path = os.path.join(base_dir, "drafts.jsonl")
        self.history_path = os.path.join(base_dir, "post_history.jsonl")
        self.index_path = os.path.join(base_dir, "index.json")
        self._index: Optional[set] = None

    @property
    def impressions_path(self) -> str:
        return os.path.join(self.base_dir, IMPRESSIONS_FILE)

    def _ensure_dir(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)

    def _load_index(self) -> set:
        if self._index is not None:
            return self._index

        self._index = set()

        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._index = set(data.get("news_ids", []))
                return self._index
            except (json.JSONDecodeError, OSError):
                pass

        # index.json がない場合、JSONL から再構築
        if os.path.exists(self.drafts_path):
            try:
                with open(self.drafts_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rec = json.loads(line)
                                news_id = rec.get("news_id")
                                if news_id:
                                    self._index.add(news_id)
                            except json.JSONDecodeError:
                                continue
            except OSError:
                pass
            self._save_index()

        return self._index

    def _save_index(self) -> None:
        self._ensure_dir()
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(
                {"news_ids": sorted(self._load_index() if self._index is None else self._index)},
                f,
                ensure_ascii=False,
                indent=2,
            )

    def add_draft(self, draft: dict) -> bool:
        """ドラフトを追加する（news_id重複時はスキップ）。

        Args:
            draft: ドラフト辞書（news_id キー必須）

        Returns:
            True: 新規追加, False: 重複スキップ
        """
        news_id = draft.get("news_id")
        if not news_id:
            logger.warning("news_id が無いドラフトはスキップ: %s", draft.get("title", "?"))
            return False

        index = self._load_index()
        if news_id in index:
            return False

        # タイムスタンプ補完
        now = datetime.now(timezone.utc).isoformat()
        draft.setdefault("created_at", now)
        draft.setdefault("updated_at", now)
        draft.setdefault("status", "draft")

        self._ensure_dir()
        with open(self.drafts_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(draft, ensure_ascii=False) + "\n")

        index.add(news_id)
        self._save_index()

        logger.debug("ドラフト追加: %s (%s)", news_id, draft.get("title", "?"))
        return True

    def update_draft_status(self, news_id: str, new_status: str, **extra) -> bool:
        """ドラフトのステータスを更新する（追記方式）。

        Args:
            news_id: 対象のnews_id
            new_status: 新しいステータス
            **extra: 追加フィールド

        Returns:
            True: 更新成功, False: news_idが見つからない
        """
        index = self._load_index()
        if news_id not in index:
            logger.warning("news_id が見つからない: %s", news_id)
            return False

        update_record = {
            "news_id": news_id,
            "status": new_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "_update": True,
        }
        update_record.update(extra)

        self._ensure_dir()
        with open(self.drafts_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(update_record, ensure_ascii=False) + "\n")

        return True

    def load_drafts(self, status_filter: Optional[str] = None) -> list:
        """全ドラフトを読み込み、news_idごとに最新状態をマージして返す。

        Args:
            status_filter: 指定時は該当ステータスのみ返す

        Returns:
            ドラフトのリスト（最新状態にマージ済み）
        """
        if not os.path.exists(self.drafts_path):
            return []

        # news_idごとに全レコードを収集
        drafts_map: Dict[str, Dict[str, Any]] = {}

        with open(self.drafts_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                news_id = rec.get("news_id")
                if not news_id:
                    continue

                if rec.get("_update"):
                    # 更新レコード: 既存にマージ
                    if news_id in drafts_map:
                        update_data = {k: v for k, v in rec.items() if k != "_update"}
                        drafts_map[news_id].update(update_data)
                else:
                    # 初期レコード
                    if news_id not in drafts_map:
                        drafts_map[news_id] = rec
                    else:
                        # 後のレコードで上書き（通常は発生しないが安全策）
                        drafts_map[news_id].update(rec)

        drafts = list(drafts_map.values())

        if status_filter:
            drafts = [d for d in drafts if d.get("status") == status_filter]

        return drafts

    def load_history(self) -> list:
        """投稿履歴を全件読み込む。

        Returns:
            投稿履歴レコードのリスト
        """
        records = []
        if not os.path.exists(self.history_path):
            return records

        with open(self.history_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        return records

    def add_history(self, record: dict) -> bool:
        """投稿履歴を追加する。

        Args:
            record: 履歴レコード辞書

        Returns:
            True: 追加成功
        """
        record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())

        self._ensure_dir()
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return True

    def is_posted(self, news_id: str) -> bool:
        """指定news_idが投稿済みか確認する。

        Args:
            news_id: 確認対象のnews_id

        Returns:
            True: 投稿済み, False: 未投稿
        """
        for rec in self.load_history():
            if (rec.get("news_id") == news_id
                    and rec.get("status") == "posted"
                    and not rec.get("dry_run", False)):
                return True
        return False

    def import_from_json(self, json_path: str) -> int:
        """review.htmlからエクスポートされたJSONを取り込む。

        Args:
            json_path: JSONファイルパス

        Returns:
            取り込み件数
        """
        with open(json_path, "r", encoding="utf-8") as f:
            items = json.load(f)

        if not isinstance(items, list):
            logger.warning("JSONファイルがリスト形式ではない: %s", json_path)
            return 0

        count = 0
        for item in items:
            news_id = item.get("news_id")
            if not news_id:
                continue

            status = item.get("status", "draft")
            index = self._load_index()

            if news_id not in index:
                # 新規ドラフトとして追加
                self.add_draft(item)
                count += 1
            else:
                # 既存のステータス更新
                extra = {}
                if "body" in item:
                    extra["body"] = item["body"]
                if "title" in item:
                    extra["title"] = item["title"]
                if "hashtags" in item:
                    extra["hashtags"] = item["hashtags"]
                if "scheduled_at" in item:
                    extra["scheduled_at"] = item["scheduled_at"]
                self.update_draft_status(news_id, status, **extra)
                count += 1

        return count

    def add_impression(self, record: dict) -> bool:
        """インプレッションデータを追記する。

        Args:
            record: インプレッションレコード辞書
                - news_id (str): ニュースID（必須）
                - tweet_url (str): ツイートURL（必須）
                - impressions (int): インプレッション数
                - likes (int): いいね数
                - retweets (int): リツイート数
                - replies (int): リプライ数
                - bookmarks (int): ブックマーク数
                - engagement_rate (float): エンゲージメント率
                - scraped_at (str): スクレイピング日時（ISO 8601）

        Returns:
            True: 追記成功, False: バリデーションエラー
        """
        news_id = record.get("news_id")
        tweet_url = record.get("tweet_url")
        if not news_id or not tweet_url:
            logger.warning("📊 news_id または tweet_url が無いレコードはスキップ: %s", record)
            return False

        record.setdefault("scraped_at", datetime.now(JST).isoformat())

        self._ensure_dir()
        with open(self.impressions_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.debug("📊 インプレッション追記: %s (%s)", news_id, tweet_url)
        return True

    def load_impressions(self, news_id: str = None) -> list:
        """インプレッションデータを読み込む。

        Args:
            news_id: 指定時はそのnews_idのみフィルタ

        Returns:
            インプレッションレコードのリスト（scraped_at昇順）
        """
        records = []
        if not os.path.exists(self.impressions_path):
            return records

        with open(self.impressions_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if news_id and rec.get("news_id") != news_id:
                        continue
                    records.append(rec)

        records.sort(key=lambda r: r.get("scraped_at", ""))
        return records

    def get_latest_impressions(self) -> dict:
        """各news_idの最新インプレッションを取得する。

        Returns:
            {news_id: latest_impression_record} の辞書
        """
        all_records = self.load_impressions()
        latest: Dict[str, dict] = {}

        for rec in all_records:
            nid = rec.get("news_id")
            if not nid:
                continue
            if nid not in latest:
                latest[nid] = rec
            else:
                if rec.get("scraped_at", "") > latest[nid].get("scraped_at", ""):
                    latest[nid] = rec

        return latest
