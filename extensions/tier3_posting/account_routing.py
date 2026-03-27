"""マルチアカウント投稿ルーティング設定。

カテゴリ/template_type/投稿内容に基づいて投稿先Xアカウントを自動決定する。
ハイブリッド方式: キーワード採点（高速）→ LLMフォールバック（曖昧時のみ）。
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── アカウント定義 ──────────────────────────────────────────

ACCOUNTS: Dict[str, dict] = {
    "maaaki": {
        "label": "maaaki@",
        "profile_path": "./x_profiles/maaaki",
        "categories": ["claude", "取締役の経験"],
    },
    "kabuki666999": {
        "label": "kabuki666999",
        "profile_path": "./x_profiles/kabuki666999",
        "categories": ["投資", "仮想通貨", "ポケカ"],
    },
}

DEFAULT_ACCOUNT = "kabuki666999"


# ── template_type → account_id ──────────────────────────────

TEMPLATE_ROUTING: Dict[str, str] = {
    "win_rate_ranking": "kabuki666999",
    "contrarian_signal": "kabuki666999",
    "weekly_report": "kabuki666999",
    "market_summary": "kabuki666999",
    "hot_picks": "kabuki666999",
    "trade_activity": "kabuki666999",
    "earnings_flash": "kabuki666999",
    # manual は本文ベース判定にフォールスルー
}


# ── コンテンツベース ルーティングキーワード ────────────────────

CONTENT_KEYWORDS: Dict[str, dict] = {
    "maaaki": {
        "keywords": [
            # Claude / AI
            "Claude", "Anthropic", "ChatGPT", "GPT-4", "GPT-5",
            "LLM", "生成AI", "プロンプト", "RAG", "MCP",
            "Cursor", "Copilot", "Claude Code", "Codex",
            "機械学習", "ディープラーニング", "深層学習",
            "AGI", "Foundation Model", "エージェント",
            "AI活用", "AI導入", "AI自動化",
            # 取締役 / 経営
            "取締役", "CTO", "CEO", "COO", "経営",
            "スタートアップ", "起業", "創業",
            "マネジメント", "リーダーシップ", "組織",
            "エンジニア組織", "開発チーム", "テックリード",
            "プロダクト", "SaaS", "B2B", "PMF",
            "採用", "評価", "意思決定", "経営会議",
        ],
        "patterns": [
            r"(?i)claude\s*(code|api|sonnet|opus|haiku)",
            r"(?i)(anthropic|openai|google\s*ai|gemini)",
            r"(?i)\b(LLM|RAG|fine.?tun|embedding)\b",
            r"(取締役|CTO|経営者)として",
            r"(エンジニア|開発)(組織|チーム|文化)",
            r"(AI|生成AI).*(業務|組織|事業|効率化|自動化)",
        ],
    },
    "kabuki666999": {
        "keywords": [
            # 投資 / 株式
            "株", "銘柄", "投資", "配当", "NISA", "IPO",
            "日経", "S&P", "ダウ", "TOPIX", "ETF",
            "利確", "損切り", "ナンピン", "買い増し",
            "決算", "業績", "増収", "上方修正", "下方修正",
            "相場", "市況", "地合い", "セクター",
            "インフルエンサー", "勝率", "逆指標",
            "高騰", "暴落", "急騰", "急落",
            # 仮想通貨 / BTC
            "BTC", "ビットコイン", "仮想通貨", "暗号資産",
            "ETH", "イーサリアム", "XRP", "リップル",
            "アルトコイン", "マイニング", "ブロックチェーン",
            "半減期", "乖離率",
            # ポケカ
            "ポケカ", "ポケモンカード", "PSA",
            "BOX", "プロモ", "SAR", "SR", "AR",
            "リザードン", "ピカチュウ",
        ],
        "patterns": [
            r"(日経平均|TOPIX|S&P|ダウ|ナスダック)",
            r"[0-9]{4}(\.T|\.JP)?[\s\u3000]",  # 証券コード
            r"(円安|円高|ドル円|ユーロ円)",
            r"(?i)(bitcoin|ethereum|crypto|defi|nft)",
            r"ポケ(モン)?カード",
            r"(勝率|的中率).*%",
        ],
    },
}

# キーワード信頼度の閾値（これ以上ならキーワード結果を採用、未満ならLLM）
KEYWORD_CONFIDENCE_THRESHOLD = 0.6


# ── キーワードベース分類 ────────────────────────────────────

def classify_content(text: str) -> Tuple[str, float]:
    """投稿本文をキーワード採点してアカウントを判定する。

    Args:
        text: 投稿本文

    Returns:
        (account_id, confidence) のタプル。confidence は 0.0-1.0。
    """
    if not text:
        return DEFAULT_ACCOUNT, 0.0

    scores: Dict[str, int] = {}

    for account_id, rules in CONTENT_KEYWORDS.items():
        score = 0

        # キーワードマッチ（+1点）
        for keyword in rules.get("keywords", []):
            if keyword in text:
                score += 1

        # パターンマッチ（+2点、より特異的）
        for pattern in rules.get("patterns", []):
            if re.search(pattern, text):
                score += 2

        scores[account_id] = score

    total = sum(scores.values())
    if total == 0:
        return DEFAULT_ACCOUNT, 0.0

    winner = max(scores, key=scores.get)
    confidence = scores[winner] / total if total > 0 else 0.0

    return winner, confidence


# ── LLMベース分類（フォールバック）──────────────────────────

def classify_content_llm(text: str) -> str:
    """Claude APIで曖昧な投稿のアカウントを判定する。

    キーワード判定の信頼度が低い場合のみ呼ばれる。
    Haiku使用でコスト最小化。

    Args:
        text: 投稿本文

    Returns:
        account_id 文字列
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY未設定、デフォルトアカウントを使用")
        return DEFAULT_ACCOUNT

    account_desc = "\n".join(
        f"- {aid}: {', '.join(acc['categories'])}"
        for aid, acc in ACCOUNTS.items()
    )

    prompt = (
        "以下のX投稿テキストを、最も適切な投稿アカウントに振り分けてください。\n\n"
        f"【アカウント定義】\n{account_desc}\n\n"
        f"【投稿テキスト】\n{text[:500]}\n\n"
        '【回答形式】\nJSON: {"account_id": "<id>", "reason": "<理由1文>"}\nJSONのみ返してください。'
    )

    request_body = {
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        data = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "anthropic-version": "2023-06-01",
                "User-Agent": "influx-account-router/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        response_text = result["content"][0]["text"].strip()
        # マークダウンコードブロック除去
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1])

        parsed = json.loads(response_text)
        account_id = parsed.get("account_id", DEFAULT_ACCOUNT)
        reason = parsed.get("reason", "")

        if account_id in ACCOUNTS:
            logger.info("LLM判定: %s → @%s (%s)", text[:30], account_id, reason)
            return account_id

        return DEFAULT_ACCOUNT

    except Exception as e:
        logger.warning("LLM分類失敗、デフォルト使用: %s", e)
        return DEFAULT_ACCOUNT


