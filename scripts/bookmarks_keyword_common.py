#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプラインの共通庫。

worklist生成（bookmarks_keyword_worklist.py）とingest（bookmarks_keyword_ingest.py・
別バッチで実装予定）の双方から import される正規化・照合・URL正規キー・台帳I/Oの
一元実装。設計正本: ~/.claude/docs/x-keywords-plan.md §A/§D/§E。

## 提供API

- normalize_text(s) -> str
    NFKC正規化 → casefold → URL/mention除去 → 絵文字・制御文字・ゼロ幅除去 → 空白圧縮。
- default_match_mode(keyword) -> "ascii_token" | "normalized_substring"
    ASCIIのみの語は英数字境界一致、それ以外は正規化部分一致をデフォルトとする。
- keyword_matches(keyword, mode, normalized_text) -> bool
    mode: "ascii_token"（[^a-z0-9]境界）/ "normalized_substring"（境界なし部分一致・
    偽陽性を仕様として許容）/ "exact_phrase"（英数字+CJK文字を単語とみなす境界一致）。
    日本語1文字キーワード（正規化後1文字かつ非ASCII）は常にFalse。
- canonical_url_key(url) -> str
    x.com/twitter.comのステータスIDを抽出して"status:<id>"。取れなければ
    scheme/host小文字化・query/fragment除去・末尾スラッシュ除去した正規化URL。
- load_bookmarks(path) -> (records, stats)
    stats = {total, empty_text, bad_lines: [行番号(1始まり)], unique_urls}
- append_event(ledger_path, event) -> None
    flock排他 + ts(UTC ISO)/host(platform.node())自動注入 + flush+fsync。
- read_ledger(ledger_path) -> [event, ...]
    flock(LOCK_SH)排他読み。全行JSON検証。末尾行のみ不正 → LedgerTailCorruption、
    途中行不正 → LedgerCorruption。
- pipeline_lock(ledger_path, blocking=True) -> contextmanager
    ledger_pathと同ディレクトリの.pipeline.lockに対するflock(LOCK_EX)排他区間。
    blocking=Falseでノンブロッキング取得を試み、取得できなければBlockingIOErrorを送出する
    （呼び出し側が「別プロセス実行中」として専用exit codeにマップする）。
- replay(events) -> dict（台帳イベントからの状態再構築。下記「台帳イベントスキーマ」参照）
- note_eval_parse(note_text, known_ids=None) -> (evaluations, unknown_values)
    known_idsは{(id_type, id), ...}。指定時、未知idの評価マーカーは無視する（ログのみ）。
    1行に評価マーカーが2個以上出現した場合はそのidだけを不採用にするのではなく行全体を
    unknown_valuesへ計上する（評価セルへのマーカー偽造・多重埋め込み対策）。
- sha256_of_file(path) / sha256_of_urls(url_keys)

## 台帳イベントスキーマ（B3 ingestが従うべき契約。replay()はこの形を前提に読む）

各イベント共通: {"type": ..., "ts": "<UTC ISO>", "host": "<platform.node()>", ...}
（ts/hostはappend_eventが自動注入するため呼び出し側は含めなくてよい）

- fetch_run: {"type": "fetch_run", "status": "SUCCESS"|"DEGRADED"|"FAILED"|"SKIPPED_MANUAL", ...}
    replayはstatus=="SUCCESS"の最新tsをlast_fetch_success_atとして返す。
- evaluation_batch: {"type": "evaluation_batch", "note_revision": "<sha256>",
    "evaluations": [{"id_type": "qid"|"pid", "id": "...", "mark": "✅"|"🔁"|"🔍"|"❌"}, ...]}
    (id_type, id, mark, note_revision) の4つ組で重複排除される（冪等）。
