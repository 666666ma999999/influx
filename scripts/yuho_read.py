#!/usr/bin/env python3
"""有報の節本文（sections.json）＋決算説明会資料（ir/*.txt）を Claude に読ませ、品目×KPI の候補行を取る（P-INF-34 工程③）。

入力: `data/edinet/yuho/<code4>/<docID>.sections.json`（`scripts/yuho_fetch.py` の出力）
      `data/edinet/yuho/<code4>/ir/FY<yyyy>*.txt`（任意・`scripts/ir_pdf_text.py` の出力・period_start の年度に合う物だけ）
出力: `output/yuho/<code4>/<docID>.read.json`
      = {doc_id, model, usage:{input_tokens, output_tokens}, prompt_chars, rows:[...], error}

Claude API は `collector/llm_classifier.py` と同じ urllib 直叩き（SDK は Docker に無い）。
`ANTHROPIC_API_KEY` は docker-compose が渡す。

使い方:
    python scripts/yuho_read.py --code 2802 --dry-run              # prompt_chars と概算トークンだけ出す（API を呼ばない）
    python scripts/yuho_read.py --code 2802 --model claude-sonnet-5  # 実走（1 docID につき 1 回・既存 read.json はスキップ）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_DATA_DIR = Path("data/edinet/yuho")
DEFAULT_OUT_DIR = Path("output/yuho")
IR_SECTION_KEY = "ir_presentation"

ROW_KEYS: List[str] = [
    "segment", "item", "kpi_name", "kpi_value", "kpi_unit", "kpi_prev_value",
    "share_scope", "label_hint", "quote", "quote_section", "expectation",
]

SECTION_TITLES: Dict[str, str] = {
    "business_policy": "経営方針、経営環境及び対処すべき課題等",
    "mdna": "経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析",
    "risks": "事業等のリスク",
    "segment_notes": "セグメント情報",
    "business_results": "主要な経営指標等の推移",
    "business_description": "事業の内容",
    "production_sales": "生産、受注及び販売の実績",
}


def _sanitize(text: str) -> str:
    """ログ・エラー文から API キーを伏せる。"""
    return re.sub(r"sk-ant-[a-zA-Z0-9\-_]+", "sk-ant-***REDACTED***", text)


def fy_label(period_start: Optional[str], period_end: Optional[str]) -> Optional[int]:
    """説明会資料のファイル名 FY<yyyy> に合わせる年度を返す（期首の年。期首が無ければ期末から推定）。

    Args:
        period_start: 期首 "YYYY-MM-DD"
        period_end: 期末 "YYYY-MM-DD"

    Returns:
        年度（例: 2025-04-01〜2026-03-31 → 2025）。どちらも無ければ None
    """
    if period_start and len(period_start) >= 4 and period_start[:4].isdigit():
        return int(period_start[:4])
    if period_end and len(period_end) >= 7 and period_end[:4].isdigit():
        year = int(period_end[:4])
        month = int(period_end[5:7]) if period_end[5:7].isdigit() else 12
        return year - 1 if month <= 6 else year
    return None


def find_ir_text(code4: str, sections_json: dict, data_dir: Path = DEFAULT_DATA_DIR) -> Optional[Path]:
    """period に合う年度ラベルの説明会資料テキスト（ir/FY<yyyy>*.txt）を 1 本探す。

    Args:
        code4: 証券コード 4 桁
        sections_json: sections.json の中身（period_start / period_end を使う）
        data_dir: `data/edinet/yuho`

    Returns:
        見つかった .txt のパス。無ければ None
    """
    fy = fy_label(sections_json.get("period_start"), sections_json.get("period_end"))
    if fy is None:
        return None
    ir_dir = data_dir / code4 / "ir"
    if not ir_dir.is_dir():
        return None
    hits = sorted(ir_dir.glob(f"FY{fy}*.txt"))
    return hits[0] if hits else None


def _segment_labels(sections_json: dict) -> List[str]:
    segs = (sections_json.get("facts") or {}).get("segments") or {}
    labels: List[str] = []
    for member, seg in segs.items():
        label = (seg or {}).get("label") or member
        if label not in labels:
            labels.append(label)
    return labels


def build_prompt(sections_json: dict, ir_text: Optional[str], max_ir_chars: int = 20000,
                 ir_name: Optional[str] = None) -> str:
    """Claude に渡す指示文＋節本文（＋説明会資料）を組み立てる。

    Args:
        sections_json: sections.json の中身
        ir_text: 説明会資料のテキスト（無ければ None）
        max_ir_chars: 説明会資料を先頭から何字まで付けるか
        ir_name: 説明会資料のファイル名（出所の表示用）

    Returns:
        プロンプト文字列（JSON 配列だけを返す指示を含む）
    """
    code4 = sections_json.get("code4", "")
    doc_id = sections_json.get("doc_id", "")
    period = f"{sections_json.get('period_start', '?')}〜{sections_json.get('period_end', '?')}"
    accounting = sections_json.get("accounting", "?")
    labels = _segment_labels(sections_json)
    seg_line = "／".join(labels) if labels else "（数値要素から取れず・節本文のセグメント名を使う）"

    head = f"""あなたは有価証券報告書を読み、会社が「何が売れると業績が上がるか」を KPI の言葉で開示している箇所を抜き出す係です。
