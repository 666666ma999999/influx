#!/usr/bin/env python3
"""有報の節本文（sections.json）＋決算説明会資料（ir/*.txt）を Claude に読ませ、品目×KPI の候補行を取る（P-INF-34 工程③・T9a）。

入力: `data/edinet/yuho/<code4>/<docID>.sections.json`（`scripts/yuho_fetch.py` の出力）
      `data/edinet/yuho/<code4>/ir/FY<yyyy>*.txt`（任意・`scripts/ir_pdf_text.py` の出力・period_start の年度に合う物だけ）
出力: `output/yuho/<code4>/<docID>.read.json`
      = {doc_id, code4, period_end, submit_date, mode, model, usage, prompt_chars, prompt_sha256, rows:[...],
         segment_figures:[...], warnings, error, ...}
      `output/yuho/<code4>/targets.json`（`--make-targets`・基準期の読みから決めた「追う品目」）

読みの型（T9・P-INF-37）:
- 基準期（その会社の sections.json で period_end が最も古い期）= mode "base"。最大 10 行・区分ごとに 1 行以上・
  数量／単価／受注／稼働率を金額より優先・全行 status "new"
- 後の期 = mode "nominated"（targets.json がある時だけ）。指名 1 件につき 1 行（status "found"／"not_disclosed"）＋
  新しく見つけた品目を最大 3 行（status "new"）。targets.json が無い期は読まずにスキップする
- XBRL に区分の数字が無い会社（facts.segments が空）には、本文のセグメント表から区分の売上・利益を
  "kind": "segment_figures" の行で返させ、read.json の `segment_figures` に分けて置く

Claude API は `collector/llm_classifier.py` と同じ urllib 直叩き（SDK は Docker に無い）。
`ANTHROPIC_API_KEY` は docker-compose が渡す。

使い方:
    python scripts/yuho_read.py --code 2802 --dry-run              # prompt_chars と概算トークンだけ出す（API を呼ばない）
    python scripts/yuho_read.py --code 2802 --model claude-sonnet-5  # 実走（1 docID につき 1 回・既存 read.json はスキップ）
    python scripts/yuho_read.py --code 2802 --make-targets           # 基準期の read.json から targets.json を作る
    python scripts/yuho_read.py --code 2802 --dump-prompts           # 代走用: prompts/<docID>.prompt.txt を書く（API なし）
    python scripts/yuho_read.py --code 2802 --import-responses       # 代走用: prompts/<docID>.response.json → read.json
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
    # T9（P-INF-37）で足した列: 品目の束ね鍵の材料と、指名読みの結果
    "geo", "value_type", "value_basis", "status", "target_id",
]

MODE_BASE = "base"            # 基準期（3 期で最も古い有報）＝追う品目を決める読み
MODE_NOMINATED = "nominated"  # 後の期＝targets.json の品目を指名して読む
MAX_ROWS_BASE = 10            # 基準期の上限行数（旧 MAX_ROWS=3 を置き換え）
MAX_NEW_ROWS_NOMINATED = 3    # 指名読みで足してよい「新しく見つけた品目」の上限（全体の上限＝指名数＋これ）

STATUS_NEW = "new"                      # 新しく見つけた品目
STATUS_FOUND = "found"                  # 指名された品目の数字が当期の資料にあった
STATUS_NOT_DISCLOSED = "not_disclosed"  # 指名された品目の数字が当期の資料に無い
ALLOWED_STATUSES = frozenset({STATUS_NEW, STATUS_FOUND, STATUS_NOT_DISCLOSED})
ALLOWED_VALUE_TYPES: Tuple[str, ...] = ("数量", "単価", "金額", "比率", "受注", "稼働率", "その他")
ALLOWED_VALUE_BASES: Tuple[str, ...] = ("水準", "前年比")

# targets.json の 1 件が持つ列（target_id の他）＝指名読みで「同じ品目」を指す鍵
TARGET_KEYS: Tuple[str, ...] = ("segment", "item", "kpi_name", "geo", "value_type", "value_basis")

# XBRL に区分の数字が無い会社（facts.segments が空）だけに返させる行（kind="segment_figures"）
SEGMENT_FIGURES_KIND = "segment_figures"
FIGURE_KEYS: Tuple[str, ...] = ("segment", "sales_cur", "sales_prev", "profit_cur", "profit_prev",
                                "profit_measure", "unit", "quote")
FIGURE_NUMERIC_KEYS: Tuple[str, ...] = ("sales_cur", "sales_prev", "profit_cur", "profit_prev")

# 代走（Claude Code サブエージェント）で読んだ read.json の model 表記。read_one は model 違いとして上書きする
PROXY_MODEL = "claude-sonnet (Claude Code subagent 代走・API 未使用)"

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


def needs_segment_figures(sections_json: dict) -> bool:
    """XBRL の数値要素から区分の数字が取れていない（facts.segments が空＝US GAAP 等）か。

    True の会社には本文のセグメント表から区分の売上・利益を segment_figures 行で返させる（T9a ④）。
    """
    return not ((sections_json.get("facts") or {}).get("segments"))


def max_rows_for(mode: str, targets: Optional[List[dict]] = None) -> int:
    """モードごとの KPI 行の上限（segment_figures 行は数えない）。基準期= 10・指名読み= 指名数＋3。"""
    if mode == MODE_NOMINATED:
        return len(targets or []) + MAX_NEW_ROWS_NOMINATED
    return MAX_ROWS_BASE


def _target_line(t: dict) -> str:
    def v(key: str) -> str:
        val = t.get(key)
        return str(val) if val is not None else "null"
    return (f"- {t.get('target_id')}: segment={v('segment')}／item={v('item')}／kpi_name={v('kpi_name')}／"
            f"geo={v('geo')}／value_type={v('value_type')}／value_basis={v('value_basis')}")


def build_prompt(sections_json: dict, ir_text: Optional[str], max_ir_chars: int = 20000,
                 ir_name: Optional[str] = None, mode: str = MODE_BASE,
                 targets: Optional[List[dict]] = None) -> str:
    """Claude に渡す指示文＋節本文（＋説明会資料）を組み立てる。

    Args:
        sections_json: sections.json の中身
        ir_text: 説明会資料のテキスト（無ければ None）
        max_ir_chars: 説明会資料を先頭から何字まで付けるか
        ir_name: 説明会資料のファイル名（出所の表示用）
        mode: "base"（基準期・最大 10 行）か "nominated"（targets を指名して読む）
        targets: 指名読みの品目（targets.json の "targets"）。mode="nominated" の時だけ使う

    Returns:
        プロンプト文字列（JSON 配列だけを返す指示を含む）
    """
    if mode not in (MODE_BASE, MODE_NOMINATED):
        raise ValueError(f"mode は base か nominated: {mode!r}")
    code4 = sections_json.get("code4", "")
    doc_id = sections_json.get("doc_id", "")
    period = f"{sections_json.get('period_start', '?')}〜{sections_json.get('period_end', '?')}"
    accounting = sections_json.get("accounting", "?")
    labels = _segment_labels(sections_json)
    seg_line = "／".join(labels) if labels else "（数値要素から取れず・節本文のセグメント名を使う）"
    targets = targets or []
    limit = max_rows_for(mode, targets)

    if mode == MODE_BASE:
        task = f"""この期は「基準期」です（この会社で追う品目をここで決めます）。