- generation: {"type": "generation", "generation": int, "revision": int,
    "generation_reason": "baseline"|"delta"|"manual_regen"|...,
    "run_id": str|null, "prompt_version": str, "schema_version": str,
    "source_snapshot": dict|null（worklist.current_snapshotのコピー）,
    "continuity_stats": list|null（worklist.continuity_statsのコピー）,
    "snapshot_urls": [canonical_url_key, ...]（世代1のみ想定・任意）,
    "delta_urls": [canonical_url_key, ...]（世代2以降・その世代が取り込んだdelta）,
    "consumed_evaluation_keys": [[id_type, id, mark, note_revision], ...],
    "evaluations_consumed": [{"id_type":, "id":, "mark":, "note_revision":}, ...]
        （consumed_evaluation_keysと同内容をdict形式で監査用に重複保持）,
    "active_clusters": [cluster, ...], "dormant_clusters": [cluster, ...],
    "proposals": [proposal, ...]（このgenerationで提示中・承認/却下待ちの提案）,
    "revision_of_reason": null（未使用の予約フィールド）}
    cluster = {"cluster_id": str, "name": str, "why": str,
        "keywords": [{"keyword": str, "mode": str}, ...],
        "queries": [{"query_id": str, "q": str, "q_simple": str, "intent": str,
                     "eval_history": [{"mark": str, "at": "<UTC ISO>"}, ...]}, ...],
        "evidence_urls": [str, ...]}
    proposal = {"proposal_id": str, "name_ja": str, "why": str,
        "keywords_ja": [str, ...], "keywords_en": [str, ...],
        "queries": [{"q":, "q_simple":, "intent":}, ...], "evidence_urls": [str, ...]}
    baseline_urls = 世代1のsnapshot_urls ∪ 以降の各generationイベントのdelta_urls。
    active_clusters/dormant_clustersは最新のgenerationイベントの値で上書き（差分累積ではない）。
    snapshotの同一性はcanonical status ID集合（canonical_url_keyの出力）で判定する
    （raw URL文字列の集合ではない。同一ツイートが著者経路違いの複数URL文字列で重複
    ブックマークされていても1件として扱う）。B3(ingest)がsnapshot_urls/delta_urlsを
    書く際もこの基準（canonical_url_key適用後の値）で統一すること。
- rejection: {"type": "rejection", "code": str, "detail": str, "run_id": str|null}
    （B3(ingest)の検証failで記録。replay()は現状パススルーで状態に反映しない）。
- proposal_decision: {"type": "proposal_decision", "proposal_id": str,
    "decision": "approve"|"reject"}
    replay()は現状パススルー。B3(ingest)は各generationイベントの"proposals"配列を
    横断するcollect_all_proposals/collect_proposal_decisions（ingest.py側の実装）で
    直接状態を再構築し、pid承認のactive昇格・却下記録に用いる（replay()の対象外）。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


class LedgerCorruption(Exception):
    """台帳の途中行が不正なJSONの場合に送出する。"""


class LedgerTailCorruption(LedgerCorruption):
    """台帳の最終行のみが不正なJSONの場合に送出する（LedgerCorruptionのサブクラス）。"""


# --- normalize_text ---------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@[A-Za-z0-9_]+")
_ZERO_WIDTH_RE = re.compile(r"[​‌‍⁠﻿]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
# よく使われる絵文字ブロックの範囲指定（形態素解析ではなく範囲ベースの除去。
# 数学記号・通貨記号等は意図的に含めない）。
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # 地域表示記号（国旗）
    "\U0001F300-\U0001F5FF"  # 絵文字・記号
    "\U0001F600-\U0001F64F"  # 顔文字
    "\U0001F680-\U0001F6FF"  # 乗り物・地図記号
    "\U0001F700-\U0001F7FF"  # 錬金術記号・幾何学図形拡張
    "\U0001F800-\U0001F8FF"  # 補助矢印-C
    "\U0001F900-\U0001F9FF"  # 補助記号・絵文字
    "\U0001FA00-\U0001FAFF"  # 記号・絵文字拡張-A/チェス記号
    "\U00002600-\U000026FF"  # その他の記号
    "\U00002700-\U000027BF"  # 装飾記号
    "\U00002B00-\U00002BFF"  # その他の記号と矢印
    "\U0000FE00-\U0000FE0F"  # 異体字セレクタ
    "\U0001F3FB-\U0001F3FF"  # 肌色修飾子
    "]+",
    flags=re.UNICODE,
)


