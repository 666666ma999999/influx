#!/usr/bin/env python3
"""正規化済み投稿からAI 2独立パスでKPI仮説候補を収穫する。

output/research/masters_20260717/normalized/tweets_*.json（原文日本語復元済み・
scripts/normalize_master_posts.py の出力）を入力に、各投稿を Anthropic API で
2つの独立したプロンプト（別文脈）で判定する。

設計（ユーザー裁定済み）:
- パスA（タクソノミー先行型）とパスB（見落とし防止・高recall型）が別プロンプトで
  全件を独立に判定する。triageが一致した場合のみ確定採用し、不一致は人間裁定へ回す。
- 収穫は目的変数（+20%/20営業日の単一銘柄ロング）に写像できるかを問わない。
  写像可否は goal_mappable フラグで別途区別する。
- 原文の主張（claim_summary）とAIによる操作化案（hypothesis_sketch）を別フィールドに
  分離し、AIが仮説を発明する問題を防ぐ。claim_summary は原文の範囲を超えないこと。
- 原文が壊れている・欠落している（X syndication APIでの復元に失敗し元textへ
  フォールバックした）レコードは、勝手に解釈せず triage="context_pending" に倒す。

前段の安全圧縮（LLM不使用・除外ではなくクラスタ束ね）:
  1. tweet_id（url由来）が同一の重複 → 1件に束ねる（時間窓境界の重複収集等を吸収）
  2. 正規化後の完全一致テキスト重複（tweet_id違い） → 代表1件に束ねる
  3. 空本文 → 専用クラスタへ束ね、LLM判定はスキップし合成判定を割り当てる
束ねられた代表のみをAI判定にかけ、結果を全メンバーへコピーする。

冪等性: judgments.json は tweet_id をキーに全件を蓄積する。既存レコードで両パス
（--pass-a-only 時はパスAのみ）が完了済みのものは再判定をスキップする。--files/--limit
で対象を絞った実行でも、対象外の既存レコードは保持される（上書きされない）。

入力ファイルは read-only（一切変更しない）。出力は
output/research/masters_20260717/harvest/ 配下にのみ書く。

Python標準ライブラリのみで動作する（pip install不要・Docker不要）。ホストで直接実行する。

Usage:
    python3 scripts/harvest_master_posts.py --dry-run
    python3 scripts/harvest_master_posts.py --limit 6 \\
        --files "output/research/masters_20260717/normalized/tweets_tomoyaasakura__2026-04-17_2026-07-18.json"
    python3 scripts/harvest_master_posts.py --pass-a-only --limit 10
    python3 scripts/harvest_master_posts.py
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from normalize_master_posts import extract_tweet_id  # noqa: E402  既存のtweet_id抽出ロジックを再利用（Canonical Module原則）

PROJECT_ROOT = SCRIPT_DIR.parent
MASTERS_DIR = PROJECT_ROOT / "output" / "research" / "masters_20260717"
NORMALIZED_DIR = MASTERS_DIR / "normalized"
DEFAULT_INPUT_GLOB = str(NORMALIZED_DIR / "tweets_*.json")
HARVEST_DIR = MASTERS_DIR / "harvest"

JUDGMENTS_PATH = HARVEST_DIR / "judgments.json"
DISAGREEMENTS_PATH = HARVEST_DIR / "disagreements.json"
AUDIT_SAMPLE_PATH = HARVEST_DIR / "audit_sample.json"
CANDIDATES_PATH = HARVEST_DIR / "candidates.json"
COMPRESSED_CLUSTERS_PATH = HARVEST_DIR / "compressed_clusters.json"
STATS_PATH = HARVEST_DIR / "_harvest_stats.json"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# ~/.zshrc の export 行からのキー取得（scripts/research_weekly_launchd.sh の
# load_key_from_zshrc() と同一パターン: grep した export 行を eval で解決するため、
# 直書き・コマンド置換のどちらの形式にも対応する。launchd/直接実行では direnv が
# 効かないため必要。キーの値は絶対にログ・stdout・保存ファイルに出力しない）


def resolve_api_key() -> Optional[str]:
    """ANTHROPIC_API_KEY を環境変数優先、無ければ ~/.zshrc の export 行から取得する。

    Returns:
        APIキー文字列。どちらからも取得できない場合は None。
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    import subprocess

    script = (
        'line=$(grep -m1 "^export ANTHROPIC_API_KEY=" "$HOME/.zshrc" 2>/dev/null || true); '
        '[ -n "$line" ] && eval "$line" && printf "%s" "$ANTHROPIC_API_KEY"'
    )
    try:
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 2048
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 2.0
TIMEOUT_SECONDS = 60
RECORD_SLEEP_SECONDS = 0.3
AUDIT_SAMPLE_SIZE = 20
AUDIT_SAMPLE_SEED = 42

