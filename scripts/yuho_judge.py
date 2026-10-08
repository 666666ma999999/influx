#!/usr/bin/env python3
"""有報の数値 facts（sections.json）と Claude の候補行（read.json）を突き合わせ、期ごとに需要／期待／未判定を付ける（P-INF-34 工程④）。

入力: `data/edinet/yuho/<code4>/<docID>.sections.json`（3 期分）＋ `output/yuho/<code4>/<docID>.read.json`（同じ docID）
出力: `output/yuho/<code4>/products.json` と `summary.md`（表＋費用の式と実測）

判定（叩き台 §5・期 t のラベルは期 t の有報の値だけで決める＝D5）:
  cond_a = share_pct ≥ 30（利益比を優先・区分利益が無い時だけ売上比・どちらも無ければ None）
  cond_b = 区分売上が前期比 +5% 以上 かつ 区分利益が増
  cond_c = 連結の粗利率（売上総利益 ÷ 売上高）が前期比で下がっていない。連結の粗利が無い会社は "判定不能"＝条件 1・2 だけで判定し
           根拠欄（judge_note）に「粗利なし・2 条件」（T9b ⑤・P-INF-36 ③a）
  segment_label = 区分の判定: 3 つ True→需要／expectation→期待／どれか None→未判定／それ以外→需要条件不成立
  label         = 品目向け（区分の判定を無限定に転用しない・Codex 2 回目 Critical 1・2・4）:
                  新しく出た品目の、出る前の期→対象外（新設前）／当期に候補行なし→未判定（当期候補なし）／
                  指名したが当期の資料に無い（status=not_disclosed）→未判定（開示なし）／
                  KPI が数値でない→根拠の揃った期待なら 期待・無ければ 未判定（KPI 非数値）／区分で成立→需要（区分）／
                  expectation＋根拠（経営方針・MD&A・説明会資料の本文に品目名つきの引用）→期待／
                  区分の数値が欠ける→未判定（区分数値なし）／
                  それ以外→需要条件不成立（区分）

T9（2026-10-08 P-INF-37 ①a②a・P-INF-36 ②〜④a）:
  品目の束ね鍵= target_id（無ければ targets.json で属性から引く）・それも無ければ (item, kpi_name, geo, value_type, value_basis)。
  区分は期ごとに持つ（区分の再編に対応・後の期の区分で前の期を埋めない）。単一セグメントの証拠がある会社だけ連結を区分とみなす（構成比 100%・区分 0 だけでは単一とみなさない）。read.json に segment_figures
  （本文のセグメント表の読み）があれば、その期は XBRL の区分の数字の代わりに使う（出所=「本文の表（読み）」）。

T10（2026-10-08 P-INF-38 c）:
  a 区分の旧→新: config/yuho_segment_map.json（任意）で、行の区分が当期の区分に無い時だけ引き直す（根拠欄に「区分名の対応表」）。
    split=true（区分の分割）は引かない（根拠欄に「区分の分割＝対応表で引かない」）
  b 出所 2 本: 品目ごとに source_complete（有報の節だけ）と source_complete_any（有報の節 か 説明会資料＋引用）・--all に
    fill_rate_yuho（＝従来の fill_rate）と fill_rate_any
  c 概数・範囲: read の value_approx／kpi_value_text を periods に載せ、--all に概数の件数
  d 期待の文: expect_quote／expect_section があればそれで期待を照合・無ければ従来の quote／quote_section
  e 追加品目: targets.json の added_from（--extend で足した品目）は足した期より前を 対象外（新設前）・足した期は added_rows の行

使い方:
    python scripts/yuho_judge.py --code 2802 [--price-in 3.0 --price-out 15.0]   # 単価は USD / 100 万トークン
    python scripts/yuho_judge.py --code 2802 --segment-map config/yuho_segment_map.json   # 既定パス（無ければ対応表なし）
    python scripts/yuho_judge.py --all   # out_dir/*/products.json を全社集計→ trial_summary.json / trial_summary.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yuho_read import IR_SECTION_KEY, parse_bool, parse_number  # noqa: E402  節キー・真偽値・数値の読み方は工程③と 1 箇所

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = PROJECT_ROOT / "data" / "center_pin" / "center_pin.jsonl"
DEFAULT_SEGMENT_MAP = PROJECT_ROOT / "config" / "yuho_segment_map.json"   # T10a（無ければ対応表なし＝従来どおり）
NOTE_SEGMENT_MAP = "区分名の対応表"
NOTE_SEGMENT_SPLIT = "区分の分割＝対応表で引かない"
NOTE_SEGMENT_AMBIGUOUS = "区分名の対応表で行き先が 1 つに決まらない＝引かない"
NOTE_SEGMENT_CHAIN_STALL = "区分名の対応表で当期の区分に届かない＝引かない"
DEFAULT_DATA_DIR = Path("data/edinet/yuho")
DEFAULT_OUT_DIR = Path("output/yuho")
SHARE_THRESHOLD = 30.0
SALES_GROWTH_MIN = 0.05

LABEL_DEMAND = "需要"
LABEL_EXPECT = "期待"
LABEL_UNDECIDED = "未判定"
LABEL_NOT_MET = "需要条件不成立"
# 品目向けのラベル（語は統括の既定で固定・2026-10-08）
LABEL_DEMAND_SEG = "需要（区分）"
LABEL_NOT_MET_SEG = "需要条件不成立（区分）"
LABEL_UNDECIDED_NO_ROW = "未判定（当期候補なし）"
LABEL_UNDECIDED_KPI_NAN = "未判定（KPI 非数値）"
LABEL_UNDECIDED_SEG = "未判定（区分数値なし）"
LABEL_UNDECIDED_NOT_DISCLOSED = "未判定（開示なし）"   # 指名した品目の数字が当期の資料に無い（T9b ②）
LABEL_OUT_OF_SCOPE = "対象外（新設前）"               # 新しく出た品目の、出る前の期＝分母から除く（T9b ②）
# 「期待」の根拠に使える節（vault §5 の経営方針に、P-INF-37 ①a で MD&A と説明会資料を追加・T9b ③）
EXPECT_SECTIONS = ("business_policy", "mdna", IR_SECTION_KEY)
EXPECT_SECTION_NAMES = {"business_policy": "経営方針", "mdna": "MD&A", IR_SECTION_KEY: "説明会資料"}
# 「期待」の引用に要る語（成長事業と明記した文か）。どれも無い引用は期待の根拠にしない（Codex 4 回目 Critical 1）
EXPECT_KEYWORDS = ("成長", "重点", "注力", "拡大", "強化", "育成")

BASIS_PROFIT = "利益比"
BASIS_SALES = "売上比"
BASIS_SINGLE = "単一セグメント（連結を区分とみなす）"   # P-INF-36 ②a
BASIS_NO_SEGMENTS = "セグメント抽出なし"               # 区分の数字が無く、単一セグメントの証拠も無い（または連結の数字も無い）
# 単一セグメントの証拠になる本文の書き方（segment_notes の節だけを見る・Codex T9 Critical 3）
SINGLE_SEGMENT_PATTERNS = (r"単一の?報告セグメント", r"単一セグメント", r"報告セグメントは.{0,20}単一", r"セグメントが単一")
# 単一セグメントの記載でも「当期の会社が単一」と言っていない文（過年度・変更・否定）は証拠にしない（Codex T9 R2 Critical）
SINGLE_SEGMENT_PAST_MARKERS = (r"前連結会計年度", r"前事業年度", r"前期", r"従来", r"であったため", r"でしたが", r"まで", r"変更")
# セグメント情報の本文がこれらを含む時は、当期に複数区分へ移った可能性がある＝本文からの単一判定をしない
SINGLE_SEGMENT_CHANGE_MARKERS = (r"当連結会計年度より", r"当事業年度より", r"報告セグメントを変更", r"報告セグメントの変更",
                                 r"[2-9２-９二三四五六七八九]つを報告セグメント", r"報告セグメントとして")


def _current_single_sentence(text: str) -> bool:
    """文単位で見て、過年度・変更・否定の印が無い「単一セグメント」の文が 1 つ以上あるか。"""
    for sent in re.split(r"[。\n]", text):
        if any(re.search(p, sent) for p in SINGLE_SEGMENT_PATTERNS) and not any(
                re.search(m, sent) for m in SINGLE_SEGMENT_PAST_MARKERS) and not re.search(r"ではな|でない|ありません", sent):
            return True
    return False


def _segment_change_sentence(text: str) -> bool:
    """「セグメント」を含む同じ文に変更の印がある時だけ、当期に区分が変わった疑いとみなす。

    製品分類の変更（例: 任天堂「当連結会計年度より、従来の…の区分を上記に変更」）は報告セグメントの変更ではないので数えない（Codex T9 R3 Critical）。
    単一セグメントを当期として言い切っている文そのものは変更の文に数えない。
    """
    for sent in re.split(r"[。\n]", text):
        if "セグメント" in sent and any(re.search(m, sent) for m in SINGLE_SEGMENT_CHANGE_MARKERS) \
                and not _current_single_sentence(sent):
            return True
    return False
BASIS_NEGATIVE = "赤字のため構成比を出せない"
BASIS_UNIT_UNKNOWN = "単位不明で構成比を出せない"     # segment_figures の単位が読めず連結と比べられない

COND_UNJUDGEABLE = "判定不能"         # cond_c: 連結の粗利が無い会社（T9b ⑤）
NOTE_NO_GROSS = "粗利なし・2 条件"

SEGMENT_SOURCE_XBRL = "有報 XBRL"
SEGMENT_SOURCE_READ = "本文の表（読み）"   # read.json の segment_figures（T9b ⑥）
SEGMENT_SOURCE_CONSOLIDATED = "連結（区分とみなす）"   # 区分 0・単一セグメント（T9b ④）
THREE_PERIODS = 3   # 3 期揃い率の「3 期」（T9b ⑧）

STATUS_NEW = "new"
STATUS_FOUND = "found"
STATUS_NOT_DISCLOSED = "not_disclosed"

# segment_figures の単位 → 円の倍率（長い語から照合する）
UNIT_SCALE = (("十億円", 1e9), ("百万円", 1e6), ("千円", 1e3), ("億円", 1e8), ("円", 1.0))
# 連結の facts に同じ種類の分母が無い区分利益（当期利益・税引前利益）。営業利益で割らない（種類違いの比を作らない）
PROFIT_KIND_OTHER = ("当期利益", "純利益", "税引前", "税前", "NetIncome", "ProfitBeforeTax", "ProfitLossBeforeTax")

SOURCE_KIND_YUHO = "有報"
SOURCE_KIND_IR = "説明会資料"

GRANULARITY_SEGMENT = "区分"
GRANULARITY_APPLIED = "区分比を品目に適用（確証に使わない）"

SHARE_DIGITS = 1        # products.json / summary に出す構成比の桁（判定は丸め前の値で行う）
MARGIN_DIGITS = 4       # 同・粗利率
PROXY_MARK = "代走"     # read.json の model にこれを含み usage.input_tokens=0 なら「API 未実行」＝費用未測定


# ---------------------------------------------------------------- 数値まわり
def share_pct(seg_profit: Optional[float], consolidated_profit: Optional[float],
              digits: Optional[int] = None) -> Optional[float]:
    """区分の値 ÷ 連結の値 × 100。分母・分子が無い／分母 ≤0（赤字・0）／分子 <0（区分が赤字）なら None。

    既定は丸めない（判定に使う）。表示用には digits=SHARE_DIGITS を渡す（2996/10000=29.96 を 30.0 に丸めて
    cond_a=True にしない＝Codex Critical 1）。

    Args:
        seg_profit: 区分の利益（または売上）
        consolidated_profit: 連結の利益（または売上）＝分母は連結で固定
        digits: 丸める桁。None なら生の値

    Returns:
        構成比 %（例: 143036 / 181163 → 78.955…・digits=1 なら 79.0）。計算できなければ None
    """
    if seg_profit is None or consolidated_profit is None:
        return None
    try:
        denom = float(consolidated_profit)
        num = float(seg_profit)
    except (TypeError, ValueError):
        return None
    if denom <= 0 or num < 0:
        return None   # 負の分母・分子では構成比が意味を持たない（Codex 2 回目 Warning）
    value = num / denom * 100.0
    return round(value, digits) if digits is not None else value


def _is_negative_pair(numerator: Optional[float], denominator: Optional[float]) -> bool:
    """分子・分母が両方あるのに符号のせいで構成比を出せない（分母 ≤0 か分子 <0）か。欠測は False。"""
    if numerator is None or denominator is None:
        return False
    try:
        return float(denominator) <= 0 or float(numerator) < 0
    except (TypeError, ValueError):
        return False


def gross_margin(gross_profit: Optional[float], net_sales: Optional[float],
                 digits: Optional[int] = None) -> Optional[float]:
    """粗利率 = 売上総利益 ÷ 売上高。どちらか無い／売上 0 なら None。既定は丸めない（表示用は digits=MARGIN_DIGITS）。"""
    if gross_profit is None or net_sales is None:
        return None
    try:
        if float(net_sales) == 0:
            return None
        value = float(gross_profit) / float(net_sales)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if digits is not None else value


def _rounded(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def _pick_ctx(d: Optional[dict], prefix: str) -> Optional[dict]:
    """ctx 名が `CurrentYearDuration` 等ちょうどの物を優先し、無ければ prefix で始まる最初のキーを返す。

    yuho_fetch の実出力はセグメントだけ `{"label", "ctx": {<ctx名>: {...}}}` と 1 段深い（連結は ctx 名が直下）ので、
    `ctx` キーがあればその下を見る（両方の形を受ける）。
    """
    if not d:
        return None
    if isinstance(d.get("ctx"), dict):
        d = d["ctx"]
    exact = f"{prefix}Duration"
    if exact in d and isinstance(d[exact], dict):
        return d[exact]
    for key, val in d.items():
        if key.startswith(prefix) and isinstance(val, dict):
            return val
    return None


def facts_slice(facts: dict, ctx_prefix: str) -> dict:
    """sections.json の facts から 1 コンテキスト分（当期 or 前期）だけを平らに取り出す。

    Args:
        facts: sections.json["facts"]
        ctx_prefix: "CurrentYear" か "Prior1Year"

    Returns:
        {consolidated:{net_sales, gross_profit, operating_profit, business_profit},
         segments:{member:{label, net_sales, segment_profit}}, profit_measure, single_segment}
        single_segment は sections.json の facts.single_segment をそのまま通す（yuho_fetch はまだ立てない＝None）
    """
    cons = _pick_ctx(facts.get("consolidated"), ctx_prefix) or {}
    segs: Dict[str, dict] = {}
    for member, seg in (facts.get("segments") or {}).items():
        vals = _pick_ctx(seg, ctx_prefix) or {}
        segs[member] = {
            "label": (seg or {}).get("label") or member,
            "net_sales": vals.get("net_sales"),
            "segment_profit": vals.get("segment_profit"),
        }
    return {
        "consolidated": {
            "net_sales": cons.get("net_sales"),
            "gross_profit": cons.get("gross_profit"),
            "operating_profit": cons.get("operating_profit"),
            "business_profit": cons.get("business_profit"),
        },
        "segments": segs,
        "profit_measure": facts.get("profit_measure"),
        "single_segment": facts.get("single_segment"),
    }


def _consolidated_profit(cons: dict, profit_measure: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """区分利益の指標に合わせた連結の分母（事業利益系なら business_profit・それ以外は operating_profit）。

    指標は XBRL の要素名（BusinessProfitIFRS 等）と、本文の表の読み（「事業利益」「営業利益」）の両方を受ける。
    当期利益・税引前利益の区分（HOYA 等）は連結 facts に同じ種類が無い＝(None, None)（営業利益で割らない）。
    """
    pm = str(profit_measure or "")
    if any(k in pm for k in PROFIT_KIND_OTHER):
        return None, None
    biz = "BusinessProfit" in pm or "事業利益" in pm
    order = ("business_profit", "operating_profit") if biz else ("operating_profit", "business_profit")
    for key in order:
        if cons.get(key) is not None:
            return cons[key], key
    return None, None


def _unit_scale(unit: Any) -> Optional[float]:
    """単位の文字列 → 円の倍率（「百万円」→1e6）。読めなければ None。"""
    if not isinstance(unit, str) or not unit.strip():
        return None
    s = unicodedata.normalize("NFKC", unit).replace(" ", "")
    for word, scale in UNIT_SCALE:
        if word in s:
            return scale
    return None


def apply_segment_figures(cur: dict, prev: dict, figures: Any, facts_unit: Any = "円") -> Tuple[dict, dict, bool]:
    """read.json の segment_figures（本文のセグメント表の読み）で、当期・前期の区分の数字を置き換える（T9b ⑥）。

    Args:
        cur: 当期の facts_slice
        prev: 前期の facts_slice
        figures: [{segment, sales_cur, sales_prev, profit_cur, profit_prev, profit_measure, unit, quote}]
        facts_unit: sections.json の facts.unit（連結の数字の単位・既定「円」）

    Returns:
        (当期 slice, 前期 slice, 使ったか)。figures が空・list でなければ元の slice をそのまま返し False。
        区分の値は連結と同じ単位に直す。単位が読めない区分は値をそのまま入れ scale_ok=False（構成比は出さない・
        前期比の cond_b は同じ単位どうしなので出せる）。
    """
    rows = [f for f in figures if isinstance(f, dict) and _clean(f.get("segment")) and f.get("single_segment") is not True
            ] if isinstance(figures, list) else []   # 単一セグメントの宣言行は区分の数字にしない
    if not rows:
        return cur, prev, False
    base = _unit_scale(facts_unit or "円") or 1.0
    segs_cur: Dict[str, dict] = {}
    segs_prev: Dict[str, dict] = {}
    for i, f in enumerate(rows):
        scale = _unit_scale(f.get("unit"))
        factor = scale / base if scale else 1.0

        def conv(v: Any) -> Optional[float]:
            n = parse_number(v)
            return None if n is None else n * factor

        member = f"read:{i}:{_clean(f.get('segment'))}"
        common = {"label": _clean(f.get("segment")), "profit_measure": _clean(f.get("profit_measure")),
                  "scale_ok": scale is not None, "quote": _clean(f.get("quote"))}
        segs_cur[member] = dict(common, net_sales=conv(f.get("sales_cur")), segment_profit=conv(f.get("profit_cur")))
        segs_prev[member] = dict(common, net_sales=conv(f.get("sales_prev")), segment_profit=conv(f.get("profit_prev")))
    measure = next((s["profit_measure"] for s in segs_cur.values() if s.get("profit_measure")), None)
    return (dict(cur, segments=segs_cur, profit_measure=measure or cur.get("profit_measure"), segment_source=SEGMENT_SOURCE_READ),
            dict(prev, segments=segs_prev, profit_measure=measure or prev.get("profit_measure"),
                 segment_source=SEGMENT_SOURCE_READ),
            True)


def single_segment_evidence(doc: dict, read: Optional[dict]) -> Optional[str]:
    """その期の会社が単一セグメントである証拠（無ければ None）。区分 0 だけでは証拠にしない（Codex T9 Critical 3）。

    証拠（どれか 1 つ）:
    - sections.json の facts.single_segment が True（yuho_fetch が立てた時）
    - sections.json の segment_notes（セグメント情報）の本文に SINGLE_SEGMENT_PATTERNS の書き方がある
    - read.json の single_segment が True で、segment_figures の宣言行の quote が当期の節本文に実在し「単一」を含む
      （LLM の自己申告だけでは立てない）

    Returns:
        証拠の 1 句（根拠欄用）か None
    """
    facts = doc.get("facts") or {}
    if facts.get("single_segment") is True:
        return "facts.single_segment"
    sections = doc.get("sections") or {}
    notes = _squash((sections.get("segment_notes") or {}).get("text"))
    if notes and not _segment_change_sentence(notes) and _current_single_sentence(notes):
        return "セグメント情報の本文に単一セグメントの記載"
    read = read or {}
    if read.get("single_segment") is True:
        all_text = "".join(_squash((sec or {}).get("text")) for sec in sections.values())
        for fig in read.get("segment_figures") or []:
            if not (isinstance(fig, dict) and fig.get("single_segment") is True):
                continue
            q = _squash(fig.get("quote"))
            if q and "単一" in q and q in all_text and _current_single_sentence(q) \
                    and not _segment_change_sentence(notes):
                return "読みの宣言（本文に単一セグメントの引用あり）"
    return None


# ---------------------------------------------------------------- セグメント名の突合
def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    return re.sub(r"[\s・･,、/／()（）「」\-_]", "", s)


def find_segment_exact(segments: dict, segment_name: Optional[str]) -> Optional[str]:
    """区分名が当期の区分の表記と完全一致（正規化後）する member（部分一致はしない）。"""
    target = _norm(segment_name)
    if not target or not segments:
        return None
    for member, seg in segments.items():
        if _norm((seg or {}).get("label")) == target:
            return member
    return None


def find_segment_member(segments: dict, segment_name: Optional[str]) -> Optional[str]:
    """LLM 行の segment 名から facts.segments の member キーを引く（完全一致→部分一致→member 名）。

    Args:
        segments: facts["segments"]（member → {label, ...}）
        segment_name: read.json の行の segment

    Returns:
        member キー。引けなければ None
    """
    target = _norm(segment_name)
    if not target or not segments:
        return None
    labels = {m: _norm((s or {}).get("label")) for m, s in segments.items()}
    for member, label in labels.items():
        if label and label == target:
            return member
    for member, label in labels.items():
        if label and (label in target or target in label):
            return member
    for member in segments:
        if target in _norm(member):
            return member
    return None


def load_segment_map(path: Optional[Path]) -> Dict[str, List[dict]]:
    """config/yuho_segment_map.json（区分の旧→新の対応表・T10a）を読み、形を検証する。

    形: {"<code4>": [{"effective_period_end": "YYYY-MM-DD", "old": "<旧区分名>", "new": "<新区分名>",
                     "split": false, "evidence": {"doc_id": "...", "quote": "..."}}]}
    根拠（evidence.doc_id・evidence.quote）の無い行は載せない約束＝検証で落とす（推測で埋めない）。

    Returns:
        code4 → 行の list。ファイルが無ければ {}（従来どおり）

    Raises:
        SystemExit: JSON として読めない・形が違う（どの行の何が違うかを並べる）
    """
    if path is None or not Path(path).exists():
        return {}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"区分名の対応表が JSON として読めない: {path}: {e}")
    if not isinstance(doc, dict):
        raise SystemExit(f"区分名の対応表の形が不正（先頭は {{code4: [...]}} の dict）: {path}")
    errors: List[str] = []
    out: Dict[str, List[dict]] = {}
    for code, entries in doc.items():
        if not re.fullmatch(r"\d{4}", str(code)):
            errors.append(f"{code}: キーは 4 桁の証券コード")
            continue
        if not isinstance(entries, list):
            errors.append(f"{code}: 値は行の list")
            continue
        for i, e in enumerate(entries):
            where = f"{code}[{i}]"
            if not isinstance(e, dict):
                errors.append(f"{where}: 行は dict")
                continue
            eff = e.get("effective_period_end")
            if not (isinstance(eff, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", eff)):
                errors.append(f"{where}: effective_period_end は YYYY-MM-DD（{eff!r}）")
            for k in ("old", "new"):
                if not _clean(e.get(k)):
                    errors.append(f"{where}: {k} が空")
            if _clean(e.get("old")) and _norm(e.get("old")) == _norm(e.get("new")):
                errors.append(f"{where}: old と new が同じ")
            if "split" in e and not isinstance(e["split"], bool):
                errors.append(f"{where}: split は true/false（{e['split']!r}）")
            ev = e.get("evidence")
            if not (isinstance(ev, dict) and _clean(ev.get("doc_id")) and _clean(ev.get("quote"))):
                errors.append(f"{where}: evidence.doc_id と evidence.quote（有報の引用）が要る")
        out[str(code)] = entries
    if errors:
        raise SystemExit(f"区分名の対応表の形が不正: {path}\n  " + "\n  ".join(errors))
    return out


def resolve_segment_by_map(segments: dict, segment_name: Optional[str], period_end: Optional[str],
                           entries: Optional[List[dict]]) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """行の区分名が当期の区分に無い時だけ、対応表（T10a）で当期の区分名に引き直す。

    - 当期の期末 ≥ effective_period_end の行: old → new（改名・統合の後の期）
    - 当期の期末 < effective_period_end の行: new → old（改名の前の期に、新しい名前で書かれた行）
    - 改名が続く時は対応表を辿る（最大で行の数だけ）
    - split=true の行（区分の分割）は引かない＝note に「区分の分割＝対応表で引かない」・member は None
    - 行き先が 2 つ以上になる（統合の逆引き＝新 1 つ ← 旧 2 つ 等）時は引かない＝note に NOTE_SEGMENT_AMBIGUOUS

    照合の順（Codex T10 R1 Critical 1）: ①当期の区分と完全一致 → それ ②行の区分名が対応表の旧名・新名に出る会社は
    対応表の規則（分割・行き先が 1 つに決まらない→引かない）を部分一致より先に通す ③対応表に出ない名前（またはこの期に
    当たる対応表の行が無い）だけ従来の部分一致。部分一致が分割の片方（"Green Energy And Mobility" ⊃ "Mobility"）を
    黙って拾うのを防ぐ。

    Returns:
        (member or None, note or None, 使った対応表の行 or None)。引く必要が無い（当期の区分で引ける・区分名が無い・
        対応表が無い）時は (find_segment_member の結果, None, None)
    """
    exact = find_segment_exact(segments, segment_name)
    if exact is not None or not segment_name or not segments:
        return exact, None, None
    names = {_norm(e.get(k)) for e in (entries or []) if isinstance(e, dict) for k in ("old", "new")}
    if _norm(segment_name) not in names:
        return find_segment_member(segments, segment_name), None, None
    member, note, entry = _resolve_by_map_rules(segments, segment_name, period_end, entries or [])
    if member is None and note is None:
        # 対応表に名前はあるが、この期に当たる行が無い（改名前の期に旧名 等）＝従来の部分一致
        return find_segment_member(segments, segment_name), None, None
    return member, note, entry


def _resolve_by_map_rules(segments: dict, segment_name: str, period_end: Optional[str],
                          entries: List[dict]) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """対応表の行を辿って当期の区分を引く（resolve_segment_by_map の本体）。最初の名前にこの期に当たる行が無ければ (None, None, None)。

    各段で同じ順（Codex T10 R2 Critical 1）: 行き先の名前が ①当期の区分と完全一致 → 引く ②今使った行の他にも対応表に出る
    名前 → 次の段で対応表の規則（分割・行き先が 1 つに決まらない → 止める）③それ以外（対応表には今の行の行き先としてしか
    出ない名前）だけ部分一致。途中の段で分割・曖昧なら
    引かない（注記つき）。途中の段で当期の区分に届かなければ引かない（NOTE_SEGMENT_CHAIN_STALL）。
    """
    end = period_end or ""
    name = segment_name
    seen = {_norm(name)}
    first_entry: Optional[dict] = None
    for _ in range(len(entries) + 1):
        hits = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            eff = str(e.get("effective_period_end") or "")
            if end >= eff and _norm(e.get("old")) == _norm(name):
                hits.append((e, _clean(e.get("new"))))
            elif end < eff and _norm(e.get("new")) == _norm(name):
                hits.append((e, _clean(e.get("old"))))
        if not hits:
            if first_entry is None:
                return None, None, None
            return None, NOTE_SEGMENT_CHAIN_STALL, first_entry
        split_hit = next((e for e, _ in hits if e.get("split") is True), None)
        if split_hit is not None:
            return None, NOTE_SEGMENT_SPLIT, split_hit
        if len({_norm(to) for _, to in hits}) > 1:
            # 統合の逆引き（新 1 つ ← 旧 2 つ）など行き先が 1 つに決まらない＝引かない（推測で 1 つに寄せない）
            return None, NOTE_SEGMENT_AMBIGUOUS, hits[0][0]
        e, to = hits[0]
        first_entry = first_entry or e
        m = find_segment_exact(segments, to)
        if m is not None:
            return m, f"{NOTE_SEGMENT_MAP}（{segment_name}→{to}）", e
        other_names = {_norm(x.get(k)) for x in entries if isinstance(x, dict) and x is not e for k in ("old", "new")}
        if _norm(to) not in other_names:
            # 行き先が今の行の他に対応表に出ない名前の時だけ部分一致（出る名前は次の段で規則を通す）
            m = find_segment_member(segments, to)
            if m is not None:
                return m, f"{NOTE_SEGMENT_MAP}（{segment_name}→{to}）", e
            return None, NOTE_SEGMENT_CHAIN_STALL, first_entry
        if _norm(to) in seen:
            return None, NOTE_SEGMENT_CHAIN_STALL, first_entry
        seen.add(_norm(to))
        name = to
    return None, NOTE_SEGMENT_CHAIN_STALL, first_entry


# ---------------------------------------------------------------- 期ごとの判定
def judge_period(prev_facts: Optional[dict], cur_facts: dict, seg_member: Optional[str]) -> Dict[str, Any]:
    """1 期分の 3 条件を計算する（ラベルは付けない・label_of が付ける）。

    Args:
        prev_facts: 前期の facts_slice（無ければ None → cond_b / cond_c は None）
        cur_facts: 当期の facts_slice
        seg_member: facts.segments の member キー。None なら share=None（segments が空でも同じ）

    単一セグメント（share=100・連結の値を区分に当てる）は `single_segment is True` の時だけ（P-INF-36 ②a は
    「真に単一セグメントの会社」への裁定）。build_products が単一の証拠（facts.single_segment・segment_notes の本文・
    read.json の single_segment 宣言）を見て立てる。区分 0 でも証拠が無ければ share=None・share_basis="セグメント抽出なし"
    （US GAAP 等で XBRL に区分の数字が無い複数区分の会社を 100% にしない＝Codex T9 Critical 3）。

    cond_c: 前期が無ければ None。当期の連結に粗利（gross_profit）が無い会社は "判定不能"＝label_of が条件 1・2 だけで
    判定し、judge_note に「粗利なし・2 条件」（T9b ⑤）。

    判定（cond_a／cond_c）は丸め前の値で行い、返す share_pct／gross_margin は表示用に丸めた値（Codex Critical 1）。

    Returns:
        {seg_sales, seg_profit, profit_measure, consolidated_profit, consolidated_measure,
         share_pct, share_basis, gross_margin, gross_margin_prev, cond_a, cond_b, cond_c, judge_note}
    """
    cons = cur_facts.get("consolidated") or {}
    segs = cur_facts.get("segments") or {}
    profit_measure = cur_facts.get("profit_measure")
    if seg_member and seg_member in segs and segs[seg_member].get("profit_measure"):
        profit_measure = segs[seg_member]["profit_measure"]   # 本文の表の読みは区分ごとに指標を持つ
    cons_profit, cons_measure = _consolidated_profit(cons, profit_measure)

    seg_sales: Optional[float] = None
    seg_profit: Optional[float] = None
    prev_sales: Optional[float] = None
    prev_profit: Optional[float] = None
    share: Optional[float] = None
    basis: Optional[str] = None

    if seg_member and seg_member in segs:
        seg_sales = segs[seg_member].get("net_sales")
        seg_profit = segs[seg_member].get("segment_profit")
        prev_segs = (prev_facts or {}).get("segments") or {}
        if seg_member in prev_segs:
            prev_sales = prev_segs[seg_member].get("net_sales")
            prev_profit = prev_segs[seg_member].get("segment_profit")
        if segs[seg_member].get("scale_ok") is False:
            basis = BASIS_UNIT_UNKNOWN   # 単位が読めない本文の表＝連結と比べない（cond_b は同じ単位どうしで出す）
        else:
            share = share_pct(seg_profit, cons_profit)
            if share is not None:
                basis = BASIS_PROFIT
            elif _is_negative_pair(seg_profit, cons_profit):
                # 区分か連結が赤字: 売上比に逃がさず share=None（cond_a=None→需要にはしない）
                basis = BASIS_NEGATIVE
            else:
                share = share_pct(seg_sales, cons.get("net_sales"))
                basis = BASIS_SALES if share is not None else None
    elif cur_facts.get("single_segment") is True:
        # 単一セグメントの証拠がある会社（P-INF-36 ②a）: 連結の値をそのまま区分に当て share=100。
        # 区分 0 だけでは単一とみなさない（US GAAP 等の複数区分の会社が 100% になる＝Codex T9 Critical 3）
        seg_sales = cons.get("net_sales")
        seg_profit = cons_profit
        prev_cons = (prev_facts or {}).get("consolidated") or {}
        prev_sales = prev_cons.get("net_sales")
        prev_profit, _ = _consolidated_profit(prev_cons, profit_measure)
        if seg_sales is not None or seg_profit is not None:
            share, basis = 100.0, BASIS_SINGLE
        else:
            basis = BASIS_NO_SEGMENTS
    elif not segs:
        basis = BASIS_NO_SEGMENTS   # 区分の数字が無く単一の証拠も無い＝share=None（未判定（区分数値なし）へ）

    cond_a = None if share is None else share >= SHARE_THRESHOLD

    cond_b: Optional[bool]
    if None in (seg_sales, seg_profit, prev_sales, prev_profit) or not prev_sales:
        cond_b = None
    else:
        cond_b = (float(seg_sales) / float(prev_sales) - 1.0 >= SALES_GROWTH_MIN) and float(seg_profit) > float(prev_profit)

    gm_cur = gross_margin(cons.get("gross_profit"), cons.get("net_sales"))
    prev_cons_all = (prev_facts or {}).get("consolidated") or {}
    gm_prev = gross_margin(prev_cons_all.get("gross_profit"), prev_cons_all.get("net_sales"))
    cond_c: Any
    note: Optional[str] = None
    if prev_facts is not None and cons.get("gross_profit") is None:
        cond_c, note = COND_UNJUDGEABLE, NOTE_NO_GROSS   # 連結の粗利が無い会社（IFRS の医薬品等）
    else:
        cond_c = None if gm_cur is None or gm_prev is None else gm_cur >= gm_prev

    return {
        "seg_sales": seg_sales,
        "seg_profit": seg_profit,
        "profit_measure": profit_measure,
        "consolidated_profit": cons_profit,
        "consolidated_measure": cons_measure,
        "share_pct": _rounded(share, SHARE_DIGITS),
        "share_basis": basis,
        "gross_margin": _rounded(gm_cur, MARGIN_DIGITS),
        "gross_margin_prev": _rounded(gm_prev, MARGIN_DIGITS),
        "cond_a": cond_a,
        "cond_b": cond_b,
        "cond_c": cond_c,
        "judge_note": note,
    }


def label_of(conds: Tuple[Any, Any, Any], expectation: bool) -> str:
    """3 条件と期待フラグからラベルを決める。

    Args:
        conds: (cond_a, cond_b, cond_c)。各 True/False/None。cond_c が "判定不能"（連結の粗利なし）なら
               条件 1・2 だけで判定する（T9b ⑤）
        expectation: 期待の根拠（経営方針・MD&A・説明会資料に品目名つきで成長事業と明記）が揃った行か

    Returns:
        需要／期待／未判定／需要条件不成立
    """
    conds = tuple(conds)
    if len(conds) == 3 and conds[2] == COND_UNJUDGEABLE:
        conds = conds[:2]
    if all(c is True for c in conds):
        return LABEL_DEMAND
    if expectation:
        return LABEL_EXPECT
    if any(c is None for c in conds):
        return LABEL_UNDECIDED
    return LABEL_NOT_MET


def _squash(value: Any) -> str:
    """引用の照合用: NFKC 正規化して空白をすべて落とす（改行・全角空白の違いで照合を落とさないため）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def _letters(value: Any) -> str:
    """品目名の照合用: _squash に加えて記号（®・括弧・中黒・下線 等）も落とし、文字と数字だけにする。"""
    return re.sub(r"[^\w]|_", "", _squash(value))


