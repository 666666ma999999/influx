"""Grok APIを用いた投資インフルエンサー候補発見クライアント。"""

import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, Field
from xai_sdk import Client
from xai_sdk.chat import system, user
from xai_sdk.tools import x_search

from collector.logger import get_logger


logger = get_logger(__name__)


DEFAULT_DISCOVERY_CONFIG = {
    "model": "grok-4-1-fast-non-reasoning",
    "keyword_batch_size": 3,
    "network_batch_size": 8,
    "max_retries": 3,
    "retry_backoff_base": 2.0,
    "timeout_seconds": 120,
    "min_followers": 3000,
    "default_max_candidates": 50,
    "batch_result_limit": 10,
}


class CandidateInfo(BaseModel):
    username: str = Field(description="X/Twitterのユーザー名（@なし）")
    display_name: str = Field(default="", description="表示名")
    description: str = Field(default="", description="アカウント概要")
    evidence: list[str] = Field(default_factory=list, description="発見の根拠となる投稿要約")
    sample_posts: list[str] = Field(default_factory=list, description="代表的な投稿")
    estimated_followers: int | None = Field(default=None, description="推定フォロワー数")
    estimated_avg_likes: int | None = Field(default=None, description="推定平均いいね数")
    investment_focus: list[str] = Field(default_factory=list, description="投資分野")
    score: float = Field(default=5.0, description="推奨スコア(1-10)", ge=1, le=10)
    confidence: str = Field(default="medium", description="確信度: high/medium/low")


class DiscoveryResult(BaseModel):
    candidates: list[CandidateInfo] = Field(default_factory=list)


class ScreenedTweet(BaseModel):
    text: str = Field(description="ツイート本文（要約可）")
    approximate_date: str = Field(default="", description="おおよその投稿日 YYYY-MM-DD")
    engagement_level: str = Field(default="medium", description="エンゲージメント水準: high/medium/low")
    investment_relevance: bool = Field(default=False, description="投資関連ツイートか")


class ScreenedCandidate(BaseModel):
    username: str = Field(description="Xユーザー名")
    investment_relevance_score: int = Field(default=0, ge=0, le=100, description="投資関連度スコア (0-100)")
    tweet_count_estimate: int = Field(default=0, description="直近30日の投資関連ツイート推定数")
    representative_tweets: list[ScreenedTweet] = Field(default_factory=list, description="代表的な投資関連ツイート (最大5件)")
    screening_summary: str = Field(default="", description="スクリーニング概要")


class ScreeningResult(BaseModel):
    candidates: list[ScreenedCandidate] = Field(default_factory=list)