def normalize_text(s: str) -> str:
    """NFKC正規化 → casefold → URL/mention除去 → 絵文字・制御文字・ゼロ幅除去 → 空白圧縮。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    s = _URL_RE.sub(" ", s)
    s = _MENTION_RE.sub(" ", s)
    s = _EMOJI_RE.sub(" ", s)
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _CONTROL_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


# --- match mode / keyword_matches -------------------------------------------

def default_match_mode(keyword: str) -> str:
    """ASCIIのみのキーワードは"ascii_token"、それ以外は"normalized_substring"。"""
    if keyword and all(ord(c) < 128 for c in keyword):
        return "ascii_token"
    return "normalized_substring"


def _is_single_non_ascii_char(needle: str) -> bool:
    return len(needle) == 1 and not needle.isascii()


def _boundary_ok_generic(text: str, start: int, end: int) -> bool:
    """開始/終了位置の前後が英数字・CJK文字等の「語」でなければ境界OKとする。"""
    if start > 0 and text[start - 1].isalnum():
        return False
    if end < len(text) and text[end].isalnum():
        return False
    return True


def _find_with_boundary(text: str, needle: str, boundary_ok) -> bool:
    idx = 0
    while True:
        pos = text.find(needle, idx)
        if pos == -1:
            return False
        if boundary_ok(text, pos, pos + len(needle)):
            return True
        idx = pos + 1


def keyword_matches(keyword: str, mode: str, normalized_text: str) -> bool:
    """keywordがnormalized_text（normalize_text適用済み）にmatch_modeに従って一致するか。

    日本語1文字キーワード（正規化後1文字かつ非ASCII）はmodeによらず常にFalse（禁止）。
    """
    if not keyword or not normalized_text:
        return False
    needle = normalize_text(keyword)
    if not needle:
        return False
    if _is_single_non_ascii_char(needle):
        return False

    if mode == "ascii_token":
        def _ascii_boundary_ok(text, start, end):
            if start > 0 and re.match(r"[a-z0-9]", text[start - 1]):
                return False
            if end < len(text) and re.match(r"[a-z0-9]", text[end]):
                return False
            return True

        return _find_with_boundary(normalized_text, needle, _ascii_boundary_ok)

    if mode == "normalized_substring":
        return needle in normalized_text

    if mode == "exact_phrase":
        return _find_with_boundary(normalized_text, needle, _boundary_ok_generic)

    raise ValueError(f"unknown match mode: {mode!r}")


# --- canonical_url_key -------------------------------------------------------

_STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")
_STATUS_HOSTS = {"x.com", "twitter.com"}


def canonical_url_key(url: str) -> str:
    """x.com/twitter.comのステータスIDを"status:<id>"へ。取れなければ正規化URL。"""
    if not url:
        return ""
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in _STATUS_HOSTS:
        m = _STATUS_RE.search(parsed.path or "")
        if m:
            return f"status:{m.group(1)}"

    scheme = (parsed.scheme or "https").lower()
    path = (parsed.path or "").rstrip("/")
    return f"{scheme}://{host}{path}"


# --- load_bookmarks -----------------------------------------------------------

def load_bookmarks(path) -> tuple:
    """raw JSONLを1行ずつstrict parseし、(records, stats)を返す。

    stats = {total, empty_text, bad_lines: [行番号(1始まり)], unique_urls}
    unique_urlsはraw url文字列としての重複排除数（canonical_url_keyでの正規化は
    行わない。同一ツイートが別著者経路のURL文字列で2件ブックマークされているような
    ケースはここでは別カウントのまま残る。canonical_url_key基準の重複排除が必要な
    箇所（delta計算・baseline_urls等）は呼び出し側でcanonical_url_keyを別途使う）。
    不正行は黙殺せずbad_linesに計上する（recordsには含めない）。
    """
    path = Path(path)
    records: list = []
    bad_lines: list = []
    empty_text = 0
    total = 0
    urls: set = set()

    if not path.exists():
        return records, {"total": 0, "empty_text": 0, "bad_lines": [], "unique_urls": 0}

    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            total += 1
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                bad_lines.append(lineno)
                continue
            records.append(rec)
            if not (rec.get("text") or "").strip():
                empty_text += 1
            url = rec.get("url")
            if url:
                urls.add(url)

    stats = {
        "total": total,
        "empty_text": empty_text,
        "bad_lines": bad_lines,
        "unique_urls": len(urls),
    }
    return records, stats


# --- 台帳I/O ------------------------------------------------------------------

@contextlib.contextmanager
def pipeline_lock(ledger_path, blocking: bool = True):
    """ledger_pathと同ディレクトリの.pipeline.lockに対するflock(LOCK_EX)排他区間(P1-1)。

    worklist生成・ingestの「台帳読込→検証→採番→append→latest→render」を単一の
    実行に限定し、並行実行時の二重採番・TOCTOUを防ぐ。blocking=Falseでノンブロッキング
    取得を試み、既に別プロセスが保持している場合はBlockingIOErrorを送出する
    （呼び出し側で「別プロセス実行中」として専用exit codeにマップすること）。
    """
    ledger_path = Path(ledger_path)
    lock_path = ledger_path.parent / ".pipeline.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
        fcntl.flock(fd, flags)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_event(ledger_path, event: dict) -> None:
    """台帳へ1件のイベントを追記する。ts/hostを自動注入し、flock排他+flush+fsyncする。"""
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    enriched = dict(event)
    enriched["ts"] = datetime.now(timezone.utc).isoformat()
    enriched["host"] = platform.node()
    line = json.dumps(enriched, ensure_ascii=False) + "\n"

    with open(ledger_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_ledger(ledger_path) -> list:
    """台帳の全行をJSON検証して返す。

    末尾行のみ不正 -> LedgerTailCorruption、途中行が不正 -> LedgerCorruption。
    台帳が存在しない場合は空リストを返す（初回=baselineモードの正常系）。
    flock(LOCK_SH)を保持して読むため、append_event(LOCK_EX)と排他し、途中経過の
    半端な書込を読まない（P1-6）。
    """
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return []

    with open(ledger_path, "r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            content = f.read()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    raw_lines = [ln for ln in content.splitlines() if ln.strip()]
    events: list = []
    last_idx = len(raw_lines) - 1
    for i, line in enumerate(raw_lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if i == last_idx:
                raise LedgerTailCorruption(
                    f"台帳の最終行(行{i + 1})が破損しています: {exc}"
                ) from exc
            raise LedgerCorruption(
                f"台帳の途中行(行{i + 1})が破損しています: {exc}"
            ) from exc
    return events


def replay(events: list) -> dict:
    """台帳イベント列から現在の状態を再構築する。モジュールdocstring「台帳イベント
    スキーマ」参照。generation/rejection/proposal_decisionの詳細な状態反映は
    B3(ingest)実装時に拡張する前提で、B2時点では以下を再構築する:
    last_generation / active_clusters / dormant_clusters / baseline_urls /
    last_fetch_success_at / consumed_evaluation_keys / pending_evaluations。
    """
    last_generation = None
    active_clusters: list = []
    dormant_clusters: list = []
    pending_proposals: list = []
    baseline_urls: set = set()
    consumed_evaluation_keys: set = set()
    last_fetch_success_at = None

    all_evaluations: list = []
    seen_eval_keys: set = set()

    for ev in events:
        etype = ev.get("type")

        if etype == "fetch_run":
            if ev.get("status") == "SUCCESS":
                last_fetch_success_at = ev.get("ts")

        elif etype == "evaluation_batch":
            note_revision = ev.get("note_revision")
            for item in ev.get("evaluations", []):
                key = (item.get("id_type"), item.get("id"), item.get("mark"), note_revision)
                if key in seen_eval_keys:
                    continue
                seen_eval_keys.add(key)
                all_evaluations.append({
                    "id_type": item.get("id_type"),
                    "id": item.get("id"),
                    "mark": item.get("mark"),
                    "note_revision": note_revision,
                })

        elif etype == "generation":
            last_generation = {
                "generation": ev.get("generation"),
                "revision": ev.get("revision", 0),
                "at": ev.get("ts"),
                "reason": ev.get("generation_reason"),
            }
            if "active_clusters" in ev:
                active_clusters = ev.get("active_clusters") or []
            if "dormant_clusters" in ev:
                dormant_clusters = ev.get("dormant_clusters") or []

            snapshot_urls = ev.get("snapshot_urls")
            if snapshot_urls:
                baseline_urls |= set(snapshot_urls)
            delta_urls = ev.get("delta_urls")
            if delta_urls:
                baseline_urls |= set(delta_urls)

            for key in ev.get("consumed_evaluation_keys", []):
                consumed_evaluation_keys.add(tuple(key))

        # rejection / proposal_decision: replay()は現状パススルー。B3(ingest)が
        # generationイベントの"proposals"配列を直接横断して状態を再構築する（モジュール
        # docstring「台帳イベントスキーマ」参照）。

    pending_evaluations = [
        e for e in all_evaluations
        if (e["id_type"], e["id"], e["mark"], e["note_revision"]) not in consumed_evaluation_keys
    ]

    return {
        "last_generation": last_generation,
        "active_clusters": active_clusters,
        "dormant_clusters": dormant_clusters,
        "pending_proposals": pending_proposals,
        "baseline_urls": baseline_urls,
        "consumed_evaluation_keys": consumed_evaluation_keys,
        "pending_evaluations": pending_evaluations,
        "last_fetch_success_at": last_fetch_success_at,
    }


# --- note_eval_parse ----------------------------------------------------------

_MARKER_RE = re.compile(r"<!--\s*(qid|pid):([a-z0-9-]+)\s*-->")
_VALID_MARKS = {"✅", "🔁", "🔍", "❌"}


def note_eval_parse(note_text: str, known_ids=None) -> tuple:
    """ノート本文から評価マークを収穫する。

    契約: 隠しマーカーは評価セル（`| ... |` のセル）の中に置かれる
    （例: `| ✅ <!-- qid:q-0007 --> |`）。テーブル行（行頭が"|"）以外に出現する
    マーカー（セル外コメント行）は無視する。マーカーを含むセルからマーカーを
    除去してstripした残りが: ""なら未評価（スキップ）/ ✅🔁🔍❌のいずれか単体なら
    評価として収穫 / それ以外の非空文字列はunknown_valuesへ収集する。

    P0-5対策: 1行に評価マーカーが2個以上出現した場合、LLM/raw由来テキストへの
    マーカー偽造・多重埋め込みの疑いがあるためその行全体をunknown_valuesへ計上し、
    どちらのマーカーも収穫しない。known_idsを指定した場合（{(id_type, id), ...}）、
    台帳replay由来の既知id以外へのマーカーはunknown_values扱いにはせず、
    マーカーごと無視する（ログのみ・偽造id混入に対する黙殺フィルタ）。

    戻り値: ([{id_type, id, mark}, ...], [unknown な生セル文字列, ...])
    """
    evaluations: list = []
    unknown_values: list = []

    for line in note_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        markers_in_line = _MARKER_RE.findall(line)
        if not markers_in_line:
            continue
        if len(markers_in_line) > 1:
            unknown_values.append(stripped)
            continue

        for cell in stripped.split("|"):
            m = _MARKER_RE.search(cell)
            if not m:
                continue
            id_type, id_value = m.group(1), m.group(2)
            remaining = _MARKER_RE.sub("", cell).strip()
            if remaining == "":
                continue
            if remaining in _VALID_MARKS:
                if known_ids is not None and (id_type, id_value) not in known_ids:
                    print(
                        f"[note_eval_parse] 未知id無視: {id_type}:{id_value} (mark={remaining})",
                        file=sys.stderr,
                    )
                    continue
                evaluations.append({"id_type": id_type, "id": id_value, "mark": remaining})
            else:
                unknown_values.append(remaining)

    return evaluations, unknown_values


# --- ハッシュ補助 --------------------------------------------------------------

def sha256_of_file(path) -> str:
    """path内容のsha256hexdigest。存在しなければ空文字列。"""
    path = Path(path)
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_urls(url_keys) -> str:
    """URL正規キー集合のsha256hexdigest（順序非依存にするため内部でソートする）。"""
    h = hashlib.sha256()
    for key in sorted(url_keys):
        h.update(key.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()