# ── ルーティング解決（メインエントリポイント）────────────────

def resolve_account(draft: dict) -> str:
    """ドラフトの投稿先アカウントを解決する。

    優先順位:
        1. draft["account_id"] が明示的に設定されている場合 → そのまま
        2. template_type によるルーティング（manual以外）
        3. 本文キーワード採点 → 高信頼度ならそれを使用
        4. LLMフォールバック（曖昧な場合のみ）
        5. デフォルトアカウント

    Args:
        draft: ドラフト辞書

    Returns:
        account_id 文字列
    """
    # 1. 手動指定
    explicit = draft.get("account_id")
    if explicit and explicit in ACCOUNTS:
        return explicit

    # 2. template_type ルーティング（自動生成投稿）
    template_type = draft.get("template_type", "")
    if template_type and template_type != "manual" and template_type in TEMPLATE_ROUTING:
        return TEMPLATE_ROUTING[template_type]

    # 3. 本文ベース判定
    body = draft.get("body", "")
    if body:
        account_id, confidence = classify_content(body)
        if confidence >= KEYWORD_CONFIDENCE_THRESHOLD:
            return account_id
        # 4. LLMフォールバック
        return classify_content_llm(body)

    # 5. デフォルト
    return DEFAULT_ACCOUNT


# ── ユーティリティ ──────────────────────────────────────────

def get_profile_path(account_id: str) -> str:
    """account_id からブラウザプロファイルパスを取得する。"""
    account = ACCOUNTS.get(account_id)
    if account:
        return account["profile_path"]
    return ACCOUNTS[DEFAULT_ACCOUNT]["profile_path"]


def get_account_label(account_id: str) -> str:
    """account_id から表示ラベルを取得する。"""
    account = ACCOUNTS.get(account_id)
    return account["label"] if account else account_id


def get_account_list() -> list:
    """UI用のアカウント一覧を返す。"""
    return [
        {"id": aid, "label": acc["label"], "categories": acc.get("categories", [])}
        for aid, acc in ACCOUNTS.items()
    ]
