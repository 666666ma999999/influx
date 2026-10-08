#!/usr/bin/env python3
"""有報の数値 facts（sections.json）と Claude の候補行（read.json）を突き合わせ、期ごとに需要／期待／未判定を付ける（P-INF-34 工程④）。

入力: `data/edinet/yuho/<code4>/<docID>.sections.json`（3 期分）＋ `output/yuho/<code4>/<docID>.read.json`（同じ docID）
出力: `output/yuho/<code4>/products.json` と `summary.md`（表＋費用の式と実測）

判定（叩き台 §5・期 t のラベルは期 t の有報の値だけで決める＝D5）:
  cond_a = share_pct ≥ 30（利益比を優先・区分利益が無い時だけ売上比・どちらも無ければ None）
  cond_b = 区分売上が前期比 +5% 以上 かつ 区分利益が増
  cond_c = 連結の粗利率（売上総利益 ÷ 売上高）が前期比で下がっていない
  segment_label = 区分の判定: 3 つ True→需要／expectation→期待／どれか None→未判定／それ以外→需要条件不成立
  label         = 品目向け（区分の判定を無限定に転用しない・Codex 2 回目 Critical 1・2・4）:
                  当期に候補行なし→未判定（当期候補なし）／KPI が数値でない→根拠の揃った期待なら 期待・無ければ 未判定（KPI 非数値）／区分で成立→需要（区分）／expectation＋根拠（経営方針の本文に品目名つきの引用）→期待／
                  区分の数値が欠ける→未判定（区分数値なし）／
                  それ以外→需要条件不成立（区分）

使い方:
    python scripts/yuho_judge.py --code 2802 [--price-in 3.0 --price-out 15.0]   # 単価は USD / 100 万トークン
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
# 「期待」の根拠に使える有報の節（vault §5: 有報の経営方針で会社が成長事業と明記）。説明会資料・MD&A は根拠にしない
EXPECT_SECTIONS = ("business_policy",)
# 「期待」の引用に要る語（成長事業と明記した文か）。どれも無い引用は期待の根拠にしない（Codex 4 回目 Critical 1）
EXPECT_KEYWORDS = ("成長", "重点", "注力", "拡大", "強化", "育成")

BASIS_PROFIT = "利益比"
BASIS_SALES = "売上比"
BASIS_SINGLE = "単一セグメント"
BASIS_NO_SEGMENTS = "セグメント抽出なし"
BASIS_NEGATIVE = "赤字のため構成比を出せない"

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
    """区分利益の指標に合わせた連結の分母（事業利益系なら business_profit・それ以外は operating_profit）。"""
    pm = (profit_measure or "")
    order = ("business_profit", "operating_profit") if "BusinessProfit" in pm else ("operating_profit", "business_profit")
    for key in order:
        if cons.get(key) is not None:
            return cons[key], key
    return None, None


# ---------------------------------------------------------------- セグメント名の突合
def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    return re.sub(r"[\s・･,、/／()（）「」\-_]", "", s)


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


# ---------------------------------------------------------------- 期ごとの判定
def judge_period(prev_facts: Optional[dict], cur_facts: dict, seg_member: Optional[str]) -> Dict[str, Any]:
    """1 期分の 3 条件を計算する（ラベルは付けない・label_of が付ける）。

    Args:
        prev_facts: 前期の facts_slice（無ければ None → cond_b / cond_c は None）
        cur_facts: 当期の facts_slice
        seg_member: facts.segments の member キー。None なら share=None（segments が空でも同じ）

    単一セグメント（share=100・連結の値を区分に当てる）は `cur_facts["single_segment"] is True` の時だけ。
    segments が空なのは「抽出に失敗した」場合も同じ状態になるので、空＝単一とは見なさない（Codex Critical 5）。
    yuho_fetch はまだこのフラグを立てない（常に None）＝今は必ず未判定になる。将来 fetch 側で「報告セグメントが
    単一」と判定できたら True を立てる。segments が空でフラグが無い時は share=None・share_basis="セグメント抽出なし"。

    判定（cond_a／cond_c）は丸め前の値で行い、返す share_pct／gross_margin は表示用に丸めた値（Codex Critical 1）。

    Returns:
        {seg_sales, seg_profit, profit_measure, consolidated_profit, consolidated_measure,
         share_pct, share_basis, gross_margin, gross_margin_prev, cond_a, cond_b, cond_c}
    """
    cons = cur_facts.get("consolidated") or {}
    segs = cur_facts.get("segments") or {}
    profit_measure = cur_facts.get("profit_measure")
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
        # 単一セグメントで内訳を書かない会社（D2）: 連結の値をそのまま区分に当て share=100
        seg_sales = cons.get("net_sales")
        seg_profit = cons_profit
        prev_cons = (prev_facts or {}).get("consolidated") or {}
        prev_sales = prev_cons.get("net_sales")
        prev_profit, _ = _consolidated_profit(prev_cons, profit_measure)
        if seg_sales is not None or seg_profit is not None:
            share, basis = 100.0, BASIS_SINGLE
    elif not segs:
        basis = BASIS_NO_SEGMENTS

    cond_a = None if share is None else share >= SHARE_THRESHOLD

    cond_b: Optional[bool]
    if None in (seg_sales, seg_profit, prev_sales, prev_profit) or not prev_sales:
        cond_b = None
    else:
        cond_b = (float(seg_sales) / float(prev_sales) - 1.0 >= SALES_GROWTH_MIN) and float(seg_profit) > float(prev_profit)

    gm_cur = gross_margin(cons.get("gross_profit"), cons.get("net_sales"))
    prev_cons_all = (prev_facts or {}).get("consolidated") or {}
    gm_prev = gross_margin(prev_cons_all.get("gross_profit"), prev_cons_all.get("net_sales"))
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
    }


def label_of(conds: Tuple[Optional[bool], Optional[bool], Optional[bool]], expectation: bool) -> str:
    """3 条件と期待フラグからラベルを決める。

    Args:
        conds: (cond_a, cond_b, cond_c)。各 True/False/None
        expectation: 有報の経営方針で成長事業・重点領域と明記された行か

    Returns:
        需要／期待／未判定／需要条件不成立
    """
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


def _expect_basis(row: Optional[dict], doc: dict) -> Tuple[bool, Optional[str]]:
    """「期待」の根拠が揃っているか（Codex 3 回目 Critical 2）。

    揃う条件（全部）: expectation が true/false として読めて True／quote_section が EXPECT_SECTIONS（経営方針）／
    その節が当期の有報に実在する（element_id あり）／quote が空でない／quote が節の本文に見つかる（空白無視）／
    quote に品目名が含まれる（記号無視・別品目の引用を拒否）／quote に EXPECT_KEYWORDS のどれかがある（成長事業の明記）。
    LLM の自己申告（expectation=True）だけでは期待にしない。意味の最終判断は人の検収（工程⑤）＝ここは機械で落とせる物を落とす。

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
    qs = _clean(row.get("quote_section"))
    if qs not in EXPECT_SECTIONS:
        return False, f"根拠節が経営方針でない（{qs}）"
    sec = (doc.get("sections") or {}).get(qs) or {}
    if not _clean(sec.get("element_id")):
        return False, "経営方針の節が当期の有報に無い"
    quote = _clean(row.get("quote"))
    if not quote:
        return False, "引用が空"
    if _squash(quote) not in _squash(sec.get("text")):
        return False, "引用が経営方針の本文に見つからない"
    item = _letters(row.get("item"))
    if not item or item not in _letters(quote):
        return False, "引用に品目名が無い（別品目の引用）"
    if not any(k in _squash(quote) for k in EXPECT_KEYWORDS):  # 語の途中の改行・空白は無視
        return False, f"引用に成長事業を示す語（{'／'.join(EXPECT_KEYWORDS)}）が無い"
    return True, "経営方針の本文に品目名つきの引用あり"