def _expect_fields(row: Optional[dict]) -> Tuple[Optional[str], Optional[str]]:
    """期待の照合に使う (引用, 節キー)（T10d）。expect_quote があればそれ（節は expect_section・無ければ quote_section）・
    無ければ従来の quote／quote_section（旧 read.json の後方互換）。"""
    if not row:
        return None, None
    eq = _clean(row.get("expect_quote"))
    if eq:
        return eq, _clean(row.get("expect_section")) or _clean(row.get("quote_section"))
    return _clean(row.get("quote")), _clean(row.get("quote_section"))


def _expect_basis(row: Optional[dict], doc: dict, ir_text: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """「期待」の根拠が揃っているか（Codex 3 回目 Critical 2）。

    引用と節は _expect_fields（T10d: expect_quote／expect_section があればそれ・無ければ quote／quote_section）。

    揃う条件（全部）: expectation が true/false として読めて True／quote_section が EXPECT_SECTIONS（経営方針・MD&A・
    説明会資料＝P-INF-37 ①a）／有報の節なら当期の有報に実在する（element_id あり）・説明会資料なら本文（ir_text）がある／
    quote が空でない／quote がその本文に見つかる（空白無視）／quote に品目名が含まれる（記号無視・別品目の引用を拒否）／
    quote に EXPECT_KEYWORDS のどれかがある（成長事業の明記）。
    LLM の自己申告（expectation=True）だけでは期待にしない。意味の最終判断は人の検収（工程⑤）＝ここは機械で落とせる物を落とす。

    Args:
        row: 当期の LLM 行
        doc: 当期の sections.json
        ir_text: 当期の説明会資料の本文（read.json の ir_source を data/edinet/yuho/<code>/ir/ から読んだ物・無ければ None）

    Returns:
        (揃ったか, 理由の 1 句)。expectation が False の時は (False, None)
    """
    if not row:
        return False, None
    val, ok = parse_bool(row.get("expectation"))
    if not ok:
        return False, f"expectation={row.get('expectation')!r} が true/false として読めない"
    if not val:
        return False, None
    quote, qs = _expect_fields(row)
    if qs not in EXPECT_SECTIONS:
        return False, f"根拠節が経営方針・MD&A・説明会資料でない（{qs}）"
    name = EXPECT_SECTION_NAMES[qs]
    if qs == IR_SECTION_KEY:
        text = ir_text
        if not _clean(text):
            return False, "説明会資料の本文が無い"
    else:
        sec = (doc.get("sections") or {}).get(qs) or {}
        if not _clean(sec.get("element_id")):
            return False, f"{name}の節が当期の有報に無い"
        text = sec.get("text")
    if not quote:
        return False, "引用が空"
    if _squash(quote) not in _squash(text):
        return False, f"引用が{name}の本文に見つからない"
    item = _letters(row.get("item"))
    if not item or item not in _letters(quote):
        return False, "引用に品目名が無い（別品目の引用）"
    if not any(k in _squash(quote) for k in EXPECT_KEYWORDS):  # 語の途中の改行・空白は無視
        return False, f"引用に成長事業を示す語（{'／'.join(EXPECT_KEYWORDS)}）が無い"
    return True, f"{name}の本文に品目名つきの引用あり"


def _row_status(row: Optional[dict]) -> Optional[str]:
    """行の status（new／found／not_disclosed）。旧 read.json は無い＝None。"""
    s = _clean((row or {}).get("status"))
    return s.lower() if s else None


def product_label(row: Optional[dict], segment_label: str, kpi_value: Any, expect_ok: bool,
                  out_of_scope: bool = False) -> str:
    """品目向けのラベル。区分の判定（segment_label）を品目に無限定で転用しない（Codex 2 回目 Critical 1・2・4／3 回目 1）。

    順: 新しく出た品目の出る前の期（out_of_scope）→ 対象外（新設前）／当期に候補行が無い → 未判定（当期候補なし）／
    行の status が not_disclosed → 未判定（開示なし）／KPI が数値でない → 根拠の揃った期待なら 期待・無ければ 未判定（KPI 非数値）
    （数値の無い行を需要にしない）／需要 3 条件が区分の数値で成立 → 需要（区分）／根拠の揃った期待 → 期待／
    区分の数値が欠けて 3 条件を判定できない → 未判定（区分数値なし）／それ以外 → 需要条件不成立（区分）

    Args:
        row: 当期の LLM 行（無ければ None）
        segment_label: label_of の結果（区分の判定）
        kpi_value: _num 後の KPI 値（数値に読めなければ None）
        expect_ok: _expect_basis の結果（期待の根拠が揃っているか）
        out_of_scope: 新しく出た品目（status=new）の、初出より前の期か（T9b ②・分母から除く）
    """
    if out_of_scope:
        return LABEL_OUT_OF_SCOPE
    if row is None:
        return LABEL_UNDECIDED_NO_ROW
    if _row_status(row) == STATUS_NOT_DISCLOSED:
        return LABEL_UNDECIDED_NOT_DISCLOSED
    if kpi_value is None:
        return LABEL_EXPECT if expect_ok else LABEL_UNDECIDED_KPI_NAN
    if segment_label == LABEL_DEMAND:
        return LABEL_DEMAND_SEG
    if expect_ok:
        return LABEL_EXPECT
    if segment_label == LABEL_UNDECIDED:
        return LABEL_UNDECIDED_SEG
    return LABEL_NOT_MET_SEG


# ---------------------------------------------------------------- 会社 1 社ぶんの組み立て
def _clean(value: Any) -> Optional[str]:
    """文字列列の有効値= str を strip した物。str 以外・空白だけは None（read 側の normalize_rows と同じ基準）。"""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


ATTR_COLUMNS = ("item", "kpi_name", "geo", "value_type", "value_basis")


def _attr_key(row: dict) -> Tuple[str, ...]:
    """品目の属性キー= (item, kpi_name, geo, value_type, value_basis) の正規化（旧 read.json は geo 等が無い＝空）。"""
    return tuple(_norm(_clean(row.get(c))) for c in ATTR_COLUMNS)


def _alias_key(row: dict) -> Tuple[str, ...]:
    """targets.json の引き当て用キー= (segment, item, kpi_name, geo, value_type, value_basis) の正規化。

    targets.json は yuho_read.make_targets が区分を含む 6 列で作る＝引き当ても区分を含める（区分だけ違う 2 件を
    1 件に潰さない・Codex T9 Critical 2）。"""
    return (_norm(_clean(row.get("segment"))),) + _attr_key(row)


def _product_key(row: dict, aliases: Optional[Dict[Tuple[str, ...], str]] = None) -> Tuple[str, ...]:
    """品目のグループキー（T9b ①）= (target_id, item, kpi_name, geo, value_type, value_basis)。

    target_id があれば (target_id, "", …)＝指名した品目は期をまたいで名前が揺れても 1 つに束ねる。行に target_id が無くても
    targets.json（aliases: 区分＋属性キー → target_id）で引ければ同じ束に入れる（基準期の行には target_id が付かないため・
    ふつうは build_products が targets.json の base_rows で行番号から引く＝これは旧 targets.json の予備経路）。
    どちらも無ければ ("", 属性キー…)。区分はキーに入れない（区分の再編 17 社に対応・区分は期ごとに持つ）。
    同じ品目でも KPI ごとに別 product（Codex Critical 3）。
    """
    tid = _clean(row.get("target_id"))
    attr = _attr_key(row)
    if not tid and aliases:
        tid = aliases.get(_alias_key(row))
    if tid:
        return (tid,) + ("",) * len(ATTR_COLUMNS)
    return ("",) + attr


def _target_aliases(targets: Any) -> Dict[Tuple[str, ...], str]:
    """targets.json（list か {"targets": [...]}）から 区分＋属性キー → target_id の辞書を作る（1 対 1）。

    正規化で 2 件以上の target が同じキーになった時は、そのキーを辞書から外す（先頭の 1 件に寄せない＝
    どれの行か決められない物は束ねない・Codex T9 Critical 2）。"""
    if isinstance(targets, dict):
        targets = targets.get("targets")
    out: Dict[Tuple[str, ...], str] = {}
    ambiguous: set = set()
    for t in targets or []:
        if isinstance(t, dict) and _clean(t.get("target_id")):
            key = _alias_key(t)
            if key in out and out[key] != _clean(t["target_id"]):
                ambiguous.add(key)
            out.setdefault(key, _clean(t["target_id"]))
    for key in ambiguous:
        out.pop(key, None)
    return out


def _base_row_map(targets: Any) -> Tuple[Optional[str], Dict[int, str]]:
    """targets.json の base_doc_id と、基準期 read.json の行番号 → target_id（make_targets が書く base_rows）。

    Returns:
        (base_doc_id or None, {行番号: target_id})。base_rows の無い旧 targets.json は (base_doc_id, {})
    """
    if not isinstance(targets, dict):
        return None, {}
    out: Dict[int, str] = {}
    for t in targets.get("targets") or []:
        if not (isinstance(t, dict) and _clean(t.get("target_id"))):
            continue
        for i in t.get("base_rows") or []:
            if isinstance(i, int) and not isinstance(i, bool):
                out[i] = _clean(t["target_id"])
    return _clean(targets.get("base_doc_id")), out


def _added_row_maps(targets: Any) -> Dict[str, Dict[int, str]]:
    """--extend で足した target（T10e）の added_from（docID）→ {その期の read.json の行番号: target_id}。"""
    if not isinstance(targets, dict):
        return {}
    out: Dict[str, Dict[int, str]] = {}
    for t in targets.get("targets") or []:
        if not (isinstance(t, dict) and _clean(t.get("target_id")) and _clean(t.get("added_from"))):
            continue
        m = out.setdefault(_clean(t["added_from"]), {})
        for i in t.get("added_rows") or []:
            if isinstance(i, int) and not isinstance(i, bool):
                m[i] = _clean(t["target_id"])
    return out


def _target_by_id(targets: Any) -> Dict[str, dict]:
    if isinstance(targets, dict):
        targets = targets.get("targets")
    return {_clean(t["target_id"]): t for t in (targets or []) if isinstance(t, dict) and _clean(t.get("target_id"))}


def _is_proxy_read(read: Optional[dict]) -> bool:
    """read.json が Claude Code サブエージェントの代走（API 未実行）か＝ model に「代走」を含み input_tokens が 0。"""
    if not read:
        return False
    u = read.get("usage") or {}
    return int(u.get("input_tokens") or 0) == 0 and PROXY_MARK in str(read.get("model") or "")


def _source_complete(period: dict) -> bool:
    """期の出所が揃っているか= doc_id と element_id（有報の節）が両方とも有効な文字列。

    説明会資料（ir_source）だけの期は揃ったと数えない（Codex 2 回目 Critical 3。IR 由来 KPI を「出所あり」と
    数えるかはオーナー裁定待ち＝それまでは未完として件数だけ summary に出す）。
    """
    src = period.get("source") or {}
    return bool(_clean(src.get("doc_id"))) and bool(_clean(src.get("element_id")))


def _source_complete_any(period: dict) -> bool:
    """T10b: 有報の節で揃う（_source_complete）か、説明会資料の〈資料名（ir_source）＋引用（quote）〉がある期。"""
    if _source_complete(period):
        return True
    src = period.get("source") or {}
    return bool(_clean(src.get("doc_id"))) and bool(_clean(src.get("ir_source"))) and bool(_clean(period.get("quote")))


def _source_ir_only(period: dict) -> bool:
    """出所が説明会資料だけの期= doc_id あり・element_id なし・ir_source あり。"""
    src = period.get("source") or {}
    return bool(_clean(src.get("doc_id"))) and not _clean(src.get("element_id")) and bool(_clean(src.get("ir_source")))


def _source_kind(element_id: Any, ir_source: Any) -> Optional[str]:
    """出所の種類: element_id があれば「有報」・無くて ir_source があれば「説明会資料」・どちらも無ければ None。"""
    if _clean(element_id):
        return SOURCE_KIND_YUHO
    if _clean(ir_source):
        return SOURCE_KIND_IR
    return None


def _share_granularity(row: Optional[dict], share: Optional[float]) -> Optional[str]:
    """構成比の粒度の注記。share が無ければ None・LLM 行の share_scope が「区分」なら "区分"・それ以外は
    「区分比を品目に適用（確証に使わない）」（粒度一致則は仕様側で扱う＝ここでは明記だけ）。"""
    if share is None:
        return None
    if row and row.get("share_scope") == GRANULARITY_SEGMENT:
        return GRANULARITY_SEGMENT
    return GRANULARITY_APPLIED


def _num(value: Any) -> Optional[float]:
    """LLM 行の KPI 値を数値に揃える（"106%"・"1,234" → 106・1234）。数値に読めなければ None（元の値は
    period.kpi_value_raw に残す）。読み方は yuho_read.parse_number と 1 箇所（Codex 2 回目 Critical 4）。

    2026-10-07 味の素の代走で kpi_value="106%" と kpi_unit="%" が重なり表に「106% %」と出た。
    """
    return parse_number(value)


def target_period_ends(periods: List[dict]) -> List[str]:
    """3 期揃いを数える期末= periods の period_end の異なる値のうち直近 3 つ（古い順）。3 つ未満ならあるだけ。

    同じ period_end の訂正有報が 2 本あっても 2 期と数えない・4 期以上あっても直近 3 期だけを見る（Codex T9 Warning 5）。"""
    ends = sorted({per.get("period_end") for per in periods if _clean(per.get("period_end"))})
    return ends[-THREE_PERIODS:]


def is_three_period_complete(periods: List[dict]) -> bool:
    """3 期揃い= 直近 3 つの異なる期末それぞれに、KPI が数値で 対象外（新設前）でない期がある（T9b ⑧）。

    期末が 3 つに満たなければ False。"""
    ends = target_period_ends(periods)
    if len(ends) < THREE_PERIODS:
        return False
    return all(any(per.get("period_end") == end and per.get("kpi_value") is not None
                   and per.get("label") != LABEL_OUT_OF_SCOPE for per in periods) for end in ends)


def is_three_period_eligible(periods: List[dict]) -> bool:
    """3 期揃い率の分母に入るか= 直近 3 期末の中に 対象外（新設前）の期を持たない品目（新設の品目は 3 期揃いようがないので除く）。"""
    ends = set(target_period_ends(periods))
    return not any(per.get("label") == LABEL_OUT_OF_SCOPE and per.get("period_end") in ends for per in periods)


def build_products(docs: List[dict], reads: Dict[str, Optional[dict]], targets: Any = None,
                   ir_texts: Optional[Dict[str, Optional[str]]] = None,
                   segment_map: Optional[List[dict]] = None) -> Dict[str, Any]:
    """3 期分の sections.json と read.json から products.json の中身を作る。

    Args:
        docs: sections.json の list（順不同・period_end で並べ直す）
        reads: doc_id → read.json の中身（無い docID は None）
        targets: output/yuho/<code>/targets.json の中身（任意・基準期の行を target_id に束ねるのに使う）
        ir_texts: doc_id → 説明会資料の本文（任意・説明会資料の引用を「期待」の根拠として照合するのに使う）
        segment_map: この会社の区分名の対応表の行（config/yuho_segment_map.json の code4 の値・任意・T10a）

    Returns:
        {code4, products_ver, products_status, products_total, complete_products, three_period_products,
         three_period_eligible, warnings, products:[...], docs:[...], usage:{...}}

    - 品目は _product_key（target_id／属性 5 列）で束ねる（文字列列は strip 済みの有効値で数える・str 以外は None）。item も
      target_id も無い行は product にしない。同じ期に同じキーの行が 2 つ来たら先頭を採り warnings に「衝突」を残す
    - 区分は期ごとに持つ: その期の行の segment（無ければ前の期までに分かった区分・指名品目は targets.json の
      区分＝基準期の区分）で find_segment_member を引く。後の期の区分で前の期を埋めない（Codex T9 Critical 1）
    - 基準期の行は targets.json の base_rows（行番号 → target_id）で束ねる（Codex T9 Critical 2）
    - read.json に segment_figures があれば、その期は区分の数字を本文の表の読みで置き換える（segment_source）
    - 新しく出た品目（初出の行が status=new で、最も古い期より後に出た）は、初出より前の期を 対象外（新設前）にする
    - 期の source には doc_id・element_id（有報の節）・ir_source（quote_section が ir_presentation の時だけ read.json の
      ir_source）・source_kind（有報／説明会資料／None）を入れる。出所が揃った product（item＋kpi_name があり、
      LLM 行のある期すべてで doc_id＋element_id が揃う）が 1 つ以上あって初めて products_status=済（Codex Critical 4）。
      説明会資料だけが出所の product は source_ir_only=True・ir_only_products に数える（Codex 2 回目 Critical 3）
    - 期のラベルは 2 本: segment_label（区分の判定・label_of）と label（品目向け・product_label）
    - usage は read.json の usage_ok が True の doc だけ合算する（代走= model に「代走」かつ input_tokens=0 は docs_proxy・
      usage_ok の無い旧 read.json と通信失敗は docs_unmeasured・Codex 2 回目 Critical 6）
    """
    docs = sorted(docs, key=lambda d: (d.get("period_end") or "", d.get("submit_date") or ""))
    code4 = docs[0].get("code4") if docs else None
    warnings: List[str] = []
    aliases = _target_aliases(targets)
    base_doc, base_map = _base_row_map(targets)
    # 行番号 → target_id の表: 基準期（base_rows）と、--extend で足した期（added_rows・T10e）
    row_maps: Dict[str, Dict[int, str]] = _added_row_maps(targets)
    if base_map and base_doc:
        row_maps[base_doc] = dict(base_map, **row_maps.get(base_doc, {}))
    target_meta = _target_by_id(targets)
    ir_texts = ir_texts or {}

    groups: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for doc in reversed(docs):  # 直近の期から見て、品目の名前は直近の表記を採る
        read = reads.get(doc["doc_id"]) or {}
        # 基準期は targets.json の base_rows（行番号 → target_id）で引く＝1 対 1（Codex T9 Critical 2）。
        # base_rows の無い旧 targets.json だけ、区分つきの属性キー（aliases）で引く
        row_map = row_maps.get(doc["doc_id"]) or {}
        for ri, row in enumerate(read.get("rows") or []):
            if not isinstance(row, dict):
                warnings.append(f"{doc['doc_id']}: dict でない行を捨てた ({type(row).__name__})")
                continue
            if row_map and not _clean(row.get("target_id")):
                tid = row_map.get(ri)
                key = (tid,) + ("",) * len(ATTR_COLUMNS) if tid else _product_key(row)
            else:
                key = _product_key(row, None if base_map else aliases)
            if not key[0] and not key[1]:
                warnings.append(f"{doc['doc_id']}: item が無い行を捨てた（item={row.get('item')!r} "
                                f"kpi_name={row.get('kpi_name')!r}）")
                continue
            tmeta = target_meta.get(key[0]) or {}
            g = groups.setdefault(key, {
                "target_id": key[0] or None,
                "segment": _clean(row.get("segment")) or _clean(tmeta.get("segment")),
                **{c: _clean(row.get(c)) or _clean(tmeta.get(c)) for c in ATTR_COLUMNS},
                "share_scope": _clean(row.get("share_scope")), "by_doc": {}})
            if doc["doc_id"] in g["by_doc"]:
                warnings.append(
                    f"衝突: {doc['doc_id']} で同じ品目キー（target_id={key[0] or '—'} item={row.get('item')} "
                    f"kpi_name={row.get('kpi_name')} geo={row.get('geo')} value_type={row.get('value_type')} "
                    f"value_basis={row.get('value_basis')}）の行が複数。先頭の行を採った"
                    f"（捨てた quote: {str(row.get('quote'))[:40]!r}）")
                continue
            g["by_doc"][doc["doc_id"]] = row
            for c in ("segment", "share_scope") + ATTR_COLUMNS:
                if not g[c] and _clean(row.get(c)):
                    g[c] = _clean(row[c])

    # 期ごとの facts（本文の表の読みがあれば区分の数字を置き換える）は品目に依らないので 1 回だけ作る
    doc_ctx: List[Dict[str, Any]] = []
    for doc in docs:
        facts = doc.get("facts") or {}
        cur = facts_slice(facts, "CurrentYear")
        prev = facts_slice(facts, "Prior1Year")
        read = reads.get(doc["doc_id"]) or {}
        cur, prev, used = apply_segment_figures(cur, prev, read.get("segment_figures"), facts.get("unit") or "円")
        seg_source = SEGMENT_SOURCE_READ if used else (SEGMENT_SOURCE_XBRL if cur.get("segments") else None)
        single = single_segment_evidence(doc, read) if not cur.get("segments") else None
        if single:
            cur = dict(cur, single_segment=True)
        doc_ctx.append({"doc": doc, "cur": cur, "prev": prev, "read": read, "segment_source": seg_source,
                        "single_evidence": single})

    products: List[Dict[str, Any]] = []
    for g in groups.values():
        idx_with_row = [i for i, c in enumerate(doc_ctx) if c["doc"]["doc_id"] in g["by_doc"]]
        first_idx = idx_with_row[0] if idx_with_row else None
        first_row = g["by_doc"][doc_ctx[first_idx]["doc"]["doc_id"]] if first_idx is not None else None
        first_status = _row_status(first_row)
        # 新しく出た品目（初出の行が status=new・最も古い期より後）＝初出より前の期は 対象外（新設前）
        new_later = first_status == STATUS_NEW and first_idx is not None and first_idx > 0
        # --extend で足した品目（T10e）: 足した期（added_from）より前は 対象外（新設前）（行の有無に依らない）
        added_from = _clean((target_meta.get(g["target_id"] or "") or {}).get("added_from"))
        added_idx = next((i for i, c in enumerate(doc_ctx) if c["doc"]["doc_id"] == added_from), None) \
            if added_from else None
        if added_idx is not None and added_idx > 0:
            new_later, first_idx = True, added_idx
        periods: List[Dict[str, Any]] = []
        carried_seg: Optional[str] = None   # 前の期までに分かった区分（古い期→新しい期へだけ持ち越す）
        for i, c in enumerate(doc_ctx):
            doc, cur, prev, read = c["doc"], c["cur"], c["prev"], c["read"]
            row = g["by_doc"].get(doc["doc_id"])
            # 区分はその期の行の物。行が区分を持たない・行が無い期は、それより前の期で分かった区分
            # （指名品目は基準期から作った targets.json の区分）だけで補う。後の期の区分で前の期を埋めない
            # ＝未来の情報で過去の判定を変えない（Codex T9 Critical 1）。どれも無ければ区分は不明のまま
            tmeta = target_meta.get(g["target_id"] or "") or {}
            seg_name = ((_clean(row.get("segment")) if row else None) or carried_seg
                        or _clean(tmeta.get("segment")))
            carried_seg = seg_name
            # T10a: 行の区分が当期の区分に無い時だけ、区分名の対応表で引き直す
            member, map_note, map_entry = resolve_segment_by_map(cur.get("segments") or {}, seg_name,
                                                                 doc.get("period_end"), segment_map)
            j = judge_period(prev, cur, member)
            if map_note:
                j["judge_note"] = "・".join(x for x in (j["judge_note"], map_note) if x)
            _, expect_sec = _expect_fields(row)
            ir_text = ir_texts.get(doc["doc_id"]) if row and expect_sec == IR_SECTION_KEY else None
            expect_ok, expect_basis = _expect_basis(row, doc, ir_text)
            segment_label = label_of((j["cond_a"], j["cond_b"], j["cond_c"]), expect_ok)
            quote_section = _clean(row.get("quote_section")) if row else None
            kpi_value = _num(row.get("kpi_value")) if row else None
            out_of_scope = new_later and i < first_idx
            label = product_label(row, segment_label, kpi_value, expect_ok, out_of_scope)
            sec = ((doc.get("sections") or {}).get(quote_section) or {}) if quote_section else {}
            ir_source = _clean(read.get("ir_source")) if (row and quote_section == IR_SECTION_KEY) else None
            element_id = _clean(sec.get("element_id"))
            periods.append({
                "period_end": doc.get("period_end"),
                "submit_date": _clean(read.get("submit_date")) or _clean(doc.get("submit_date")),
                "doc_id": doc["doc_id"],
                "read_mode": _clean(read.get("mode")),
                "row_status": _row_status(row),
                "target_id": _clean(row.get("target_id")) if row else None,
                "segment": seg_name,
                "segment_source": c["segment_source"] if member else (
                    SEGMENT_SOURCE_CONSOLIDATED if j["share_basis"] == BASIS_SINGLE else None),
                "kpi_name": _clean(row.get("kpi_name")) if row else None,
                "share_scope": _clean(row.get("share_scope")) if row else None,
                "share_granularity": _share_granularity(row, j["share_pct"]),
                "kpi_value": kpi_value,
                "kpi_value_raw": row.get("kpi_value") if row else None,
                # T10c: 読みの原文と概数の種類（範囲／概数／下限／上限・素の数値は None）
                "kpi_value_text": _clean(row.get("kpi_value_text")) if row else None,
                "value_approx": _clean(row.get("value_approx")) if row else None,
                "kpi_unit": _clean(row.get("kpi_unit")) if row else None,
                "kpi_prev_value": _num(row.get("kpi_prev_value")) if row else None,
                "prev_value_approx": _clean(row.get("prev_value_approx")) if row else None,
                "seg_sales": j["seg_sales"],
                "seg_profit": j["seg_profit"],
                "profit_measure": j["profit_measure"],
                "consolidated_profit": j["consolidated_profit"],
                "consolidated_measure": j["consolidated_measure"],
                "share_pct": j["share_pct"],
                "share_basis": j["share_basis"],
                "gross_margin": j["gross_margin"],
                "gross_margin_prev": j["gross_margin_prev"],
                "cond_a": j["cond_a"],
                "cond_b": j["cond_b"],
                "cond_c": j["cond_c"],
                "judge_note": j["judge_note"],
                "single_segment_evidence": c["single_evidence"] if j["share_basis"] == BASIS_SINGLE else None,
                "expectation": expect_ok,
                "expect_basis": expect_basis,
                "segment_label": segment_label,
                "label": label,
                "label_hint": _clean(row.get("label_hint")) if row else None,
                "quote": _clean(row.get("quote")) if row else None,
                "quote_section": quote_section,
                "expect_quote": _clean(row.get("expect_quote")) if row else None,     # T10d
                "expect_section": _clean(row.get("expect_section")) if row else None,
                "segment_map": ({"to": map_note, "doc_id": _clean((map_entry.get("evidence") or {}).get("doc_id")),
                                 "quote": _clean((map_entry.get("evidence") or {}).get("quote")),
                                 "split": map_entry.get("split") is True} if map_entry else None),
                "source": {
                    "doc_id": doc["doc_id"],
                    "element_id": element_id,
                    "ir_source": ir_source,
                    "source_kind": _source_kind(element_id, ir_source),
                    "segment_member": member,
                    "profit_element": j["profit_measure"],
                },
            })
        with_row = [per for per in periods if per["doc_id"] in g["by_doc"]]
        has_names = bool(g.get("item")) and bool(g.get("kpi_name")) and bool(with_row)
        source_complete = has_names and all(_source_complete(per) for per in with_row)
        # T10b: 説明会資料の〈資料名＋引用〉も出所と数える版（source_complete は従来のまま）
        source_complete_any = has_names and all(_source_complete_any(per) for per in with_row)
        # 説明会資料だけが出所（旧ルールなら揃っていた）＝未完のまま件数を出す
        source_ir_only = has_names and not source_complete and all(
            _source_complete(per) or _source_ir_only(per) for per in with_row)
        products.append({
            "target_id": g["target_id"],
            "segment": g["segment"],
            "item": g["item"],
            "kpi_name": g["kpi_name"],
            "geo": g["geo"],
            "value_type": g["value_type"],
            "value_basis": g["value_basis"],
            "first_status": first_status,
            "share_scope": g["share_scope"],
            "source_complete": source_complete,
            "source_complete_any": source_complete_any,
            "source_ir_only": source_ir_only,
            "added_from": added_from,
            "three_period_complete": is_three_period_complete(periods),
            "periods": periods,
            "x_backcheck": None,
        })

    complete = [p for p in products if p["source_complete"]]
    complete_any = [p for p in products if p["source_complete_any"]]
    ir_only = [p for p in products if p["source_ir_only"]]
    three = [p for p in products if p["three_period_complete"]]
    eligible = [p for p in products if is_three_period_eligible(p["periods"])]
    status = "済" if complete else LABEL_UNDECIDED

    usage = {"input_tokens": 0, "output_tokens": 0, "prompt_chars": 0, "docs_read": 0, "docs_error": 0,
             "docs_measured": 0, "docs_proxy": 0, "docs_unmeasured": 0}
    doc_rows = []
    for doc in docs:
        read = reads.get(doc["doc_id"])
        u = (read or {}).get("usage") or {}
        proxy = _is_proxy_read(read)
        usage_ok = bool(read) and read.get("usage_ok") is True
        if read:
            usage["docs_read"] += 1
            if read.get("error"):
                usage["docs_error"] += 1
            if proxy:
                usage["docs_proxy"] += 1
            elif usage_ok:
                # error があっても usage を取れていれば消費は実測（JSON 抽出失敗でも課金は起きている）
                usage["docs_measured"] += 1
                usage["input_tokens"] += int(u.get("input_tokens") or 0)
                usage["output_tokens"] += int(u.get("output_tokens") or 0)
                usage["prompt_chars"] += int(read.get("prompt_chars") or 0)
            else:
                usage["docs_unmeasured"] += 1   # 通信失敗で usage が無い・usage_ok の無い旧 read.json
        doc_rows.append({
            "doc_id": doc["doc_id"], "period_end": doc.get("period_end"), "accounting": doc.get("accounting"),
            "submit_date": _clean((read or {}).get("submit_date")) or _clean(doc.get("submit_date")),
            "mode": _clean((read or {}).get("mode")),
            "segment_figures": len((read or {}).get("segment_figures") or []) if isinstance(
                (read or {}).get("segment_figures"), list) else 0,
            "sections_with_text": sum(1 for s in (doc.get("sections") or {}).values() if s and s.get("text")),
            "segments": len((doc.get("facts") or {}).get("segments") or {}),
            "rows": len((read or {}).get("rows") or []), "model": (read or {}).get("model"),
            "proxy": proxy,
            "usage_ok": usage_ok,
            "usage": {"input_tokens": int(u.get("input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0)},
            "error": (read or {}).get("error"),
        })

    return {
        "code4": code4,
        "products_ver": 2,
        "products_status": status,
        "products_total": len(products),
        "complete_products": len(complete),
        "complete_any_products": len(complete_any),
        "ir_only_products": len(ir_only),
        "three_period_products": len(three),
        "three_period_eligible": len(eligible),
        "warnings": warnings,
        "products": products,
        "docs": doc_rows,
        "usage": usage,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "T" if v else "F"
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) >= 10 else f"{v:.3f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def render_summary(result: dict, price_in: Optional[float] = None, price_out: Optional[float] = None) -> str:
    """products.json の中身から summary.md（表＋費用の式と実測）を作る。

    Args:
        result: build_products の返り値
        price_in: 入力の単価（USD / 100 万トークン・未指定なら式だけ）
        price_out: 出力の単価（USD / 100 万トークン）
    """
    u = result["usage"]
    lines = [
        f"# yuho judge {result['code4']} — products_status={result['products_status']} "
        f"products={result.get('products_total', len(result['products']))} "
        f"complete_products={result.get('complete_products', 0)} "
        f"complete_any_products={result.get('complete_any_products', 0)} "
        f"IR 由来のみで出所未完={result.get('ir_only_products', 0)} products",
        "",
        f"- 3 期揃い（3 期とも KPI が数値）= {result.get('three_period_products', 0)} / "
        f"{result.get('three_period_eligible', 0)} 品目（分母は 対象外（新設前）の期を持つ品目を除く）",
        "",
    ]
    if result.get("warnings"):
        lines += ["## 警告", ""] + [f"- {w}" for w in result["warnings"]] + [""]
    lines += [
        "## 期ごとの判定",
        "",
        "| segment | item | kpi_name | period_end | doc_id | kpi_value | share_pct (basis) | share_granularity | profit_measure | cond_a/b/c | segment_label | label | source |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in result["products"]:
        for per in p["periods"]:
            basis = per["share_basis"] or "—"
            if per.get("segment_source") == SEGMENT_SOURCE_READ:
                basis += f"・{SEGMENT_SOURCE_READ}"
            share = f"{_fmt(per['share_pct'])} ({basis})"
            conds = "/".join(_fmt(per[c]) for c in ("cond_a", "cond_b", "cond_c"))
            if per.get("judge_note"):
                conds += f"（{per['judge_note']}）"
            src = per.get("source") or {}
            src_txt = src.get("element_id") or src.get("ir_source") or "—"
            if src.get("source_kind"):
                src_txt = f"{src['source_kind']}: {src_txt}"
            kpi_txt = _fmt(per["kpi_value"]) + ((" " + per["kpi_unit"]) if per.get("kpi_unit") else "")
            if per.get("value_approx"):
                kpi_txt += f"（{per['value_approx']}: {str(per.get('kpi_value_text') or '')[:20]}）"
            if per["kpi_value"] is None and per.get("kpi_value_raw") is not None:
                kpi_txt = f"— (raw={str(per['kpi_value_raw'])[:20]!r})"
            lines.append(
                f"| {(per.get('segment') if 'segment' in per else p['segment']) or '—'} | {p['item'] or '—'} | {per.get('kpi_name') or p['kpi_name'] or '—'} | {per['period_end']} | {per['doc_id']} "
                f"| {kpi_txt} | {share} "
                f"| {per.get('share_granularity') or '—'} "
                f"| {per['profit_measure'] or '—'} | {conds} | {per.get('segment_label') or '—'} | {per['label']} | {src_txt} |"
            )
    lines += ["", "## 読んだ資料", "", "| doc_id | period_end | accounting | sections | segments | rows | model | usage_ok | input_tokens | output_tokens | error |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in result["docs"]:
        model = (d.get("model") or "—") + ("（API 未実行・代走）" if d.get("proxy") else "")
        lines.append(f"| {d['doc_id']} | {d['period_end']} | {d['accounting']} | {d['sections_with_text']}/7 | {d['segments']} "
                     f"| {d['rows']} | {model} | {_fmt(bool(d.get('usage_ok')))} | {d['usage']['input_tokens']:,} "
                     f"| {d['usage']['output_tokens']:,} | {d['error'] or '—'} |")
    measured = int(u.get("docs_measured") or 0)
    proxy = int(u.get("docs_proxy") or 0)
    unmeasured = int(u.get("docs_unmeasured") or 0)
    lines += [
        "",
        "## 費用（実測）" if measured else "## 費用（未測定）",
        "",
        "式: 費用 = 入力トークン合計 × 単価_in ＋ 出力トークン合計 × 単価_out（単価は USD / 100 万トークン）",
    ]
    if proxy:
        lines.append(f"API 未実行（代走）＝費用未測定: {proxy} doc（model に「{PROXY_MARK}」・input_tokens=0。費用集計から除外）")
    if unmeasured:
        lines.append(f"usage 未取得＝費用未測定: {unmeasured} doc（通信失敗で usage が無い・usage_ok の無い旧 read.json。費用集計から除外）")
    if measured:
        lines.append(f"実測 doc={measured}: input_tokens={u['input_tokens']} output_tokens={u['output_tokens']} "
                     f"prompt_chars={u['prompt_chars']} docs_read={u['docs_read']} docs_error={u['docs_error']}")
        if u["prompt_chars"]:
            lines.append(f"文字→トークン係数（実測）: input_tokens ÷ prompt_chars = {u['input_tokens'] / u['prompt_chars']:.3f}")
        if price_in is not None and price_out is not None:
            cost = u["input_tokens"] / 1e6 * price_in + u["output_tokens"] / 1e6 * price_out
            lines.append(f"単価: in={price_in} out={price_out} USD/Mtok → 1 社あたり ${cost:.4f}（実測 {measured} doc 分）")
        else:
            lines.append("単価: 未設定（--price-in / --price-out で与えると 1 社あたりの金額を出す）")
    else:
        lines.append(f"未測定: API を実際に叩いた doc が 0（docs_read={u['docs_read']} 代走={proxy} usage 未取得={unmeasured}）。"
                     "1 社あたりの金額は scripts/yuho_read.py を API で実走してから出す")
    lines.append("")
    return "\n".join(lines)


def load_inputs(code4: str, data_dir: Path, out_dir: Path) -> Tuple[List[dict], Dict[str, Optional[dict]]]:
    """sections.json（全部）と、同じ docID の read.json（無ければ None）を読む。"""
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((data_dir / code4).glob("*.sections.json"))]
    reads: Dict[str, Optional[dict]] = {}
    for doc in docs:
        rp = out_dir / code4 / f"{doc['doc_id']}.read.json"
        reads[doc["doc_id"]] = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else None
    return docs, reads


def load_extras(code4: str, data_dir: Path, out_dir: Path,
                reads: Dict[str, Optional[dict]]) -> Tuple[Any, Dict[str, Optional[str]]]:
    """targets.json（あれば）と、各期の read.json が指す説明会資料の本文（data_dir/<code>/ir/<ir_source>）を読む。

    Returns:
        (targets の中身 or None, doc_id → 説明会資料の本文 or None)
    """
    tp = out_dir / code4 / "targets.json"
    targets = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else None
    ir_texts: Dict[str, Optional[str]] = {}
    for doc_id, read in reads.items():
        name = _clean((read or {}).get("ir_source"))
        path = data_dir / code4 / "ir" / name if name else None
        ir_texts[doc_id] = path.read_text(encoding="utf-8", errors="replace") if path and path.is_file() else None
    return targets, ir_texts


# ---------------------------------------------------------------- 全社集計（--all）
def _load_ledger(ledger_path: Path) -> Dict[str, dict]:
    """center_pin.jsonl（1 行 1 JSON）を code4 → 行の辞書にする。ファイルが無ければ空。

    Args:
        ledger_path: center_pin.jsonl のパス

    Returns:
        4 桁コード → {"name", "s33", ...} の辞書
    """
    ledger: Dict[str, dict] = {}
    if not ledger_path.exists():
        return ledger
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = str(row.get("code") or "").strip()
        if code:
            ledger[code.zfill(4)] = row
    return ledger


def _count(values: List[Any]) -> Dict[str, int]:
    """値ごとの件数（None は「—」に寄せる・キーは昇順）。"""
    keys = ["—" if v is None else str(v) for v in values]
    return {k: keys.count(k) for k in sorted(set(keys))}


def _company_row(code4: str, prod: dict, ledger: Dict[str, dict]) -> Dict[str, Any]:
    """1 社の products.json から集計表の 1 行を作る。"""
    meta = ledger.get(code4) or {}
    docs = prod.get("docs") or []
    plist = prod.get("products") or []
    periods = [per for p in plist for per in (p.get("periods") or [])]
    # 3 期揃いは periods から数え直す（旧 products.json にも効く・T9b ⑧）
    three = sum(1 for p in plist if is_three_period_complete(p.get("periods") or []))
    eligible = sum(1 for p in plist if is_three_period_eligible(p.get("periods") or []))
    # T10b: 説明会資料も出所と数える品目（旧 products.json は source_complete で代える）
    if "complete_any_products" in prod:
        complete_any = int(prod.get("complete_any_products") or 0)
    else:
        complete_any = sum(1 for p in plist if p.get("source_complete_any", p.get("source_complete")))
    approx = [per.get("value_approx") for per in periods if per.get("value_approx")]
    usage = prod.get("usage") or {}
    docs_proxy = usage.get("docs_proxy")
    if docs_proxy is None:
        docs_proxy = sum(1 for d in docs if d.get("proxy"))
    return {
        "code4": code4,
        "name": meta.get("name") or "—",
        "s33": meta.get("s33") or "—",
        "pin": meta.get("pin") or "—",
        "sign": meta.get("sign") or "—",
        "products_status": prod.get("products_status"),
        "products_total": int(prod.get("products_total") or len(prod.get("products") or [])),
        "complete_products": int(prod.get("complete_products") or 0),
        "complete_any_products": complete_any,
        "ir_only_products": int(prod.get("ir_only_products") or 0),
        "approx_periods": len(approx),       # T10c: KPI 値が概数・範囲・下限・上限の期
        "added_targets_total": sum(1 for p in plist if _clean(p.get("added_from"))),   # T10e: --extend で足した品目
        "added_two_period": sum(1 for p in plist if is_added_two_period(p)),          # 足した期＋最後の期とも数値
        "value_approx": _count(approx),
        "three_period_products": three,
        "three_period_eligible": eligible,
        "docs": len(docs),
        "docs_with_rows": sum(1 for d in docs if int(d.get("rows") or 0) > 0),
        "docs_proxy": int(docs_proxy),
        "labels": _count([per.get("label") for per in periods]),
        "segment_labels": _count([per.get("segment_label") for per in periods]),
        "warnings": len(prod.get("warnings") or []),
    }


def is_added_two_period(product: dict) -> bool:
    """--extend で足した品目（added_from あり・T10e）が、足した期と最後の期の両方で KPI が数値か（Codex T10 R1 Warning 5）。

    T9 の 3 期揃い（追加品目は 対象外（新設前）の期を持つので分母に入らない）は変えず、追加品目だけ別の物差しで数える。"""
    added_from = _clean(product.get("added_from"))
    periods = product.get("periods") or []
    if not added_from or not periods:
        return False
    latest_end = max((per.get("period_end") or "") for per in periods)
    mid = [per for per in periods if per.get("doc_id") == added_from]
    last = [per for per in periods if (per.get("period_end") or "") == latest_end]
    return bool(mid) and bool(last) and latest_end > (mid[0].get("period_end") or "") \
        and any(per.get("kpi_value") is not None for per in mid) \
        and any(per.get("kpi_value") is not None for per in last)


def _rate(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.3f}"


def _cell(v: Any) -> str:
    """md の表のセル用: | を全角に・改行を空白に（台帳の pin は自由文）。"""
    return str(v if v not in (None, "") else "—").replace("|", "｜").replace("\n", " ")


def render_trial_summary(agg: dict) -> str:
    """aggregate_all の返り値から trial_summary.md（全体集計＋1 社 1 行の表）を作る。

    Args:
        agg: aggregate_all の返り値

    Returns:
        Markdown の文字列
    """
    t = agg["totals"]
    lines = [
        f"# yuho trial summary — companies={t['companies_total']} status_done={t['status_done']} "
        f"complete_ge1={t['complete_ge1']} fill_rate={t['fill_rate']:.3f} fill_rate_any={t['fill_rate_any']:.3f}",
        "",
        f"- 埋まり率 = complete_products ≥ 1 の社数 ÷ companies_total = {t['complete_ge1']} / {t['companies_total']} "
        f"= {t['fill_rate']:.3f}",
        f"- 埋まり率（有報だけ＝fill_rate_yuho）= {t['complete_ge1']} / {t['companies_total']} = {t['fill_rate_yuho']:.3f}"
        f"・（有報＋説明会資料＝fill_rate_any）= {t['complete_any_ge1']} / {t['companies_total']} = {t['fill_rate_any']:.3f}",
        f"- KPI 値が概数・範囲・下限・上限の期 = {t['approx_periods']}（{t['value_approx'] or '—'}）",
        f"- 追加品目（--extend・3 期揃いの分母には入らない）の 2 期揃い = 足した期と最後の期の両方で KPI が数値 = "
        f"{t['added_two_period']} / {t['added_targets_total']}",
        "- 分母は「物なし」を除く前の仮置き（「物なし」は人が付けるので機械では除外しない）",
        f"- products_status=済 の社数: {t['status_done']}",
        f"- docs 合計={_fmt(t['docs_total'])} うち rows>0={_fmt(t['docs_with_rows_total'])} 代走={_fmt(t['docs_proxy_total'])}",
        f"- products.json の無い 4 桁ディレクトリ（分母に入っていない社）: {', '.join(t.get('missing_products') or []) or 'なし'}",
        f"- **3 期揃い率（品目）** = 3 期とも KPI が数値の品目 ÷ 品目（対象外（新設前）の期を持つ品目を除く）= "
        f"{t['three_period_products']} / {t['three_period_eligible']} = {_rate(t['three_period_rate'])}",
        f"- **3 期揃い率（社）** = 3 期揃いの品目を 1 つ以上持つ社 ÷ companies_total = "
        f"{t['three_period_companies']} / {t['companies_total']} = {_rate(t['three_period_company_rate'])}",
        f"- 未判定の期の合計（対象外（新設前）は含めない）= {t['undecided_periods']}・対象外（新設前）= {t['out_of_scope_periods']}",
        f"- label 件数（合計）: {t['labels'] or '—'}",
        f"- segment_label 件数（合計）: {t['segment_labels'] or '—'}",
        "",
        "## 会社ごと",
        "",
        "| code4 | name | s33 | pin | sign | products_status | products_total | complete_products | complete_any_products "
        "| ir_only_products | 3 期揃い | 概数の期 | docs | docs rows>0 | docs_proxy | labels | segment_labels | warnings |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in agg["companies"]:
        lab = ", ".join(f"{k}={v}" for k, v in r["labels"].items()) or "—"
        seg = ", ".join(f"{k}={v}" for k, v in r["segment_labels"].items()) or "—"
        lines.append(
            f"| {r['code4']} | {r['name']} | {r['s33']} | {_cell(r.get('pin'))} | {_cell(r.get('sign'))} "
            f"| {_fmt(r['products_status'])} | {_fmt(r['products_total'])} "
            f"| {_fmt(r['complete_products'])} | {_fmt(r.get('complete_any_products', 0))} | {_fmt(r['ir_only_products'])} "
            f"| {r.get('three_period_products', 0)}/{r.get('three_period_eligible', 0)} | {_fmt(r.get('approx_periods', 0))} "
            f"| {_fmt(r['docs'])} "
            f"| {_fmt(r['docs_with_rows'])} | {_fmt(r['docs_proxy'])} | {lab} | {seg} | {_fmt(r['warnings'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def aggregate_all(out_dir: Path, ledger_path: Path = DEFAULT_LEDGER) -> Dict[str, Any]:
    """out_dir/<4 桁コード>/products.json を全部読み、全社の集計を trial_summary.json / .md に書く。

    埋まり率の分母は「物なし」を除く前の companies_total（「物なし」は人が付けるので機械では除外しない）。

    Args:
        out_dir: 各社の products.json がある親ディレクトリ（例: output/yuho）
        ledger_path: 会社名・業種（s33）を引く center_pin.jsonl

    Returns:
        {"generated_at", "companies", "totals", "paths"} の辞書

    Raises:
        SystemExit: products.json が 1 件も無い時
    """
    paths = sorted(p for p in out_dir.glob("*/products.json")
                   if re.fullmatch(r"\d{4}", p.parent.name))
    if not paths:
        raise SystemExit(f"products.json が無い: {out_dir}/<4 桁コード>/products.json（先に --code で判定）")
    # 4 桁のディレクトリはあるのに products.json が無い社（判定し忘れ）は分母に入らないので別に数えて見せる（Codex 2026-10-08）
    missing_products = sorted(d.name for d in out_dir.iterdir()
                              if d.is_dir() and re.fullmatch(r"\d{4}", d.name) and not (d / "products.json").exists())
    ledger = _load_ledger(ledger_path)
    companies = [_company_row(p.parent.name, json.loads(p.read_text(encoding="utf-8")), ledger) for p in paths]

    labels: Dict[str, int] = {}
    seg_labels: Dict[str, int] = {}
    for r in companies:
        for k, v in r["labels"].items():
            labels[k] = labels.get(k, 0) + v
        for k, v in r["segment_labels"].items():
            seg_labels[k] = seg_labels.get(k, 0) + v
    n = len(companies)
    complete_ge1 = sum(1 for r in companies if r["complete_products"] >= 1)
    complete_any_ge1 = sum(1 for r in companies if r["complete_any_products"] >= 1)
    approx_kinds: Dict[str, int] = {}
    for r in companies:
        for k, v in r["value_approx"].items():
            approx_kinds[k] = approx_kinds.get(k, 0) + v
    three_total = sum(r["three_period_products"] for r in companies)
    eligible_total = sum(r["three_period_eligible"] for r in companies)
    three_companies = sum(1 for r in companies if r["three_period_products"] >= 1)
    totals = {
        "companies_total": n,
        "status_done": sum(1 for r in companies if r["products_status"] == "済"),
        "complete_ge1": complete_ge1,
        "fill_rate": complete_ge1 / n,
        "fill_rate_yuho": complete_ge1 / n,          # T10b: 有報の節だけ（＝fill_rate の別名）
        "complete_any_ge1": complete_any_ge1,
        "fill_rate_any": complete_any_ge1 / n,       # T10b: 有報の節 か 説明会資料〈資料名＋引用〉
        "approx_periods": sum(r["approx_periods"] for r in companies),   # T10c
        "added_targets_total": sum(r["added_targets_total"] for r in companies),   # T10e（3 期揃いとは別の物差し）
        "added_two_period": sum(r["added_two_period"] for r in companies),
        "value_approx": dict(sorted(approx_kinds.items())),
        "fill_rate_note": "分母は「物なし」を除く前の仮置き（「物なし」は人が付けるので機械では除外しない）",
        "three_period_products": three_total,
        "three_period_eligible": eligible_total,
        "three_period_rate": three_total / eligible_total if eligible_total else None,
        "three_period_companies": three_companies,
        "three_period_company_rate": three_companies / n,
        "undecided_periods": sum(v for k, v in labels.items() if k.startswith(LABEL_UNDECIDED)),
        "out_of_scope_periods": labels.get(LABEL_OUT_OF_SCOPE, 0),
        "labels": dict(sorted(labels.items())),
        "segment_labels": dict(sorted(seg_labels.items())),
        "docs_total": sum(r["docs"] for r in companies),
        "docs_with_rows_total": sum(r["docs_with_rows"] for r in companies),
        "docs_proxy_total": sum(r["docs_proxy"] for r in companies),
        "missing_products": missing_products,
    }
    json_path = out_dir / "trial_summary.json"
    md_path = out_dir / "trial_summary.md"
    agg: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "companies": companies,
        "totals": totals,
    }
    json_path.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_trial_summary(agg), encoding="utf-8")
    agg["paths"] = [str(json_path), str(md_path)]
    return agg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--code", help="証券コード 4 桁（例: 2802）")
    mode.add_argument("--all", action="store_true", help="out_dir/*/products.json を全社集計して trial_summary.json/.md を書く")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--price-in", type=float, default=None, help="入力の単価 USD / 100 万トークン")
    ap.add_argument("--price-out", type=float, default=None, help="出力の単価 USD / 100 万トークン")
    ap.add_argument("--segment-map", type=Path, default=DEFAULT_SEGMENT_MAP,
                    help="区分名の旧→新の対応表（T10a・既定 config/yuho_segment_map.json・無ければ対応表なし）")
    args = ap.parse_args()

    if args.all:
        agg = aggregate_all(args.out_dir)
        t = agg["totals"]
        print(f"companies={t['companies_total']} status_done={t['status_done']} complete_ge1={t['complete_ge1']} "
              f"fill_rate={t['fill_rate']:.2f} wrote {' '.join(agg['paths'])}")
        print(f"fill_rate_yuho={t['fill_rate_yuho']:.3f} fill_rate_any={t['fill_rate_any']:.3f} "
              f"complete_any_ge1={t['complete_any_ge1']} approx_periods={t['approx_periods']} {t['value_approx']} "
              f"added_two_period={t['added_two_period']}/{t['added_targets_total']}")
        print(f"three_period={t['three_period_products']}/{t['three_period_eligible']} "
              f"rate={_rate(t['three_period_rate'])} companies={t['three_period_companies']}/{t['companies_total']} "
              f"rate={_rate(t['three_period_company_rate'])} undecided_periods={t['undecided_periods']} "
              f"out_of_scope_periods={t['out_of_scope_periods']}")
        return

    code4 = args.code.zfill(4)
    seg_map = load_segment_map(args.segment_map)   # 形が壊れていれば SystemExit（判定の前に止める）
    docs, reads = load_inputs(code4, args.data_dir, args.out_dir)
    if not docs:
        raise SystemExit(f"sections.json が無い: {args.data_dir / code4}/*.sections.json（先に scripts/yuho_fetch.py）")
    missing = [d for d, r in reads.items() if r is None]
    if missing:
        print(f"warn: read.json が無い docID={missing}（先に scripts/yuho_read.py・この期は kpi_value=null で判定だけ出す）")

    targets, ir_texts = load_extras(code4, args.data_dir, args.out_dir, reads)
    result = build_products(docs, reads, targets, ir_texts, segment_map=seg_map.get(code4))
    out = args.out_dir / code4
    out.mkdir(parents=True, exist_ok=True)
    (out / "products.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(render_summary(result, args.price_in, args.price_out), encoding="utf-8")
    n_periods = len(result["products"][0]["periods"]) if result["products"] else 0
    labels = [per["label"] for p in result["products"] for per in p["periods"]]
    seg_labels = [per["segment_label"] for p in result["products"] for per in p["periods"]]
    if result["warnings"]:
        for w in result["warnings"]:
            print(f"warn: {w}")
    print(f"{code4} products={result['products_total']} complete_products={result['complete_products']} "
          f"complete_any_products={result['complete_any_products']} "
          f"ir_only_products={result['ir_only_products']} periods={n_periods} "
          f"three_period={result['three_period_products']}/{result['three_period_eligible']} "
          f"status={result['products_status']} labels={dict((l, labels.count(l)) for l in sorted(set(labels)))} "
          f"segment_labels={dict((l, seg_labels.count(l)) for l in sorted(set(seg_labels)))} "
          f"input_tokens={result['usage']['input_tokens']} output_tokens={result['usage']['output_tokens']} "
          f"docs_measured={result['usage']['docs_measured']} docs_proxy={result['usage']['docs_proxy']} "
          f"docs_unmeasured={result['usage']['docs_unmeasured']} "
          f"wrote {out}/products.json {out}/summary.md")


if __name__ == "__main__":
    main()
