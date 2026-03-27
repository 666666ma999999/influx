#!/usr/bin/env python3
"""ブックマーク教師データセット構築CLI。

ブックマークJSONLを正規化・ラベリングして教師データセットを構築する。

Usage:
    python -m extensions.tier3_posting.cli.build_style_dataset
    python -m extensions.tier3_posting.cli.build_style_dataset --use-llm
    python -m extensions.tier3_posting.cli.build_style_dataset --input path/to/bookmarks.jsonl
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# デフォルトパス
DEFAULT_INPUT = PROJECT_ROOT / "data" / "writing_style" / "bookmarks" / "raw" / "x_bookmarks.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "writing_style" / "bookmarks" / "normalized.jsonl"

# topic_domain判定キーワード
TOPIC_KEYWORDS = {
    "ai": [
        "AI", "Claude", "GPT", "LLM", "ChatGPT", "Gemini", "機械学習",
        "ディープラーニング", "深層学習", "自然言語処理", "NLP", "プロンプト",
        "生成AI", "Copilot", "OpenAI", "Anthropic",
    ],
    "investing": [
        "株", "投資", "銘柄", "配当", "PER", "PBR", "決算", "NISA",
        "インデックス", "S&P", "ナスダック", "日経", "TOPIX", "ETF",
        "信用取引", "利回り", "四季報", "増配", "減配", "上方修正",
        "下方修正", "高配当",
    ],
    "crypto": [
        "BTC", "ビットコイン", "仮想通貨", "暗号資産", "ETH", "イーサリアム",
        "XRP", "リップル", "NFT", "DeFi", "ブロックチェーン", "マイニング",
        "取引所", "Binance", "Coinbase",
    ],
    "pokeca": [
        "ポケカ", "ポケモンカード", "Pokemon Card", "PSA", "鑑定",
        "カドショ", "未開封", "BOX", "SR", "SAR", "UR",
    ],
    "management": [
        "経営", "取締役", "CEO", "CTO", "マネジメント", "組織",
        "採用", "スタートアップ", "起業", "ビジネスモデル", "資金調達",
        "IPO上場", "経営戦略",
    ],
}

# topic_domain → target_account マッピング
ACCOUNT_MAPPING = {
    "ai": "maaaki",
    "management": "maaaki",
    "investing": "kabuki666999",
    "crypto": "kabuki666999",
    "pokeca": "kabuki666999",
}

# hook_pattern判定用
SURPRISE_PATTERNS = re.compile(r"^(これ|すごい|やばい|まじ|えぐい|ガチ|衝撃)", re.IGNORECASE)
PROBLEM_PATTERNS = re.compile(r"^(なぜ|どうして|問題|課題|〜できない|悩み|困った)")
NUMBER_FIRST_PATTERN = re.compile(r"^\d")

# CTA判定
CTA_PATTERNS = re.compile(
    r"(フォロー|いいね|RT|リツイート|リプ|シェア|拡散|保存|ブックマーク|"
    r"チェック|見て|読んで|試して|やってみ|プロフ|固ツイ)",
    re.IGNORECASE,
)


def read_jsonl(path: Path) -> list:
    """JSONLファイルを読み込む。"""
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return items


def write_jsonl(path: Path, items: list) -> None:
    """JSONLファイルに書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def make_bookmark_id(url: str) -> str:
    """URLからbookmark_idを生成する。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def extract_features(text: str) -> dict:
    """テキストからルールベースで特徴量を抽出する。"""
    lines = text.strip().split("\n")
    numbered_list = bool(re.search(r"(?:^|\n)\s*\d+[\.\)）]\s", text))
    bullet_list = bool(re.search(r"(?:^|\n)\s*[・\-\*•▶▷→]\s", text))
    emoji_count = len(re.findall(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF"
        r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF"
        r"\U0000FE00-\U0000FE0F\U0000200D]",
        text,
    ))
    has_cta = bool(CTA_PATTERNS.search(text))
    has_numbers = bool(re.search(r"\d+", text))

    return {
        "char_len": len(text),
        "line_count": len(lines),
        "has_numbered_list": numbered_list,
        "has_bullet_list": bullet_list,
        "emoji_count": emoji_count,
        "has_cta": has_cta,
        "has_numbers": has_numbers,
    }


def detect_topic_domains(text: str) -> list:
    """テキストからtopic_domainをルールベースで判定する。"""
    domains = []
    for domain, keywords in TOPIC_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text.lower():
                domains.append(domain)
                break
    return domains if domains else ["general"]


def detect_target_account(topic_domains: list) -> str:
    """topic_domainからtarget_accountを推定する。"""
    for domain in topic_domains:
        if domain in ACCOUNT_MAPPING:
            return ACCOUNT_MAPPING[domain]
    return "maaaki"


def detect_style_format(features: dict) -> str:
    """構造からstyle_formatを判定する。"""
    if features["has_numbered_list"]:
        return "listicle"
    if features["has_bullet_list"]:
        return "checklist"
    if features["char_len"] < 100:
        return "breaking"
    return "explainer"


def detect_hook_pattern(text: str) -> str:
    """冒頭からhook_patternを判定する。"""
    first_line = text.strip().split("\n")[0].strip()
    if NUMBER_FIRST_PATTERN.match(first_line):
        return "number-first"
    if SURPRISE_PATTERNS.match(first_line):
        return "surprise-first"
    if PROBLEM_PATTERNS.match(first_line):
        return "problem-first"
    return "thesis-first"


def detect_tone(text: str, features: dict) -> list:
    """キーワード/構造からtoneを推定する。"""
    tones = []
    if features["has_numbered_list"] or features["has_bullet_list"]:
        tones.append("practical")
    if re.search(r"(急騰|暴落|速報|緊急|衝撃)", text):
        tones.append("urgent")
    if re.search(r"(分析|考察|要因|背景|理由)", text):
        tones.append("analytical")
    if re.search(r"(おすすめ|便利|使える|知っておく)", text):
        tones.append("helpful")
    if features["emoji_count"] >= 3:
        tones.append("casual")
    if not tones:
        tones.append("neutral")
    return tones


def call_llm_for_style_notes(text: str, api_key: str) -> list:
    """Claude Haikuにスタイル特徴を問い合わせる。"""
    url = "https://api.anthropic.com/v1/messages"
    prompt = (
        "以下のX(Twitter)投稿テキストの文体・スタイル上の特徴を3〜5個、"
        "日本語の短い文で列挙してください。"
        "JSONの文字列配列として出力してください。説明は不要です。\n\n"
        f"投稿テキスト:\n{text[:500]}"
    )
    body = json.dumps({
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content_text = result.get("content", [{}])[0].get("text", "[]")
        # JSONの配列部分を抽出
        match = re.search(r"\[.*\]", content_text, re.DOTALL)
        if match:
            notes = json.loads(match.group())
            if isinstance(notes, list):
                return [str(n) for n in notes[:5]]
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  LLM呼び出しエラー: {e}", file=sys.stderr)

    return []


def normalize_bookmark(raw: dict) -> Optional[dict]:
    """生ブックマークデータを正規化する。"""
    text = raw.get("text", "").strip()
    if not text:
        return None

    url = raw.get("url", "")
    if not url:
        return None

    features = extract_features(text)
    topic_domains = detect_topic_domains(text)
    target_account = detect_target_account(topic_domains)
    style_format = detect_style_format(features)
    hook_pattern = detect_hook_pattern(text)
    tone = detect_tone(text, features)

    return {
        "bookmark_id": make_bookmark_id(url),
        "url": url,
        "text": text,
        "author": raw.get("author", raw.get("username", "")),
        "created_at": raw.get("created_at", raw.get("posted_at", "")),
        "metrics": {
            "like_count": raw.get("like_count", raw.get("metrics", {}).get("like_count", 0)) or 0,
            "retweet_count": raw.get("retweet_count", raw.get("metrics", {}).get("retweet_count", 0)) or 0,
            "reply_count": raw.get("reply_count", raw.get("metrics", {}).get("reply_count", 0)) or 0,
        },
        "labels": {
            "target_account": target_account,
            "topic_domain": topic_domains,
            "post_intent": "curation",
            "style_format": style_format,
            "hook_pattern": hook_pattern,
            "tone": tone,
        },
        "features": features,
        "style_notes": [],
    }


def deduplicate(items: list) -> list:
    """bookmark_idで重複除去する。"""
    seen = set()
    result = []
    for item in items:
        bid = item.get("bookmark_id", "")
        if bid and bid not in seen:
            seen.add(bid)
            result.append(item)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="ブックマークJSONLを正規化・ラベリングして教師データセットを構築する"
    )
    parser.add_argument(
        "--input", "-i",
        type=str,
        default=str(DEFAULT_INPUT),
        help=f"入力JSONLファイルパス (デフォルト: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=str(DEFAULT_OUTPUT),
        help=f"出力JSONLファイルパス (デフォルト: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Claude Haikuでstyle_notesを補助ラベリングする",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"エラー: 入力ファイルが見つかりません: {input_path}", file=sys.stderr)
        sys.exit(1)

    # LLM使用時のAPIキー確認
    api_key = None
    if args.use_llm:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("警告: ANTHROPIC_API_KEYが未設定のためLLMラベリングをスキップします", file=sys.stderr)
            args.use_llm = False

    # 1. 読み込み
    print(f"入力ファイル読み込み: {input_path}")
    raw_items = read_jsonl(input_path)
    print(f"  読み込み件数: {len(raw_items)}")

    # 2. 正規化
    normalized = []
    skipped = 0
    for raw in raw_items:
        item = normalize_bookmark(raw)
        if item is not None:
            normalized.append(item)
        else:
            skipped += 1

    print(f"  正規化完了: {len(normalized)}件 (スキップ: {skipped}件)")

    # 3. 重複除去
    before_dedup = len(normalized)
    normalized = deduplicate(normalized)
    print(f"  重複除去: {before_dedup} → {len(normalized)}件")

    # 4. LLM補助ラベリング
    if args.use_llm and api_key:
        print("LLM補助ラベリング開始...")
        for i, item in enumerate(normalized):
            print(f"  [{i + 1}/{len(normalized)}] {item['bookmark_id'][:8]}...", end=" ")
            notes = call_llm_for_style_notes(item["text"], api_key)
            item["style_notes"] = notes
            print(f"→ {len(notes)}個の特徴を取得")

    # 5. 出力
    write_jsonl(output_path, normalized)
    print(f"\n出力完了: {output_path}")
    print(f"  総件数: {len(normalized)}")

    # サマリー表示
    account_counts = {}
    domain_counts = {}
    format_counts = {}
    for item in normalized:
        labels = item["labels"]
        acc = labels["target_account"]
        account_counts[acc] = account_counts.get(acc, 0) + 1
        for d in labels["topic_domain"]:
            domain_counts[d] = domain_counts.get(d, 0) + 1
        fmt = labels["style_format"]
        format_counts[fmt] = format_counts.get(fmt, 0) + 1

    print("\n--- サマリー ---")
    print("target_account:")
    for k, v in sorted(account_counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print("topic_domain:")
    for k, v in sorted(domain_counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print("style_format:")
    for k, v in sorted(format_counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
