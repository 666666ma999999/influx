"""スタイル対応プロンプトビルダー。

アカウント別スタイルガイド + few-shot参考例を組み込んだ
LLMプロンプトを構築する。
"""
import json
import os
import random
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _read_jsonl(path: Path) -> list:
    """JSONLファイルを読み込む共通ヘルパー。"""
    if not path.exists():
        return []
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


def load_style_guide(account_id: str) -> dict:
    """アカウント別スタイルガイドを読み込む。"""
    path = PROJECT_ROOT / "data" / "writing_style" / "style_guides" / f"{account_id}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_normalized_bookmarks() -> list:
    """正規化済みブックマークを読み込む。"""
    return _read_jsonl(PROJECT_ROOT / "data" / "writing_style" / "bookmarks" / "normalized.jsonl")


def load_learned_rewrites(account_id: str) -> list:
    """学習済みリライトデータを読み込む。"""
    return _read_jsonl(PROJECT_ROOT / "data" / "writing_style" / "rewrites" / f"{account_id}_learned.jsonl")


def select_few_shot_examples(
    bookmarks: list,
    target_account: str,
    style_format: str = None,
    topic_domain: str = None,
    max_examples: int = 3,
    instruction: str = None,
) -> list:
    """条件に合うfew-shot参考例を選定する。

    優先順位:
    1. target_account + style_format + topic_domain が一致
    2. target_account + style_format が一致
    3. target_account が一致
    4. ランダム

    instruction指定時、過去のリライト成功例で指示が類似するものを優先する。
    """
    scored = []
    for bm in bookmarks:
        if not bm.get("text"):
            continue
        labels = bm.get("labels", {})
        score = 0
        if labels.get("target_account") == target_account:
            score += 3
        if style_format and labels.get("style_format") == style_format:
            score += 2
        if topic_domain and topic_domain in labels.get("topic_domain", []):
            score += 1
        # リライト学習データの指示マッチングボーナス
        if instruction and labels.get("instruction_trigger"):
            if instruction.lower() in labels["instruction_trigger"].lower() or \
               labels["instruction_trigger"].lower() in instruction.lower():
                score += 4
        if labels.get("source") == "rewrite":
            score += 2
        scored.append((score, bm))

    scored.sort(key=lambda x: (-x[0], random.random()))
    return [bm for _, bm in scored[:max_examples]]


def _format_style_guide(account_id: str, guide: dict) -> str:
    """スタイルガイドをプロンプト用テキストに整形する。"""
    text = "【スタイルガイド: @" + account_id + "】\n"
    if guide.get("persona"):
        text += f"ペルソナ: {guide['persona']}\n"
    if guide.get("tone_rules"):
        text += "トーン: " + ", ".join(guide["tone_rules"]) + "\n"
    if guide.get("preferred_formats"):
        text += "好みの形式: " + ", ".join(guide["preferred_formats"]) + "\n"
    if guide.get("hook_patterns"):
        text += "フックパターン: " + ", ".join(guide["hook_patterns"]) + "\n"
    if guide.get("banned_patterns"):
        text += "禁止: " + ", ".join(guide["banned_patterns"]) + "\n"
    return text


def _format_few_shot_examples(examples: list) -> str:
    """few-shot参考例をプロンプト用テキストに整形する。"""
    text = "【参考文体（丸写し禁止、スタイルのみ参考にすること）】\n"
    for i, ex in enumerate(examples, 1):
        text += f"\n例{i} (@{ex.get('author', '?')}):\n{ex['text'][:200]}\n"
        notes = ex.get("style_notes", [])
        if notes:
            text += f"→ 特徴: {', '.join(notes[:3])}\n"
    return text


def build_style_aware_prompt(
    task: str,
    source_data: str,
    target_account: str,
    target_style: str = None,
    topic: str = None,
    char_limit: int = 280,
) -> str:
    """スタイル対応LLMプロンプトを構築する。

    Args:
        task: 生成タスク名 (market_summary, hot_picks等)
        source_data: 入力データ（ツイート群等）
        target_account: 投稿先アカウントID
        target_style: 目標スタイル (listicle, explainer等)
        topic: トピックドメイン
        char_limit: 文字数制限

    Returns:
        完成したプロンプト文字列
    """
    guide = load_style_guide(target_account)
    bookmarks = load_normalized_bookmarks()
    examples = select_few_shot_examples(bookmarks, target_account, target_style, topic)

    sections = []

    sections.append(
        f"【制約】\n"
        f"- {char_limit}文字以内\n"
        f"- ハッシュタグは本文に含めない（後付け）\n"
        f"- 事実に基づく内容のみ"
    )

    if guide:
        sections.append(_format_style_guide(target_account, guide))

    task_instructions = {
        "market_summary": "入力ツイート群から市況トレンドを要約し、X投稿を1件作成してください。",
        "hot_picks": "入力ツイート群から注目銘柄/推奨資産をピックアップし、X投稿を1件作成してください。",
        "trade_activity": "入力ツイート群から売買動向をまとめ、X投稿を1件作成してください。",
        "earnings_flash": "入力ツイート群から決算関連ニュースをまとめ、X投稿を1件作成してください。",
        "manual": "以下のデータを元に、X投稿を1件作成してください。",
    }
    sections.append("【タスク】\n" + task_instructions.get(task, task_instructions["manual"]))

    if examples:
        sections.append(_format_few_shot_examples(examples))

    sections.append(f"【入力データ】\n{source_data}")
    sections.append("【出力】\nX投稿テキスト1件のみ。説明や前置きは不要。")

    return "\n\n".join(sections)


def build_rewrite_prompt(
    original_body: str,
    instruction: str,
    account_id: str = "",
    char_limit: int = 280,
) -> str:
    """ドラフト本文の修正指示に基づくリライトプロンプトを構築する。

    Args:
        original_body: 現在の投稿本文
        instruction: ユーザーの修正指示 (例: "もっとカジュアルに")
        account_id: 投稿アカウントID (スタイルガイド参照用)
        char_limit: 文字数上限

    Returns:
        LLMに渡すプロンプト文字列
    """
    sections = [
        f"【制約】\n"
        f"- {char_limit}文字以内\n"
        f"- ハッシュタグは本文に含めない（後付け）\n"
        f"- 事実を変えない（数字・固有名詞は原文のまま）"
    ]

    if account_id:
        guide = load_style_guide(account_id)
        if guide:
            sections.append(_format_style_guide(account_id, guide))

        bookmarks = load_normalized_bookmarks() + load_learned_rewrites(account_id)
        examples = select_few_shot_examples(
            bookmarks, account_id, max_examples=2, instruction=instruction,
        )
        if examples:
            sections.append(_format_few_shot_examples(examples))

    sections.append(f"【元の投稿】\n{original_body}")
    sections.append(f"【修正指示】\n{instruction}")
    sections.append("【出力】\n修正後の投稿テキストのみ。説明や前置きは不要。")

    return "\n\n".join(sections)
