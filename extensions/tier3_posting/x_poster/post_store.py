"""JSONL追記型の投稿ドラフト・履歴ストア。

ドラフトと投稿履歴をJSONL形式で追記保存し、news_idベースの重複排除を行う。
"""

import fcntl
import glob
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

    def _append_with_lock(self, filepath: str, line: str) -> None:
        """ファイルロック付きでJSONL行を追記する。"""
        self._ensure_dir()
        with open(filepath, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

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
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(
                    {"news_ids": sorted(self._load_index() if self._index is None else self._index)},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

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

        self._append_with_lock(self.drafts_path, json.dumps(draft, ensure_ascii=False))

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

        self._append_with_lock(self.drafts_path, json.dumps(update_record, ensure_ascii=False))

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

        self._append_with_lock(self.history_path, json.dumps(record, ensure_ascii=False))

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

        self._append_with_lock(self.impressions_path, json.dumps(record, ensure_ascii=False))

        logger.debug("📊 インプレッション追記: %s (%s)", news_id, tweet_url)
        return True

    def add_impression_schedule(
        self,
        news_id: str,
        tweet_url: str,
        account_id: str = "",
        posted_at: str = "",
        intervals_hours: Optional[List[float]] = None,
        experiment_id: str = "",
    ) -> int:
        """plan.md M1 T1.2: 投稿成功時に 1h/4h/24h 後の追跡予約エントリを impressions.jsonl に追記する。

        plan.md M2 T2.5: experiment_id を継承し ER を A/B バージョン単位で集計可能にする。

        Args:
            news_id: ニュースID
            tweet_url: 投稿 URL
            account_id: 投稿アカウント
            posted_at: 投稿日時 ISO（省略時は now(JST)）
            intervals_hours: 予約間隔の時間リスト（デフォルト [1, 4, 24]）
            experiment_id: A/B 実験タグ（template_version-fewshot_version-scoring_version）

        Returns:
            追加した予約エントリ数
        """
        if not news_id or not tweet_url:
            logger.warning("📊 add_impression_schedule: news_id / tweet_url 必須")
            return 0

        base = datetime.fromisoformat(posted_at) if posted_at else datetime.now(JST)
        if base.tzinfo is None:
            base = base.replace(tzinfo=JST)

        hours_list = intervals_hours if intervals_hours is not None else [1, 4, 24]
        added = 0
        for h in hours_list:
            scheduled = (base + timedelta(hours=h)).isoformat()
            record = {
                "news_id": news_id,
                "tweet_url": tweet_url,
                "account_id": account_id,
                "status": "scheduled",
                "scheduled_at": scheduled,
                "interval_hours": h,
                "recorded_at": datetime.now(JST).isoformat(),
            }
            if experiment_id:
                record["experiment_id"] = experiment_id
            self._append_with_lock(
                self.impressions_path, json.dumps(record, ensure_ascii=False)
            )
            added += 1

        logger.info(
            "📊 追跡予約 %d 件: news_id=%s @%s",
            added, news_id, [f"+{h}h" for h in hours_list],
        )
        return added

    def load_due_schedules(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """plan.md M1 T1.2 follow-up: 期限到来した予約エントリのうち、未実行のものを返す。

        「未実行」の判定: 同じ news_id + interval_hours の組で status != 'scheduled' なレコードが
        まだ存在しない場合、その scheduled エントリは未消費とみなす。

        Args:
            now: 現在時刻（省略時は datetime.now(JST)）

        Returns:
            期限到来した未消費の scheduled エントリ list（scheduled_at 昇順）
        """
        if now is None:
            now = datetime.now(JST)

        all_records = self.load_impressions()

        # 既にスクレイピング済み (status != 'scheduled') な (news_id, interval_hours) セット
        consumed: set = set()
        for rec in all_records:
            if rec.get("status") != "scheduled" and rec.get("interval_hours") is not None:
                consumed.add((rec.get("news_id"), rec.get("interval_hours")))

        due: List[Dict[str, Any]] = []
        for rec in all_records:
            if rec.get("status") != "scheduled":
                continue
            scheduled_at = rec.get("scheduled_at", "")
            if not scheduled_at:
                continue
            try:
                sched_dt = datetime.fromisoformat(scheduled_at)
            except ValueError:
                continue
            if sched_dt.tzinfo is None:
                sched_dt = sched_dt.replace(tzinfo=JST)
            if sched_dt > now:
                continue
            key = (rec.get("news_id"), rec.get("interval_hours"))
            if key in consumed:
                continue
            due.append(rec)

        due.sort(key=lambda r: r.get("scheduled_at", ""))
        return due

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
        """各news_idの最新 scraping 済みインプレッションを取得する。

        plan.md M1 T1.2/T1.4: UI 表示対象は status=='ok' のみ。以下は除外:
        - scheduled: 予約エントリ（scrape 未完了）
        - rate_limited / login_required / error: 失敗レコード（metrics 欠落で UI ノイズ）

        Returns:
            {news_id: latest_impression_record} の辞書
        """
        all_records = self.load_impressions()
        latest: Dict[str, dict] = {}

        for rec in all_records:
            nid = rec.get("news_id")
            if not nid:
                continue
            # status が未設定の旧レコードは metrics が揃っていれば ok 扱い（後方互換）
            status = rec.get("status")
            if status is not None and status != "ok":
                continue
            if nid not in latest:
                latest[nid] = rec
            else:
                if rec.get("scraped_at", "") > latest[nid].get("scraped_at", ""):
                    latest[nid] = rec

        return latest

    def archive_draft(self, news_id: str, reason: str = "manual") -> bool:
        """ドラフトをアーカイブ済みに更新する。

        Args:
            news_id: 対象のnews_id
            reason: アーカイブ理由（デフォルト: "manual"）

        Returns:
            True: 更新成功, False: news_idが見つからない
        """
        return self.update_draft_status(news_id, "archived", archived_reason=reason)

    def bulk_update_status(self, news_ids: List[str], new_status: str, **extra) -> int:
        """複数ドラフトのステータスを一括更新する。

        Args:
            news_ids: 対象のnews_idリスト
            new_status: 新しいステータス
            **extra: 追加フィールド

        Returns:
            更新成功件数
        """
        count = 0
        for news_id in news_ids:
            if self.update_draft_status(news_id, new_status, **extra):
                count += 1
        return count

    def _collect_screenshot_paths(self) -> List[str]:
        """output/posting/ 配下のスクリーンショットPNGパスを収集する。

        Returns:
            schedule_error_*.png と schedule_dryrun_*.png のパスリスト
        """
        patterns = [
            os.path.join(self.base_dir, "schedule_error_*.png"),
            os.path.join(self.base_dir, "schedule_dryrun_*.png"),
        ]
        paths: List[str] = []
        for pattern in patterns:
            paths.extend(glob.glob(pattern))
        paths.sort()
        return paths

    def get_full_draft(self, news_id: str) -> Optional[Dict[str, Any]]:
        """単一ドラフトの完全情報を取得する。

        load_drafts() から該当アイテムを取得し、load_history() から全履歴、
        get_latest_impressions() からインプレッションをマージして返す。

        Args:
            news_id: 対象のnews_id

        Returns:
            マージ済みドラフト辞書。見つからない場合は None。
            追加フィールド:
                - history: そのnews_idの全履歴レコードのリスト
                - has_dry_run: 履歴に dry_run=True のレコードがあるか
                - has_real_post: 履歴に dry_run=False かつ status="posted" のレコードがあるか
                - posted_url: 履歴から取得（real post優先）
                - posted_at: 履歴から取得（real post優先）
                - error: 最新のfailedレコードのerrorテキスト
                - impressions: 最新インプレッションレコード
                - screenshot_paths: 関連スクリーンショットのパスリスト
        """
        # ドラフト取得
        drafts = self.load_drafts()
        draft: Optional[Dict[str, Any]] = None
        for d in drafts:
            if d.get("news_id") == news_id:
                draft = d.copy()
                break
        if draft is None:
            return None

        # 履歴取得（該当news_idのみ）
        all_history = self.load_history()
        history_records = [r for r in all_history if r.get("news_id") == news_id]

        draft["history"] = history_records
        draft["has_dry_run"] = any(r.get("dry_run", False) for r in history_records)
        draft["has_real_post"] = any(
            not r.get("dry_run", False) and r.get("status") == "posted"
            for r in history_records
        )

        # posted_url / posted_at: real post優先、なければdry_runのもの
        real_posts = [
            r for r in history_records
            if not r.get("dry_run", False) and r.get("status") == "posted"
        ]
        dry_runs = [
            r for r in history_records
            if r.get("dry_run", False) and r.get("status") == "posted"
        ]
        source_for_url = real_posts[-1] if real_posts else (dry_runs[-1] if dry_runs else None)
        if source_for_url:
            if source_for_url.get("posted_url"):
                draft["posted_url"] = source_for_url["posted_url"]
            if source_for_url.get("posted_at"):
                draft["posted_at"] = source_for_url["posted_at"]

        # error: 最新のfailedレコードから
        failed_records = [r for r in history_records if r.get("status") == "failed"]
        if failed_records:
            draft["error"] = failed_records[-1].get("error")

        # インプレッション
        latest_impressions = self.get_latest_impressions()
        if news_id in latest_impressions:
            draft["impressions"] = latest_impressions[news_id]

        # スクリーンショット
        draft["screenshot_paths"] = self._collect_screenshot_paths()

        return draft

    def get_all_with_history(self) -> List[Dict[str, Any]]:
        """全ドラフトに履歴・インプレッション・メタ情報をマージして返す。

        build_review_page.py の行62-93のロジックを拡張したメソッド。
        各ドラフトに以下をマージする:
            - load_history() から posted_url, posted_at, error, dry_run, status(履歴)
            - get_latest_impressions() からインプレッション
            - has_dry_run: 履歴に dry_run=True のレコードがあるか
            - has_real_post: 履歴に dry_run=False かつ status="posted" のレコードがあるか
            - screenshot_paths: output/posting/ 内の関連PNGリスト

        Returns:
            マージ済みドラフトのリスト
        """
        drafts = self.load_drafts()
        all_history = self.load_history()

        # news_idごとに全履歴レコードをグループ化
        history_by_nid: Dict[str, List[Dict[str, Any]]] = {}
        for rec in all_history:
            nid = rec.get("news_id")
            if nid:
                history_by_nid.setdefault(nid, []).append(rec)

        # インプレッション
        latest_impressions = self.get_latest_impressions()

        # スクリーンショット（全ドラフト共通で一度だけ収集）
        screenshot_paths = self._collect_screenshot_paths()

        for draft in drafts:
            nid = draft.get("news_id")
            if not nid:
                continue

            records = history_by_nid.get(nid, [])

            # has_dry_run / has_real_post 判定
            draft["has_dry_run"] = any(r.get("dry_run", False) for r in records)
            draft["has_real_post"] = any(
                not r.get("dry_run", False) and r.get("status") == "posted"
                for r in records
            )

            # posted_url / posted_at: real post優先、なければdry_runのもの
            real_posts = [
                r for r in records
                if not r.get("dry_run", False) and r.get("status") == "posted"
            ]
            dry_runs = [
                r for r in records
                if r.get("dry_run", False) and r.get("status") == "posted"
            ]
            source = real_posts[-1] if real_posts else (dry_runs[-1] if dry_runs else None)
            if source:
                if source.get("posted_url"):
                    draft["posted_url"] = source["posted_url"]
                if source.get("posted_at"):
                    draft["posted_at"] = source["posted_at"]
                draft["dry_run"] = source.get("dry_run", False)

            # error: 最新のfailedレコードから
            failed_records = [r for r in records if r.get("status") == "failed"]
            if failed_records:
                draft["error"] = failed_records[-1].get("error")

            # インプレッション
            if nid in latest_impressions:
                draft["impressions"] = latest_impressions[nid]

            # スクリーンショット
            draft["screenshot_paths"] = screenshot_paths

        return drafts