def product_label(row: Optional[dict], segment_label: str, kpi_value: Any, expect_ok: bool) -> str:
    """品目向けのラベル。区分の判定（segment_label）を品目に無限定で転用しない（Codex 2 回目 Critical 1・2・4／3 回目 1）。

    順: 当期に候補行が無い → 未判定（当期候補なし）／KPI が数値でない → 根拠の揃った期待なら 期待・無ければ 未判定（KPI 非数値）
    （数値の無い行を需要にしない）／需要 3 条件が区分の数値で成立 → 需要（区分）／根拠の揃った期待 → 期待／
    区分の数値が欠けて 3 条件を判定できない → 未判定（区分数値なし）／それ以外 → 需要条件不成立（区分）

    Args:
        row: 当期の LLM 行（無ければ None）
        segment_label: label_of の結果（区分の判定）
        kpi_value: _num 後の KPI 値（数値に読めなければ None）
        expect_ok: _expect_basis の結果（期待の根拠が揃っているか）
    """
    if row is None:
        return LABEL_UNDECIDED_NO_ROW
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


def _product_key(row: dict) -> Tuple[str, str, str]:
    """品目のグループキー= (segment, item, kpi_name) の正規化。同じ品目でも KPI ごとに別 product（Codex Critical 3）。"""
    return (_norm(_clean(row.get("segment"))), _norm(_clean(row.get("item"))), _norm(_clean(row.get("kpi_name"))))


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