class GrokClient:
    """xai-sdk を使って投資インフルエンサー候補を発見するクライアント。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "grok-4-1-fast-non-reasoning",
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        timeout_seconds: int = 120,
    ):
        try:
            from .config import DISCOVERY_CONFIG
            config = {**DEFAULT_DISCOVERY_CONFIG, **DISCOVERY_CONFIG}
        except (ImportError, AttributeError):
            config = DEFAULT_DISCOVERY_CONFIG.copy()

        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "APIキーが設定されていません。環境変数XAI_API_KEYを設定するか、"
                "コンストラクタでapi_keyを指定してください。"
            )

        self.model = model or config.get("model", DEFAULT_DISCOVERY_CONFIG["model"])
        self.max_retries = max_retries or config.get("max_retries", DEFAULT_DISCOVERY_CONFIG["max_retries"])
        self.retry_backoff_base = retry_backoff_base or config.get(
            "retry_backoff_base", DEFAULT_DISCOVERY_CONFIG["retry_backoff_base"]
        )
        self.timeout_seconds = timeout_seconds or config.get(
            "timeout_seconds", DEFAULT_DISCOVERY_CONFIG["timeout_seconds"]
        )
        self.keyword_batch_size = config.get(
            "keyword_batch_size", DEFAULT_DISCOVERY_CONFIG["keyword_batch_size"]
        )
        self.network_batch_size = config.get(
            "network_batch_size", DEFAULT_DISCOVERY_CONFIG["network_batch_size"]
        )
        self.min_followers = config.get("min_followers", DEFAULT_DISCOVERY_CONFIG["min_followers"])
        self.default_max_candidates = config.get(
            "default_max_candidates", DEFAULT_DISCOVERY_CONFIG["default_max_candidates"]
        )
        self.batch_result_limit = config.get(
            "batch_result_limit", DEFAULT_DISCOVERY_CONFIG["batch_result_limit"]
        )

        self.client = Client(api_key=self.api_key, timeout=self.timeout_seconds)

    def discover_by_keywords(
        self,
        keywords: list[str],
        max_candidates: int = 50,
        from_date=None,
        to_date=None,
        excluded_handles=None,
    ) -> dict:
        """キーワード群から投資インフルエンサー候補を発見する。"""
        keywords = [keyword.strip() for keyword in keywords if keyword and keyword.strip()]
        if not keywords:
            return {"candidates": [], "errors": ["keywords が空です"], "meta": {"source": "keywords"}}

        excluded_handles = self._normalize_handles(excluded_handles)
        from_dt, to_dt = self._resolve_dates(from_date, to_date)
        batches = self._chunk_list(keywords, self.keyword_batch_size)

        candidates_by_username: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []

        for batch in batches:
            prompt = self._build_keyword_prompt(batch, max_candidates=min(self.batch_result_limit, max_candidates))
            try:
                result = self._search_batch(
                    prompt=prompt,
                    from_date=from_dt,
                    to_date=to_dt,
                    excluded_handles=excluded_handles,
                    max_candidates=min(self.batch_result_limit, max_candidates),
                )
                self._merge_candidates(candidates_by_username, result.get("candidates", []), excluded_handles)
                errors.extend(result.get("errors", []))
            except Exception as exc:
                error = {"batch": batch, "error": self._sanitize_log(str(exc))}
                errors.append(error)
                logger.error("Keyword discovery batch failed", extra={"extra_data": error})

        final_candidates = self._finalize_candidates(candidates_by_username, max_candidates, excluded_handles)
        return {
            "candidates": final_candidates,
            "errors": errors,
            "meta": {
                "source": "keywords",
                "keywords": keywords,
                "from_date": from_dt.isoformat(),
                "to_date": to_dt.isoformat(),
            },
        }

    def discover_by_network(
        self,
        existing_handles: list[str],
        max_candidates: int = 50,
        from_date=None,
        to_date=None,
        excluded_handles=None,
    ) -> dict:
        """既存アカウント群のネットワークから関連投資アカウント候補を発見する。"""
        handles = self._normalize_handles(existing_handles)
        if not handles:
            return {"candidates": [], "errors": ["existing_handles が空です"], "meta": {"source": "network"}}

        excluded = self._normalize_handles(excluded_handles)
        excluded.update(handles)
        from_dt, to_dt = self._resolve_dates(from_date, to_date)
        batch_size = min(10, max(8, self.network_batch_size))
        batches = self._chunk_list(sorted(handles), batch_size)

        candidates_by_username: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []

        for batch in batches:
            prompt = self._build_network_prompt(batch, max_candidates=min(self.batch_result_limit, max_candidates))
            try:
                result = self._search_batch(
                    prompt=prompt,
                    from_date=from_dt,
                    to_date=to_dt,
                    excluded_handles=excluded,
                    max_candidates=min(self.batch_result_limit, max_candidates),
                )
                self._merge_candidates(candidates_by_username, result.get("candidates", []), excluded)
                errors.extend(result.get("errors", []))
            except Exception as exc:
                error = {"batch": batch, "error": self._sanitize_log(str(exc))}
                errors.append(error)
                logger.error("Network discovery batch failed", extra={"extra_data": error})

        final_candidates = self._finalize_candidates(candidates_by_username, max_candidates, excluded)
        return {
            "candidates": final_candidates,
            "errors": errors,
            "meta": {
                "source": "network",
                "existing_handles": sorted(handles),
                "from_date": from_dt.isoformat(),
                "to_date": to_dt.isoformat(),
            },
        }

    def _search_batch(
        self,
        prompt: str,
        from_date=None,
        to_date=None,
        allowed_handles=None,
        excluded_handles=None,
        max_candidates=10,
    ) -> dict:
        """x_search を用いて1バッチ分の候補を構造化取得する。"""
        from_dt, to_dt = self._resolve_dates(from_date, to_date)
        allowed = self._normalize_handles(allowed_handles)
        excluded = self._normalize_handles(excluded_handles)
        structured_prompt = self._build_structured_prompt(prompt, allowed, excluded, max_candidates)

        for attempt in range(self.max_retries):
            try:
                chat = self.client.chat.create(
                    model=self.model,
                    tools=[x_search(from_date=from_dt, to_date=to_dt)],
                )
                chat.append(system("あなたはX/Twitter上の投資インフルエンサー調査アナリストです。"))
                chat.append(user(structured_prompt))

                response, parsed = chat.parse(DiscoveryResult)
                _ = response
                if not parsed or not isinstance(parsed, DiscoveryResult):
                    message = "DiscoveryResult の parse に失敗しました"
                    logger.warning("Grok parse returned invalid result", extra={"extra_data": {"attempt": attempt + 1}})
                    raise ValueError(message)

                candidates = [candidate.model_dump() for candidate in parsed.candidates]
                if not candidates:
                    return {"candidates": [], "errors": [{"prompt": self._sanitize_log(prompt), "error": "候補が空でした"}]}

                filtered = self._post_filter_candidates(candidates, allowed, excluded)
                if not filtered:
                    return {
                        "candidates": [],
                        "errors": [{"prompt": self._sanitize_log(prompt), "error": "有効な候補がありませんでした"}],
                    }
                return {"candidates": filtered, "errors": []}
            except Exception as exc:
                sanitized_error = self._sanitize_log(str(exc))
                logger.warning(
                    "Grok batch search failed",
                    extra={
                        "extra_data": {
                            "attempt": attempt + 1,
                            "max_retries": self.max_retries,
                            "error": sanitized_error,
                            "prompt": self._sanitize_log(prompt),
                        }
                    },
                )
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_backoff_base ** attempt
                    time.sleep(wait_time)
                    continue
                return {
                    "candidates": [],
                    "errors": [{"prompt": self._sanitize_log(prompt), "error": sanitized_error}],
                }

        return {"candidates": [], "errors": [{"prompt": self._sanitize_log(prompt), "error": "unknown error"}]}

    def _sanitize_log(self, text: str, max_length: int = 200) -> str:
        """APIキー等をマスクしつつログ向けに短縮する。"""
        sanitized = text or ""
        sanitized = re.sub(r"xai-[A-Za-z0-9_\-]+", "xai-***REDACTED***", sanitized)
        sanitized = re.sub(
            r'(?i)(api[_-]?key|token|secret)["\s:=]+["\']?([A-Za-z0-9_\-]{12,})',
            r"\1: ***REDACTED***",
            sanitized,
        )
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length] + "..."
        return sanitized

    def _build_keyword_prompt(self, keywords: list[str], max_candidates: int) -> str:
        keyword_text = "、".join(keywords)
        return (
            "以下のキーワードに関連する日本株の投資インフルエンサーを探してください。"
            f"キーワード: {keyword_text}。"
            f"フォロワー{self.min_followers}人以上、具体的な投資判断（銘柄推奨、売買報告、決算分析）を行っているアカウントを優先。"
            "大型アカウントだけでなく、フォロワー3000〜30000人程度の中小規模で質の高い発信をしているアカウントも積極的に含めること。"
            "直近30日以内に具体的な銘柄名・銘柄コードを含む投稿をしているアカウントを優先すること。"
            "除外対象: bot/企業公式/ニュースメディア/仮想通貨のみ/FXのみ/情報商材販売/有料サロン勧誘/アフィリエイト中心。"
            f"最大{max_candidates}件まで候補を挙げてください。"
        )

    def _build_network_prompt(self, handles: list[str], max_candidates: int) -> str:
        handle_text = ", ".join(f"@{handle}" for handle in handles)
        return (
            "以下のアカウントが頻繁に言及・引用・議論している投資系アカウントを探してください。"
            f"対象アカウント: {handle_text}。"
            f"フォロワー{self.min_followers}人以上かつ、株式や企業分析など具体的な投資発信を継続している個人アカウントを優先。"
            "特にフォロワー3000〜30000人程度の中小規模で、独自の分析や売買判断を発信しているアカウントを重視すること。"
            "除外対象: bot/企業公式/ニュースメディア/仮想通貨のみ/FXのみ/情報商材販売/有料サロン勧誘/アフィリエイト中心。"
            f"最大{max_candidates}件まで候補を挙げてください。"
        )

    def _build_structured_prompt(
        self,
        prompt: str,
        allowed_handles: set[str],
        excluded_handles: set[str],
        max_candidates: int,
    ) -> str:
        constraints = [
            prompt,
            "出力は構造化データのみ。X上で実際に確認できるアカウントのみ返すこと。直近投稿や言及から妥当な根拠を含めること。",
            f"候補数は最大{max_candidates}件。",
            f"estimated_followers は可能なら数値で推定し、{self.min_followers}未満は含めないこと。",
            "evidence には発見の根拠を短い要約で1-3件、sample_posts には代表投稿の要約を1-3件入れること。",
            "confidence は high / medium / low のいずれかにすること。",
        ]
        if allowed_handles:
            constraints.append("候補に含めてよいアカウント: " + ", ".join(f"@{handle}" for handle in sorted(allowed_handles)))
        if excluded_handles:
            constraints.append("除外アカウント: " + ", ".join(f"@{handle}" for handle in sorted(excluded_handles)))
        return "\n".join(constraints)

    def _resolve_dates(self, from_date, to_date) -> tuple[datetime, datetime]:
        to_dt = self._coerce_datetime(to_date) or datetime.utcnow()
        from_dt = self._coerce_datetime(from_date) or (to_dt - timedelta(days=10))
        if from_dt > to_dt:
            raise ValueError("from_date は to_date 以下である必要があります")
        return from_dt, to_dt

    def _coerce_datetime(self, value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            normalized = value.strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                return datetime.strptime(value.strip(), "%Y-%m-%d")
        raise TypeError("from_date/to_date は datetime か ISO 形式文字列で指定してください")

    def _normalize_handles(self, handles) -> set[str]:
        normalized: set[str] = set()
        for handle in handles or []:
            if not handle:
                continue
            cleaned = str(handle).strip().lstrip("@").lower()
            if cleaned:
                normalized.add(cleaned)
        return normalized

    def _chunk_list(self, items: list[str], chunk_size: int) -> list[list[str]]:
        return [items[index:index + chunk_size] for index in range(0, len(items), chunk_size)]

    def _post_filter_candidates(
        self,
        candidates: list[dict[str, Any]],
        allowed_handles: set[str],
        excluded_handles: set[str],
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for candidate in candidates:
            username = str(candidate.get("username", "")).strip().lstrip("@").lower()
            if not username:
                continue
            if allowed_handles and username not in allowed_handles:
                continue
            if username in excluded_handles:
                continue
            followers = candidate.get("estimated_followers")
            if followers is not None and followers < self.min_followers:
                continue
            candidate["username"] = username
            filtered.append(candidate)
        return filtered

    def _merge_candidates(
        self,
        existing: dict[str, dict[str, Any]],
        new_candidates: list[dict[str, Any]],
        excluded_handles: set[str],
    ) -> None:
        for candidate in new_candidates:
            username = str(candidate.get("username", "")).strip().lstrip("@").lower()
            if not username or username in excluded_handles:
                continue

            if username not in existing:
                candidate["username"] = username
                existing[username] = candidate
                continue

            current = existing[username]
            current["display_name"] = current.get("display_name") or candidate.get("display_name", "")
            current["description"] = current.get("description") or candidate.get("description", "")
            current["estimated_followers"] = max(
                current.get("estimated_followers") or 0,
                candidate.get("estimated_followers") or 0,
            ) or None
            current["estimated_avg_likes"] = max(
                current.get("estimated_avg_likes") or 0,
                candidate.get("estimated_avg_likes") or 0,
            ) or None
            current["score"] = max(float(current.get("score", 0)), float(candidate.get("score", 0)))
            current["confidence"] = self._merge_confidence(
                str(current.get("confidence", "medium")),
                str(candidate.get("confidence", "medium")),
            )
            current["investment_focus"] = sorted(
                set(current.get("investment_focus", [])) | set(candidate.get("investment_focus", []))
            )
            current["evidence"] = self._merge_text_list(current.get("evidence", []), candidate.get("evidence", []), limit=5)
            current["sample_posts"] = self._merge_text_list(
                current.get("sample_posts", []), candidate.get("sample_posts", []), limit=5
            )

    def _merge_text_list(self, left: list[str], right: list[str], limit: int) -> list[str]:
        merged: list[str] = []
        for text in [*(left or []), *(right or [])]:
            item = str(text).strip()
            if item and item not in merged:
                merged.append(item)
            if len(merged) >= limit:
                break
        return merged

    def _merge_confidence(self, left: str, right: str) -> str:
        order = {"low": 0, "medium": 1, "high": 2}
        return left if order.get(left, 1) >= order.get(right, 1) else right

    def _finalize_candidates(
        self,
        candidates_by_username: dict[str, dict[str, Any]],
        max_candidates: int,
        excluded_handles: set[str],
    ) -> list[dict[str, Any]]:
        filtered = []
        for username, candidate in candidates_by_username.items():
            if username in excluded_handles:
                continue
            followers = candidate.get("estimated_followers")
            if followers is not None and followers < self.min_followers:
                continue
            filtered.append(candidate)

        filtered.sort(
            key=lambda item: (
                float(item.get("score", 0)),
                item.get("estimated_followers") or 0,
                item.get("estimated_avg_likes") or 0,
            ),
            reverse=True,
        )
        limit = max_candidates or self.default_max_candidates
        return filtered[:limit]

    def _build_screening_prompt(self, handles: list[str]) -> str:
        """スクリーニング用プロンプトを生成する。"""
        handle_text = ", ".join(f"@{h}" for h in handles)
        return (
            f"以下のアカウントの直近30日間の投稿を調査し、投資関連度を評価してください。\n"
            f"対象アカウント: {handle_text}\n\n"
            "各アカウントについて以下を評価:\n"
            "1. investment_relevance_score (0-100): 投資関連度スコア\n"
            "   - 80-100: 具体的な銘柄コード・銘柄名を含む売買報告、決算分析を頻繁に投稿\n"
            "   - 50-79: 投資関連の話題が多いが、具体的な銘柄言及は少ない\n"
            "   - 20-49: 投資に触れることもあるが、主な話題は別\n"
            "   - 0-19: 投資にほぼ無関係\n"
            "2. tweet_count_estimate: 直近30日の投資関連ツイート推定数\n"
            "3. representative_tweets: 代表的な投資関連ツイート (最大5件)\n"
            "4. screening_summary: 1-2文の概要\n\n"
            "出力は構造化データのみ。全アカウントを漏れなく評価すること。"
        )

    def _screen_batch(
        self,
        handles: list[str],
        prompt: str,
        from_date: datetime,
        to_date: datetime,
    ) -> dict:
        """1バッチ分のスクリーニングを実行する。"""
        for attempt in range(self.max_retries):
            try:
                chat = self.client.chat.create(
                    model=self.model,
                    tools=[x_search(
                        from_date=from_date,
                        to_date=to_date,
                        allowed_x_handles=list(handles),
                    )],
                )
                chat.append(system("あなたはX/Twitter上の投資インフルエンサー調査アナリストです。"))
                chat.append(user(prompt))

                response, parsed = chat.parse(ScreeningResult)
                _ = response
                if not parsed or not isinstance(parsed, ScreeningResult):
                    raise ValueError("ScreeningResult の parse に失敗しました")

                candidates = [c.model_dump() for c in parsed.candidates]
                return {"candidates": candidates, "errors": []}
            except Exception as exc:
                sanitized_error = self._sanitize_log(str(exc))
                logger.warning(
                    "Screening batch failed",
                    extra={
                        "extra_data": {
                            "attempt": attempt + 1,
                            "max_retries": self.max_retries,
                            "error": sanitized_error,
                            "handles": handles,
                        }
                    },
                )
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_backoff_base ** attempt
                    time.sleep(wait_time)
                    continue
                return {
                    "candidates": [],
                    "errors": [{"handles": handles, "error": sanitized_error}],
                }

        return {"candidates": [], "errors": [{"handles": handles, "error": "unknown error"}]}

    def screen_candidates(
        self,
        handles: list[str],
        batch_size: int = 10,
        from_date=None,
        to_date=None,
    ) -> dict:
        """候補アカウントの投資関連度をスクリーニングする。

        Args:
            handles: スクリーニング対象のXユーザー名リスト
            batch_size: 1バッチあたりのハンドル数（x_search allowed_x_handles上限）
            from_date: 検索開始日
            to_date: 検索終了日

        Returns:
            {"candidates": [...], "errors": [...], "meta": {...}}
        """
        try:
            from .config import SCREENING_CONFIG
            cooldown = SCREENING_CONFIG.get("screen_cooldown_sec", 2)
        except (ImportError, AttributeError):
            cooldown = 2

        handles = [h.strip().lstrip("@").lower() for h in handles if h and h.strip()]
        handles = sorted(set(handles))
        if not handles:
            return {"candidates": [], "errors": ["handles が空です"], "meta": {"source": "screening"}}

        from_dt, to_dt = self._resolve_dates(from_date, to_date)
        batches = self._chunk_list(handles, batch_size)

        candidates_by_username: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []

        for i, batch in enumerate(batches):
            prompt = self._build_screening_prompt(batch)
            logger.info(
                f"Screening batch {i + 1}/{len(batches)}",
                extra={"extra_data": {"handles": batch}},
            )
            try:
                result = self._screen_batch(
                    handles=batch,
                    prompt=prompt,
                    from_date=from_dt,
                    to_date=to_dt,
                )
                for candidate in result.get("candidates", []):
                    username = str(candidate.get("username", "")).strip().lstrip("@").lower()
                    if not username:
                        continue
                    candidate["username"] = username
                    if username not in candidates_by_username:
                        candidates_by_username[username] = candidate
                    else:
                        # 高いスコアを採用
                        existing = candidates_by_username[username]
                        if candidate.get("investment_relevance_score", 0) > existing.get("investment_relevance_score", 0):
                            candidates_by_username[username] = candidate
                errors.extend(result.get("errors", []))
            except Exception as exc:
                error = {"batch": batch, "error": self._sanitize_log(str(exc))}
                errors.append(error)
                logger.error("Screening batch failed", extra={"extra_data": error})

            # バッチ間クールダウン
            if i < len(batches) - 1:
                time.sleep(cooldown)

        # スコア降順ソート
        all_candidates = sorted(
            candidates_by_username.values(),
            key=lambda x: x.get("investment_relevance_score", 0),
            reverse=True,
        )

        return {
            "candidates": all_candidates,
            "errors": errors,
            "meta": {
                "source": "screening",
                "total_handles": len(handles),
                "batch_count": len(batches),
                "from_date": from_dt.isoformat(),
                "to_date": to_dt.isoformat(),
            },
        }