# 概算コスト計算用の単価（USD/1Mトークン）。Claude Sonnetファミリーの実勢価格を参考にした
# 概算値であり、正確な最新価格ではない。_harvest_stats.json の値は目安として扱うこと。
COST_PER_MTOK_INPUT_USD = 3.0
COST_PER_MTOK_OUTPUT_USD = 15.0

LABELS = [
    "売買シグナル",
    "価格パターン",
    "需給・ポジショニング",
    "ファンダメンタルズ",
    "決算期待差",
    "マクロ・クロスアセット",
    "セクター連動・ローテーション",
    "センチメント・バブル警戒",
    "イベント",
    "リスク管理・exit",
    "検証不能",
    "文脈欠落",
    "非投資投稿",
]
TRIAGE_VALUES = ("hypothesis_candidate", "position_disclosure", "context_pending", "non_candidate")
CONFIDENCE_VALUES = ("high", "medium", "low")

LABELS_TEXT = "、".join(LABELS)

OUTPUT_SCHEMA_TEXT = """出力は必ず以下のキーを持つ1つのJSONオブジェクトのみを返すこと（説明文・マークダウンのコードフェンス禁止）:
{
  "triage": "hypothesis_candidate|position_disclosure|context_pending|non_candidate",
  "labels": [該当するラベル文字列の配列。上記13種から0個以上],
  "claim_summary": "投稿者が実際に述べている主張の要約（原文の範囲を超えないこと。推測や補完をしない）",
  "hypothesis_sketch": "検証可能な形に落とし込んだ操作化案（原文にない部分を含めてよい）。該当なしはnull",
  "goal_mappable": true または false（単一銘柄ロング・保有20営業日・目標+20%の枠組みに変換できるか）,
  "confidence": "high|medium|low"
}"""

CONTEXT_RULE_TEXT = """【文脈欠落レコードの扱い】
入力の context_flags.used_fallback_text が true の場合、本文はX syndication APIによる原文復元に
失敗し、自動翻訳・途中切断の可能性がある元テキストにフォールバックしたものである。この場合は
本文から勝手に主張を補って解釈しないこと。明らかに投資と無関係と判断できる場合を除き、triage は
"context_pending" とし、labels に "文脈欠落" を含め、hypothesis_sketch は null とすること。"""


def build_pass_a_system_prompt() -> str:
    """パスA: タクソノミー先行型システムプロンプトを構築する（ラベル定義を先に提示）。"""
    return f"""あなたは株式投資インフルエンサーの投稿からKPI仮説の種を収穫する分類者です。
まず以下のラベル定義（分類法）を理解してから、各投稿を判定してください。

【ラベル定義（13種、複数付与可）】
{LABELS_TEXT}

【triage判定（上記ラベルを踏まえて判定する）】
- hypothesis_candidate: 検証可能な仮説の種になりうる主張を含む
- position_disclosure: 自身の売買・保有の開示のみで、一般化可能な仮説の主張を伴わない
- context_pending: 本文が壊れている・欠落していて判定できない
- non_candidate: 上記いずれにも該当しない（雑談・非投資・仮説性のない一般論等）

{CONTEXT_RULE_TEXT}

{OUTPUT_SCHEMA_TEXT}

重要: hypothesis_sketch はあなたによる操作化案であり、claim_summary（原文の主張）とは
明確に分離すること。原文にない主張を claim_summary に書き込まない。"""