def build_products(docs: List[dict], reads: Dict[str, Optional[dict]]) -> Dict[str, Any]:
    """3 期分の sections.json と read.json から products.json の中身を作る。

    Args:
        docs: sections.json の list（順不同・period_end で並べ直す）
        reads: doc_id → read.json の中身（無い docID は None）

    Returns:
        {code4, products_ver, products_status, products_total, complete_products, warnings,
         products:[...], docs:[...], usage:{...}}

    - 品目は (segment, item, kpi_name) で束ねる（文字列列は strip 済みの有効値で数える・str 以外は None）。item が
      無い行は product にしない。同じ期に同じキーの行が 2 つ来たら先頭を採り warnings に「衝突」を残す
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

    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for doc in reversed(docs):  # 直近の期から見て、品目の名前は直近の表記を採る
        read = reads.get(doc["doc_id"]) or {}
        for row in read.get("rows") or []:
            if not isinstance(row, dict):
                warnings.append(f"{doc['doc_id']}: dict でない行を捨てた ({type(row).__name__})")
                continue
            key = _product_key(row)
            if not key[1]:
                warnings.append(f"{doc['doc_id']}: item が無い行を捨てた（item={row.get('item')!r} "
                                f"kpi_name={row.get('kpi_name')!r}）")
                continue
            g = groups.setdefault(key, {"segment": _clean(row.get("segment")), "item": _clean(row.get("item")),
                                        "kpi_name": _clean(row.get("kpi_name")),
                                        "share_scope": _clean(row.get("share_scope")), "by_doc": {}})
            if doc["doc_id"] in g["by_doc"]:
                warnings.append(
                    f"衝突: {doc['doc_id']} で同じ (segment, item, kpi_name)=({row.get('segment')}, {row.get('item')}, "
                    f"{row.get('kpi_name')}) の行が複数。先頭の行を採った（捨てた quote: {str(row.get('quote'))[:40]!r}）")
                continue
            g["by_doc"][doc["doc_id"]] = row
            if not g["share_scope"] and _clean(row.get("share_scope")):
                g["share_scope"] = _clean(row["share_scope"])

    products: List[Dict[str, Any]] = []
    for g in groups.values():
        periods: List[Dict[str, Any]] = []
        for doc in docs:
            facts = doc.get("facts") or {}
            cur = facts_slice(facts, "CurrentYear")
            prev = facts_slice(facts, "Prior1Year")
            member = find_segment_member(facts.get("segments") or {}, g["segment"])
            j = judge_period(prev, cur, member)
            row = g["by_doc"].get(doc["doc_id"])
            expect_ok, expect_basis = _expect_basis(row, doc)
            segment_label = label_of((j["cond_a"], j["cond_b"], j["cond_c"]), expect_ok)
            quote_section = _clean(row.get("quote_section")) if row else None
            kpi_value = _num(row.get("kpi_value")) if row else None
            label = product_label(row, segment_label, kpi_value, expect_ok)
            sec = ((doc.get("sections") or {}).get(quote_section) or {}) if quote_section else {}
            read = reads.get(doc["doc_id"]) or {}
            ir_source = _clean(read.get("ir_source")) if (row and quote_section == IR_SECTION_KEY) else None
            element_id = _clean(sec.get("element_id"))
            periods.append({
                "period_end": doc.get("period_end"),
                "doc_id": doc["doc_id"],
                "kpi_name": _clean(row.get("kpi_name")) if row else None,
                "share_scope": _clean(row.get("share_scope")) if row else None,
                "share_granularity": _share_granularity(row, j["share_pct"]),
                "kpi_value": kpi_value,
                "kpi_value_raw": row.get("kpi_value") if row else None,
                "kpi_unit": _clean(row.get("kpi_unit")) if row else None,
                "kpi_prev_value": _num(row.get("kpi_prev_value")) if row else None,
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
                "expectation": expect_ok,
                "expect_basis": expect_basis,
                "segment_label": segment_label,
                "label": label,
                "label_hint": _clean(row.get("label_hint")) if row else None,
                "quote": _clean(row.get("quote")) if row else None,
                "quote_section": quote_section,
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
        # 説明会資料だけが出所（旧ルールなら揃っていた）＝未完のまま件数を出す
        source_ir_only = has_names and not source_complete and all(
            _source_complete(per) or _source_ir_only(per) for per in with_row)
        products.append({
            "segment": g["segment"],
            "item": g["item"],
            "kpi_name": g["kpi_name"],
            "share_scope": g["share_scope"],
            "source_complete": source_complete,
            "source_ir_only": source_ir_only,
            "periods": periods,
            "x_backcheck": None,
        })

    complete = [p for p in products if p["source_complete"]]
    ir_only = [p for p in products if p["source_ir_only"]]
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
        "products_ver": 1,
        "products_status": status,
        "products_total": len(products),
        "complete_products": len(complete),
        "ir_only_products": len(ir_only),
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
        f"IR 由来のみで出所未完={result.get('ir_only_products', 0)} products",
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
            share = f"{_fmt(per['share_pct'])} ({per['share_basis'] or '—'})"
            conds = "/".join(_fmt(per[c]) for c in ("cond_a", "cond_b", "cond_c"))
            src = per.get("source") or {}
            src_txt = src.get("element_id") or src.get("ir_source") or "—"
            if src.get("source_kind"):
                src_txt = f"{src['source_kind']}: {src_txt}"
            kpi_txt = _fmt(per["kpi_value"]) + ((" " + per["kpi_unit"]) if per.get("kpi_unit") else "")
            if per["kpi_value"] is None and per.get("kpi_value_raw") is not None:
                kpi_txt = f"— (raw={str(per['kpi_value_raw'])[:20]!r})"
            lines.append(
                f"| {p['segment'] or '—'} | {p['item'] or '—'} | {per.get('kpi_name') or p['kpi_name'] or '—'} | {per['period_end']} | {per['doc_id']} "
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
    periods = [per for p in (prod.get("products") or []) for per in (p.get("periods") or [])]
    usage = prod.get("usage") or {}
    docs_proxy = usage.get("docs_proxy")
    if docs_proxy is None:
        docs_proxy = sum(1 for d in docs if d.get("proxy"))
    return {
        "code4": code4,
        "name": meta.get("name") or "—",
        "s33": meta.get("s33") or "—",
        "products_status": prod.get("products_status"),
        "products_total": int(prod.get("products_total") or len(prod.get("products") or [])),
        "complete_products": int(prod.get("complete_products") or 0),
        "ir_only_products": int(prod.get("ir_only_products") or 0),
        "docs": len(docs),
        "docs_with_rows": sum(1 for d in docs if int(d.get("rows") or 0) > 0),
        "docs_proxy": int(docs_proxy),
        "labels": _count([per.get("label") for per in periods]),
        "segment_labels": _count([per.get("segment_label") for per in periods]),
        "warnings": len(prod.get("warnings") or []),
    }


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
        f"complete_ge1={t['complete_ge1']} fill_rate={t['fill_rate']:.3f}",
        "",
        f"- 埋まり率 = complete_products ≥ 1 の社数 ÷ companies_total = {t['complete_ge1']} / {t['companies_total']} "
        f"= {t['fill_rate']:.3f}",
        "- 分母は「物なし」を除く前の仮置き（「物なし」は人が付けるので機械では除外しない）",
        f"- products_status=済 の社数: {t['status_done']}",
        f"- docs 合計={_fmt(t['docs_total'])} うち rows>0={_fmt(t['docs_with_rows_total'])} 代走={_fmt(t['docs_proxy_total'])}",
        f"- products.json の無い 4 桁ディレクトリ（分母に入っていない社）: {', '.join(t.get('missing_products') or []) or 'なし'}",
        f"- label 件数（合計）: {t['labels'] or '—'}",
        f"- segment_label 件数（合計）: {t['segment_labels'] or '—'}",
        "",
        "## 会社ごと",
        "",
        "| code4 | name | s33 | products_status | products_total | complete_products | ir_only_products | docs "
        "| docs rows>0 | docs_proxy | labels | segment_labels | warnings |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in agg["companies"]:
        lab = ", ".join(f"{k}={v}" for k, v in r["labels"].items()) or "—"
        seg = ", ".join(f"{k}={v}" for k, v in r["segment_labels"].items()) or "—"
        lines.append(
            f"| {r['code4']} | {r['name']} | {r['s33']} | {_fmt(r['products_status'])} | {_fmt(r['products_total'])} "
            f"| {_fmt(r['complete_products'])} | {_fmt(r['ir_only_products'])} | {_fmt(r['docs'])} "
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
    totals = {
        "companies_total": n,
        "status_done": sum(1 for r in companies if r["products_status"] == "済"),
        "complete_ge1": complete_ge1,
        "fill_rate": complete_ge1 / n,
        "fill_rate_note": "分母は「物なし」を除く前の仮置き（「物なし」は人が付けるので機械では除外しない）",
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
    args = ap.parse_args()

    if args.all:
        agg = aggregate_all(args.out_dir)
        t = agg["totals"]
        print(f"companies={t['companies_total']} status_done={t['status_done']} complete_ge1={t['complete_ge1']} "
              f"fill_rate={t['fill_rate']:.2f} wrote {' '.join(agg['paths'])}")
        return

    code4 = args.code.zfill(4)
    docs, reads = load_inputs(code4, args.data_dir, args.out_dir)
    if not docs:
        raise SystemExit(f"sections.json が無い: {args.data_dir / code4}/*.sections.json（先に scripts/yuho_fetch.py）")
    missing = [d for d, r in reads.items() if r is None]
    if missing:
        print(f"warn: read.json が無い docID={missing}（先に scripts/yuho_read.py・この期は kpi_value=null で判定だけ出す）")

    result = build_products(docs, reads)
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
          f"ir_only_products={result['ir_only_products']} periods={n_periods} "
          f"status={result['products_status']} labels={dict((l, labels.count(l)) for l in sorted(set(labels)))} "
          f"segment_labels={dict((l, seg_labels.count(l)) for l in sorted(set(seg_labels)))} "
          f"input_tokens={result['usage']['input_tokens']} output_tokens={result['usage']['output_tokens']} "
          f"docs_measured={result['usage']['docs_measured']} docs_proxy={result['usage']['docs_proxy']} "
          f"docs_unmeasured={result['usage']['docs_unmeasured']} "
          f"wrote {out}/products.json {out}/summary.md")


if __name__ == "__main__":
    main()
