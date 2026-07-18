#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプライン: ingest CLI（柵・台帳・render）。

設計正本: ~/.claude/docs/x-keywords-plan.md §B/§C/§D/§F/§G（B3バッチ）。
LLM が生成した result md（``` json x-keywords-generation フェンス1個）を検証し、
台帳（keywords_ledger.jsonl）へ generation イベントを追記、latest.json を再導出、
Vault ノートを決定的に render する。bookmarks_keyword_common.py（共通庫）の
既存関数はそのまま利用し、変更は行わない。

## 処理順（検証は§B/§Fの通り厳格に・失敗したら理由を構造化してrejectイベント+exit 6）

0. パイプライン全体を.pipeline.lockのflock(LOCK_EX・ノンブロッキング)で排他取得
   （取得失敗=別プロセス実行中はexit 7。P1-1）
0.5. --baseline/--render-only以外の通常ingestでは、worklistの真正性・鮮度を検証する
   （trigger.fired・fetch_status・raw urls_sha256一致・台帳末尾世代との一致。P0-3）
1. result md 全文の秘密スキャン（9パターン・raw + NFKC/制御除去 + 全空白除去の3ビュー）
2. ```json x-keywords-generation フェンスがちょうど1個であることの確認 + JSON parse
3. schema検証（op種別・前世代active idのちょうど1回出現・name/why/keywords長・queries数）
4. クエリ実用性検証（120字・OR/引用句/除外上限・演算子allowlist・q_simpleのengagement演算子禁止）
5. evidence検証（URL実在・本文非空・keyword一致・使い回し上限）
6. 評価制約（✅逐語保持・❌再出現禁止。pending_evaluationsは台帳replayから都度再取得）
7. slot処理（pid✅承認の昇格・proposals 3件切り詰め・active合計12上限）
8. ノートのTOCTOU比較（worklist.note_hash vs 現在のノートhash）を台帳append前に実施
   （不一致ならexit 3・台帳には何も書かない。P0-4）

受理後: proposal_decision イベント（あれば）→ generation イベントの順で台帳へ追記 →
replay で latest.json 再導出 → ノート render（最終書込直前にもTOCTOU再比較・多重防御）。

## CLI

    python3 scripts/bookmarks_keyword_ingest.py <result_md> [--worklist PATH] \\
        [--raw PATH] [--ledger PATH] [--latest PATH] [--note PATH] \\
        [--baseline] [--render-only]

exit code: 0=受理(UPDATED) / 3=note TOCTOU中止 / 5=台帳破損 / 6=検証reject /
7=別プロセス実行中(パイプラインロック取得失敗)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bookmarks_keyword_common as common  # noqa: E402

DEFAULT_WORKLIST = "output/bookmarks/keyword_worklist.json"
DEFAULT_RAW = "output/bookmarks.jsonl"
DEFAULT_LEDGER = "output/bookmarks/keywords_ledger.jsonl"
DEFAULT_LATEST = "output/bookmarks/keywords_latest.json"
DEFAULT_NOTE = str(
    Path.home() / "Documents" / "Obsidian Vault" / "02_Ai" / "x-buzz" / "influx-x" / "influx_x_search_keywords.md"
)

ACTIVE_CLUSTER_LIMIT = 12  # bookmarks_keyword_worklist.ACTIVE_CLUSTER_LIMIT と同値を維持すること
PROPOSALS_DISPLAY_LIMIT = 3
NAME_MAX = 60
WHY_MAX = 200
KEYWORD_MIN_LEN = 2
KEYWORD_MAX_LEN = 40
KEYWORDS_MIN_TOTAL = 2
QUERIES_MIN_PER_CLUSTER = 2
QUERIES_MAX_PER_CLUSTER = 5
NOTES_MAX = 2000
EVIDENCE_MIN_PROPOSE = 3
EVIDENCE_MIN_EXISTING = 1
EVIDENCE_URL_REUSE_MAX = 2
QUERY_MAX_LEN = 120
OR_MAX = 3
QUOTE_MAX = 2
EXCLUDE_MAX = 2
ALLOWED_OPERATORS = {"min_faves", "min_retweets", "lang", "filter", "since", "until"}
ENGAGEMENT_OPERATORS = {"min_faves", "min_retweets"}


