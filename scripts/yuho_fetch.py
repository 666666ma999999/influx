#!/usr/bin/env python3
"""有価証券報告書（EDINET type=5 CSV）から節本文と数値 facts を抜いて sections.json にする（P-INF-34 工程①）。

設計の正本= `src/features/disclosure_kpi/plan.md` T1。流れ:
    documents_all（docID）→ EDINET `/documents/{docID}?type=5`（zip）→ `extract_sections`
    → `data/edinet/yuho/<code4>/<docID>.sections.json`

EDINET への本体リクエストは docID ごとに 1 回だけ（既存 zip があれば再取得しない）。
認証（`get_api_key`）・クエリ注入（`_inject_api_key`）・`BASE_URL` は `scripts/edinet_fetch.py`
を import して再利用する（コピーしない）。

CSV の実構造（2026-10-07 S100Y992 で実測）:
    UTF-16 LE（BOM 付き）・タブ区切り・9 列= 要素ID／項目名／コンテキストID／相対年度／
    連結・個別／期間・時点／ユニットID／単位／値。zip 内の本体は
    `XBRL_TO_CSV/jpcrp030000-asr-*.csv`（jpaud-* は監査報告書なので読まない）。
    数値は円単位の生の整数（例 199412000000）・欠測は「－」。
    セグメントの日本語名は CSV に無い（コンテキストの member ID から英語ラベルを作る）。

使い方:
    python scripts/yuho_fetch.py --code 2802 --n 3 [--no-fetch]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from edinet_fetch import BASE_URL, _inject_api_key, get_api_key  # noqa: E402

DOCS_ALL_DIR = PROJECT_ROOT / "data" / "edinet" / "documents_all"
YUHO_DIR = PROJECT_ROOT / "data" / "edinet" / "yuho"
TIMEOUT_SECONDS = 60
REQUEST_INTERVAL_SECONDS = 1.0
YUHO_DOC_TYPE_CODE = "120"  # 有価証券報告書（130=訂正は対象外）
CSV_BODY_RE = re.compile(r"jpcrp030000-asr-.*\.csv$")

# 読む節 7 つ（要素 ID の部分一致・TextBlock のみ）。複数当たったら本文が一番長いものを採る
SECTION_PATTERNS: dict[str, str] = {
    "business_policy": "BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock",
    "mdna": "ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock",
    "risks": "BusinessRisksTextBlock",
    "segment_notes": "SegmentInformation",
    "business_results": "BusinessResultsOfGroupTextBlock",
    "business_description": "DescriptionOfBusinessTextBlock",
    "production_sales": "ProductionOrdersReceivedAndSales",
}
SECTION_KEYS: tuple[str, ...] = tuple(SECTION_PATTERNS)

# 連結の数値要素（ローカル名の候補・前が優先。完全一致が無ければ部分一致で最短名を採る）
CONSOLIDATED_ELEMENTS: dict[str, tuple[str, ...]] = {
    "net_sales": ("NetSalesIFRS", "RevenueIFRS", "NetSales"),
    "gross_profit": ("GrossProfitIFRS", "GrossProfit"),
    "operating_profit": ("OperatingProfitLossIFRS", "OperatingIncome"),
    "business_profit": ("BusinessProfit",),
}
PERIOD_CONTEXTS: tuple[str, ...] = ("CurrentYearDuration", "Prior1YearDuration")
SEGMENT_SALES_RE = re.compile(r"NetSales|Revenue")
SEGMENT_PROFIT_PRIORITY: tuple[str, ...] = (
    "OperatingIncome",
    "OperatingProfit",
    "BusinessProfit",
    "SegmentProfit",
)
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
TAG_RE = re.compile(r"<[^>]+>")
ENTITY_PREFIX_RE = re.compile(r"^jpcrp\d{6}-asr_E\d{5}-\d{3}")
# 金額として採るのはユニット ID（CSV 7 列目）が JPY の行だけ（pure=比率・shares=株数・JPYPerShares=1 株あたり は採らない）
MONEY_UNIT_ID = "JPY"
UNIT_LABELS: dict[str, str] = {"JPY": "円"}
# 部分一致の候補から外すローカル名（GrossProfitMargin・RevenueIFRSSummaryOfBusinessResults 等の取り違え防止）
EXCLUDED_LOCAL_RE = re.compile(r"Margin|Ratio|Rate|Percent|PerShare|SummaryOfBusinessResults")
REDACTED = "***"


def _redact(text: str, api_key: Optional[str]) -> str:
    """例外文・ログに載せる前に API キーの文字列を `***` に置き換える（api_key が None/空なら何もしない）。"""
    if not api_key:
        return text
    return text.replace(api_key, REDACTED)


# ---------------------------------------------------------------------------
# 1. documents_all から有報の docID を引く
# ---------------------------------------------------------------------------
def find_yuho_docs(sec_code5: str, n: int, docs_dir: Path = DOCS_ALL_DIR) -> list[dict]:
    """documents_all の日次 gz から、証券コード一致の有価証券報告書（docTypeCode=120）を新しい順に n 件返す。

    Args:
        sec_code5: EDINET の secCode（5 桁・例 "28020"）。
        n: 返す件数の上限。
        docs_dir: `*.json.gz` が並ぶディレクトリ。`.rev` 入りのファイル名は読まない。

    Returns:
        各要素= {docID, submitDateTime, periodStart, periodEnd, csvFlag}。submitDateTime 降順・
        同じ docID は 1 回だけ（提出日時が新しい方を残す）。
    """
    found: dict[str, dict] = {}
    for path in sorted(docs_dir.glob("*.json.gz")):
        if ".rev" in path.name:
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        results = payload.get("results", []) if isinstance(payload, dict) else payload
        for rec in results or []:
            if not isinstance(rec, dict):
                continue
            if rec.get("docTypeCode") != YUHO_DOC_TYPE_CODE or rec.get("secCode") != sec_code5:
                continue
            doc_id = rec.get("docID")
            if not doc_id:
                continue
            item = {
                "docID": doc_id,
                "submitDateTime": rec.get("submitDateTime") or "",
                "periodStart": rec.get("periodStart"),
                "periodEnd": rec.get("periodEnd"),
                "csvFlag": rec.get("csvFlag"),
            }
            prev = found.get(doc_id)
            if prev is None or item["submitDateTime"] > prev["submitDateTime"]:
                found[doc_id] = item
    ordered = sorted(found.values(), key=lambda d: d["submitDateTime"], reverse=True)
    return ordered[:n]


# ---------------------------------------------------------------------------
# 2. EDINET から CSV zip を 1 回だけ取る
# ---------------------------------------------------------------------------
def fetch_csv_zip(doc_id: str, api_key: Optional[str], out_dir: Path) -> Path:
    """`{BASE_URL}/documents/{docID}?type=5` の zip を `out_dir/<docID>.csv.zip` に保存する。

    既存ファイル（サイズ>0）があれば EDINET を叩かずそのパスを返す（docID ごとに 1 回だけ）。

    Args:
        doc_id: EDINET docID（例 "S100Y992"）。
        api_key: `edinet_fetch.get_api_key()` の戻り値。
        out_dir: 保存先ディレクトリ（無ければ作る）。

    Returns:
        保存した zip のパス。

    Raises:
        RuntimeError: HTTP エラー・EDINET の JSON 埋め込みエラー・zip でない応答。例外文に載せる本文は
            `_redact` で api_key を `***` に伏せてある（EDINET がエラー本文にクエリをそのまま返しても鍵が漏れない）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{doc_id}.csv.zip"
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    query = _inject_api_key({"type": "5"}, api_key)
    url = f"{BASE_URL}/documents/{doc_id}?{urllib.parse.urlencode(query)}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        err_body = _redact(e.read().decode("utf-8", errors="replace"), api_key)
        raise RuntimeError(f"EDINET HTTP {e.code} for {doc_id}: {err_body[:200]}") from None
    if body[:1] in (b"{", b"["):
        # EDINET は認証エラー等を HTTP 200 + JSON body で返す（edinet_fetch.fetch_document_body と同じ検査）
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            status = parsed.get("StatusCode") or (parsed.get("metadata") or {}).get("status")
            msg = parsed.get("message") or (parsed.get("metadata") or {}).get("message", "")
            raise RuntimeError(f"EDINET StatusCode {status} for {doc_id}: {_redact(str(msg), api_key)}")
    if body[:2] != b"PK":
        # 順序は「デコード→鍵を伏せる→40 字に切る」。先に切ると鍵が途中で切れて _redact に掛からず断片が漏れる
        # （Codex 2 回目 Critical 7）
        head = _redact(body.decode("utf-8", errors="replace"), api_key)[:40]
        raise RuntimeError(f"EDINET response for {doc_id} is not a zip (head={head!r})")
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_bytes(body)
    tmp_path.replace(out_path)
    return out_path


