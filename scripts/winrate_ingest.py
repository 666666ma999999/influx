#!/usr/bin/env python3
"""インフルエンサー勝率アルゴ P1: 抽出結果の検証・取り込み。

docs/influencer-winrate-spec.md §5 F2 の後段を担う。Claude セッション内サブエージェント
（docs/prompts/influencer_signal_extraction_v2.md）が生成したシグナル抽出結果 JSON
（トップレベル配列）を検証し、ResearchStore 経由で signals.jsonl へ重複排除 append する。
スキーマ違反は明確なエラーで拒否し（該当レコードのみ・全体は継続処理）、正常な項目のみ
取り込む。signal_id は tweet_url + ticker から本スクリプトが採番する（サブエージェントの
出力には不要。ResearchStore.get_signal_id と同一のハッシュ規則を使うため計算し直しても
既存レコードと衝突しない）。

Usage:
    python3 scripts/winrate_ingest.py --input /path/to/extraction_result.json
    python3 scripts/winrate_ingest.py --input /path/to/extraction_result.json --dry-run
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extensions.tier1_collection.grok_discoverer.research_store import ResearchStore  # noqa: E402

RESEARCH_DIR = "output/research"

REQUIRED_FIELDS = [
    "tweet_url", "username", "posted_at", "ticker", "direction",
    "confidence", "matched_text", "reasoning", "extraction_model", "extracted_at",
]

# 日本株ティッカー（4桁の証券コード相当。英数字混在コード対応）: 例 "7203.T" "285A.T"
# collector/signal_extractor.py._resolve_ticker の r'^\d{4}\.T$' を英数字混在コードにも
# 対応させた版（既存 signals.jsonl の "285A.T" 等は数字のみパターンに一致しないため）。
TICKER_JP_RE = re.compile(r'^\d[0-9A-Z]{3}\.T$')
# 米国株/ETF/指数/暗号資産等の一般ティッカー（英大文字・数字・.^=-のみ、10文字以内）
TICKER_GENERIC_RE = re.compile(r'^[A-Z0-9.\^=\-]{1,10}$')

MATCHED_TEXT_MAXLEN = 200
REASONING_MAXLEN = 500


def _parse_iso8601(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_signal(raw) -> tuple:
    """1件のシグナル候補を検証し、正規化済みdictまたはエラー理由を返す。

    Returns:
        (normalized_signal_dict, None): 検証成功
        (None, エラーメッセージ): 検証失敗
    """
    if not isinstance(raw, dict):
        return None, "レコードがdict形式ではありません"

    missing = [f for f in REQUIRED_FIELDS if not str(raw.get(f, "")).strip()]
    if missing:
        return None, f"必須フィールド欠落: {missing}"

    tweet_url = str(raw["tweet_url"]).strip()
    if not tweet_url.startswith("http"):
        return None, f"tweet_url がURL形式ではありません: {tweet_url!r}"

    ticker = str(raw["ticker"]).strip().upper()
    if not (TICKER_JP_RE.match(ticker) or TICKER_GENERIC_RE.match(ticker)):
        return None, f"ticker が既知の形式(日本株4桁.T または英数字ティッカー)と一致しません: {ticker!r}"

    direction = str(raw["direction"]).strip().upper()
    if direction not in ("LONG", "SHORT"):
        return None, f"direction はLONG/SHORTのみ有効です: {direction!r}"

    try:
        confidence = float(raw["confidence"])
    except (TypeError, ValueError):
        return None, f"confidence が数値ではありません: {raw['confidence']!r}"
    if not (0.0 <= confidence <= 1.0):
        return None, f"confidence は0.0-1.0の範囲外です: {confidence}"

    posted_at = str(raw["posted_at"]).strip()
    if not _parse_iso8601(posted_at):
        return None, f"posted_at がISO8601形式ではありません: {posted_at!r}"

    extraction_model = str(raw["extraction_model"]).strip()
    extracted_at = str(raw["extracted_at"]).strip()
    if not _parse_iso8601(extracted_at):
        return None, f"extracted_at がISO8601形式ではありません: {extracted_at!r}"

    normalized = {
        "tweet_url": tweet_url,
        "username": str(raw["username"]).strip(),
        "display_name": str(raw.get("display_name", "")).strip(),
        "posted_at": posted_at,
        "is_contrarian": bool(raw.get("is_contrarian", False)),
        "ticker": ticker,
        "direction": direction,
        "confidence": confidence,
        "matched_text": str(raw["matched_text"]).strip()[:MATCHED_TEXT_MAXLEN],
        "reasoning": str(raw["reasoning"]).strip()[:REASONING_MAXLEN],
        "extraction_model": extraction_model,
        "extracted_at": extracted_at,
    }
    return normalized, None


def ingest(input_path: str, research_dir: str, dry_run: bool) -> dict:
    """抽出結果ファイルを読み込み、検証・取り込みを行う。"""
    with open(input_path, "r", encoding="utf-8") as f:
        raw_records = json.load(f)
    if not isinstance(raw_records, list):
        raise SystemExit(f"FATAL: 入力JSONはトップレベル配列である必要があります: {input_path}")

    store = ResearchStore(base_dir=research_dir)
    existing_ids_for_dry_run = None
    if dry_run:
        # dry-run時は実ストアを汚さずに重複判定だけ行う（読み取り専用でsignals.jsonlを走査）
        existing_ids_for_dry_run = {s.get("signal_id") for s in store.load_signals()}

    accepted = 0
    duplicated = 0
    rejected = []

    for i, raw in enumerate(raw_records):
        normalized, error = validate_signal(raw)
        if error:
            rejected.append({
                "index": i,
                "error": error,
                "raw_ticker": raw.get("ticker") if isinstance(raw, dict) else None,
                "raw_tweet_url": raw.get("tweet_url") if isinstance(raw, dict) else None,
            })
            continue

        signal_id = ResearchStore.get_signal_id(normalized["tweet_url"], normalized["ticker"])
        normalized["signal_id"] = signal_id

        if dry_run:
            if signal_id in existing_ids_for_dry_run:
                duplicated += 1
            else:
                accepted += 1
                existing_ids_for_dry_run.add(signal_id)  # 同一入力内の重複も検出する
            continue

        added = store.add_signal(normalized)
        if added:
            accepted += 1
        else:
            duplicated += 1

    return {
        "input_records": len(raw_records),
        "accepted": accepted,
        "duplicated": duplicated,
        "rejected_count": len(rejected),
        "rejected": rejected,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="抽出結果の検証・signals.jsonl取り込み（インフルエンサー勝率アルゴ P1）")
    parser.add_argument("--input", required=True, help="サブエージェント抽出結果JSON（トップレベル配列）のパス")
    parser.add_argument("--research-dir", default=RESEARCH_DIR)
    parser.add_argument("--dry-run", action="store_true", help="実際にはsignals.jsonlへ書き込まず検証結果のみ表示する")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        raise SystemExit(f"FATAL: 入力ファイルが見つかりません: {args.input}")

    result = ingest(args.input, args.research_dir, args.dry_run)

    print(f"=== 抽出結果 取り込み{'（DRY-RUN）' if result['dry_run'] else ''} ===")
    print(f"入力レコード数: {result['input_records']}")
    print(f"取り込み成功（新規）: {result['accepted']}")
    print(f"重複（既存signal_idのためスキップ）: {result['duplicated']}")
    print(f"拒否（スキーマ違反）: {result['rejected_count']}")
    for r in result["rejected"]:
        print(f"  - [index={r['index']}] {r['error']} (tweet_url={r['raw_tweet_url']!r})")

    return 0 if result["input_records"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