class ValidationReject(Exception):
    """検証failで台帳にrejectionイベントを記録しexit 6すべき場合に送出する。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class LedgerFailure(Exception):
    """台帳破損。exit 5で即終了すべき場合に送出する。"""


# --- 秘密スキャン（~/.claude/scripts/wiki_ingest_apply.py の9パターンを参考に同等実装） ---

_SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"sk-ant-[a-zA-Z0-9-]{20,}",
    r"sk-[a-zA-Z0-9]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"xox[baprs]-[0-9a-zA-Z-]{10,}",
    r"AIza[0-9A-Za-z_\-]{35}",
    r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY",
    r"(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[\"']?[^\s\"']{6,}",
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.",
]
_SECRET_RE = [re.compile(p, re.IGNORECASE) for p in _SECRET_PATTERNS]


def _sanitize_view(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")


_ALL_WHITESPACE_RE = re.compile(r"\s+")


def _whitespace_collapsed_view(s: str) -> str:
    """秘密スキャン回避対策(P1-3): 改行を含む全空白を除去したビュー。
    改行等で分割された秘密文字列（例: AWSキーの途中に改行を挟んで検出を回避する手口）を
    連結した状態で検出できるようにする。"""
    return _ALL_WHITESPACE_RE.sub("", _sanitize_view(s))


def scan_secrets(text: str):
    """秘密パターンにヒットしたら該当パターン文字列、無ければNone。
    raw+行結合ビュー+全空白除去ビューの3ビューを走査する。"""
    for view in (text, _sanitize_view(text), _whitespace_collapsed_view(text)):
        for rx in _SECRET_RE:
            if rx.search(view):
                return rx.pattern
    return None


# --- フェンス抽出 -------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^```json[ \t]+x-keywords-generation\b", re.MULTILINE)
_FENCE_FULL_RE = re.compile(
    r"^```json[ \t]+x-keywords-generation[ \t]*\r?\n(.*?)\r?\n```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def extract_fence(result_text: str) -> dict:
    opens = _FENCE_OPEN_RE.findall(result_text)
    blocks = _FENCE_FULL_RE.findall(result_text)
    if len(opens) != 1 or len(blocks) != 1:
        raise ValidationReject(
            "fence_count", f"x-keywords-generation フェンスが1個でない (open={len(opens)} closed={len(blocks)})"
        )
    try:
        payload = json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise ValidationReject("fence_json_invalid", str(exc)) from exc
    if not isinstance(payload, dict):
        raise ValidationReject("fence_not_object", "フェンス中身がJSONオブジェクトでない")
    return payload


_FRONTMATTER_KV_RE = re.compile(r"^prompt_version:\s*(.+)$", re.MULTILINE)


def extract_prompt_version(result_text: str) -> str:
    """result md先頭のfrontmatterから prompt_version: を拾う。無ければ"unknown"。"""
    if result_text.startswith("---"):
        end = result_text.find("\n---", 3)
        if end != -1:
            fm = result_text[:end]
            m = _FRONTMATTER_KV_RE.search(fm)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return "unknown"


# --- schema検証 ---------------------------------------------------------------

def _require_str(d, key, where):
    v = d.get(key)
    if not isinstance(v, str):
        raise ValidationReject("malformed_schema", f"{where}.{key} が文字列でない")
    return v


def _require_list(d, key, where):
    v = d.get(key)
    if not isinstance(v, list):
        raise ValidationReject("malformed_schema", f"{where}.{key} が配列でない")
    return v


def _is_single_non_ascii_char(s: str) -> bool:
    return len(s) == 1 and not s.isascii()


def validate_keywords(keywords_ja, keywords_en, where):
    all_kw = list(keywords_ja) + list(keywords_en)
    if len(all_kw) < KEYWORDS_MIN_TOTAL:
        raise ValidationReject("keywords_too_few", f"{where}: keywords合計が{KEYWORDS_MIN_TOTAL}未満")
    for kw in all_kw:
        if not isinstance(kw, str) or not (KEYWORD_MIN_LEN <= len(kw) <= KEYWORD_MAX_LEN):
            raise ValidationReject("keyword_length", f"{where}: keyword長不正 ({kw!r})")
        if _is_single_non_ascii_char(kw):
            raise ValidationReject("keyword_single_ja_char", f"{where}: 日本語1文字キーワード禁止 ({kw!r})")
    return all_kw


def validate_name_why(entry, where):
    name = _require_str(entry, "name_ja", where)
    if not (1 <= len(name) <= NAME_MAX):
        raise ValidationReject("name_length", f"{where}.name_ja 長さ不正")
    why = _require_str(entry, "why", where)
    if not (1 <= len(why) <= WHY_MAX):
        raise ValidationReject("why_length", f"{where}.why 長さ不正")
    return name, why


def validate_query_text(q: str, where: str, is_simple: bool):
    if not isinstance(q, str) or not q:
        raise ValidationReject("query_empty", f"{where} が空/非文字列")
    if len(q) > QUERY_MAX_LEN:
        raise ValidationReject("query_too_long", f"{where} が{QUERY_MAX_LEN}字超")
    if re.search(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", q):
        raise ValidationReject("query_control_char", f"{where} に改行/制御文字")
    if "://" in q:
        raise ValidationReject("query_url_forbidden", f"{where} にURLを含む")
    if len(re.findall(r"\bOR\b", q)) > OR_MAX:
        raise ValidationReject("query_or_exceeded", f"{where} のOR演算子が{OR_MAX}件超")
    if len(re.findall(r'"[^"]*"', q)) > QUOTE_MAX:
        raise ValidationReject("query_quote_exceeded", f"{where} の引用句が{QUOTE_MAX}件超")
    if len(re.findall(r"(?:^|\s)-\S+", q)) > EXCLUDE_MAX:
        raise ValidationReject("query_exclude_exceeded", f"{where} の除外(-)が{EXCLUDE_MAX}件超")

    seen_engagement = False
    for m in re.finditer(r"-?\b([a-zA-Z_]+):(\S*)", q):
        op_name = m.group(1)
        if op_name not in ALLOWED_OPERATORS:
            raise ValidationReject("query_unknown_operator", f"{where} に未知の演算子 {op_name}:")
        if op_name in ENGAGEMENT_OPERATORS:
            seen_engagement = True
    if is_simple and seen_engagement:
        raise ValidationReject("query_simple_has_engagement_op", f"{where} にエンゲージ系演算子を含む")


def validate_queries(queries, where):
    if not (QUERIES_MIN_PER_CLUSTER <= len(queries) <= QUERIES_MAX_PER_CLUSTER):
        raise ValidationReject("queries_count", f"{where}: queries数が{QUERIES_MIN_PER_CLUSTER}-{QUERIES_MAX_PER_CLUSTER}の範囲外")
    for i, q_entry in enumerate(queries):
        if not isinstance(q_entry, dict):
            raise ValidationReject("malformed_schema", f"{where}.queries[{i}] がオブジェクトでない")
        q = _require_str(q_entry, "q", f"{where}.queries[{i}]")
        q_simple = _require_str(q_entry, "q_simple", f"{where}.queries[{i}]")
        _require_str(q_entry, "intent", f"{where}.queries[{i}]")
        validate_query_text(q, f"{where}.queries[{i}].q", is_simple=False)
        validate_query_text(q_simple, f"{where}.queries[{i}].q_simple", is_simple=True)


def validate_evidence_urls(entry, where, min_count):
    urls = _require_list(entry, "evidence_urls", where)
    if len(urls) < min_count:
        raise ValidationReject("evidence_too_few", f"{where}: evidence_urlsが{min_count}件未満")
    for u in urls:
        if not isinstance(u, str) or not u:
            raise ValidationReject("malformed_schema", f"{where}.evidence_urls に非文字列/空")
    return urls


def validate_notes(payload):
    notes = payload.get("notes")
    if notes is not None:
        if not isinstance(notes, str) or len(notes) > NOTES_MAX:
            raise ValidationReject("notes_too_long", f"notes が{NOTES_MAX}字超")


# --- id coverage検証 -----------------------------------------------------------

def check_active_id_coverage(cluster_entries, prev_active_ids, prev_dormant_ids):
    """前世代active idがclusters[]にちょうど1回、reactivateはdormant idのみ、を検証する。"""
    id_counts = Counter()
    unknown_active = []
    unknown_dormant = []
    for entry in cluster_entries:
        cid = entry.get("id")
        op = entry.get("op")
        if not isinstance(cid, str) or not cid:
            raise ValidationReject("malformed_schema", "clusters[].id が空/非文字列")
        id_counts[cid] += 1
        if op in ("keep", "revise") and cid not in prev_active_ids:
            unknown_active.append(cid)
        elif op == "reactivate" and cid not in prev_dormant_ids:
            unknown_dormant.append(cid)
        elif op not in ("keep", "revise", "reactivate"):
            raise ValidationReject("clusters_invalid_op", f"clusters[]に不正なop: {op!r}")

    if unknown_active:
        raise ValidationReject("unknown_active_id", f"未知/非active id: {unknown_active}")
    if unknown_dormant:
        raise ValidationReject("unknown_dormant_id", f"reactivateがdormant以外を参照: {unknown_dormant}")

    dup_ids = [cid for cid, c in id_counts.items() if c > 1]
    if dup_ids:
        raise ValidationReject("duplicate_cluster_id", f"clusters[]内でid重複: {dup_ids}")

    missing = [cid for cid in prev_active_ids if id_counts.get(cid, 0) == 0]
    if missing:
        raise ValidationReject("missing_active_id", f"前世代active idが欠落: {missing}")


# --- id採番 ---------------------------------------------------------------

def new_cluster_id() -> str:
    return f"c-{uuid.uuid4().hex[:8]}"


def new_query_id() -> str:
    return f"q-{uuid.uuid4().hex[:8]}"


def new_proposal_id() -> str:
    return f"p-{uuid.uuid4().hex[:8]}"


def resolve_query_ids(queries_in, prev_cluster, run_ts):
    """前世代同一clusterでqが逐語一致ならqidを継承し、eval_historyを引き継ぐ。それ以外は新規採番。"""
    prev_by_text = {}
    if prev_cluster:
        for pq in prev_cluster.get("queries", []):
            prev_by_text[pq.get("q")] = pq
    out = []
    for q_in in queries_in:
        prev_q = prev_by_text.get(q_in["q"])
        if prev_q:
            qid = prev_q.get("query_id") or new_query_id()
            eval_history = list(prev_q.get("eval_history", []))
        else:
            qid = new_query_id()
            eval_history = []
        out.append({
            "query_id": qid,
            "q": q_in["q"],
            "q_simple": q_in["q_simple"],
            "intent": q_in.get("intent", ""),
            "eval_history": eval_history,
        })
    return out


def build_keyword_objs(keywords_ja, keywords_en):
    return [
        {"keyword": kw, "mode": common.default_match_mode(kw)}
        for kw in list(keywords_ja) + list(keywords_en)
    ]


# --- evidence 実データ照合 -----------------------------------------------------

def build_raw_lookup(records):
    """canonical_url_key -> record（最初に出現したものを採用）。"""
    lookup = {}
    for rec in records:
        url = rec.get("url")
        if not url:
            continue
        key = common.canonical_url_key(url)
        if key not in lookup:
            lookup[key] = rec
    return lookup


def validate_evidence_against_raw(evidence_urls, keyword_objs, raw_lookup, where):
    """URL実在+本文非空+keyword一致(≥1)をPythonで再検証する。"""
    for u in evidence_urls:
        key = common.canonical_url_key(u)
        rec = raw_lookup.get(key)
        if rec is None:
            raise ValidationReject("evidence_url_not_found", f"{where}: rawに実在しないURL {u!r}")
        text = (rec.get("text") or "").strip()
        if not text:
            raise ValidationReject("evidence_url_empty_text", f"{where}: 本文が空のURL {u!r}")
        norm = common.normalize_text(text)
        matched = any(common.keyword_matches(kw["keyword"], kw["mode"], norm) for kw in keyword_objs)
        if not matched:
            raise ValidationReject("evidence_keyword_mismatch", f"{where}: keyword不一致のURL {u!r}")


def check_url_reuse(entries_evidence):
    """entries_evidence = [(where, [url,...]), ...]。同一canonical URLの使い回しを検出する。"""
    usage = Counter()
    for where, urls in entries_evidence:
        for u in set(common.canonical_url_key(x) for x in urls):
            usage[u] += 1
    overused = [k for k, c in usage.items() if c > EVIDENCE_URL_REUSE_MAX]
    if overused:
        raise ValidationReject("evidence_url_reuse_exceeded", f"URL使い回し上限超過: {overused}")


# --- 評価制約（qidの✅逐語保持・❌再出現禁止） --------------------------------

def build_qid_index(prev_active_clusters, prev_dormant_clusters):
    """qid -> (cluster_id, 旧q文本) のマップを構築する。"""
    idx = {}
    for cluster in list(prev_active_clusters) + list(prev_dormant_clusters):
        cid = cluster.get("cluster_id")
        for q in cluster.get("queries", []):
            qid = q.get("query_id")
            if qid:
                idx[qid] = (cid, q.get("q"))
    return idx


def check_checkmark_constraints(pending_evaluations, qid_index, resolved_cluster_texts):
    """✅の付いたqidの旧qがresolved後のclusterに逐語で残っているか、❌なら消えているかを検証する。

    resolved_cluster_texts: {cluster_id: set(qのテキスト)}（このgenerationでclusters[]により
    処理されたclusterのみ。dormantのまま/reactivateされないclusterは対象外＝評価は保留のまま残す）。
    戻り値: 消費してよい (id_type, id, mark) のリスト（qid分のみ。pidは別処理）。
    """
    consumable = []
    for ev in pending_evaluations:
        if ev.get("id_type") != "qid":
            continue
        qid = ev.get("id")
        mark = ev.get("mark")
        info = qid_index.get(qid)
        if info is None:
            continue  # 未知/既に消えたqid: 判定不能なので保留のまま無視
        cid, old_text = info
        if cid not in resolved_cluster_texts:
            continue  # このgenerationで触られていないcluster: 保留のまま
        texts = resolved_cluster_texts[cid]
        if mark == "✅" and old_text not in texts:
            raise ValidationReject("checkmark_violated", f"✅付きqid {qid} の文言が保持されていない")
        if mark == "❌" and old_text in texts:
            raise ValidationReject("reject_mark_violated", f"❌付きqid {qid} の文言が再出現している")
        consumable.append(ev)
    return consumable


def append_consumed_eval_history(resolved_clusters, consumed_qid_evals, run_ts):
    """今回consumeしたqid評価をeval_historyへ追記する。

    render_note の _cluster_section が eval_history[-1] を「前回評価」列に表示する
    実装のため、ここで書かないと評価がconsumeされても列が永久に空欄のままになる。
    """
    by_qid = {}
    for cluster in resolved_clusters:
        for q in cluster.get("queries", []):
            by_qid[q["query_id"]] = q
    at_iso = run_ts.isoformat()
    for ev in consumed_qid_evals:
        q = by_qid.get(ev.get("id"))
        if q is None:
            continue
        q.setdefault("eval_history", []).append({"mark": ev.get("mark"), "at": at_iso})


# --- proposal（pid）処理 -------------------------------------------------------

def collect_all_proposals(events):
    """全generationイベントのproposals配列からproposal_id -> proposal本体を集める。"""
    by_id = {}
    for ev in events:
        if ev.get("type") == "generation":
            for p in ev.get("proposals", []) or []:
                pid = p.get("proposal_id")
                if pid:
                    by_id[pid] = p
    return by_id


def collect_proposal_decisions(events):
    approved, rejected = set(), set()
    for ev in events:
        if ev.get("type") == "proposal_decision":
            pid = ev.get("proposal_id")
            if ev.get("decision") == "approve":
                approved.add(pid)
            elif ev.get("decision") == "reject":
                rejected.add(pid)
    return approved, rejected


def build_cluster_from_proposal(proposal, run_ts):
    keyword_objs = [
        {"keyword": kw, "mode": common.default_match_mode(kw)}
        for kw in list(proposal.get("keywords_ja", [])) + list(proposal.get("keywords_en", []))
    ]
    queries = [
        {
            "query_id": new_query_id(),
            "q": q["q"], "q_simple": q["q_simple"], "intent": q.get("intent", ""),
            "eval_history": [],
        }
        for q in proposal.get("queries", [])
    ]
    return {
        "cluster_id": new_cluster_id(),
        "name": proposal.get("name_ja", ""),
        "why": proposal.get("why", ""),
        "keywords": keyword_objs,
        "queries": queries,
        "evidence_urls": proposal.get("evidence_urls", []),
    }


def process_pid_evaluations(pending_evaluations, all_proposals_by_id, approved_ids, rejected_ids,
                             current_active_count, active_limit, run_ts):
    """pidへの✅/❌評価を処理する。

    戻り値: (decision_events, promoted_clusters, consumed_pid_evals)
    """
    decision_events = []
    promoted_clusters = []
    consumed = []
    active_count = current_active_count
    decided_this_run = set()

    for ev in pending_evaluations:
        if ev.get("id_type") != "pid":
            continue
        pid = ev.get("id")
        mark = ev.get("mark")
        if pid in decided_this_run:
            continue
        proposal = all_proposals_by_id.get(pid)
        if proposal is None or pid in approved_ids or pid in rejected_ids:
            continue  # 未知/既決定のpid評価: 保留のまま無視

        if mark == "❌":
            decision_events.append({"type": "proposal_decision", "proposal_id": pid, "decision": "reject"})
            consumed.append(ev)
            decided_this_run.add(pid)
        elif mark == "✅":
            if active_count >= active_limit:
                continue  # slot満杯: このpidは次世代へ持ち越し（保留のまま）
            decision_events.append({"type": "proposal_decision", "proposal_id": pid, "decision": "approve"})
            promoted_clusters.append(build_cluster_from_proposal(proposal, run_ts))
            active_count += 1
            consumed.append(ev)
            decided_this_run.add(pid)

    return decision_events, promoted_clusters, consumed


# --- worklist / raw ロード -----------------------------------------------------

def load_worklist(path):
    path = Path(path)
    if not path.exists():
        raise ValidationReject("worklist_missing", f"worklistが存在しない: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationReject("worklist_invalid", str(exc)) from exc


def validate_worklist_freshness(worklist_data, replay_state, records):
    """--baseline/--render-only以外の通常ingestで、worklistの真正性・鮮度を検証する(P0-3)。

    (a) worklist.trigger.fired が真でなければreject（未発火のworklistを誤って
        消費しないため）。
    (b) worklist.fetch_status が SUCCESS、またはforce起因のSKIPPED_MANUAL以外なら
        reject（DEGRADED/FAILEDのworklistでの生成を防ぐ）。
    (c) 現在rawのcanonical urls_sha256がworklist記録時のcurrent_snapshotと一致
        しなければreject（worklist生成後にrawが変化した場合の不整合検出）。
    (d) worklist.previous_generationが現在の台帳末尾世代(generation/revision)と
        一致しなければreject（worklist生成後に他プロセスがingestを進めていた場合の
        古いworklistの再利用を防ぐ）。
    """
    trigger = worklist_data.get("trigger") or {}
    if trigger.get("fired") is not True:
        raise ValidationReject("trigger_not_fired", "worklist.trigger.fired が真でない")

    trigger_reason = trigger.get("reason", "")
    is_force = isinstance(trigger_reason, str) and trigger_reason.startswith("force:")
    fetch_status = worklist_data.get("fetch_status")
    if not (fetch_status == "SUCCESS" or (fetch_status == "SKIPPED_MANUAL" and is_force)):
        raise ValidationReject(
            "fetch_status_invalid",
            f"worklist.fetch_status が不正: {fetch_status!r} (force={is_force})",
        )

    current_keys = {common.canonical_url_key(r["url"]) for r in records if r.get("url")}
    current_sha = common.sha256_of_urls(current_keys)
    worklist_sha = (worklist_data.get("current_snapshot") or {}).get("urls_sha256")
    if current_sha != worklist_sha:
        raise ValidationReject(
            "raw_changed_since_worklist",
            "現在のraw urls_sha256がworklist記録時のcurrent_snapshotと一致しない",
        )

    last_gen = replay_state.get("last_generation")
    expected_gen = last_gen.get("generation") if last_gen else None
    expected_rev = last_gen.get("revision") if last_gen else None
    prev_gen_ref = worklist_data.get("previous_generation") or {}
    if prev_gen_ref.get("generation") != expected_gen or prev_gen_ref.get("revision") != expected_rev:
        raise ValidationReject(
            "ledger_advanced_since_worklist",
            f"worklist.previous_generation(gen={prev_gen_ref.get('generation')} "
            f"rev={prev_gen_ref.get('revision')})が現在の台帳末尾"
            f"(gen={expected_gen} rev={expected_rev})と一致しない",
        )


def atomic_write_json(path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-latest-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-note-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- generation番号決定 ---------------------------------------------------------

def determine_generation_numbering(is_baseline, replay_state, worklist_data):
    if is_baseline:
        if replay_state["last_generation"] is not None:
            raise ValidationReject("baseline_on_existing_ledger", "既にgenerationが存在するのに--baseline指定")
        return 1, 0, "baseline"

    last_gen = replay_state["last_generation"]
    if last_gen is None:
        raise ValidationReject("no_baseline_yet", "台帳にgenerationが無い状態での通常ingest（--baseline必要）")

    trigger_reason = (worklist_data.get("trigger") or {}).get("reason", "")
    is_force = isinstance(trigger_reason, str) and trigger_reason.startswith("force:")
    prev_snapshot_sha = common.sha256_of_urls(replay_state["baseline_urls"])
    current_sha = (worklist_data.get("current_snapshot") or {}).get("urls_sha256")

    if is_force and current_sha == prev_snapshot_sha:
        return last_gen["generation"], last_gen.get("revision", 0) + 1, "manual_regen"
    return last_gen["generation"] + 1, 0, "delta"


# --- latest.json 導出 -----------------------------------------------------------

def derive_latest(events):
    state = common.replay(events)
    all_proposals = collect_all_proposals(events)
    approved, rejected = collect_proposal_decisions(events)
    pending_proposal_ids = [pid for pid in all_proposals if pid not in approved and pid not in rejected]
    pending_proposals = [all_proposals[pid] for pid in pending_proposal_ids]

    generation_events = [e for e in events if e.get("type") == "generation"]
    history = [
        {
            "generation": e.get("generation"), "revision": e.get("revision", 0),
            "generation_reason": e.get("generation_reason"), "ts": e.get("ts"),
        }
        for e in generation_events[-5:]
    ]

    last_gen_event = generation_events[-1] if generation_events else None

    return {
        "generation": state["last_generation"]["generation"] if state["last_generation"] else None,
        "revision": state["last_generation"].get("revision", 0) if state["last_generation"] else None,
        "generation_reason": state["last_generation"].get("reason") if state["last_generation"] else None,
        "generated_at": state["last_generation"].get("at") if state["last_generation"] else None,
        "active_clusters": state["active_clusters"],
        "dormant_clusters": state["dormant_clusters"],
        "proposals": pending_proposals,
        "baseline_url_count": len(state["baseline_urls"]),
        "last_fetch_success_at": state["last_fetch_success_at"],
        "source_snapshot": last_gen_event.get("source_snapshot") if last_gen_event else None,
        "history": history,
    }


# --- ノート render --------------------------------------------------------------

_EVAL_LEGEND = "✅使えた ／ 🔁多すぎ ／ 🔍少なすぎ ／ ❌使わない（セルには記号1つだけを入れてください）"


def _sanitize_md_text(value) -> str:
    """LLM/raw由来の全文字列（name_ja, why, q, q_simple, intent, 抜粋）をrenderに使う前に
    無害化する(P0-5a): テーブル破壊(|)・改行によるレイアウト崩れ・評価マーカー偽造
    (<!--)を防ぐ。マーカー自体（<!-- qid:.../pid:... -->）はingestが自ら生成する
    query_id/proposal_idを埋め込む箇所であり、この関数の対象外（呼び出し側で別途組み立てる）。
    """
    s = "" if value is None else str(value)
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = s.replace("|", "\\|")
    s = s.replace("<!--", "<!ー-")
    return s


def _window_counts(evidence_records_created_at, now):
    windows = {"d30": 0, "d90": 0, "d365": 0, "all": len(evidence_records_created_at)}
    for created_at in evidence_records_created_at:
        if not created_at:
            continue
        try:
            v = str(created_at).replace("Z", "+00:00")
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        age_days = (now - dt).total_seconds() / 86400.0
        if age_days <= 30:
            windows["d30"] += 1
        if age_days <= 90:
            windows["d90"] += 1
        if age_days <= 365:
            windows["d365"] += 1
    return windows


def _excerpt_lines(evidence_urls, raw_lookup, limit=3):
    lines = []
    for u in evidence_urls[:limit]:
        rec = raw_lookup.get(common.canonical_url_key(u))
        if not rec:
            continue
        text = (rec.get("text") or "").replace("\n", " ").strip()[:80]
        lines.append(f"  - {_sanitize_md_text(text)}")
    return lines


def _cluster_section(cluster, raw_lookup, now):
    name = _sanitize_md_text(cluster.get("name", ""))
    why = _sanitize_md_text(cluster.get("why", ""))
    lines = [f"## {name}", "", why, "",
              "| コピー用クエリ | 簡易版 | 意図 | 評価 | 前回評価 |", "| --- | --- | --- | --- | --- |"]
    for q in cluster.get("queries", []):
        eval_history = q.get("eval_history", [])
        prev_mark = ""
        if eval_history:
            last = eval_history[-1]
            at = (last.get("at") or "")[:10]
            prev_mark = f"{last.get('mark', '')} ({at})" if at else last.get("mark", "")
        q_text = _sanitize_md_text(q.get("q", ""))
        q_simple_text = _sanitize_md_text(q.get("q_simple", ""))
        intent_text = _sanitize_md_text(q.get("intent", ""))
        lines.append(
            f"| {q_text} | {q_simple_text} | {intent_text} | "
            f"<!-- qid:{q.get('query_id', '')} --> | {prev_mark} |"
        )
    lines.append("")

    evidence_urls = cluster.get("evidence_urls", [])
    created_ats = [raw_lookup[common.canonical_url_key(u)].get("created_at")
                   for u in evidence_urls if common.canonical_url_key(u) in raw_lookup]
    windows = _window_counts(created_ats, now)
    lines.append(
        f"根拠: {len(evidence_urls)}件（直近30日={windows['d30']} / 90日={windows['d90']} / "
        f"365日={windows['d365']} / 全期間={windows['all']}）"
    )
    lines.extend(_excerpt_lines(evidence_urls, raw_lookup))
    lines.append("")
    lines.append("※日付は投稿日時（ブックマーク日時ではない）")
    lines.append("")
    return lines


def render_note(latest, raw_lookup, canonical_total, empty_text_excluded, now=None):
    now = now or datetime.now(timezone.utc)
    lines = []
    lines.append("---")
    lines.append("project: influx")
    lines.append("type: auto-generated-mirror")
    lines.append('folder: "02_Ai/x-buzz/influx-x/"')
    lines.append('categories: "[[influx_ope]]"')
    lines.append(f"last_updated: {now.strftime('%Y-%m-%d')}")
    lines.append("tags: [x-search, keywords]")
    lines.append("---")
    lines.append("")
    lines.append("> [!note] 🤖 自動生成ミラー（週次 x-keywords が上書き・**評価列のみ編集可**）")
    lines.append("> 正本: `influx/output/bookmarks/keywords_ledger.jsonl`")
    lines.append("> 評価列以外への追記・編集は次回実行で消えます。")
    lines.append("")
    lines.append("使い方: ①コピー用クエリをコピー ②X検索窓に貼り付けて検索 ③良ければ評価セルに記号を1つ入力")
    lines.append(f"評価記号の凡例: {_EVAL_LEGEND}")
    lines.append("")

    fetch_at = latest.get("last_fetch_success_at") or "unknown"
    gen = latest.get("generation")
    rev = latest.get("revision")
    reason = latest.get("generation_reason")
    generated_at = (latest.get("generated_at") or "")[:10]
    lines.append(
        f"データ鮮度: 最終fetch成功={fetch_at} ｜ 総件数(canonical)={canonical_total} ｜ "
        f"text空除外={empty_text_excluded}件 ｜ 世代: gen{gen} rev{rev} ({reason}) {generated_at}"
    )
    lines.append("")

    for cluster in latest.get("active_clusters", []):
        lines.extend(_cluster_section(cluster, raw_lookup, now))

    lines.append("## 提案（✅で承認）")
    lines.append("")
    proposals = latest.get("proposals", [])[:PROPOSALS_DISPLAY_LIMIT]
    if proposals:
        lines.append("| 名称 | 理由 | 承認 |")
        lines.append("| --- | --- | --- |")
        for p in proposals:
            p_name = _sanitize_md_text(p.get("name_ja", ""))
            p_why = _sanitize_md_text(p.get("why", ""))
            lines.append(f"| {p_name} | {p_why} | <!-- pid:{p.get('proposal_id', '')} --> |")
    else:
        lines.append("（現在提案なし）")
    lines.append("")

    lines.append("## 休眠クラスタ")
    lines.append("")
    dormant = latest.get("dormant_clusters", [])
    if dormant:
        for c in dormant:
            lines.append(f"- {c.get('name', '')}")
    else:
        lines.append("（現在休眠クラスタなし）")
    lines.append("")

    lines.append("## 世代履歴")
    lines.append("")
    history = latest.get("history", [])
    if history:
        for h in history:
            lines.append(f"- gen{h.get('generation')} rev{h.get('revision')} ({h.get('generation_reason')}) {h.get('ts')}")
    else:
        lines.append("（履歴なし）")
    lines.append("")

    return "\n".join(lines)


def write_note_with_toctou(note_path, new_text, reference_hash):
    """書込直前にnote_pathのsha256をreference_hashと比較する。既存かつ不一致ならFalse（中止）。"""
    note_path = Path(note_path)
    if note_path.exists():
        current_hash = common.sha256_of_file(note_path)
        if reference_hash is not None and current_hash != reference_hash:
            return False
    atomic_write_text(note_path, new_text)
    return True


def check_note_toctou(note_path, reference_hash) -> bool:
    """noteの現在hashがreference_hash（worklistがnote_hashを記録した時点のhash）と
    一致するかを検証する(P0-4)。台帳へgeneration/proposal_decisionをappendする前に
    呼び出し、不一致（=処理中に人間がノートを編集した）ならFalseを返して台帳append
    自体を中止させる。noteが存在しない、またはreference_hashがNone（比較対象なし）の
    場合は常にTrue。"""
    note_path = Path(note_path)
    if not note_path.exists() or reference_hash is None:
        return True
    return common.sha256_of_file(note_path) == reference_hash


# --- proposals入力の解決 --------------------------------------------------------

def resolve_incoming_proposals(proposals_in, raw_lookup, where_prefix):
    """fenceのproposals[]（propose_new限定）を検証し、proposal dict化する（proposal_idはまだ振らない）。"""
    resolved = []
    evidence_pairs = []
    for i, entry in enumerate(proposals_in):
        where = f"{where_prefix}[{i}]"
        if not isinstance(entry, dict):
            raise ValidationReject("malformed_schema", f"{where} がオブジェクトでない")
        if entry.get("op") != "propose_new":
            raise ValidationReject("proposals_invalid_op", f"{where}.op が propose_new でない")
        name, why = validate_name_why(entry, where)
        keywords_ja = _require_list(entry, "keywords_ja", where)
        keywords_en = _require_list(entry, "keywords_en", where)
        validate_keywords(keywords_ja, keywords_en, where)
        queries = _require_list(entry, "queries", where)
        validate_queries(queries, where)
        evidence_urls = validate_evidence_urls(entry, where, EVIDENCE_MIN_PROPOSE)
        keyword_objs = build_keyword_objs(keywords_ja, keywords_en)
        validate_evidence_against_raw(evidence_urls, keyword_objs, raw_lookup, where)
        evidence_pairs.append((where, evidence_urls))
        resolved.append({
            "name_ja": name, "why": why,
            "keywords_ja": keywords_ja, "keywords_en": keywords_en,
            "queries": queries, "evidence_urls": evidence_urls,
        })
    return resolved, evidence_pairs


# --- メイン処理 -----------------------------------------------------------------

def run_ingest(args, run_ts):
    """パイプライン全体を.pipeline.lockのflock(LOCK_EX・ノンブロッキング)で排他する
    薄いラッパー(P1-1)。取得できなければ「別プロセス実行中」としてexit 7で停止する。"""
    ledger_path = Path(args.ledger)
    try:
        with common.pipeline_lock(ledger_path, blocking=False):
            return _run_ingest_locked(args, run_ts, ledger_path)
    except BlockingIOError:
        sys.stderr.write(
            "[bookmarks_keyword_ingest] LOCKED: 別プロセスが.pipeline.lockを保持中のため中止\n"
        )
        return 7


def _run_ingest_locked(args, run_ts, ledger_path):
    try:
        events = common.read_ledger(ledger_path)
    except common.LedgerCorruption as exc:
        sys.stderr.write(f"[bookmarks_keyword_ingest] LEDGER CORRUPTION: {exc}\n")
        return 5

    if args.render_only:
        if not events:
            print("[bookmarks_keyword_ingest] render-only: 台帳なし。何もしない。")
            return 0
        records, raw_stats = common.load_bookmarks(args.raw)
        raw_lookup = build_raw_lookup(records)
        latest = derive_latest(events)
        atomic_write_json(args.latest, latest)
        canonical_total = len({common.canonical_url_key(r["url"]) for r in records if r.get("url")})
        note_text = render_note(latest, raw_lookup, canonical_total, raw_stats["empty_text"])
        reference_hash = common.sha256_of_file(args.note) if Path(args.note).exists() else None
        ok = write_note_with_toctou(args.note, note_text, reference_hash)
        if not ok:
            sys.stderr.write("[bookmarks_keyword_ingest] TOCTOU: ノートが再収穫前に変更されたため中止\n")
            return 3
        print("[bookmarks_keyword_ingest] render-only: latest.json/ノートを再導出しました")
        return 0

    replay_state = common.replay(events)

    result_path = Path(args.result_md)
    if not result_path.is_file():
        sys.stderr.write(f"[bookmarks_keyword_ingest] result_md が存在しない: {result_path}\n")
        return 6
    result_text = result_path.read_text(encoding="utf-8")

    records, raw_stats = common.load_bookmarks(args.raw)
    raw_lookup = build_raw_lookup(records)
    worklist_data = {}

    try:
        if Path(args.worklist).exists():
            worklist_data = load_worklist(args.worklist)
        elif not args.baseline:
            raise ValidationReject("worklist_missing", f"worklistが存在しない: {args.worklist}")

        if not args.baseline:
            validate_worklist_freshness(worklist_data, replay_state, records)

        sec = scan_secrets(result_text)
        if sec:
            raise ValidationReject("secret_detected", f"result mdに秘密パターン検出: {sec}")

        fence = extract_fence(result_text)
        prompt_version = extract_prompt_version(result_text)
        validate_notes(fence)

        clusters_in = _require_list(fence, "clusters", "fence")
        proposals_in_raw = _require_list(fence, "proposals", "fence")

        prev_active = {c["cluster_id"]: c for c in replay_state["active_clusters"]}
        prev_dormant = {c["cluster_id"]: c for c in replay_state["dormant_clusters"]}

        generation, revision, generation_reason = determine_generation_numbering(
            args.baseline, replay_state, worklist_data
        )

        if args.baseline and clusters_in:
            raise ValidationReject("baseline_requires_empty_clusters", "--baseline指定時はclusters[]が空である必要")

        check_active_id_coverage(clusters_in, set(prev_active), set(prev_dormant))

        resolved_clusters = []
        resolved_cluster_texts = {}
        evidence_pairs = []
        for i, entry in enumerate(clusters_in):
            where = f"fence.clusters[{i}]"
            op = entry.get("op")
            cid = entry.get("id")
            name, why = validate_name_why(entry, where)
            keywords_ja = _require_list(entry, "keywords_ja", where)
            keywords_en = _require_list(entry, "keywords_en", where)
            validate_keywords(keywords_ja, keywords_en, where)
            queries_in = _require_list(entry, "queries", where)
            validate_queries(queries_in, where)
            evidence_urls = validate_evidence_urls(entry, where, EVIDENCE_MIN_EXISTING)
            keyword_objs = build_keyword_objs(keywords_ja, keywords_en)
            validate_evidence_against_raw(evidence_urls, keyword_objs, raw_lookup, where)
            evidence_pairs.append((where, evidence_urls))

            prev_cluster = prev_active.get(cid) or prev_dormant.get(cid)
            queries_out = resolve_query_ids(queries_in, prev_cluster, run_ts)
            cluster_obj = {
                "cluster_id": cid, "name": name, "why": why,
                "keywords": keyword_objs, "queries": queries_out,
                "evidence_urls": evidence_urls,
            }
            resolved_clusters.append(cluster_obj)
            resolved_cluster_texts[cid] = {q["q"] for q in queries_out}

        proposals_source = proposals_in_raw if args.baseline else proposals_in_raw[:PROPOSALS_DISPLAY_LIMIT]
        resolved_proposals_in, proposal_evidence_pairs = resolve_incoming_proposals(
            proposals_source, raw_lookup, "fence.proposals"
        )
        evidence_pairs.extend(proposal_evidence_pairs)
        check_url_reuse(evidence_pairs)

        qid_index = build_qid_index(replay_state["active_clusters"], replay_state["dormant_clusters"])
        consumed_qid_evals = check_checkmark_constraints(
            replay_state["pending_evaluations"], qid_index, resolved_cluster_texts
        )
        append_consumed_eval_history(resolved_clusters, consumed_qid_evals, run_ts)

        all_proposals_by_id = collect_all_proposals(events)
        approved_ids, rejected_ids = collect_proposal_decisions(events)

        active_clusters = list(resolved_clusters)
        decision_events = []
        proposals_field = []
        pid_evals_consumed = []

        if args.baseline:
            for p in resolved_proposals_in:
                if len(active_clusters) >= ACTIVE_CLUSTER_LIMIT:
                    proposals_field.append({**p, "proposal_id": new_proposal_id()})
                    continue
                pid = new_proposal_id()
                cluster_obj = build_cluster_from_proposal({**p, "proposal_id": pid}, run_ts)
                active_clusters.append(cluster_obj)
            proposals_field = proposals_field[:PROPOSALS_DISPLAY_LIMIT]
            dormant_clusters = []
        else:
            decision_events, promoted_clusters, pid_evals_consumed = process_pid_evaluations(
                replay_state["pending_evaluations"], all_proposals_by_id, approved_ids, rejected_ids,
                len(active_clusters), ACTIVE_CLUSTER_LIMIT, run_ts,
            )
            active_clusters.extend(promoted_clusters)

            reactivated_ids = {
                entry.get("id") for entry in clusters_in if entry.get("op") == "reactivate"
            }
            dormant_clusters = [
                c for cid, c in prev_dormant.items() if cid not in reactivated_ids
            ]

            for p in resolved_proposals_in:
                pid = new_proposal_id()
                proposals_field.append({**p, "proposal_id": pid})

            if len(active_clusters) > ACTIVE_CLUSTER_LIMIT:
                raise ValidationReject(
                    "slot_exceeded",
                    f"active合計が上限{ACTIVE_CLUSTER_LIMIT}を超過 ({len(active_clusters)})",
                )

        consumed_eval_items = consumed_qid_evals + pid_evals_consumed
        consumed_keys = [
            [e["id_type"], e["id"], e["mark"], e.get("note_revision")] for e in consumed_eval_items
        ]

        delta_urls = [common.canonical_url_key(u) for u in (worklist_data.get("delta", {}).get("urls", []))]
        source_snapshot = worklist_data.get("current_snapshot")

        generation_event = {
            "type": "generation",
            "generation": generation,
            "revision": revision,
            "generation_reason": generation_reason,
            "run_id": worklist_data.get("run_id"),
            "prompt_version": prompt_version,
            "schema_version": "1",
            "source_snapshot": source_snapshot,
            "continuity_stats": worklist_data.get("continuity_stats"),
            "active_clusters": active_clusters,
            "dormant_clusters": dormant_clusters,
            "proposals": proposals_field,
            "consumed_evaluation_keys": consumed_keys,
            "evaluations_consumed": consumed_eval_items,
            "revision_of_reason": None,
        }
        if generation_reason == "baseline":
            generation_event["snapshot_urls"] = [
                common.canonical_url_key(r["url"]) for r in records if r.get("url")
            ]
        else:
            generation_event["delta_urls"] = delta_urls

    except ValidationReject as exc:
        common.append_event(ledger_path, {
            "type": "rejection", "code": exc.code, "detail": exc.detail,
            "run_id": worklist_data.get("run_id") if isinstance(worklist_data, dict) else None,
        })
        sys.stderr.write(f"[bookmarks_keyword_ingest] REJECT: {exc.code}: {exc.detail}\n")
        return 6

    reference_hash = worklist_data.get("note_hash") if isinstance(worklist_data, dict) else None
    if not check_note_toctou(args.note, reference_hash):
        sys.stderr.write(
            "[bookmarks_keyword_ingest] TOCTOU: ノートが処理中に変更されたため中止 "
            "(台帳には何も記録していない。worklistを再生成してから再実行してください)\n"
        )
        return 3

    for dec_ev in decision_events:
        common.append_event(ledger_path, dec_ev)
    common.append_event(ledger_path, generation_event)

    events = common.read_ledger(ledger_path)
    latest = derive_latest(events)
    atomic_write_json(args.latest, latest)

    canonical_total = len({common.canonical_url_key(r["url"]) for r in records if r.get("url")})
    note_text = render_note(latest, raw_lookup, canonical_total, raw_stats["empty_text"], now=run_ts)
    ok = write_note_with_toctou(args.note, note_text, reference_hash)
    if not ok:
        # 直前のcheck_note_toctouとgeneration append/render間の極小レース窓のみが
        # 該当しうる多重防御。通常の人間編集はcheck_note_toctouでappend前に検出済み。
        sys.stderr.write(
            "[bookmarks_keyword_ingest] TOCTOU: ノートが処理中に変更されたため上書きを中止 "
            "(generationは受理済み。次回runで再収穫→--render-onlyで修復可能)\n"
        )
        return 3

    print(
        f"[bookmarks_keyword_ingest] UPDATED: gen{generation} rev{revision} ({generation_reason}) "
        f"active={len(active_clusters)} dormant={len(dormant_clusters)} proposals={len(proposals_field)}"
    )
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description="Xブックマーク→キーワード抽出パイプライン: ingest CLI（柵）")
    parser.add_argument("result_md", help="LLM生成のresult mdパス")
    parser.add_argument("--worklist", default=DEFAULT_WORKLIST)
    parser.add_argument("--raw", default=DEFAULT_RAW)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--latest", default=DEFAULT_LATEST)
    parser.add_argument("--note", default=DEFAULT_NOTE)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_ts = datetime.now(timezone.utc)
    return run_ingest(args, run_ts)


if __name__ == "__main__":
    sys.exit(main())