# ---------------------------------------------------------------------------
# 3. CSV → 節本文 + 数値 facts
# ---------------------------------------------------------------------------
def read_csv_rows(zip_path: Path) -> list[list[str]]:
    """zip 内の `jpcrp030000-asr-*.csv`（UTF-16・タブ区切り）をヘッダ抜きの行リストで返す。

    Raises:
        FileNotFoundError: 本体 CSV が zip に無い。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if CSV_BODY_RE.search(n)]
        if not names:
            raise FileNotFoundError(f"jpcrp030000-asr-*.csv not in {zip_path}")
        text = zf.read(names[0]).decode("utf-16")
    rows = list(csv.reader(io.StringIO(text), delimiter="\t", quotechar='"'))
    return [r for r in rows[1:] if len(r) >= 9]


def _local_name(element_id: str) -> str:
    """`jpigp_cor:NetSalesIFRS` → `NetSalesIFRS`。"""
    return element_id.split(":", 1)[1] if ":" in element_id else element_id


def _clean_text(value: str) -> str:
    """TextBlock の値からタグを落とし、HTML 実体（`&amp;` 等）を戻し、改行を揃える。

    CSV の値は `&amp;` のまま入っている（2026-10-07 S100Y992 の MD&A を XBRL 原本と文単位で照合・
    「FP&A」「バイオ&ファイン」を含む 12 文が実体のせいで不一致になった）。
    """
    text = html.unescape(TAG_RE.sub("", value))
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _parse_number(value: str) -> Optional[float]:
    """円単位の値を数値にする。「－」・空・数値でない物は None。整数なら int。"""
    v = (value or "").strip().replace(",", "")
    if not NUMBER_RE.match(v):
        return None
    num = float(v)
    return int(num) if num.is_integer() else num


def _split_context(ctx: str) -> Optional[tuple[str, str]]:
    """`CurrentYearDuration_<member>` → ("CurrentYearDuration", "<member>")。期間が当期/前期でなければ None。"""
    if "_" not in ctx:
        return None
    period, member = ctx.split("_", 1)
    if period not in PERIOD_CONTEXTS:
        return None
    return period, member


def _member_label(member: str) -> str:
    """`jpcrp030000-asr_E00436-000SeasoningsAndFoodsReportableSegmentMember` → `Seasonings And Foods`。"""
    name = ENTITY_PREFIX_RE.sub("", member)
    name = re.sub(r"ReportableSegmentMember$", "", name)
    name = re.sub(r"Member$", "", name)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).strip() or member


def _extract_text_sections(rows: list[list[str]]) -> dict[str, Optional[dict]]:
    """7 節を要素 ID の部分一致で引く。複数当たれば本文が一番長い行を採る。"""
    sections: dict[str, Optional[dict]] = {}
    for key, pattern in SECTION_PATTERNS.items():
        best: Optional[dict] = None
        for r in rows:
            element_id = r[0]
            if pattern not in element_id or not element_id.endswith("TextBlock"):
                continue
            text = _clean_text(r[8])
            if not text:
                continue
            if best is None or len(text) > best["chars"]:
                best = {"element_id": element_id, "chars": len(text), "text": text}
        sections[key] = best
    return sections


def _pick_element(local_names: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    """候補名の優先順で完全一致→部分一致（最短名）の順に要素ローカル名を選ぶ。

    部分一致では `EXCLUDED_LOCAL_RE`（Margin／Ratio／Rate／Percent／PerShare／SummaryOfBusinessResults）
    を含む名前を候補から外す（`GrossProfit` の部分一致で `GrossProfitMargin` を採らない）。
    """
    for cand in candidates:
        if cand in local_names:
            return cand
    for cand in candidates:
        partial = sorted((n for n in local_names if cand in n and not EXCLUDED_LOCAL_RE.search(n)), key=len)
        if partial:
            return partial[0]
    return None


def _is_money_row(r: list[str]) -> bool:
    """数値行のうち金額として採ってよい行か（TextBlock でなく、ユニット ID が JPY）。"""
    return not r[0].endswith("TextBlock") and r[6] == MONEY_UNIT_ID


def _extract_consolidated(rows: list[list[str]]) -> tuple[dict, dict, set[str]]:
    """コンテキストが当期/前期ちょうどの行から連結 4 指標を取る（ユニット ID が JPY の行だけ）。

    Returns:
        (consolidated, elements, units)。consolidated= {ctx: {net_sales, gross_profit, operating_profit,
        business_profit}}・elements= {指標: 採用した要素 ID}（見つからなければ None）・units= 採用した行の
        ユニット ID の集合（金額行だけを採るので JPY のみになる）。
    """
    by_ctx: dict[str, dict[str, Any]] = {ctx: {} for ctx in PERIOD_CONTEXTS}
    full_ids: dict[str, str] = {}
    unit_of: dict[str, str] = {}
    for r in rows:
        ctx = r[2]
        if ctx not in by_ctx or not _is_money_row(r):
            continue
        local = _local_name(r[0])
        num = _parse_number(r[8])
        if num is None:
            continue
        by_ctx[ctx][local] = num
        full_ids[local] = r[0]
        unit_of[local] = r[6]
    local_names = sorted(set().union(*(d.keys() for d in by_ctx.values())))
    elements: dict[str, Optional[str]] = {}
    consolidated: dict[str, dict[str, Optional[float]]] = {ctx: {} for ctx in PERIOD_CONTEXTS}
    units: set[str] = set()
    for key, candidates in CONSOLIDATED_ELEMENTS.items():
        picked = _pick_element(local_names, candidates)
        elements[key] = full_ids.get(picked) if picked else None
        if picked:
            units.add(unit_of[picked])
        for ctx in PERIOD_CONTEXTS:
            consolidated[ctx][key] = by_ctx[ctx].get(picked) if picked else None
    return consolidated, elements, units


def _extract_segments(rows: list[list[str]]) -> dict:
    """セグメント member 行から売上・利益を取る（ユニット ID が JPY の行だけ）。

    利益要素は `SEGMENT_PROFIT_PRIORITY` の順で最初に見つかった物を全セグメント共通の
    `profit_measure` にする。`ReconcilingItemsMember`・`TotalOfReportableSegments*` は別キー。
    返り値の `units` は採用した売上・利益要素の行のユニット ID の集合。
    """
    # (member, local_name, ctx) → value
    cells: dict[tuple[str, str, str], float] = {}
    full_ids: dict[str, str] = {}
    unit_of: dict[str, str] = {}
    members: dict[str, str] = {}  # member → 種別 segment|reconciling|total
    for r in rows:
        split = _split_context(r[2])
        if split is None or not _is_money_row(r):
            continue
        period, member = split
        if "ReportableSegmentMember" in member:
            kind = "segment"
        elif "ReconcilingItems" in member:
            kind = "reconciling"
        elif "TotalOfReportableSegments" in member:
            kind = "total"
        else:
            continue
        num = _parse_number(r[8])
        if num is None:
            continue
        local = _local_name(r[0])
        cells[(member, local, period)] = num
        full_ids[local] = r[0]
        unit_of[local] = r[6]
        members[member] = kind

    seg_locals = sorted({local for (m, local, _p) in cells if members[m] == "segment"})
    profit_local = _pick_element(seg_locals, SEGMENT_PROFIT_PRIORITY)
    sales_candidates = [
        n for n in seg_locals
        if SEGMENT_SALES_RE.search(n) and "Intersegment" not in n and not EXCLUDED_LOCAL_RE.search(n)
    ]
    sales_local = sorted(sales_candidates, key=len)[0] if sales_candidates else None
    units = {unit_of[n] for n in (profit_local, sales_local) if n}

    def _ctx_values(member: str, with_sales: bool) -> dict[str, dict[str, Optional[float]]]:
        out: dict[str, dict[str, Optional[float]]] = {}
        for period in PERIOD_CONTEXTS:
            entry: dict[str, Optional[float]] = {}
            if with_sales:
                entry["net_sales"] = cells.get((member, sales_local, period)) if sales_local else None
            entry["segment_profit"] = cells.get((member, profit_local, period)) if profit_local else None
            out[period] = entry
        return out

    segments: dict[str, dict] = {}
    reconciling: dict[str, dict] = {}
    total_segments: dict[str, dict] = {}
    for member, kind in members.items():
        if kind == "segment":
            segments[member] = {"label": _member_label(member), "ctx": _ctx_values(member, True)}
        elif kind == "reconciling":
            reconciling = _ctx_values(member, True)
        else:
            total_segments = _ctx_values(member, True)
    return {
        "segments": segments,
        "profit_measure": full_ids.get(profit_local) if profit_local else None,
        "sales_measure": full_ids.get(sales_local) if sales_local else None,
        "reconciling": reconciling,
        "total_segments": total_segments,
        "units": units,
    }


def _unit_label(units: set[str]) -> Optional[str]:
    """採用した行のユニット ID から facts.unit の表示（JPY→"円"）を作る。採用行が無ければ None。

    金額行は JPY だけを採るので通常は "円" 1 種。万一 2 種以上混ざったら「／」で連結して明示する。
    """
    if not units:
        return None
    return "／".join(UNIT_LABELS.get(u, u) for u in sorted(units))


def extract_sections(zip_path: Path) -> dict:
    """CSV zip から 7 節の本文・連結 4 指標・セグメント売上/利益・会計基準を抜く。

    Args:
        zip_path: `fetch_csv_zip` が保存した zip。

    Returns:
        {accounting, sections:{key:{element_id, chars, text}|None}, facts:{unit, consolidated,
        elements, segments, profit_measure, sales_measure, reconciling, total_segments}}。
        `facts.unit` は実際に採用した行のユニット ID から出す（JPY→"円"・何も採れなければ None）。
        `facts.single_segment` は未実装（常に None）: 将来 fetch 側で「報告セグメントが単一」と判定できたら
        True を立て、yuho_judge がその時だけ share=100 の単一セグメント扱いにする。
    """
    rows = read_csv_rows(zip_path)
    accounting = "IFRS" if any(r[0].startswith("jpigp_cor:") for r in rows) else "JGAAP"
    consolidated, elements, cons_units = _extract_consolidated(rows)
    seg = _extract_segments(rows)
    facts = {
        "unit": _unit_label(cons_units | seg["units"]),
        "single_segment": None,
        "consolidated": consolidated,
        "elements": elements,
        "segments": seg["segments"],
        "profit_measure": seg["profit_measure"],
        "sales_measure": seg["sales_measure"],
        "reconciling": seg["reconciling"],
        "total_segments": seg["total_segments"],
    }
    return {"accounting": accounting, "sections": _extract_text_sections(rows), "facts": facts}


# ---------------------------------------------------------------------------
# 4. 1 書類の通し + CLI
# ---------------------------------------------------------------------------
def build_sections_json(code4: str, doc: dict, zip_path: Path) -> dict:
    """documents_all のメタ + `extract_sections` の結果を sections.json の形に組む。"""
    extracted = extract_sections(zip_path)
    submit = doc.get("submitDateTime") or ""
    return {
        "code4": code4,
        "sec_code": f"{code4}0",
        "doc_id": doc["docID"],
        "submit_date": submit[:10] or None,
        "period_start": doc.get("periodStart"),
        "period_end": doc.get("periodEnd"),
        "accounting": extracted["accounting"],
        "sections": extracted["sections"],
        "facts": extracted["facts"],
    }


def summary_line(path: Path, payload: dict) -> str:
    """`saved <path> sections=<有本文数>/7 segments=<数> accounting=<IFRS|JGAAP>` の 1 行。"""
    filled = sum(1 for k in SECTION_KEYS if payload["sections"].get(k))
    n_seg = len(payload["facts"]["segments"])
    return (
        f"saved {path} sections={filled}/{len(SECTION_KEYS)} segments={n_seg} "
        f"accounting={payload['accounting']}"
    )


def run(code4: str, n: int, no_fetch: bool, docs_dir: Path = DOCS_ALL_DIR,
        out_root: Path = YUHO_DIR) -> tuple[list[Path], list[dict]]:
    """`--code` の直近 n 期分を通して sections.json を書く。

    Returns:
        (saved, failed)。saved= 書いた sections.json のパス・failed= 保存できなかった docID と理由
        （`{"docID", "reason"}`・reason は "CSV なし（csvFlag=0）"／"zip なし（--no-fetch）"／"鍵なし"）。
        csvFlag が "0" の docID は EDINET を叩かずスキップする。鍵が無い時も他の docID（zip 済み）は処理を続ける。
    """
    docs = find_yuho_docs(f"{code4}0", n, docs_dir)
    if not docs:
        print(f"no yuho docs for {code4} in {docs_dir}", file=sys.stderr)
        return [], []
    out_dir = out_root / code4
    out_dir.mkdir(parents=True, exist_ok=True)
    api_key = None if no_fetch else get_api_key()
    saved: list[Path] = []
    failed: list[dict] = []
    requests_made = 0
    for doc in docs:
        doc_id = doc["docID"]
        zip_path = out_dir / f"{doc_id}.csv.zip"
        if not zip_path.exists():
            if str(doc.get("csvFlag")) == "0":
                print(f"skip {doc_id}: CSV なし（csvFlag=0・EDINET を叩かない）", file=sys.stderr)
                failed.append({"docID": doc_id, "reason": "CSV なし（csvFlag=0）"})
                continue
            if no_fetch:
                print(f"skip {doc_id}: no zip (--no-fetch)", file=sys.stderr)
                failed.append({"docID": doc_id, "reason": "zip なし（--no-fetch）"})
                continue
            if not api_key:
                print(f"skip {doc_id}: EDINET_API_KEY が無い（環境変数か ~/.zshrc）", file=sys.stderr)
                failed.append({"docID": doc_id, "reason": "鍵なし"})
                continue
            if requests_made > 0:
                time.sleep(REQUEST_INTERVAL_SECONDS)
            requests_made += 1
            zip_path = fetch_csv_zip(doc_id, api_key, out_dir)
        payload = build_sections_json(code4, doc, zip_path)
        out_path = out_dir / f"{doc_id}.sections.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(summary_line(out_path, payload))
        saved.append(out_path)
    return saved, failed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="有報 CSV → sections.json（P-INF-34 工程①）")
    parser.add_argument("--code", required=True, help="証券コード 4 桁（例 2802）")
    parser.add_argument("--n", type=int, default=3, help="直近何期分か（既定 3）")
    parser.add_argument("--no-fetch", action="store_true", help="EDINET を叩かず既存 zip だけ処理する")
    args = parser.parse_args(argv)
    code4 = args.code.strip()
    if not re.fullmatch(r"\d{4}", code4):
        parser.error("--code は 4 桁の数字")
    saved, failed = run(code4, args.n, args.no_fetch, DOCS_ALL_DIR, YUHO_DIR)
    if failed:
        reasons = ", ".join(f"{f['docID']}={f['reason']}" for f in failed)
        print(f"failed {len(failed)}/{len(saved) + len(failed)} docs: {reasons}", file=sys.stderr)
    # 保存できなかった docID が 1 件でもあれば exit 1（成功分の sections.json は書いたまま）
    return 0 if saved and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
