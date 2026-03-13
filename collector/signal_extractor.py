"""LLMベースの売買シグナル抽出モジュール。

ツイートからティッカー・売買方向・確信度を抽出する。
xAI Grok API (OpenAI互換REST) を使用（urllibベース）。
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from collector.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SIGNAL_CONFIG = {
    "model": "grok-3-mini-fast",
    "batch_size": 15,
    "max_tokens": 4096,
    "max_retries": 3,
    "retry_backoff_base": 2.0,
}

XAI_API_URL = "https://api.x.ai/v1/chat/completions"

SYSTEM_PROMPT = """あなたは日本語の株式投資ツイートから売買シグナルを抽出する専門家です。

【タスク】
各ツイートから以下を抽出してください:
- ticker: 言及されている銘柄のティッカーシンボル（日本株は証券コード.T形式、米国株は$記号なし）
- direction: 売買方向（LONG=買い推奨/購入報告/強気、SHORT=売り推奨/売却報告/弱気）
- confidence: 確信度（0.0-1.0）
- matched_text: シグナルの根拠となるテキスト部分
- reasoning: 抽出理由

【ルール】
1. 具体的な銘柄名/ティッカーが含まれないツイートはスキップ（空配列を返す）
2. 1ツイートから複数のシグナルを抽出可能（複数銘柄言及時）
3. 日本株ティッカーは証券コード.T形式（例: 7203.T）、証券コードが不明な場合のみカタカナ/英語名を使用
4. 米国株は標準ティッカー（例: AAPL, MSFT）
5. ETF/投信はファンド略称を使用（例: ACWI, VOO, eMAXIS）
6. confidence 0.5未満のシグナルは出力に含めない
7. 市況全般のコメント（具体的銘柄なし）はスキップ

【is_contrarian について】
is_contrarian=true のアカウントの場合:
- 強気発言（買い推奨、上昇予想）→ direction を反転して SHORT にする
- 弱気発言（売り推奨、下落予想）→ direction を反転して LONG にする
- reasoning に「逆指標アカウントのため方向反転」と記載

【出力形式】
JSON配列で以下の形式:
[
  {
    "tweet_id": <ツイートのインデックス>,
    "signals": [
      {
        "ticker": "7203.T",
        "direction": "LONG",
        "confidence": 0.85,
        "matched_text": "トヨタ株を1000株買いました",
        "reasoning": "購入報告のため LONG シグナル"
      }
    ]
  }
]