以下の節本文（有価証券報告書・EDINET の XBRL テキストブロック）を読み、事業区分→品目→KPI の 3 段で候補を最大 {limit} 行、JSON 配列だけで返してください。
- 報告セグメントごとに、資料に KPI の開示があれば少なくとも 1 行を出す（開示が無い区分は出さなくてよい）。
- KPI の種類は 数量・単価・受注・稼働率 を 金額 より優先する。売上高は区分の売上とほぼ同じで情報が少ないので、その区分に他の KPI が無い時だけ使う。
- status は全行 "new"・target_id は全行 null。"""
    else:
        target_lines = "\n".join(_target_line(t) for t in targets) or "（指名なし）"
        task = f"""この期は「指名読み」です。基準期の読みで、この会社について追う品目（target）が下のとおり決まっています。
以下の節本文（有価証券報告書・EDINET の XBRL テキストブロック）を読み、JSON 配列だけで返してください（最大 {limit} 行）。
- 下の target 1 件につき、必ずちょうど 1 行を返す。target_id にその ID を入れる。
- segment は当期の報告セグメント名で書く（区分が再編・改名されていれば当期の名前に直す）。item・kpi_name・geo・value_type・value_basis は target のまま。
- 当期の資料にその品目の KPI の数字があれば status="found" にして kpi_value・kpi_prev_value・quote を埋める。
- 当期の資料にその数字が無ければ status="not_disclosed" にする（kpi_value・kpi_prev_value は null・quote は null でよい）。似た別の数字で代用しない。
- target に無い品目で、新しく KPI の開示が見つかった物は最大 {MAX_NEW_ROWS_NOMINATED} 行まで足してよい（status="new"・target_id=null）。

追う品目（target）:
{target_lines}"""

    status_desc = ('"new" に固定（基準期）' if mode == MODE_BASE else
                   '"found"（指名の数字あり）・"not_disclosed"（指名の数字が当期の資料に無い）・"new"（新しく見つけた品目）のいずれか')
    target_desc = ("null（基準期）" if mode == MODE_BASE else
                   "指名行は target の ID（例: T01）・new の行は null")

    # 「期待」の根拠に使える資料（P-INF-37 ①a: 経営方針・MD&A・決算説明会資料＝yuho_judge.EXPECT_SECTIONS と同じ）。
    # 説明会資料は本文を付けた時だけ挙げる（付けていない資料からは引用できない）
    expect_src = "経営方針（business_policy）・経営者による分析（mdna）" + (
        f"・決算説明会資料（{IR_SECTION_KEY}）" if ir_text else "")
    expect_src_short = "経営方針・MD&A" + ("・決算説明会資料" if ir_text else "")

    head = f"""あなたは有価証券報告書を読み、会社が「何が売れると業績が上がるか」を KPI の言葉で開示している箇所を抜き出す係です。
対象: 証券コード {code4}・docID {doc_id}・対象期間 {period}・会計基準 {accounting}
報告セグメント（segment にはこの表記をそのまま使う。本文が日本語名でも、この一覧の表記に揃える）: {seg_line}

{task}

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
- expectation: true/false。{expect_src}のどれかで、会社が「成長事業」「重点領域」「注力」「拡大」など成長・重点の言葉でその品目を明記した時だけ true（quote にその文を写し、quote_section にその節キーを入れる）
- geo: 地域。"国内"・"海外"・"全体"・または開示の地域名（北米・中国 など）。読み取れなければ null
- value_type: KPI の種類。{"・".join(f'"{v}"' for v in ALLOWED_VALUE_TYPES)} のいずれか
- value_basis: 数字の基準。"水準"（120 万トン・稼働率 85% など量そのもの）か "前年比"（数量 105%・+8% など前年との比較）
- status: {status_desc}
- target_id: {target_desc}