対象: 証券コード {code4}・docID {doc_id}・対象期間 {period}・会計基準 {accounting}
報告セグメント（segment にはこの表記をそのまま使う。本文が日本語名でも、この一覧の表記に揃える）: {seg_line}

以下の節本文（有価証券報告書・EDINET の XBRL テキストブロック）を読み、事業区分→品目→KPI の 3 段で候補を最大 3 行、JSON 配列だけで返してください。

各行の列（必ず全列を含める・該当なしは null）:
- segment: 事業区分。有報の報告セグメント名をそのまま
- item: 品目。会社が開示で使う名前をそのまま（一般名に言い換えない）
- kpi_name: 会社が開示する KPI の名前（販売数量・出荷量・単価・稼働率・受注高 など）
- kpi_value: 当期の KPI の数値（開示に数字が無ければ null）
- kpi_unit: 単位（トン・台・%・百万円 など。無ければ null）
- kpi_prev_value: 前期の KPI の数値（開示に無ければ null）
- share_scope: 構成比が測られている範囲。"区分" または "品目"（品目別の内訳・感応度が開示にある時だけ "品目"）
- label_hint: "需要"・"期待"・"未判定" のいずれか（最終判定は機械が行う・参考値）
- quote: 根拠の原文 1 文（節本文からそのまま写す・要約しない）
- quote_section: quote を取った節のキー（business_policy / mdna / risks / segment_notes / business_results / business_description / production_sales / {IR_SECTION_KEY}）
- expectation: true/false。有報の経営方針で「成長事業」「重点領域」「新規事業」と会社が明記した品目だけ true

守ること:
- 数字を補わない・推定しない。開示に無い KPI・数値は null にする。
- 品目名・KPI 名は会社が開示で使う名前を使う。
- expectation=true は経営方針の節で成長事業・重点領域と明記された物だけ。
- 候補が無ければ空配列 [] を返す。
- 出力は JSON 配列だけ。前後の説明文・コードフェンス（```）は付けない。
"""

    parts: List[str] = [head, "=== 節本文（有価証券報告書） ==="]
    sections = sections_json.get("sections") or {}
    for key in SECTION_TITLES:
        sec = sections.get(key)
        if not sec or not sec.get("text"):
            continue
        parts.append(f"\n[{key}] {SECTION_TITLES[key]}（{sec.get('element_id', '')}）\n{sec['text']}")

    if ir_text:
        clipped = ir_text[:max_ir_chars]
        name = ir_name or "決算説明会資料"
        parts.append(
            f"\n=== 決算説明会資料（{name}・先頭 {len(clipped):,} 字・任意開示） ===\n"
            "この資料は任意開示です。有報と食い違う数字は有報を正とし、この資料だけにある KPI は "
            f"quote_section を \"{IR_SECTION_KEY}\" にしてください。\n"
            f"{clipped}"
        )
    return "\n".join(parts)


def extract_json_array(text: str) -> Optional[list]:
    """応答テキストから JSON 配列を取り出す（コードフェンス・前後の文は捨てる）。

    Args:
        text: モデルの応答テキスト

    Returns:
        list。配列として読めなければ None
    """
    if not text:
        return None
    body = text.strip()
    if body.startswith("```"):
        lines = body.split("\n")
        body = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
    start = body.find("[")
    end = body.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