def build_pass_b_system_prompt() -> str:
    """パスB: 見落とし防止型システムプロンプトを構築する（recall優先の原則を先に提示）。"""
    return f"""あなたは株式投資インフルエンサーの投稿からKPI仮説の見落としを防ぐレビュアーです。

【最優先原則】これはKPI仮説の種になり得るか？ 迷ったら候補（hypothesis_candidate）に倒すこと。
仮説の芽を見逃す方が、後で人間が却下するコストより高くつく。ただし原文にない主張を
勝手に作り出すのは禁止（claim_summary は原文の範囲内、hypothesis_sketch のみ操作化案）。

{CONTEXT_RULE_TEXT}

判定は以下の4区分から選ぶ:
- hypothesis_candidate / position_disclosure / context_pending / non_candidate

ラベルは以下13種（複数付与可）から選ぶ:
{LABELS_TEXT}

{OUTPUT_SCHEMA_TEXT}
"""


def normalize_for_hash(text: str) -> str:
    """NFKC正規化+空白圧縮したテキストを返す（完全一致重複判定用）。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def select_body_text(record: dict[str, Any]) -> tuple[str, bool]:
    """本文を選択する。

    norm_status=='ok' かつ norm_text があればそれを採用する。それ以外は元 text
    フィールドへフォールバックし、context_flag=True を立てる（norm_text があれば
    それを優先的にフォールバック先とする）。

    Returns:
        (body_text, context_flag) のタプル。
    """
    norm_status = record.get("norm_status")
    norm_text = record.get("norm_text")
    if norm_status == "ok" and norm_text:
        return norm_text, False
    fallback = record.get("text") or norm_text or ""
    return fallback, True


def load_input_records(files_glob: str, limit: Optional[int]) -> tuple[list[dict[str, Any]], list[Path]]:
    """入力ファイル群からレコードを読み込む。_で始まる台帳ファイルは除外する。

    limit 指定時は各ファイル先頭 limit 件のみを対象にする（テスト用）。
    """
    input_paths = sorted(
        p for p in (Path(x) for x in glob.glob(files_glob)) if not p.name.startswith("_")
    )
    all_records: list[dict[str, Any]] = []
    for path in input_paths:
        with open(path, encoding="utf-8") as f:
            file_records: list[dict[str, Any]] = json.load(f)
        if limit is not None:
            file_records = file_records[:limit]
        for rec in file_records:
            rec = dict(rec)
            rec["_source_file"] = path.name
            all_records.append(rec)
    return all_records, input_paths


def build_items(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各レコードに tweet_id・本文・context_flag を付与した作業用アイテムを作る。"""
    items = []
    for idx, rec in enumerate(records):
        tweet_id = extract_tweet_id(rec.get("url", "") or "")
        if not tweet_id:
            tweet_id = f"_no_id_{rec.get('_source_file', 'unknown')}_{idx}"
        body_text, context_flag = select_body_text(rec)
        items.append({"tweet_id": tweet_id, "record": rec, "body_text": body_text, "context_flag": context_flag})
    return items