守ること:
- 数字を補わない・推定しない。開示に無い KPI・数値は null にする。
- 品目名・KPI 名は会社が開示で使う名前を使う。
- expectation=true は{expect_src_short}のどれかで、品目名つきで成長事業・重点領域と明記された物だけ（その文を quote に写す）。
- 候補が無ければ空配列 [] を返す。
- 出力は JSON 配列だけ。前後の説明文・コードフェンス（```）は付けない。
"""
    if needs_segment_figures(sections_json):
        head += f"""
追加の依頼（この会社は XBRL の数値要素から区分ごとの売上・利益が取れていません）:
- 本文（segment_notes・mdna など）に報告セグメントごとの売上・利益の表があれば、区分ごとに 1 行ずつ、"kind": "{SEGMENT_FIGURES_KIND}" の行を同じ JSON 配列に足してください。
- 列: kind・segment・sales_cur（当期の売上）・sales_prev（前期の売上）・profit_cur（当期の区分利益）・profit_prev（前期の区分利益）・profit_measure（セグメント利益・営業利益 など本文での呼び名）・unit（百万円 など）・quote（表の該当箇所をそのまま写す）
- 数字は本文の表から写すだけ。計算・推定・合算はしない。表に無い数字は null。表が無ければこの行は返さない。
- この行は上の KPI の行数の上限に数えない。
- 本文に「単一セグメント」「報告セグメントは単一」など、報告セグメントが 1 つだけだと書いてある時は、上の行の代わりに
  {{"kind": "{SEGMENT_FIGURES_KIND}", "single_segment": true, "quote": "<その記載をそのまま写す>"}} の 1 行だけを返す
  （本文に書いていなければ返さない。区分の表が無いことから単一と推定しない）。
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
# 行を残す必須列。quote は必須にしない（not_disclosed の行は quote が null でよい・T9a ③）
REQUIRED_KEYS: Tuple[str, ...] = ("item", "kpi_name")
STRING_KEYS: Tuple[str, ...] = ("segment", "item", "kpi_name", "quote", "quote_section", "share_scope",
                                "label_hint", "kpi_unit", "geo", "value_type", "value_basis", "status",
                                "target_id")
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


def _figure_number(value: Any) -> Tuple[Optional[float], bool]:
    """segment_figures の数字を読む（"△1,234"・"▲5" は負数）。(値, 受け付けたか)。None は (None, True)。"""
    if value is None:
        return None, True
    if isinstance(value, str):
        s = unicodedata.normalize("NFKC", value).strip()
        if not s:
            return None, True
        if s[0] in "△▲":
            s = "-" + s[1:].strip()
        n = parse_number(s)
        return n, n is not None
    n = parse_number(value)
    return n, n is not None


def normalize_segment_figure(i: int, row: dict) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """kind="segment_figures" の 1 行を FIGURE_KEYS の dict にする（数字は本文から写した物だけ・推定しない）。

    - segment が無い・数字（売上・利益の当期／前期）が 1 つも読めない行は捨てて警告
    - 数字は parse_number で数値にする（"△" "▲" は負数）。読めない値は None にして警告
    - 文字列列（segment・profit_measure・unit・quote）は str 以外を None にして警告
    - "single_segment": true の行は単一セグメントの宣言＝{single_segment: True, segment: None, quote} にする
      （quote が無ければ捨てる・Codex T9 Critical 3）。yuho_judge は quote が本文に実在する時だけ単一とみなす

    Returns:
        (dict か None, warnings)
    """
    warnings: List[str] = []
    if row.get("single_segment") is True:
        # 単一セグメントの宣言行（数字なし）。quote が無ければ捨てる＝本文の記載が無い自己申告は採らない
        q = row.get("quote")
        q = q.strip() if isinstance(q, str) else ""
        if not q:
            warnings.append(f"row{i}: 単一セグメントの宣言に quote が無いので捨てた")
            return None, warnings
        return {"single_segment": True, "segment": None, "quote": q}, warnings
    fig: Dict[str, Any] = {}
    for key in ("segment", "profit_measure", "unit", "quote"):
        v = row.get(key)
        if v is not None and not isinstance(v, str):
            warnings.append(f"row{i}: segment_figures の {key}={v!r} は文字列でない ({type(v).__name__}) ので None にした")
            v = None
        fig[key] = (v.strip() or None) if isinstance(v, str) else None
    for key in FIGURE_NUMERIC_KEYS:
        n, ok = _figure_number(row.get(key))
        if not ok:
            warnings.append(f"row{i}: segment_figures の {key}={row.get(key)!r} は数値に読めないので None にした")
        fig[key] = n
    fig = {k: fig.get(k) for k in FIGURE_KEYS}
    if fig["segment"] is None:
        warnings.append(f"row{i}: segment_figures に segment が無いので捨てた")
        return None, warnings
    if all(fig[k] is None for k in FIGURE_NUMERIC_KEYS):
        warnings.append(f"row{i}: segment_figures（{fig['segment']}）に数字が 1 つも無いので捨てた")
        return None, warnings
    return fig, warnings


def normalize_rows(rows: list, mode: str = MODE_BASE, targets: Optional[List[dict]] = None,
                   segment_figures: Optional[List[Dict[str, Any]]] = None
                   ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """各行に ROW_KEYS の列を揃え、型を検証する（dict でない要素は捨てる）。

    - kind="segment_figures" の行は KPI の行から外し、`segment_figures`（渡された list）へ足す。
      list を渡さなければ捨てて警告（T9a ④）
    - 文字列列（STRING_KEYS）: str 以外（dict/list/bool/数値）は None にして警告・str は strip し空白だけなら None
    - 数値列（kpi_value／kpi_prev_value）: int/float か「数値に読める文字列」だけ採用（文字列は strip して残す・
      数値化は判定側）。bool/dict/list/読めない文字列は None にして警告。空文字は None（警告なし）
    - status／target_id（T9a ③）:
      - 基準期（mode="base"）: status は "new" に揃える（他の値は警告）・target_id は None（値があれば警告）
      - 指名読み（mode="nominated"）: status は found／not_disclosed／new だけ（他は警告して捨てる）。
        found・not_disclosed は targets にある target_id が必須（無い・未知は警告して捨てる・同じ ID の 2 行目も捨てる）。
        指名行の item・kpi_name・geo・value_type・value_basis・segment が null なら target の値で埋める。
        not_disclosed の kpi_value・kpi_prev_value は None に揃える（値があれば警告）・quote は null でよい。
        new の行の target_id は None（値があれば警告）
    - item か kpi_name が None になった行は「必須列欠落」で捨てて警告（Codex 2 回目 Critical 5）
    - expectation: `parse_bool` で読めない値は False にして警告
    - quote_section: 節キー（SECTION_TITLES のキー＋ ir_presentation）以外は None にして警告
    - label_hint: 需要／期待／未判定 以外は None にして警告
    - value_type: 数量／単価／金額／比率／受注／稼働率／その他 以外は None にして警告
    - value_basis: 水準／前年比 以外は None にして警告
    - 行数の上限: 基準期は MAX_ROWS_BASE（10）を超えたら先頭だけ採って警告。指名読みは new の行が
      MAX_NEW_ROWS_NOMINATED（3）を超えたら先頭だけ採って警告（全体の上限＝指名数＋3）。
      指名読みで行の無い target は警告に残す（行は作らない＝数字を補わない）

    Args:
        rows: モデルが返した JSON 配列
        mode: "base" か "nominated"
        targets: 指名読みの品目（targets.json の "targets"）
        segment_figures: segment_figures 行の受け皿（None なら捨てる）

    Returns:
        (rows, warnings)。warnings は read.json に残す文字列の list。
    """
    if mode not in (MODE_BASE, MODE_NOMINATED):
        raise ValueError(f"mode は base か nominated: {mode!r}")
    targets_by_id: Dict[str, dict] = {t["target_id"]: t for t in (targets or []) if t.get("target_id")}
    out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_targets: List[str] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            warnings.append(f"row{i}: dict でない要素を捨てた ({type(row).__name__})")
            continue
        if row.get("kind") == SEGMENT_FIGURES_KIND:
            fig, fig_warnings = normalize_segment_figure(i, row)
            warnings.extend(fig_warnings)
            if fig is not None:
                if segment_figures is None:
                    warnings.append(f"row{i}: segment_figures の受け皿が無いので捨てた")
                else:
                    segment_figures.append(fig)
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

        status = fixed["status"]
        if mode == MODE_BASE:
            if status not in (None, STATUS_NEW):
                warnings.append(f"row{i}: status={status!r} は基準期では使えないので \"new\" にした")
            fixed["status"] = STATUS_NEW
            if fixed["target_id"] is not None:
                warnings.append(f"row{i}: 基準期の target_id={fixed['target_id']!r} は None にした")
                fixed["target_id"] = None
        else:
            if status not in ALLOWED_STATUSES:
                warnings.append(f"row{i}: status={status!r} は found/not_disclosed/new でないので捨てた")
                continue
            if status == STATUS_NEW:
                if fixed["target_id"] is not None:
                    warnings.append(f"row{i}: new の行の target_id={fixed['target_id']!r} は None にした")
                    fixed["target_id"] = None
            else:
                tid = fixed["target_id"]
                if tid not in targets_by_id:
                    warnings.append(f"row{i}: target_id={tid!r} は指名に無いので捨てた（status={status}）")
                    continue
                if tid in seen_targets:
                    warnings.append(f"row{i}: target_id={tid!r} の 2 行目なので捨てた（指名 1 件につき 1 行）")
                    continue
                target = targets_by_id[tid]
                for key in TARGET_KEYS:
                    if fixed[key] is None and target.get(key) is not None:
                        fixed[key] = target.get(key)
                if status == STATUS_NOT_DISCLOSED:
                    for key in NUMERIC_KEYS:
                        if fixed[key] is not None:
                            warnings.append(f"row{i}: not_disclosed の {key}={fixed[key]!r} は None にした")
                            fixed[key] = None

        missing = [k for k in REQUIRED_KEYS if fixed[k] is None]
        if missing:
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
        if fixed["value_type"] is not None and fixed["value_type"] not in ALLOWED_VALUE_TYPES:
            warnings.append(f"row{i}: value_type={fixed['value_type']!r} は {'/'.join(ALLOWED_VALUE_TYPES)} でないので None にした")
            fixed["value_type"] = None
        if fixed["value_basis"] is not None and fixed["value_basis"] not in ALLOWED_VALUE_BASES:
            warnings.append(f"row{i}: value_basis={fixed['value_basis']!r} は 水準/前年比 でないので None にした")
            fixed["value_basis"] = None
        if fixed["target_id"] is not None:
            seen_targets.append(fixed["target_id"])
        out.append(fixed)

    if mode == MODE_BASE:
        if len(out) > MAX_ROWS_BASE:
            warnings.append(f"行数 {len(out)} が上限 {MAX_ROWS_BASE} を超えたので先頭 {MAX_ROWS_BASE} 行だけ採った")
            out = out[:MAX_ROWS_BASE]
    else:
        new_rows = [r for r in out if r["status"] == STATUS_NEW]
        if len(new_rows) > MAX_NEW_ROWS_NOMINATED:
            drop = {id(r) for r in new_rows[MAX_NEW_ROWS_NOMINATED:]}
            warnings.append(f"new の行数 {len(new_rows)} が上限 {MAX_NEW_ROWS_NOMINATED} を超えたので先頭 "
                            f"{MAX_NEW_ROWS_NOMINATED} 行だけ採った")
            out = [r for r in out if id(r) not in drop]
        for tid in targets_by_id:
            if tid not in seen_targets:
                warnings.append(f"指名 {tid} の行が無い（found/not_disclosed のどちらも返っていない）")
    return out, warnings


def normalize_response(raw: list, mode: str = MODE_BASE, targets: Optional[List[dict]] = None
                       ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """応答の配列を (KPI の行, segment_figures, warnings) に分けて検証する（API 経路と代走の取り込みの共通入口）。"""
    figures: List[Dict[str, Any]] = []
    rows, warnings = normalize_rows(raw, mode=mode, targets=targets, segment_figures=figures)
    return rows, figures, warnings


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
                retry_wait: float = 2.0, mode: str = MODE_BASE,
                targets: Optional[List[dict]] = None) -> Dict[str, Any]:
    """Claude を呼び、JSON 配列の行と usage を返す。JSON が取れなければ 1 回だけ再試行する。

    Args:
        prompt: build_prompt の出力
        model: モデル名
        api_key: ANTHROPIC_API_KEY
        max_tokens: 応答の上限トークン
        retry_wait: 再試行前の待ち秒
        mode: normalize_rows に渡す読みの型（"base"／"nominated"）
        targets: 指名読みの品目（mode="nominated" の時）

    Returns:
        {rows, segment_figures, warnings, usage:{input_tokens, output_tokens}, usage_ok, error, attempts, stop_reason}
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
        fixed, figures, warnings = normalize_response(rows, mode=mode, targets=targets)
        return {"rows": fixed, "segment_figures": figures, "warnings": warnings, "usage": usage,
                "usage_ok": usage_ok, "error": None, "attempts": attempts, "stop_reason": stop_reason}
    return {"rows": [], "segment_figures": [], "warnings": [], "usage": usage, "usage_ok": usage_ok,
            "error": "; ".join(errors), "attempts": attempts, "stop_reason": stop_reason}


def load_sections(code4: str, data_dir: Path = DEFAULT_DATA_DIR) -> List[dict]:
    """`<data_dir>/<code4>/*.sections.json` を period_end 降順で読む。"""
    paths = sorted((data_dir / code4).glob("*.sections.json"))
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    docs.sort(key=lambda d: (d.get("period_end") or "", d.get("submit_date") or ""), reverse=True)
    return docs


def base_doc_id(docs: List[dict]) -> Optional[str]:
    """基準期の docID＝period_end が最も古い有報（同じ期末なら提出日が古い方）。docs が空なら None。"""
    if not docs:
        return None
    oldest = min(docs, key=lambda d: (d.get("period_end") or "", d.get("submit_date") or ""))
    return oldest.get("doc_id")


def targets_path(code4: str, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    return out_dir / code4 / "targets.json"


def load_targets(code4: str, out_dir: Path = DEFAULT_OUT_DIR) -> Optional[dict]:
    """`<out_dir>/<code4>/targets.json` を読む。無ければ None・形が壊れていれば SystemExit。"""
    path = targets_path(code4, out_dir)
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    tlist = doc.get("targets") if isinstance(doc, dict) else None
    if not isinstance(tlist, list) or not all(isinstance(t, dict) and t.get("target_id") for t in tlist):
        raise SystemExit(f"targets.json の形が不正（targets は target_id つきの dict の list）: {path}")
    return doc


def doc_mode(sections_json: dict, base_id: Optional[str], targets_doc: Optional[dict]
             ) -> Tuple[Optional[str], Optional[List[dict]]]:
    """この期をどの型で読むかを決める。

    Returns:
        (mode, targets)。基準期は ("base", None)。後の期は targets.json があれば ("nominated", targets)・
        無ければ (None, None)＝読まずにスキップ。targets.json の基準期が今の基準期と違えば SystemExit
        （期の入れ替わりで別の基準の指名が混ざるのを防ぐ）
    """
    if sections_json.get("doc_id") == base_id:
        return MODE_BASE, None
    if targets_doc is None:
        return None, None
    if targets_doc.get("base_doc_id") != base_id:
        raise SystemExit(f"targets.json の base_doc_id={targets_doc.get('base_doc_id')!r} が今の基準期 {base_id!r} と違う"
                         "（--make-targets --force で作り直す）")
    return MODE_NOMINATED, targets_doc.get("targets") or []


def make_prompt(sections_json: dict, data_dir: Path, max_ir_chars: int, mode: str,
                targets: Optional[List[dict]]) -> Tuple[str, Optional[Path]]:
    """1 docID のプロンプトを作る（API 経路・--dump-prompts・--import-responses の照合の共通入口）。"""
    ir_path = find_ir_text(sections_json["code4"], sections_json, data_dir)
    ir_text = ir_path.read_text(encoding="utf-8", errors="replace") if ir_path else None
    prompt = build_prompt(sections_json, ir_text, max_ir_chars=max_ir_chars,
                          ir_name=ir_path.name if ir_path else None, mode=mode, targets=targets)
    return prompt, ir_path


def _record(sections_json: dict, mode: str, model: str, result: Dict[str, Any], prompt_chars: Optional[int],
            prompt_sha256: Optional[str], ir_source: Optional[str]) -> Dict[str, Any]:
    """read.json の 1 件を組み立てる（API 経路と代走の取り込みで同じ列）。"""
    return {
        "doc_id": sections_json["doc_id"],
        "code4": sections_json["code4"],
        "period_end": sections_json.get("period_end"),
        "submit_date": sections_json.get("submit_date"),
        "mode": mode,
        "model": model,
        "usage": result["usage"],
        "usage_ok": result.get("usage_ok") is True,
        "prompt_chars": prompt_chars,
        "prompt_sha256": prompt_sha256,
        "ir_source": ir_source,
        "rows": result["rows"],
        "segment_figures": result.get("segment_figures") or [],
        # 読みが本文から「報告セグメントが単一」と宣言したか（宣言行の quote は segment_figures に残す・Codex T9 Critical 3）
        "single_segment": any(isinstance(f, dict) and f.get("single_segment") is True
                              for f in (result.get("segment_figures") or [])),
        "warnings": result.get("warnings") or [],
        "error": result["error"],
        "attempts": result["attempts"],
        "stop_reason": result["stop_reason"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def read_one(sections_json: dict, model: str, api_key: Optional[str], data_dir: Path,
             out_dir: Path, dry_run: bool, max_ir_chars: int, max_tokens: int,
             force: bool = False, mode: str = MODE_BASE,
             targets: Optional[List[dict]] = None) -> Dict[str, Any]:
    """1 docID 分を処理する（dry-run なら prompt を作るだけ・書かない）。mode／targets は doc_mode の結果。"""
    code4 = sections_json["code4"]
    doc_id = sections_json["doc_id"]
    prompt, ir_path = make_prompt(sections_json, data_dir, max_ir_chars, mode, targets)
    prompt_chars = len(prompt)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    info: Dict[str, Any] = {"doc_id": doc_id, "prompt_chars": prompt_chars, "est_tokens": prompt_chars,
                            "ir": ir_path.name if ir_path else None, "mode": mode}
    if dry_run:
        print(f"{doc_id} mode={mode} prompt_chars={prompt_chars} est_tokens={prompt_chars} ir={info['ir'] or 'none'}")
        return info

    out_path = out_dir / code4 / f"{doc_id}.read.json"
    if out_path.exists() and not force:
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        # スキップは「同じ model で rows があり error なし・保存済みの prompt_sha256 が今回の prompt と一致」の時だけ。
        # 代走（Claude Code サブエージェント）やモデル違いの結果は上書きして呼び直す（Codex Critical 8）。
        # 入力（節本文・説明会資料・プロンプト文・指名）が変わった時や hash の無い旧 read.json も呼び直す（Codex 2 回目 Warning）
        if (prev.get("model") == model and prev.get("rows") and not prev.get("error")
                and prev.get("prompt_sha256") == prompt_sha256):
            print(f"{doc_id} skip (exists model={model} rows={len(prev['rows'])} prompt_sha256 一致) {out_path}")
            info["skipped"] = True
            return info
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY が無い（docker-compose の environment で渡す）")

    result = call_claude(prompt, model, api_key, max_tokens=max_tokens, mode=mode, targets=targets)
    record = _record(sections_json, mode, model, result, prompt_chars, prompt_sha256,
                     ir_path.name if ir_path else None)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    u = result["usage"]
    print(f"{doc_id} mode={mode} rows={len(result['rows'])} segment_figures={len(record['segment_figures'])} "
          f"warnings={len(record['warnings'])} input_tokens={u['input_tokens']} "
          f"output_tokens={u['output_tokens']} error={'none' if not result['error'] else result['error'][:80]} "
          f"wrote {out_path}")
    info.update({"rows": len(result["rows"]), "usage": u, "error": result["error"],
                 "warnings": record["warnings"]})
    return info


def make_targets(code4: str, data_dir: Path = DEFAULT_DATA_DIR, out_dir: Path = DEFAULT_OUT_DIR,
                 force: bool = False) -> dict:
    """基準期の read.json から `<out_dir>/<code4>/targets.json`（追う品目）を作る（T9a ②）。

    normalize_rows（基準期の型）を通って残った行だけを、出た順に T01・T02… と番号を振って並べる。
    （segment・item・kpi_name・geo・value_type・value_basis）が同じ行は 1 件にまとめる。
    各 target の base_rows= 基準期 read.json の rows の何行目から作ったか（yuho_judge が基準期の行を target_id へ
    1 対 1 で引く・Codex T9 Critical 2）。

    Raises:
        SystemExit: sections.json が無い・基準期の read.json が無い／基準期の読みでない／error あり・
            targets.json が既にある（force なし）
    """
    docs = load_sections(code4, data_dir)
    base_id = base_doc_id(docs)
    if base_id is None:
        raise SystemExit(f"sections.json が無い: {data_dir / code4}/*.sections.json（先に scripts/yuho_fetch.py）")
    base_doc = next(d for d in docs if d.get("doc_id") == base_id)
    read_path = out_dir / code4 / f"{base_id}.read.json"
    if not read_path.exists():
        raise SystemExit(f"基準期の read.json が無い: {read_path}（先に基準期を読む）")
    rec = json.loads(read_path.read_text(encoding="utf-8"))
    if rec.get("mode") != MODE_BASE:
        raise SystemExit(f"基準期の read.json が基準期の型で読まれていない（mode={rec.get('mode')!r}）: {read_path}"
                         "（T9 以前の読みは基準期の型で読み直す）")
    if rec.get("error"):
        raise SystemExit(f"基準期の read.json に error がある: {read_path}（読み直してから）")
    tpath = targets_path(code4, out_dir)
    if tpath.exists() and not force:
        raise SystemExit(f"targets.json が既にある: {tpath}（作り直す時は --force・後の期は指名が変わるので読み直しになる）")
    # 行ごとに検証し、read.json の行番号（base_rows）を target に残す＝yuho_judge が基準期の行を target_id へ
    # 1 対 1 で引く（属性キーで引くと区分だけ違う 2 件が潰れる・Codex T9 Critical 2）
    warnings: List[str] = []
    targets: List[dict] = []
    by_key: Dict[tuple, dict] = {}
    kept = 0
    for ri, raw in enumerate(rec.get("rows") or []):
        rows_i, w_i = normalize_rows([raw], mode=MODE_BASE)
        warnings.extend(f"row{ri}: {w.split(': ', 1)[-1]}" for w in w_i)
        if not rows_i:
            continue
        if kept >= MAX_ROWS_BASE:
            warnings.append(f"row{ri}: 基準期の上限 {MAX_ROWS_BASE} 行を超えたので捨てた")
            continue
        kept += 1
        row = rows_i[0]
        key = tuple(row.get(k) for k in TARGET_KEYS)
        if key in by_key:
            by_key[key]["base_rows"].append(ri)
            warnings.append(f"同じ品目の行（{row.get('item')}・{row.get('kpi_name')}）は 1 件にまとめた")
            continue
        t = {"target_id": f"T{len(targets) + 1:02d}", **{k: row.get(k) for k in TARGET_KEYS}, "base_rows": [ri]}
        by_key[key] = t
        targets.append(t)
    doc = {"code4": code4, "base_doc_id": base_id, "base_period_end": base_doc.get("period_end"),
           "targets": targets}
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{code4} targets={len(targets)} base={base_id} ({base_doc.get('period_end')}) wrote {tpath}")
    for w in warnings:
        print(f"  warn: {w}")
    if not targets:
        print(f"  warn: 追う品目が 0 件（後の期は new の行だけを読む）")
    return doc


def dump_prompts(code4: str, data_dir: Path = DEFAULT_DATA_DIR, out_dir: Path = DEFAULT_OUT_DIR,
                 max_ir_chars: int = 20000) -> Dict[str, List[str]]:
    """代走用に `<out_dir>/<code4>/prompts/<docID>.prompt.txt` を書く（API を呼ばない・T9a ⑤）。

    基準期は基準期の型。後の期は targets.json がある時だけ指名読みの型で書き、無ければ書かずに docID を表示する。

    Returns:
        {"written": [docID...], "skipped": [docID...]}
    """
    docs = load_sections(code4, data_dir)
    if not docs:
        raise SystemExit(f"sections.json が無い: {data_dir / code4}/*.sections.json（先に scripts/yuho_fetch.py）")
    base_id = base_doc_id(docs)
    targets_doc = load_targets(code4, out_dir)
    pdir = out_dir / code4 / "prompts"
    written: List[str] = []
    skipped: List[str] = []
    for sec in docs:
        mode, targets = doc_mode(sec, base_id, targets_doc)
        if mode is None:
            skipped.append(sec["doc_id"])
            continue
        prompt, ir_path = make_prompt(sec, data_dir, max_ir_chars, mode, targets)
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / f"{sec['doc_id']}.prompt.txt"
        path.write_text(prompt, encoding="utf-8")
        written.append(sec["doc_id"])
        print(f"{code4} {sec['doc_id']} mode={mode} period={sec.get('period_start')}..{sec.get('period_end')} "
              f"prompt_chars={len(prompt)} ir={ir_path.name if ir_path else 'none'} wrote {path}")
    if skipped:
        print(f"{code4} skipped（targets.json が無いので指名読みのプロンプトを書かない・先に --make-targets）: "
              + " ".join(skipped))
    return {"written": written, "skipped": skipped}


def import_responses(code4: str, data_dir: Path = DEFAULT_DATA_DIR, out_dir: Path = DEFAULT_OUT_DIR,
                     max_ir_chars: int = 20000, agent_tokens: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, List[str]]:
    """代走の応答 `prompts/<docID>.response.json` を `<docID>.read.json` にする（API 未使用・T9a ⑤）。

    model は PROXY_MODEL（「代走」）・usage は 0・usage_ok は False。行は normalize_response で API 経路と同じ検証を通す。
    prompt_sha256 は prompts/<docID>.prompt.txt から取る（実 API で読み直す時に「代走の結果」として上書き対象になる）。
    prompt.txt が今の入力から作るプロンプトと違えば warnings に残す（古い指名・古い型で読んだ応答の取り込みに気づく）。

    Returns:
        {"converted": [...], "missing": [...]（応答なし）, "errors": ["docID: 理由", ...]}
    """
    docs = load_sections(code4, data_dir)
    if not docs:
        raise SystemExit(f"sections.json が無い: {data_dir / code4}/*.sections.json（先に scripts/yuho_fetch.py）")
    base_id = base_doc_id(docs)
    targets_doc = load_targets(code4, out_dir)
    pdir = out_dir / code4 / "prompts"
    agent_tokens = agent_tokens or {}
    converted: List[str] = []
    missing: List[str] = []
    errors: List[str] = []
    for sec in docs:
        doc_id = sec["doc_id"]
        resp_path = pdir / f"{doc_id}.response.json"
        if not resp_path.exists():
            missing.append(doc_id)
            print(f"{doc_id}: no response {resp_path}")
            continue
        mode, targets = doc_mode(sec, base_id, targets_doc)
        if mode is None:
            errors.append(f"{doc_id}: targets.json が無いので指名読みの応答を検証できない（先に --make-targets）")
            print(f"{doc_id}: skip（targets.json が無い）")
            continue
        raw = extract_json_array(resp_path.read_text(encoding="utf-8"))
        error = None if raw is not None else "response に JSON 配列が無い"
        rows, figures, warnings = normalize_response(raw or [], mode=mode, targets=targets)
        prompt_file = pdir / f"{doc_id}.prompt.txt"
        prompt_text = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else None
        if prompt_text is None:
            warnings.append("prompt.txt が無い（prompt_sha256 は null）")
        else:
            current, _ = make_prompt(sec, data_dir, max_ir_chars, mode, targets)
            if current != prompt_text:
                warnings.append("prompt.txt が今の入力から作るプロンプトと違う（古い型・古い指名で読んだ応答の可能性）")
        result = {"rows": rows, "segment_figures": figures, "warnings": warnings,
                  "usage": {"input_tokens": 0, "output_tokens": 0}, "usage_ok": False, "error": error,
                  "attempts": 1, "stop_reason": "subagent"}
        record = _record(sec, mode, PROXY_MODEL, result,
                         len(prompt_text) if prompt_text is not None else None,
                         hashlib.sha256(prompt_text.encode("utf-8")).hexdigest() if prompt_text is not None else None,
                         (find_ir_text(code4, sec, data_dir) or Path("")).name or None)
        record["subagent_tokens_total"] = agent_tokens.get(doc_id)
        out_path = out_dir / code4 / f"{doc_id}.read.json"
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        converted.append(doc_id)
        if error:
            errors.append(f"{doc_id}: {error}")
        print(f"{doc_id} mode={mode} rows={len(rows)} segment_figures={len(figures)} warnings={len(warnings)} "
              f"error={error} prompt_chars={record['prompt_chars']} -> {out_path}")
    print(f"{code4} converted={len(converted)} missing={len(missing)} errors={len(errors)}")
    return {"converted": converted, "missing": missing, "errors": errors}


def main(argv: Optional[List[str]] = None) -> int:
    """CLI。1 件でも `error` ありなら 1 を返す（read.json は保存したまま）。鍵なし・sections.json なしは SystemExit(1)。

    基準期は基準期の型で読む。後の期は targets.json がある時だけ指名読みで読み、無ければスキップして表示する。
    """
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--code", required=True, help="証券コード 4 桁（例: 2802）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="prompt_chars と概算トークンだけ出し API を呼ばない")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--max-ir-chars", type=int, default=20000)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--force", action="store_true",
                    help="既存の read.json があっても呼び直す（--make-targets では既存の targets.json を作り直す）")
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--make-targets", action="store_true", help="基準期の read.json から targets.json を作る")
    action.add_argument("--dump-prompts", action="store_true", help="代走用に prompts/<docID>.prompt.txt を書く（API なし）")
    action.add_argument("--import-responses", action="store_true",
                        help="代走の prompts/<docID>.response.json を read.json にする（API なし）")
    ap.add_argument("--agent-tokens", default=None,
                    help='--import-responses 用: 代走の消費トークン {"<docID>": n, ...}（JSON 文字列・任意）')
    args = ap.parse_args(argv)

    code4 = args.code.zfill(4)
    if args.make_targets:
        make_targets(code4, args.data_dir, args.out_dir, force=args.force)
        return 0
    if args.dump_prompts:
        dump_prompts(code4, args.data_dir, args.out_dir, args.max_ir_chars)
        return 0
    if args.import_responses:
        tokens = json.loads(args.agent_tokens) if args.agent_tokens else None
        res = import_responses(code4, args.data_dir, args.out_dir, args.max_ir_chars, agent_tokens=tokens)
        if res["errors"]:
            print("error docs: " + " | ".join(res["errors"]), file=sys.stderr)
            return 1
        return 0

    docs = load_sections(code4, args.data_dir)
    if not docs:
        raise SystemExit(f"sections.json が無い: {args.data_dir / code4}/*.sections.json（先に scripts/yuho_fetch.py）")
    base_id = base_doc_id(docs)
    targets_doc = load_targets(code4, args.out_dir)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    total_in = total_out = 0
    errors: List[str] = []
    skipped: List[str] = []
    for doc in docs:
        mode, targets = doc_mode(doc, base_id, targets_doc)
        if mode is None:
            skipped.append(doc["doc_id"])
            continue
        info = read_one(doc, args.model, api_key, args.data_dir, args.out_dir, args.dry_run,
                        args.max_ir_chars, args.max_tokens, force=args.force, mode=mode, targets=targets)
        if info.get("usage"):
            total_in += info["usage"]["input_tokens"]
            total_out += info["usage"]["output_tokens"]
        if info.get("error"):
            errors.append(f"{info['doc_id']}: {info['error'][:120]}")
    if skipped:
        print(f"skipped（targets.json が無いので後の期は読まない・基準期を読んだら --make-targets）: " + " ".join(skipped))
    if not args.dry_run:
        print(f"total docs={len(docs) - len(skipped)} input_tokens={total_in} output_tokens={total_out} "
              f"errors={len(errors)}")
    if errors:
        print("error docs: " + " | ".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
