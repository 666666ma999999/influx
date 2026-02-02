"""
LLMベースのツイート分類器
Claude APIを使用して日本語の株式投資ツイートを7カテゴリに分類
"""

import os
import json
import urllib.request
import urllib.error
import time
from typing import List, Dict, Any, Optional

from .config import CLASSIFICATION_RULES


# LLM設定（config.pyに存在しない場合のデフォルト）
DEFAULT_LLM_CONFIG = {
    "model": "claude-3-5-haiku-20241022",
    "batch_size": 10,
    "max_tokens": 4096,
    "few_shot_path": None,
    "max_retries": 3,
    "retry_backoff_base": 2.0
}


class LLMClassifier:
    """Claude APIを使用したツイート分類器"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        batch_size: Optional[int] = None,
        few_shot_path: Optional[str] = None
    ):
        """
        初期化

        Args:
            api_key: Anthropic APIキー（Noneの場合は環境変数ANTHROPIC_API_KEYから取得）
            model: 使用するモデル名（Noneの場合はデフォルト）
            batch_size: 一度に処理するツイート数（Noneの場合はデフォルト）
            few_shot_path: Few-shot例のJSONファイルパス（Noneの場合は使用しない）
        """
        # APIキー取得
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "APIキーが設定されていません。環境変数ANTHROPIC_API_KEYを設定するか、"
                "コンストラクタでapi_keyを指定してください。"
            )

        # 設定の取得（config.pyにLLM_CONFIGがあればそれを使用、なければデフォルト）
        try:
            from .config import LLM_CONFIG
            config = LLM_CONFIG
        except ImportError:
            config = DEFAULT_LLM_CONFIG

        # パラメータ設定
        self.model = model or config.get("model", DEFAULT_LLM_CONFIG["model"])
        self.batch_size = batch_size or config.get("batch_size", DEFAULT_LLM_CONFIG["batch_size"])
        self.max_tokens = config.get("max_tokens", DEFAULT_LLM_CONFIG["max_tokens"])
        self.max_retries = config.get("max_retries", DEFAULT_LLM_CONFIG["max_retries"])
        self.retry_backoff_base = config.get("retry_backoff_base", DEFAULT_LLM_CONFIG["retry_backoff_base"])

        # Few-shot例のロード
        self.few_shot_path = few_shot_path or config.get("few_shot_path")
        self.few_shot_examples = self._load_few_shot_examples()

        # システムプロンプトの構築
        self.system_prompt = self._build_system_prompt()

    def _load_few_shot_examples(self) -> str:
        """
        Few-shot例をJSONファイルから読み込んでテキスト形式に変換

        Returns:
            フォーマットされたFew-shot例のテキスト
        """
        if not self.few_shot_path or not os.path.exists(self.few_shot_path):
            return ""

        try:
            with open(self.few_shot_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                examples = data.get("examples", data) if isinstance(data, dict) else data

            formatted_examples = []
            for ex in examples:
                example_text = f"""
例{len(formatted_examples) + 1}:
ユーザー: {ex.get('username', 'unknown')}
ツイート: {ex['text']}
逆指標: {ex.get('is_contrarian', False)}
→ カテゴリ: {', '.join(ex['categories'])}
→ 理由: {ex['reasoning']}
"""
                formatted_examples.append(example_text.strip())

            return "\n\n".join(formatted_examples)
        except Exception as e:
            print(f"警告: Few-shot例の読み込みに失敗しました: {e}")
            return ""

    def _build_system_prompt(self) -> str:
        """
        システムプロンプトを構築

        Returns:
            システムプロンプト文字列
        """
        # カテゴリ定義
        categories_desc = []
        for cat_key, cat_info in CLASSIFICATION_RULES.items():
            categories_desc.append(f"- {cat_key}: {cat_info['name']}")

        categories_text = "\n".join(categories_desc)

        # 基本プロンプト
        base_prompt = f"""あなたは日本語の株式投資ツイートを分類する専門家です。

【分類カテゴリ】
{categories_text}

【分類ルール】
1. 各ツイートは複数のカテゴリに該当する可能性があります
2. 該当するカテゴリが1つもない場合は空配列を返します
3. is_contrarian=true（逆指標アカウント）のツイートで強気内容の場合:
   - bullish_assets（高騰している資産）には分類しない
   - warning_signals（警戒すべき動き）に分類する
   - 理由: 逆指標アカウントの強気発言は市場の天井シグナルとして機能