シグナルが抽出できないツイートは signals を空配列にしてください。
必ずJSON配列のみを返してください。余計な説明は不要です。"""


class SignalExtractor:
    """xAI Grok APIを使用した売買シグナル抽出器。

    urllib.requestによるHTTP直接呼び出しでxAI REST API (OpenAI互換) を利用する。
    """

    @staticmethod
    def _sanitize_log(text: str) -> str:
        """ログ出力からAPIキーなどの機密情報をマスク。"""
        text = re.sub(r'xai-[A-Za-z0-9_\-]+', 'xai-***REDACTED***', text)
        text = re.sub(r'sk-ant-[a-zA-Z0-9\-_]+', 'sk-ant-***REDACTED***', text)
        text = re.sub(
            r'(?i)(api[_-]?key|token|secret|bearer)["\s:=]+["\']?([a-zA-Z0-9\-_]{20,})',
            r'\1: ***REDACTED***', text
        )
        return text

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        batch_size: Optional[int] = None,
    ):
        """初期化。

        Args:
            api_key: xAI APIキー（Noneの場合は環境変数XAI_API_KEYから取得）
            model: 使用するモデル名
            batch_size: 一度に処理するツイート数
        """
        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "APIキーが設定されていません。環境変数XAI_API_KEYを設定するか、"
                "コンストラクタでapi_keyを指定してください。"
            )

        config = DEFAULT_SIGNAL_CONFIG
        self.model = model or config["model"]
        self.batch_size = batch_size or config["batch_size"]
        self.max_tokens = config["max_tokens"]
        self.max_retries = config["max_retries"]
        self.retry_backoff_base = config["retry_backoff_base"]

    def _call_api(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """xAI REST APIを呼び出し（OpenAI互換フォーマット）。

        Args:
            messages: メッセージリスト

        Returns:
            APIレスポンス

        Raises:
            Exception: API呼び出しに失敗した場合
        """
        for attempt in range(self.max_retries):
            try:
                # OpenAI互換: system はmessagesの先頭に含める
                full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

                request_body = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "messages": full_messages,
                }

                data = json.dumps(request_body).encode("utf-8")

                req = urllib.request.Request(
                    XAI_API_URL,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                        "User-Agent": "influx-signal-extractor/1.0",
                        "Accept": "application/json",
                    },
                )

                with urllib.request.urlopen(req, timeout=60) as response:
                    response_data = json.loads(response.read().decode("utf-8"))
                    return response_data

            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8")
                logger.error(
                    "API HTTP error",
                    extra={
                        "extra_data": {
                            "status_code": e.code,
                            "attempt": attempt + 1,
                            "max_retries": self.max_retries,
                            "error_body": self._sanitize_log(error_body[:500]),
                        }
                    },
                )

                if e.code in [429, 500, 502, 503, 504] and attempt < self.max_retries - 1:
                    wait_time = self.retry_backoff_base ** attempt
                    print(f"{wait_time}秒待機してリトライします...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"API呼び出し失敗: {e.code} - {self._sanitize_log(error_body[:500])}")

            except urllib.error.URLError as e:
                print(f"ネットワークエラー (試行 {attempt + 1}/{self.max_retries}): {e.reason}")
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_backoff_base ** attempt
                    print(f"{wait_time}秒待機してリトライします...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"ネットワークエラー: {e.reason}")

            except Exception as e:
                print(f"予期しないエラー (試行 {attempt + 1}/{self.max_retries}): {e}")
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_backoff_base ** attempt
                    print(f"{wait_time}秒待機してリトライします...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise

        raise Exception("最大リトライ回数に到達しました")

    @staticmethod
    def _resolve_ticker(ticker: str) -> str:
        """LLM出力のティッカーをJAPANESE_TICKER_MAP/ETF_MAPで正規化する。

        Args:
            ticker: LLMが出力したティッカー文字列

        Returns:
            正規化されたティッカー（マッピングにない場合はそのまま返す）
        """
        from collector.ticker_extractor import JAPANESE_TICKER_MAP, ETF_MAP

        # 既にティッカー形式（大文字英字のみ or XXXX.T）ならスキップ
        if re.match(r'^[A-Z]{1,5}$', ticker) or re.match(r'^\d{4}\.T$', ticker):
            return ticker

        # JAPANESE_TICKER_MAP で検索
        if ticker in JAPANESE_TICKER_MAP:
            return JAPANESE_TICKER_MAP[ticker]

        # ETF_MAP で検索
        if ticker in ETF_MAP:
            return ETF_MAP[ticker]

        return ticker

    def extract_batch(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """ツイートバッチからシグナルを抽出する。

        Args:
            tweets: ツイートのリスト

        Returns:
            シグナル抽出結果のリスト
        """
        if not tweets:
            return []

        tweets_for_llm = []
        for i, tweet in enumerate(tweets):
            tweets_for_llm.append({
                "id": i,
                "username": tweet.get("username", "unknown"),
                "text": tweet.get("text", ""),
                "is_contrarian": tweet.get("is_contrarian", False),
            })

        user_content = json.dumps(tweets_for_llm, ensure_ascii=False, indent=2)

        messages = [
            {
                "role": "user",
                "content": f"以下のツイートから売買シグナルを抽出してください:\n\n{user_content}",
            }
        ]

        try:
            response = self._call_api(messages)

            # OpenAI互換レスポンス: choices[0].message.content
            choices = response.get("choices", [])
            if not choices:
                print("警告: APIレスポンスにchoicesが含まれていません")
                return []

            response_text = choices[0].get("message", {}).get("content", "")

            if not response_text:
                return []

            response_text = response_text.strip()
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1])

            raw_results = json.loads(response_text)

            # Flatten signals with tweet info
            all_signals = []
            for result in raw_results:
                if not isinstance(result, dict):
                    continue
                tweet_id = result.get("tweet_id", 0)
                try:
                    tweet_id = int(tweet_id)
                except (ValueError, TypeError):
                    tweet_id = 0

                if tweet_id < 0 or tweet_id >= len(tweets):
                    continue

                tweet = tweets[tweet_id]
                for sig in result.get("signals", []):
                    if not isinstance(sig, dict):
                        continue

                    ticker = str(sig.get("ticker", "")).strip()
                    ticker = self._resolve_ticker(ticker)
                    direction = str(sig.get("direction", "")).upper()
                    if not ticker or direction not in ("LONG", "SHORT"):
                        continue

                    confidence = sig.get("confidence", 0.5)
                    try:
                        confidence = max(0.0, min(1.0, float(confidence)))
                    except (ValueError, TypeError):
                        confidence = 0.5

                    if confidence < 0.5:
                        continue

                    all_signals.append({
                        "tweet_url": tweet.get("url", ""),
                        "username": tweet.get("username", ""),
                        "display_name": tweet.get("display_name", ""),
                        "posted_at": tweet.get("posted_at", ""),
                        "is_contrarian": tweet.get("is_contrarian", False),
                        "ticker": ticker,
                        "direction": direction,
                        "confidence": confidence,
                        "matched_text": str(sig.get("matched_text", ""))[:200],
                        "reasoning": str(sig.get("reasoning", ""))[:500],
                    })

            return all_signals

        except json.JSONDecodeError as e:
            logger.error("API response JSON parse error", extra={"extra_data": {"error": str(e)}})
            return []
        except Exception as e:
            print(f"エラー: バッチシグナル抽出に失敗しました: {e}")
            return []

    def extract_all(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """全ツイートからシグナルを抽出（バッチ処理）。

        Args:
            tweets: ツイートのリスト

        Returns:
            全シグナルのリスト
        """
        if not tweets:
            return []

        print(f"シグナル抽出を開始: 全{len(tweets)}件のツイートを{self.batch_size}件ずつ処理")

        all_signals = []
        total_batches = (len(tweets) + self.batch_size - 1) // self.batch_size

        for batch_idx in range(total_batches):
            start_idx = batch_idx * self.batch_size
            end_idx = min(start_idx + self.batch_size, len(tweets))
            batch = tweets[start_idx:end_idx]

            print(f"バッチ {batch_idx + 1}/{total_batches} を処理中 ({start_idx + 1}-{end_idx}件目)...")

            signals = self.extract_batch(batch)
            all_signals.extend(signals)

            if batch_idx < total_batches - 1:
                time.sleep(1)

        print(f"シグナル抽出完了: {len(all_signals)}件のシグナルを抽出")
        return all_signals

    def cross_validate_with_extractor(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """TickerExtractorでクロスバリデーションを行う。

        LLM抽出ティッカーをTickerExtractorの正規化マッピングと突合し、
        一致するものにフラグを付与する。

        Args:
            signals: extract_all() の出力

        Returns:
            validated フィールドが追加されたシグナルリスト
        """
        try:
            from collector.ticker_extractor import TickerExtractor
            extractor = TickerExtractor()
        except ImportError:
            logger.warning("TickerExtractor が利用できません。バリデーションをスキップします。")
            for sig in signals:
                sig["cross_validated"] = False
            return signals

        for sig in signals:
            # TickerExtractor で同じツイートからティッカーを抽出し、LLM結果と突合
            dummy_tweet = {"text": sig.get("matched_text", ""), "categories": []}
            regex_tickers = extractor.extract(dummy_tweet)
            regex_ticker_set = {t["ticker"] for t in regex_tickers}

            sig["cross_validated"] = sig["ticker"] in regex_ticker_set

        return signals