ALLOWED_SECTIONS = frozenset(SECTION_TITLES) | {IR_SECTION_KEY}
ALLOWED_LABEL_HINTS = frozenset({"需要", "期待", "未判定"})
REQUIRED_KEYS: Tuple[str, ...] = ("segment", "item", "kpi_name", "quote")
MAX_ROWS = 3
STRING_KEYS: Tuple[str, ...] = ("segment", "item", "kpi_name", "quote", "quote_section", "share_scope",
                                "label_hint", "kpi_unit")
NUMERIC_KEYS: Tuple[str, ...] = ("kpi_value", "kpi_prev_value")


def parse_bool(value: Any) -> Tuple[bool, bool]:
    """LLM 行の真偽値を厳密に読む。

    受け付けるのは true/false（bool）・"true"/"false"（大文字小文字は問わない・前後空白可）・1/0 だけ。
    `bool("false")` が True になる事故（Codex Critical 2）を防ぐ。

    Returns:
        (値, 受け付けたか)。受け付けない値は (False, False)。None は (False, True)＝「該当なし」。
    """
    if value is None:
        return False, True
    if isinstance(value, bool):
        return value, True
    if isinstance(value, int) and value in (0, 1):
        return bool(value), True
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "true":
            return True, True
        if s == "false":
            return False, True
    return False, False