4. is_contrarian=true のツイートで弱気内容の場合:
   - bearish_assets（下落している資産）には分類しない
   - 通常通り分類するか、該当なしとする
5. 「オススメしている資産」と「購入した資産」は明確に区別:
   - 「〜がいい」「おすすめ」→ recommended_assets
   - 「買った」「購入」「イン」→ purchased_assets
6. 信頼度（confidence）は以下の基準で設定:
   - 0.9-1.0: カテゴリが明確、根拠が確実
   - 0.7-0.9: カテゴリが妥当、根拠が十分
   - 0.5-0.7: カテゴリが推測、根拠がやや弱い
   - 0.3-0.5: カテゴリが不明瞭、根拠が不十分

【カテゴリ詳細】
- recommended_assets: 他者に推奨している資産。キーワード例: 割安、おすすめ、一択、〜がいい
- purchased_assets: 本人が実際に購入・保有した資産。キーワード例: 買った、購入、イン、エントリー、保有
- ipo: IPOに関する内容。キーワード例: IPO、新規公開、抽選、当選
- market_trend: 市況全体のトレンド分析。キーワード例: 相場、地合い、トレンド、利上げ、円安
- bullish_assets: 高騰している資産の報告。キーワード例: 爆上げ、急騰、ストップ高、絶好調
- bearish_assets: 下落している資産の報告。キーワード例: 暴落、急落、ストップ安、が弱い
- warning_signals: 警戒すべき動き。逆指標アカウントの強気発言、バブルサイン等