def dedupe_by_tweet_id(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """同一tweet_id（同一URL、時間窓境界の重複収集等）を1件へ束ねる。

    norm_status=='ok'（context_flag=False）のものを優先して代表に選ぶ。

    Returns:
        (canonical_items, url_dup_clusters) — canonical_items は tweet_id ごとに1件。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        tid = item["tweet_id"]
        if tid not in groups:
            groups[tid] = []
            order.append(tid)
        groups[tid].append(item)

    canonical_items = []
    clusters = []
    for tid in order:
        group = groups[tid]
        if len(group) == 1:
            canonical_items.append(group[0])
            continue
        best = next((it for it in group if not it["context_flag"]), group[0])
        canonical_items.append(best)
        clusters.append(
            {
                "representative_tweet_id": tid,
                "kind": "dup_url",
                "member_count": len(group),
                "member_source_files": [it["record"].get("_source_file") for it in group],
            }
        )
    return canonical_items, clusters


def dedupe_by_text(
    canonical_items: list[dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]], set[str]]:
    """正規化後の完全一致テキスト重複（tweet_id違い）を代表へ束ね、空本文を専用クラスタへ束ねる。

    Returns:
        (representative_map, clusters, empty_ids)
    """
    representative_map: dict[str, str] = {}
    hash_to_rep: dict[str, str] = {}
    dup_text_members: dict[str, list[str]] = {}
    empty_ids: list[str] = []

    for item in canonical_items:
        tid = item["tweet_id"]
        body = item["body_text"].strip()
        if not body:
            representative_map[tid] = tid
            empty_ids.append(tid)
            continue
        key = hashlib.sha256(normalize_for_hash(body).encode("utf-8")).hexdigest()
        if key in hash_to_rep:
            rep = hash_to_rep[key]
            representative_map[tid] = rep
            dup_text_members.setdefault(rep, []).append(tid)
        else:
            hash_to_rep[key] = tid
            representative_map[tid] = tid

    clusters = [
        {"representative_tweet_id": rep, "kind": "dup_text", "member_tweet_ids": members}
        for rep, members in dup_text_members.items()
    ]
    if empty_ids:
        clusters.append({"representative_tweet_id": None, "kind": "empty_body", "member_tweet_ids": empty_ids})

    return representative_map, clusters, set(empty_ids)


def compress_records(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str], set[str], list[dict[str, Any]]]:
    """安全圧縮パイプライン全体（LLM不使用）: tweet_id重複 → テキスト完全一致/空本文の順に束ねる。

    Returns:
        (canonical_items, representative_map, empty_ids, clusters)
    """
    canonical_items, url_clusters = dedupe_by_tweet_id(items)
    representative_map, text_clusters, empty_ids = dedupe_by_text(canonical_items)
    return canonical_items, representative_map, empty_ids, url_clusters + text_clusters


def synthetic_empty_judgment() -> dict[str, Any]:
    """空本文レコード用の合成判定（LLM不使用）。"""
    return {
        "triage": "non_candidate",
        "labels": ["文脈欠落"],
        "claim_summary": "",
        "hypothesis_sketch": None,
        "goal_mappable": False,
        "confidence": "high",
        "synthetic": True,
        "synthetic_reason": "empty_body",
    }


def build_user_payload(item: dict[str, Any]) -> str:
    """1レコード分のユーザーメッセージ（JSON文字列）を組み立てる。"""
    payload = {
        "username": item["record"].get("username"),
        "text": item["body_text"],
        "posted_at": item["record"].get("posted_at"),
        "like_count": item["record"].get("like_count"),
        "context_flags": {
            "norm_status": item["record"].get("norm_status"),
            "used_fallback_text": item["context_flag"],
            "text_was_translated": item["record"].get("text_was_translated"),
            "text_was_truncated": item["record"].get("text_was_truncated"),
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def call_claude_api(api_key: str, model: str, system_prompt: str, user_content: str) -> dict[str, Any]:
    """Claude Messages APIを1回呼び出す（429/5xxは指数バックオフで最大MAX_RETRIES回まで再試行）。"""
    request_body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    data = json.dumps(request_body).encode("utf-8")
    last_error: Optional[str] = None

    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_error = f"http_{e.code}:{body[:200]}"
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE_SECONDS ** attempt)
                continue
            raise RuntimeError(f"Claude API呼び出し失敗: {last_error}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_error = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE_SECONDS ** attempt)
                continue
            raise RuntimeError(f"Claude APIネットワークエラー: {last_error}")

    raise RuntimeError(f"Claude API呼び出し失敗（最大リトライ到達）: {last_error}")


def extract_text_and_usage(response: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """APIレスポンスからテキストとトークン使用量を抽出する。"""
    content_blocks = response.get("content", [])
    text = ""
    for block in content_blocks:
        if block.get("type") == "text":
            text = block.get("text", "")
            break
    usage = response.get("usage") or {}
    tokens = {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }
    return text, tokens


def parse_judgment_text(text: str) -> Optional[dict[str, Any]]:
    """JSON応答テキストを判定スキーマにパースする。不正な場合はNoneを返す。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        cleaned = "\n".join(lines).strip()

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    triage = obj.get("triage")
    if triage not in TRIAGE_VALUES:
        return None

    labels = obj.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    labels = [label for label in labels if isinstance(label, str) and label in LABELS]

    confidence = obj.get("confidence")
    if confidence not in CONFIDENCE_VALUES:
        confidence = "medium"

    hypothesis_sketch = obj.get("hypothesis_sketch")
    if hypothesis_sketch is not None and not isinstance(hypothesis_sketch, str):
        hypothesis_sketch = None

    raw_goal_mappable = obj.get("goal_mappable", False)
    if isinstance(raw_goal_mappable, bool):
        goal_mappable = raw_goal_mappable
    elif isinstance(raw_goal_mappable, str):
        goal_mappable = raw_goal_mappable.strip().lower() == "true"
    else:
        goal_mappable = bool(raw_goal_mappable)

    return {
        "triage": triage,
        "labels": labels,
        "claim_summary": str(obj.get("claim_summary", ""))[:1000],
        "hypothesis_sketch": hypothesis_sketch,
        "goal_mappable": goal_mappable,
        "confidence": confidence,
    }


def classify_one_pass(
    api_key: str, model: str, system_prompt: str, user_content: str, usage_acc: dict[str, int]
) -> dict[str, Any]:
    """1パス分の判定を実行する。JSONパース失敗時は1回だけ同一内容+注意書きで再試行する。"""
    retry_notice = (
        "\n\n(前回の応答はJSONとして不正でした。説明文やコードフェンスを付けず、"
        "有効なJSONオブジェクトのみを返してください。)"
    )
    for attempt in range(2):
        content = user_content if attempt == 0 else user_content + retry_notice
        response = call_claude_api(api_key, model, system_prompt, content)
        text, tokens = extract_text_and_usage(response)
        usage_acc["input_tokens"] += tokens["input_tokens"]
        usage_acc["output_tokens"] += tokens["output_tokens"]
        usage_acc["api_calls"] += 1
        parsed = parse_judgment_text(text)
        if parsed is not None:
            return parsed

    return {
        "triage": "context_pending",
        "labels": ["文脈欠落"],
        "claim_summary": "",
        "hypothesis_sketch": None,
        "goal_mappable": False,
        "confidence": "low",
        "parse_error": True,
    }


def classify_representative(
    item: dict[str, Any],
    api_key: str,
    model: str,
    pass_a_only: bool,
    usage_acc: dict[str, int],
    pass_a_system: str,
    pass_b_system: str,
) -> dict[str, Any]:
    """代表レコード1件をパスA（+パスB）で判定し、judgments.json 1レコード分を組み立てる。"""
    user_content = build_user_payload(item)
    pass_a = classify_one_pass(api_key, model, pass_a_system, user_content, usage_acc)

    if pass_a_only:
        pass_b = None
        agreement = None
        final_triage = None
    else:
        pass_b = classify_one_pass(api_key, model, pass_b_system, user_content, usage_acc)
        agreement = pass_a["triage"] == pass_b["triage"]
        final_triage = pass_a["triage"] if agreement else None

    return {
        "tweet_id": item["tweet_id"],
        "url": item["record"].get("url"),
        "username": item["record"].get("username"),
        "norm_text": item["body_text"],
        "context_flag": item["context_flag"],
        "source_file": item["record"].get("_source_file"),
        "passA": pass_a,
        "passB": pass_b,
        "agreement": agreement,
        "final_triage": final_triage,
        "compressed_from": None,
    }


def is_already_done(entry: Optional[dict[str, Any]], pass_a_only: bool) -> bool:
    """既存judgmentが今回の実行モードに対して完了済みかを判定する（冪等性チェック）。"""
    if entry is None or entry.get("passA") is None:
        return False
    if pass_a_only:
        return True
    return entry.get("passB") is not None


def load_existing_judgments() -> dict[str, dict[str, Any]]:
    """既存のjudgments.jsonをtweet_idキーの辞書として読み込む。"""
    if not JUDGMENTS_PATH.exists():
        return {}
    try:
        data = json.loads(JUDGMENTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {rec["tweet_id"]: rec for rec in data if rec.get("tweet_id")}


def build_stats(
    final_records: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    usage_acc: dict[str, int],
    total_raw: int,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """_harvest_stats.json の内容を組み立てる。"""
    triage_dist: dict[str, int] = {}
    label_dist: dict[str, int] = {}
    agreement_count = 0
    agreement_total = 0

    for rec in final_records:
        if rec["agreement"] is not None:
            agreement_total += 1
            if rec["agreement"]:
                agreement_count += 1
        key = rec["final_triage"] or "disagreement"
        triage_dist[key] = triage_dist.get(key, 0) + 1
        for pass_key in ("passA", "passB"):
            pass_result = rec.get(pass_key)
            if not pass_result:
                continue
            for label in pass_result.get("labels", []):
                label_dist[label] = label_dist.get(label, 0) + 1

    estimated_cost = (
        usage_acc["input_tokens"] / 1_000_000 * COST_PER_MTOK_INPUT_USD
        + usage_acc["output_tokens"] / 1_000_000 * COST_PER_MTOK_OUTPUT_USD
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "total_raw_records_this_run": total_raw,
        "total_output_records": len(final_records),
        "compressed_cluster_count": len(clusters),
        "agreement_rate": (agreement_count / agreement_total) if agreement_total else None,
        "agreement_checked": agreement_total,
        "triage_distribution": triage_dist,
        "label_distribution": label_dist,
        "api_call_count": usage_acc["api_calls"],
        "input_tokens": usage_acc["input_tokens"],
        "output_tokens": usage_acc["output_tokens"],
        "estimated_cost_usd": round(estimated_cost, 4),
        "cost_note": "概算値。単価はスクリプト冒頭のCOST_PER_MTOK_*定数を参照（要最新価格確認）",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="正規化済み投稿からAI 2独立パスでKPI仮説候補を収穫する")
    parser.add_argument("--files", default=DEFAULT_INPUT_GLOB, help="入力ファイルのglobパターン")
    parser.add_argument("--limit", type=int, default=None, help="テスト用: 各ファイル先頭N件のみ対象にする")
    parser.add_argument("--dry-run", action="store_true", help="判定せず対象件数と見積もりのみ表示する")
    parser.add_argument("--pass-a-only", action="store_true", help="デバッグ用: パスAのみ実行しパスBをスキップする")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="使用するモデル名")
    args = parser.parse_args()

    all_records, input_paths = load_input_records(args.files, args.limit)
    if not input_paths:
        print(f"対象ファイルなし: {args.files}", file=sys.stderr)
        return 1

    items = build_items(all_records)
    canonical_items, representative_map, empty_ids, clusters = compress_records(items)
    items_by_id = {item["tweet_id"]: item for item in canonical_items}
    representatives = [
        item for item in canonical_items
        if representative_map[item["tweet_id"]] == item["tweet_id"] and item["tweet_id"] not in empty_ids
    ]

    existing_judgments = load_existing_judgments()

    if args.dry_run:
        need_classification = [it for it in representatives if not is_already_done(existing_judgments.get(it["tweet_id"]), args.pass_a_only)]
        calls_per_record = 1 if args.pass_a_only else 2
        print(f"[DRY-RUN] 入力ファイル数={len(input_paths)} 生レコード数={len(all_records)}")
        print(f"[DRY-RUN] tweet_id重複除去後={len(canonical_items)}件（URL重複除去={len(all_records) - len(canonical_items)}件）")
        print(f"[DRY-RUN] 空本文={len(empty_ids)}件（LLM不使用） 束ね後の判定対象代表={len(representatives)}件")
        print(f"[DRY-RUN] 既存judgmentsで完了済み={len(representatives) - len(need_classification)}件 新規判定対象={len(need_classification)}件")
        print(f"[DRY-RUN] 見積もりAPI呼び出し数={len(need_classification) * calls_per_record}回（パース失敗時の追加リトライは含まず）")
        return 0

    api_key = resolve_api_key()
    if not api_key:
        print(
            "エラー: ANTHROPIC_API_KEYが環境変数にも ~/.zshrc にも見つかりません。実走しません。",
            file=sys.stderr,
        )
        return 1

    usage_acc = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}
    output_map: dict[str, dict[str, Any]] = dict(existing_judgments)

    # 空本文の代表はLLM不使用で合成判定を割り当てる（既存があれば維持）
    for tid in empty_ids:
        if is_already_done(output_map.get(tid), args.pass_a_only):
            continue
        item = items_by_id[tid]
        synth = synthetic_empty_judgment()
        output_map[tid] = {
            "tweet_id": tid,
            "url": item["record"].get("url"),
            "username": item["record"].get("username"),
            "norm_text": item["body_text"],
            "context_flag": item["context_flag"],
            "source_file": item["record"].get("_source_file"),
            "passA": synth,
            "passB": dict(synth),
            "agreement": True,
            "final_triage": synth["triage"],
            "compressed_from": None,
        }

    pass_a_system = build_pass_a_system_prompt()
    pass_b_system = build_pass_b_system_prompt()
    processed_count = 0

    for item in representatives:
        tid = item["tweet_id"]
        if is_already_done(output_map.get(tid), args.pass_a_only):
            continue
        entry = classify_representative(
            item, api_key, args.model, args.pass_a_only, usage_acc, pass_a_system, pass_b_system
        )
        output_map[tid] = entry
        processed_count += 1
        time.sleep(RECORD_SLEEP_SECONDS)

    # 束ねられたメンバー（非代表）へ代表の判定をコピーする
    for item in canonical_items:
        tid = item["tweet_id"]
        rep_tid = representative_map[tid]
        if tid == rep_tid:
            continue
        rep_entry = output_map.get(rep_tid)
        if rep_entry is None:
            continue
        output_map[tid] = {
            "tweet_id": tid,
            "url": item["record"].get("url"),
            "username": item["record"].get("username"),
            "norm_text": item["body_text"],
            "context_flag": item["context_flag"],
            "source_file": item["record"].get("_source_file"),
            "passA": rep_entry["passA"],
            "passB": rep_entry["passB"],
            "agreement": rep_entry["agreement"],
            "final_triage": rep_entry["final_triage"],
            "compressed_from": rep_tid,
        }

    final_records = list(output_map.values())

    HARVEST_DIR.mkdir(parents=True, exist_ok=True)
    JUDGMENTS_PATH.write_text(json.dumps(final_records, ensure_ascii=False, indent=2), encoding="utf-8")

    disagreements = [r for r in final_records if r["agreement"] is False]
    DISAGREEMENTS_PATH.write_text(json.dumps(disagreements, ensure_ascii=False, indent=2), encoding="utf-8")

    candidates = [r for r in final_records if r["final_triage"] == "hypothesis_candidate"]
    CANDIDATES_PATH.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")

    both_non_candidate = [
        r
        for r in final_records
        if r["passA"]
        and r["passB"]
        and r["passA"].get("triage") == "non_candidate"
        and r["passB"].get("triage") == "non_candidate"
    ]
    rng = random.Random(AUDIT_SAMPLE_SEED)
    audit_sample = rng.sample(both_non_candidate, min(AUDIT_SAMPLE_SIZE, len(both_non_candidate)))
    AUDIT_SAMPLE_PATH.write_text(json.dumps(audit_sample, ensure_ascii=False, indent=2), encoding="utf-8")

    COMPRESSED_CLUSTERS_PATH.write_text(json.dumps(clusters, ensure_ascii=False, indent=2), encoding="utf-8")

    scope = {"files_glob": args.files, "limit": args.limit, "pass_a_only": args.pass_a_only, "model": args.model}
    stats = build_stats(final_records, clusters, usage_acc, len(all_records), scope)
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"今回新規判定={processed_count}件 API呼び出し={usage_acc['api_calls']}回")
    print(f"累積出力レコード数={len(final_records)}件 一致率={stats['agreement_rate']}")
    print(f"candidates={len(candidates)}件 disagreements={len(disagreements)}件 audit_sample={len(audit_sample)}件")
    print(f"概算コスト=${stats['estimated_cost_usd']}")
    print(f"出力先: {HARVEST_DIR}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