def parse_number(value: Any) -> Optional[float]:
    """KPI 値を数値に読む（"106%"・"1,234"・全角数字 → 106・1234）。読めなければ None。

    bool・dict・list は数値扱いしない（True が 1 に化ける事故を防ぐ）。int/float はそのまま（nan/inf は None）。
    文字列は NFKC 正規化→前後空白と桁区切り「,」を除き→末尾の % を落として float に読む。
    判定側（yuho_judge._num）もこの関数を使う＝数値の読み方は 1 箇所（Codex 2 回目 Critical 4・5）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return value
    if not isinstance(value, str):
        return None
    s = unicodedata.normalize("NFKC", value).strip().replace(",", "").rstrip("%").strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f) if f.is_integer() else f


def normalize_rows(rows: list) -> Tuple[List[Dict[str, Any]], List[str]]:
    """各行に 11 列を揃え、型を検証する（dict でない要素は捨てる）。

    - 文字列列（STRING_KEYS）: str 以外（dict/list/bool/数値）は None にして警告・str は strip し空白だけなら None
    - 数値列（kpi_value／kpi_prev_value）: int/float か「数値に読める文字列」だけ採用（文字列は strip して残す・
      数値化は判定側）。bool/dict/list/読めない文字列は None にして警告。空文字は None（警告なし）
    - item か kpi_name が None になった行は「必須列欠落」で捨てて警告（Codex 2 回目 Critical 5）
    - expectation: `parse_bool` で読めない値は False にして警告
    - quote_section: 節キー（SECTION_TITLES のキー＋ ir_presentation）以外は None にして警告
    - label_hint: 需要／期待／未判定 以外は None にして警告
    - 行数が MAX_ROWS（3）を超えたら先頭 3 行だけ採って警告

    Returns:
        (rows, warnings)。warnings は read.json に残す文字列の list。
    """
    out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            warnings.append(f"row{i}: dict でない要素を捨てた ({type(row).__name__})")
            continue
        fixed: Dict[str, Any] = {k: row.get(k) for k in ROW_KEYS}
        for key in STRING_KEYS:
            v = fixed[key]
            if v is None:
                continue
            if not isinstance(v, str):
                warnings.append(f"row{i}: {key}={v!r} は文字列でない ({type(v).__name__}) ので None にした")
                fixed[key] = None
                continue
            v = v.strip()
            fixed[key] = v if v else None
        for key in NUMERIC_KEYS:
            v = fixed[key]
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                warnings.append(f"row{i}: {key}={v!r} は数値でない ({type(v).__name__}) ので None にした")
                fixed[key] = None
                continue
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    fixed[key] = None
                elif parse_number(v) is None:
                    warnings.append(f"row{i}: {key}={v!r} は数値に読めないので None にした")
                    fixed[key] = None
                else:
                    fixed[key] = v
        if fixed["item"] is None or fixed["kpi_name"] is None:
            warnings.append(f"row{i}: 必須列欠落（item={fixed['item']!r} kpi_name={fixed['kpi_name']!r}）なので捨てた")
            continue
        exp, ok = parse_bool(fixed["expectation"])
        if not ok:
            warnings.append(f"row{i}: expectation={fixed['expectation']!r} は true/false として読めないので False にした")
        fixed["expectation"] = exp
        if fixed["quote_section"] is not None and fixed["quote_section"] not in ALLOWED_SECTIONS:
            warnings.append(f"row{i}: quote_section={fixed['quote_section']!r} は節キーでないので None にした")
            fixed["quote_section"] = None
        if fixed["label_hint"] is not None and fixed["label_hint"] not in ALLOWED_LABEL_HINTS:
            warnings.append(f"row{i}: label_hint={fixed['label_hint']!r} は 需要/期待/未判定 でないので None にした")
            fixed["label_hint"] = None
        out.append(fixed)
    if len(out) > MAX_ROWS:
        warnings.append(f"行数 {len(out)} が上限 {MAX_ROWS} を超えたので先頭 {MAX_ROWS} 行だけ採った")
        out = out[:MAX_ROWS]
    return out, warnings


def _post_messages(prompt: str, model: str, api_key: str, max_tokens: int, timeout: int = 180) -> dict:
    """Messages API を 1 回叩いて応答 JSON を返す（例外はそのまま投げる）。"""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "anthropic-version": API_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _response_text(resp: dict) -> str:
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def call_claude(prompt: str, model: str, api_key: str, max_tokens: int = 4000,
                retry_wait: float = 2.0) -> Dict[str, Any]:
    """Claude を呼び、JSON 配列の行と usage を返す。JSON が取れなければ 1 回だけ再試行する。

    Args:
        prompt: build_prompt の出力
        model: モデル名
        api_key: ANTHROPIC_API_KEY
        max_tokens: 応答の上限トークン
        retry_wait: 再試行前の待ち秒

    Returns:
        {rows, warnings, usage:{input_tokens, output_tokens}, usage_ok, error, attempts, stop_reason}
        usage は試行分を合算（失敗した試行の分も費用として数える）。usage_ok は API 応答から usage を 1 回でも
        取れた時だけ True（通信失敗だけの doc は False＝費用の実測に数えない・Codex 2 回目 Critical 6）。
        warnings は normalize_rows の型検証の記録
    """
    usage = {"input_tokens": 0, "output_tokens": 0}
    usage_ok = False
    errors: List[str] = []
    stop_reason: Optional[str] = None
    attempts = 0
    for attempt in range(2):
        attempts = attempt + 1
        if attempt > 0 and retry_wait > 0:
            time.sleep(retry_wait)
        try:
            resp = _post_messages(prompt, model, api_key, max_tokens)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            errors.append(f"attempt{attempts}: http {e.code} {_sanitize(body[:300])}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(f"attempt{attempts}: network {_sanitize(str(e))[:300]}")
            continue
        except json.JSONDecodeError as e:
            # 応答本文が JSON でない（ゲートウェイの HTML 等）。usage は取れない＝未測定のまま次の試行へ
            errors.append(f"attempt{attempts}: bad json body {_sanitize(str(e))[:200]}")
            continue
        u = resp.get("usage")
        if isinstance(u, dict) and "input_tokens" in u:
            usage_ok = True
        u = u or {}
        usage["input_tokens"] += int(u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        stop_reason = resp.get("stop_reason")
        text = _response_text(resp)
        rows = extract_json_array(text)
        if rows is None:
            errors.append(f"attempt{attempts}: no json array (stop_reason={stop_reason}) head={_sanitize(text[:120])!r}")
            continue
        fixed, warnings = normalize_rows(rows)
        return {"rows": fixed, "warnings": warnings, "usage": usage, "usage_ok": usage_ok, "error": None,
                "attempts": attempts, "stop_reason": stop_reason}
    return {"rows": [], "warnings": [], "usage": usage, "usage_ok": usage_ok, "error": "; ".join(errors),
            "attempts": attempts, "stop_reason": stop_reason}


def load_sections(code4: str, data_dir: Path = DEFAULT_DATA_DIR) -> List[dict]:
    """`<data_dir>/<code4>/*.sections.json` を period_end 降順で読む。"""
    paths = sorted((data_dir / code4).glob("*.sections.json"))
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    docs.sort(key=lambda d: (d.get("period_end") or "", d.get("submit_date") or ""), reverse=True)
    return docs


def read_one(sections_json: dict, model: str, api_key: Optional[str], data_dir: Path,
             out_dir: Path, dry_run: bool, max_ir_chars: int, max_tokens: int,
             force: bool = False) -> Dict[str, Any]:
    """1 docID 分を処理する（dry-run なら prompt を作るだけ・書かない）。"""
    code4 = sections_json["code4"]
    doc_id = sections_json["doc_id"]
    ir_path = find_ir_text(code4, sections_json, data_dir)
    ir_text = ir_path.read_text(encoding="utf-8", errors="replace") if ir_path else None
    prompt = build_prompt(sections_json, ir_text, max_ir_chars=max_ir_chars,
                          ir_name=ir_path.name if ir_path else None)
    prompt_chars = len(prompt)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    info: Dict[str, Any] = {"doc_id": doc_id, "prompt_chars": prompt_chars, "est_tokens": prompt_chars,
                            "ir": ir_path.name if ir_path else None}
    if dry_run:
        print(f"{doc_id} prompt_chars={prompt_chars} est_tokens={prompt_chars} ir={info['ir'] or 'none'}")
        return info

    out_path = out_dir / code4 / f"{doc_id}.read.json"
    if out_path.exists() and not force:
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        # スキップは「同じ model で rows があり error なし・保存済みの prompt_sha256 が今回の prompt と一致」の時だけ。
        # 代走（Claude Code サブエージェント）やモデル違いの結果は上書きして呼び直す（Codex Critical 8）。
        # 入力（節本文・説明会資料・プロンプト文）が変わった時や hash の無い旧 read.json も呼び直す（Codex 2 回目 Warning）
        if (prev.get("model") == model and prev.get("rows") and not prev.get("error")
                and prev.get("prompt_sha256") == prompt_sha256):
            print(f"{doc_id} skip (exists model={model} rows={len(prev['rows'])} prompt_sha256 一致) {out_path}")
            info["skipped"] = True
            return info
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY が無い（docker-compose の environment で渡す）")

    result = call_claude(prompt, model, api_key, max_tokens=max_tokens)
    record = {
        "doc_id": doc_id,
        "code4": code4,
        "period_end": sections_json.get("period_end"),
        "model": model,
        "usage": result["usage"],
        "usage_ok": result.get("usage_ok") is True,
        "prompt_chars": prompt_chars,
        "prompt_sha256": prompt_sha256,
        "ir_source": ir_path.name if ir_path else None,
        "rows": result["rows"],
        "warnings": result.get("warnings") or [],
        "error": result["error"],
        "attempts": result["attempts"],
        "stop_reason": result["stop_reason"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    u = result["usage"]
    print(f"{doc_id} rows={len(result['rows'])} warnings={len(record['warnings'])} input_tokens={u['input_tokens']} "
          f"output_tokens={u['output_tokens']} error={'none' if not result['error'] else result['error'][:80]} "
          f"wrote {out_path}")
    info.update({"rows": len(result["rows"]), "usage": u, "error": result["error"],
                 "warnings": record["warnings"]})
    return info


def main(argv: Optional[List[str]] = None) -> int:
    """CLI。1 件でも `error` ありなら 1 を返す（read.json は保存したまま）。鍵なし・sections.json なしは SystemExit(1)。"""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--code", required=True, help="証券コード 4 桁（例: 2802）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="prompt_chars と概算トークンだけ出し API を呼ばない")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--max-ir-chars", type=int, default=20000)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--force", action="store_true", help="既存の read.json があっても呼び直す")
    args = ap.parse_args(argv)

    code4 = args.code.zfill(4)
    docs = load_sections(code4, args.data_dir)
    if not docs:
        raise SystemExit(f"sections.json が無い: {args.data_dir / code4}/*.sections.json（先に scripts/yuho_fetch.py）")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    total_in = total_out = 0
    errors: List[str] = []
    for doc in docs:
        info = read_one(doc, args.model, api_key, args.data_dir, args.out_dir, args.dry_run,
                        args.max_ir_chars, args.max_tokens, force=args.force)
        if info.get("usage"):
            total_in += info["usage"]["input_tokens"]
            total_out += info["usage"]["output_tokens"]
        if info.get("error"):
            errors.append(f"{info['doc_id']}: {info['error'][:120]}")
    if not args.dry_run:
        print(f"total docs={len(docs)} input_tokens={total_in} output_tokens={total_out} errors={len(errors)}")
    if errors:
        print("error docs: " + " | ".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