【出力形式】
JSON配列で以下の形式で返してください:
[
  {{
    "id": <ツイートID>,
    "categories": ["category1", "category2"],
    "reasoning": "分類の理由を日本語で簡潔に説明",
    "confidence": 0.85
  }}
]
"""

        # Few-shot例を追加
        if self.few_shot_examples:
            base_prompt += f"\n\n【分類例】\n{self.few_shot_examples}"

        base_prompt += "\n\n必ずJSON配列のみを返してください。余計な説明は不要です。"

        return base_prompt

    def _call_api(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Claude APIを呼び出し

        Args:
            messages: メッセージリスト

        Returns:
            APIレスポンス

        Raises:
            Exception: API呼び出しに失敗した場合
        """
        for attempt in range(self.max_retries):
            try:
                # リクエストボディ
                request_body = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": self.system_prompt,
                    "messages": messages
                }

                data = json.dumps(request_body).encode('utf-8')

                # HTTPリクエスト作成
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": self.api_key,
                        "anthropic-version": "2023-06-01"
                    }
                )

                # API呼び出し
                with urllib.request.urlopen(req, timeout=60) as response:
                    response_data = json.loads(response.read().decode('utf-8'))
                    return response_data

            except urllib.error.HTTPError as e:
                error_body = e.read().decode('utf-8')
                print(f"HTTPエラー (試行 {attempt + 1}/{self.max_retries}): {e.code} - {error_body}")

                # 429 (Rate Limit) または 500系エラーの場合はリトライ
                if e.code in [429, 500, 502, 503, 504] and attempt < self.max_retries - 1:
                    wait_time = self.retry_backoff_base ** attempt
                    print(f"{wait_time}秒待機してリトライします...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"API呼び出し失敗: {e.code} - {error_body}")

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

    def classify_batch(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        ツイートのバッチを分類

        Args:
            tweets: ツイートのリスト（各要素は辞書）

        Returns:
            分類結果のリスト [{"id": int, "llm_categories": [...], "llm_reasoning": str, "llm_confidence": float}]
        """
        if not tweets:
            return []

        # ユーザーメッセージの構築
        tweets_for_llm = []
        for i, tweet in enumerate(tweets):
            tweets_for_llm.append({
                "id": i,
                "username": tweet.get("username", "unknown"),
                "text": tweet.get("text", ""),
                "is_contrarian": tweet.get("is_contrarian", False)
            })

        user_content = json.dumps(tweets_for_llm, ensure_ascii=False, indent=2)

        messages = [
            {
                "role": "user",
                "content": f"以下のツイートを分類してください:\n\n{user_content}"
            }
        ]

        # API呼び出し
        try:
            response = self._call_api(messages)

            # レスポンスからテキストを抽出
            content_blocks = response.get("content", [])
            if not content_blocks:
                print("警告: APIレスポンスにcontentが含まれていません")
                return []

            # テキストブロックを探す
            response_text = ""
            for block in content_blocks:
                if block.get("type") == "text":
                    response_text = block.get("text", "")
                    break

            if not response_text:
                print("警告: APIレスポンスにテキストが含まれていません")
                return []

            # JSON配列を抽出（前後の余計なテキストを除去）
            response_text = response_text.strip()
            # ```json ブロックで囲まれている場合は除去
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(lines[1:-1])

            # JSONパース
            classifications = json.loads(response_text)

            # 結果の整形
            results = []
            for cls in classifications:
                results.append({
                    "id": cls.get("id", 0),
                    "llm_categories": cls.get("categories", []),
                    "llm_reasoning": cls.get("reasoning", ""),
                    "llm_confidence": cls.get("confidence", 0.5)
                })

            return results

        except json.JSONDecodeError as e:
            print(f"エラー: APIレスポンスのJSONパースに失敗しました: {e}")
            print(f"レスポンステキスト: {response_text[:500]}")
            return []
        except Exception as e:
            print(f"エラー: バッチ分類に失敗しました: {e}")
            return []

    def classify_all(self, tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        全ツイートを分類（バッチ処理）

        Args:
            tweets: ツイートのリスト

        Returns:
            LLM分類結果を追加したツイートのリスト
            （各ツイートに llm_categories, llm_reasoning, llm_confidence フィールドが追加される）
        """
        if not tweets:
            return []

        print(f"LLM分類を開始: 全{len(tweets)}件のツイートを{self.batch_size}件ずつ処理")

        # バッチに分割
        total_batches = (len(tweets) + self.batch_size - 1) // self.batch_size

        for batch_idx in range(total_batches):
            start_idx = batch_idx * self.batch_size
            end_idx = min(start_idx + self.batch_size, len(tweets))
            batch = tweets[start_idx:end_idx]

            print(f"バッチ {batch_idx + 1}/{total_batches} を処理中 ({start_idx + 1}-{end_idx}件目)...")

            # バッチ分類
            results = self.classify_batch(batch)

            # 結果をツイートにマージ
            for i, result in enumerate(results):
                tweet_idx = start_idx + result["id"]
                if tweet_idx < len(tweets):
                    tweets[tweet_idx]["llm_categories"] = result["llm_categories"]
                    tweets[tweet_idx]["llm_reasoning"] = result["llm_reasoning"]
                    tweets[tweet_idx]["llm_confidence"] = result["llm_confidence"]

            # レート制限対策（バッチ間で少し待機）
            if batch_idx < total_batches - 1:
                time.sleep(1)

        print(f"LLM分類完了: 全{len(tweets)}件のツイートを処理しました")
        return tweets


def test_classifier():
    """分類器のテスト"""
    # テストデータ
    test_tweets = [
        {
            "username": "test_user1",
            "text": "今日はトヨタ株を1000株買いました！",
            "is_contrarian": False
        },
        {
            "username": "test_user2",
            "text": "ビットコインがおすすめです。今が買い時。",
            "is_contrarian": False
        },
        {
            "username": "gihuboy",
            "text": "日経平均は5万円を超える！絶対に上がる！",
            "is_contrarian": True
        },
        {
            "username": "test_user3",
            "text": "米国株が急落。円高が進んでいる。",
            "is_contrarian": False
        }
    ]

    # 分類器の初期化
    classifier = LLMClassifier()

    # 分類実行
    results = classifier.classify_all(test_tweets)

    # 結果表示
    print("\n=== 分類結果 ===")
    for tweet in results:
        print(f"\nユーザー: {tweet['username']}")
        print(f"ツイート: {tweet['text']}")
        print(f"逆指標: {tweet.get('is_contrarian', False)}")
        print(f"カテゴリ: {tweet.get('llm_categories', [])}")
        print(f"理由: {tweet.get('llm_reasoning', '')}")
        print(f"信頼度: {tweet.get('llm_confidence', 0.0)}")


if __name__ == "__main__":
    test_classifier()
