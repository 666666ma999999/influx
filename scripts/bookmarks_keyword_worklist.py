#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプライン: worklist生成CLI。

設計正本: ~/.claude/docs/x-keywords-plan.md §A-2/§B/§D（B2バッチ）。
bookmarks_keyword_common.py（共通庫）を用いて、raw取得済みブックマーク・台帳
（keywords_ledger.jsonl）・Vaultノートの評価マークから、次回LLM生成の材料と
なるworklist.jsonを生成する。台帳・ノートへの書込みはこのCLI自身は行わない
（評価収穫のevaluation_batchイベント追記を除く）。ingest（bookmarks_keyword_ingest.py・
別バッチ）が世代生成イベントの追記とノートrenderを担う。

## 処理順（§A-2フロー）

1. raw ロード（output/bookmarks.jsonl）。bad_lines > 0 なら fetch_status を
   （SUCCESSの場合に限り）DEGRADED へ格下げする。
2. 台帳replay（台帳が存在しない = 初回 = baselineモード）。
3. ノートが存在すれば評価マークを収穫する。未知の評価値が1つでもあれば
   worklist.jsonに {"abort": {"reason": "unknown_eval_values", "values": [...]}}
   を書いてexit 3（ノートは台帳側に何も追記しない）。正常なら未消費の新規評価を
   evaluation_batchイベントとして台帳へ追記する（同一評価の再収穫はイベントを
   増やさない＝冪等）。
4. delta = 現在のraw URL集合 − baseline_urls（canonical_url_key基準）。
5. 継続シグナル = 各active/dormantクラスタのkeywords × delta非空本文の照合統計
   （強化・退役の判断はしない。統計として記録するのみ）。
6. 統計候補語 = raw全体の非空本文から抽出したASCII語/カタカナ連/漢字連の
   上位60語（文書頻度 + like_countのlog重み・期間窓付き）。
7. トリガー判定（§A-2d）。
8. worklist.jsonをtempfile+os.replaceで原子的に書く。

## 使い方（influxディレクトリで実行）

    python3 scripts/bookmarks_keyword_worklist.py
    python3 scripts/bookmarks_keyword_worklist.py --force --reason "手動確認のため"
    python3 scripts/bookmarks_keyword_worklist.py --fetch-status DEGRADED
    python3 scripts/bookmarks_keyword_worklist.py --raw /tmp/bookmarks.jsonl \\
        --ledger /tmp/ledger.jsonl --note /tmp/note.md --out /tmp/worklist.json

## worklist.json スキーマ

    {
      "run_id": str, "worklist_version": "1", "fetch_status": str,
      "current_snapshot": {
        "url_count": int,       # canonical_url_key基準のユニーク数（同一ツイートの重複ブックマークは1件）
        "raw_line_count": int,  # rawの行数（重複排除前）
        "urls_sha256": str,     # ソート済みcanonical url key列のsha256
      },
      "excluded": {"empty_text_count": int, "bad_lines": [int, ...]},
      "delta": {
        "count": int, "eligible_nonempty_count": int, "urls": [str, ...],
        "samples": [{"url":, "author":, "text":（先頭300字・normalize前）,
                     "like_count":, "created_at":}, ...]  # 最大40件
      },
      "continuity_stats": [{"cluster_id":, "matched_delta_count":,
                             "matched_urls": [...],  # 最大5件
                             "by_keyword": {kw: count},
                             "digest_hits": {"count": int, "matched_urls": [...],  # 最大5件
                                             "by_seed_query_id": {qid: count}}
                             # digest_hitsの定義: 「digest掲載後に初観測された同一URLとの
                             # 相関」（真の検索的中率ではない・自動reinforce/retireの
                             # 単独根拠には使わない。観測相関の記録のみ）
                            }, ...],
      "previous_generation": {"generation": int|null, "revision": int|null,
                              "active": [...], "dormant": [...], "queries": [...]},
      # generation/revisionはingest側のP0-3鮮度検証（台帳末尾世代との一致確認）用の参照値
      "pending_evaluations": [...],
      "pending_proposals": [...],
      "free_slots": int,  # 12 - active数
      "trigger": {"fired": bool, "reason": str},
      "stats_candidates": [{"term":, "count":, "weighted":,
                             "windows": {"d30":, "d90":, "d365":, "all":}}, ...],  # 上位60
      "note_hash": str | null,
      "baseline_url_count": int
    }

## exit code

0=正常（trigger.firedの真偽はJSON内）/ 3=評価に未知の値があり中止 /
4=raw整合性エラー（--force --fetch-status SKIPPED_MANUAL時のみ発生しうる）/
5=台帳破損
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bookmarks_keyword_common as common  # noqa: E402

DEFAULT_RAW = "output/bookmarks.jsonl"
DEFAULT_LEDGER = "output/bookmarks/keywords_ledger.jsonl"
DEFAULT_NOTE = str(
    Path.home() / "Documents" / "Obsidian Vault" / "02_Ai" / "influx" / "influx_x_search_keywords.md"
)
DEFAULT_OUT = "output/bookmarks/keyword_worklist.json"
DEFAULT_DOWNLOADS_DIR = str(Path.home() / "Downloads")

ACTIVE_CLUSTER_LIMIT = 12
STATS_CANDIDATES_TOP_N = 60
DELTA_SAMPLE_LIMIT = 40
DELTA_TRIGGER_THRESHOLD = 10
ELAPSED_TRIGGER_DAYS = 28
LOG_WEIGHT_CAP = 3.0

# --- Web評価エクスポート回収（~/Downloads自動回収）の定数 -----------------------
WEB_EXPORT_GLOB = "x-keywords-evals-*.json"
WEB_EXPORT_SCHEMA = "x-keywords-evals/1"
WEB_EXPORT_MAX_BYTES = 100_000
WEB_EXPORT_MAX_EVALS = 200
WEB_EXPORT_PROCESSED_DIRNAME = "evals_processed"
WEB_EXPORT_REJECTED_DIRNAME = "evals_rejected"
# markごとに許可されるnoteチップ語彙（空文字=チップ未選択も常に許可）。
# render_html.py の _EVAL_CHIPS と対応させること。
WEB_EXPORT_NOTE_VOCAB = {
    "✅": {""},
    "🔁": {"", "min_favesを上げる", "絞り込み語を足す"},
    "🔍": {"", "min_favesを下げる", "語を減らす"},
    "❌": {""},
}

_ASCII_TERM_RE = re.compile(r"[a-z0-9_#]{3,}")
_KATAKANA_RUN_RE = re.compile(r"[゠-ヿ]{3,}")
_KANJI_RUN_RE = re.compile(r"[一-鿿]{2,5}")
# URL残渣（本文中でURLの一部だけが正規化後も生き残ったもの）+ 純粋な年号 + 数字のみの
# トークンはノイズとして統計候補語から除外する。
_URL_RESIDUE_TERMS = {"https", "http", "t", "co", "com", "www", "x", "amp"}
_YEAR_RE = re.compile(r"^20\d\d$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Xブックマーク→キーワード抽出パイプライン: worklist生成CLI"
    )
    parser.add_argument("--force", action="store_true", help="トリガー条件を無視して再生成させる")
    parser.add_argument("--reason", default=None, help="--force時に必須の理由")
    parser.add_argument(
        "--fetch-status",
        choices=["SUCCESS", "DEGRADED", "FAILED", "SKIPPED_MANUAL"],
        default="SUCCESS",
        help="直前のfetch結果（デフォルト: SUCCESS）",
    )
    parser.add_argument(
        "--record-fetch-run",
        action="store_true",
        help="fetch_runイベント（--fetch-status確定値・件数統計）を台帳へ記録してから通常処理を行う",
    )
    parser.add_argument(
        "--observed-url-count",
        type=int,
        default=0,
        help="今回のfetchでDOM+GraphQLが観測したユニークURL数（wrapper側のstatus-json由来。"
             "--record-fetch-runと併用しfetch_runイベントに記録し、次回以降の部分取得検知に使う）",
    )
    parser.add_argument("--raw", default=DEFAULT_RAW, help="raw bookmarks.jsonlのパス")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="台帳(keywords_ledger.jsonl)のパス")
    parser.add_argument("--note", default=DEFAULT_NOTE, help="Vaultノートのパス")
    parser.add_argument("--out", default=DEFAULT_OUT, help="worklist.json出力先")
    parser.add_argument(
        "--downloads-dir", default=DEFAULT_DOWNLOADS_DIR,
        help="Web評価エクスポート(x-keywords-evals-*.json)を回収するディレクトリ（既定: ~/Downloads）",
    )
    args = parser.parse_args()

    if args.force and not args.reason:
        parser.error("--force には --reason が必須です")
    return args


def atomic_write_json(path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-worklist-", suffix=".json")
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


def _write_abort_and_exit(out_path, abort_reason: str, exit_code: int, **extra) -> int:
    payload = {"abort": {"reason": abort_reason, **extra}}
    atomic_write_json(out_path, payload)
    return exit_code


def _parse_created_at(value):
    if not value:
        return None
    try:
        v = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _elapsed_days(iso_ts: str) -> float:
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def compute_delta(records, baseline_urls):
    """現在のraw全体からbaseline_urlsに含まれない新規URLレコードを抽出する。"""
    delta_records = []
    seen_keys = set()
    for rec in records:
        url = rec.get("url")
        if not url:
            continue
        key = common.canonical_url_key(url)
        if key in baseline_urls or key in seen_keys:
            continue
        seen_keys.add(key)
        delta_records.append(rec)
    eligible_nonempty = [r for r in delta_records if (r.get("text") or "").strip()]
    return delta_records, eligible_nonempty


def build_delta_samples(delta_records, limit=DELTA_SAMPLE_LIMIT):
    samples = []
    for rec in delta_records[:limit]:
        text = rec.get("text", "") or ""
        samples.append({
            "url": rec.get("url", ""),
            "author": rec.get("author", ""),
            "text": text[:300],
            "like_count": rec.get("like_count", 0),
            "created_at": rec.get("created_at", ""),
        })
    return samples


def _keyword_and_mode(kw_entry):
    if isinstance(kw_entry, dict):
        keyword = kw_entry.get("keyword", "")
        mode = kw_entry.get("mode") or common.default_match_mode(keyword)
    else:
        keyword = kw_entry
        mode = common.default_match_mode(keyword)
    return keyword, mode


def compute_continuity_stats(delta_nonempty_records, clusters):
    """各クラスタのkeywordsとdelta非空本文の照合統計（強化/退役の判断はしない）。"""
    stats = []
    for cluster in clusters:
        keywords = cluster.get("keywords", [])
        by_keyword: dict = {}
        matched_urls: list = []
        matched_delta_count = 0
        for rec in delta_nonempty_records:
            norm_text = common.normalize_text(rec.get("text", ""))
            matched_this_record = False
            for kw_entry in keywords:
                keyword, mode = _keyword_and_mode(kw_entry)
                if common.keyword_matches(keyword, mode, norm_text):
                    by_keyword[keyword] = by_keyword.get(keyword, 0) + 1
                    matched_this_record = True
            if matched_this_record:
                matched_delta_count += 1
                if len(matched_urls) < 5 and rec.get("url"):
                    matched_urls.append(rec["url"])
        stats.append({
            "cluster_id": cluster.get("cluster_id"),
            "matched_delta_count": matched_delta_count,
            "matched_urls": matched_urls,
            "by_keyword": by_keyword,
        })
    return stats


def _empty_digest_hits() -> dict:
    return {"count": 0, "matched_urls": [], "by_seed_query_id": {}}


def compute_digest_hits(delta_records, digest_items) -> dict:
    """delta（新規ブックマーク・canonical URL）と過去digest掲載URL(digest_items)の
    積集合をcluster_id別に集計する。

    定義: 「digest掲載後に初観測された同一URLとの相関」（真の検索的中率ではない・
    自動reinforce/retireの単独根拠には使わない・観測相関の記録のみ）。

    戻り値: {cluster_id: {"count": int, "matched_urls": [...最大5件],
                          "by_seed_query_id": {qid: count}}}
    """
    result: dict = {}
    for rec in delta_records:
        url = rec.get("url")
        if not url:
            continue
        hit = digest_items.get(common.canonical_url_key(url))
        if not hit:
            continue
        cluster_id = hit.get("cluster_id")
        entry = result.setdefault(cluster_id, _empty_digest_hits())
        entry["count"] += 1
        if len(entry["matched_urls"]) < 5:
            entry["matched_urls"].append(url)
        seed_query_id = hit.get("seed_query_id")
        if seed_query_id:
            entry["by_seed_query_id"][seed_query_id] = entry["by_seed_query_id"].get(seed_query_id, 0) + 1
    return result


def _is_stoplisted(term: str) -> bool:
    if term in _URL_RESIDUE_TERMS:
        return True
    if term.isdigit():
        return True
    if _YEAR_RE.match(term):
        return True
    return False


def _extract_terms(normalized_text: str) -> set:
    terms = set()
    terms.update(_ASCII_TERM_RE.findall(normalized_text))
    terms.update(_KATAKANA_RUN_RE.findall(normalized_text))
    terms.update(_KANJI_RUN_RE.findall(normalized_text))
    return {t for t in terms if not _is_stoplisted(t)}


def compute_stats_candidates(records, top_n=STATS_CANDIDATES_TOP_N):
    """raw全体の非空本文から統計候補語を抽出し、文書頻度+like_countのlog重みで上位N語を返す。"""
    now = datetime.now(timezone.utc)
    term_docs: dict = {}

    for rec in records:
        text = (rec.get("text") or "").strip()
        if not text:
            continue
        norm = common.normalize_text(text)
        terms = _extract_terms(norm)
        if not terms:
            continue
        like_count = rec.get("like_count") or 0
        created_dt = _parse_created_at(rec.get("created_at"))
        for term in terms:
            term_docs.setdefault(term, []).append((like_count, created_dt))

    candidates = []
    for term, docs in term_docs.items():
        doc_freq = len(docs)
        weight_sum = sum(min(math.log(1 + max(lc, 0)), LOG_WEIGHT_CAP) for lc, _ in docs)
        weighted = doc_freq + weight_sum
        windows = {"d30": 0, "d90": 0, "d365": 0, "all": doc_freq}
        for _, created_dt in docs:
            if created_dt is None:
                continue
            age_days = (now - created_dt).total_seconds() / 86400.0
            if age_days <= 30:
                windows["d30"] += 1
            if age_days <= 90:
                windows["d90"] += 1
            if age_days <= 365:
                windows["d365"] += 1
        candidates.append({
            "term": term,
            "count": doc_freq,
            "weighted": round(weighted, 3),
            "windows": windows,
        })

    candidates.sort(key=lambda c: (-c["weighted"], c["term"]))
    return candidates[:top_n]


def _collect_known_eval_ids(events):
    """台帳replay由来の既知qid/pid集合を返す({(id_type, id), ...})。

    note_eval_parseへのknown_ids引数として渡し、LLM/raw由来テキストへ埋め込まれた
    未知id（偽造マーカー）を無視させる（P0-5対策）。
    """
    known = set()
    for ev in events:
        if ev.get("type") != "generation":
            continue
        for cluster in (ev.get("active_clusters") or []) + (ev.get("dormant_clusters") or []):
            for q in cluster.get("queries", []):
                qid = q.get("query_id")
                if qid:
                    known.add(("qid", qid))
        for p in ev.get("proposals") or []:
            pid = p.get("proposal_id")
            if pid:
                known.add(("pid", pid))
    return known


def harvest_evaluations(note_path, ledger_path, replay_state, run_id):
    """ノートから評価マークを収穫し、未消費の新規評価をevaluation_batchとして台帳へ追記する。

    戻り値: (note_hash | None, unknown_values | None)
    unknown_valuesが非Noneならabortすべきことを示す（台帳へは何も書かない）。
    """
    note_path = Path(note_path)
    if not note_path.exists():
        return None, None

    note_hash = common.sha256_of_file(note_path)
    note_text = note_path.read_text(encoding="utf-8")
    known_ids = _collect_known_eval_ids(common.read_ledger(ledger_path))
    evaluations, unknown_values = common.note_eval_parse(note_text, known_ids=known_ids)
    if unknown_values:
        return note_hash, unknown_values

    already = set(replay_state["consumed_evaluation_keys"])
    already.update(
        (e["id_type"], e["id"], e["mark"], e["note_revision"])
        for e in replay_state["pending_evaluations"]
    )

    new_items = []
    for e in evaluations:
        key = (e["id_type"], e["id"], e["mark"], note_hash)
        if key in already:
            continue
        already.add(key)
        new_items.append(e)

    if new_items:
        common.append_event(ledger_path, {
            "type": "evaluation_batch",
            "note_revision": note_hash,
            "evaluations": new_items,
            "run_id": run_id,
        })

    return note_hash, None


# --- Web評価エクスポート回収（~/Downloads自動回収） ----------------------------

def _notify_mac(message: str) -> None:
    """Mac通知（失敗しても処理は続行するfail-soft。obs-x-keywordsのnotify()と同じosascript方式）。"""
    safe_message = message.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_message}" with title "x-keywords"'],
            check=False, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _is_safe_export_file(path: Path, downloads_root: Path) -> bool:
    """symlink自体、およびsymlink経由でdownloads_root外を指すファイルを拒否する。
    ファイルの実体がdownloads_root直下（再帰なし）に存在することを要求する。"""
    if path.is_symlink():
        return False
    try:
        real = path.resolve(strict=True)
        real_root = downloads_root.resolve(strict=True)
    except OSError:
        return False
    return real.parent == real_root and real.is_file()


def _parse_export_exported_at(value):
    if not isinstance(value, str):
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _validate_export_entry(entry, known_ids):
    """evals[]内の1エントリを検証する。妥当なら{id_type,id,mark,note}を返し、
    不正（未知id/不正mark/不正note/型不正）ならNone（呼び出し側でエントリ単体を無視する）。"""
    if not isinstance(entry, dict):
        return None
    id_type = entry.get("id_type")
    id_value = entry.get("id")
    mark = entry.get("mark")
    note = entry.get("note", "") or ""
    if id_type not in ("qid", "pid"):
        return None
    if not isinstance(id_value, str) or not id_value:
        return None
    if mark not in common.VALID_MARKS:
        return None
    if note not in WEB_EXPORT_NOTE_VOCAB.get(mark, {""}):
        return None
    if known_ids is not None and (id_type, id_value) not in known_ids:
        return None
    return {"id_type": id_type, "id": id_value, "mark": mark, "note": note}


def load_web_export_file(path: Path, known_ids):
    """1ファイルを検証する。戻り値はdict。

    ok=Falseはファイルレベルの破損（不正JSON・schema不一致・サイズ超過・evals型不正・
    件数超過・exported_at欠落）で、呼び出し側がファイル全体をevals_rejected/へ隔離すべき
    ことを示す。ok=Trueの場合、evals[]内の個別に不正なエントリ（未知id/不正mark/不正note）
    は"entries"から除外されるだけで（"skipped"にカウント）、ファイル自体はそのまま
    evals_processed/へ回収される（fail-softの粒度をファイル単位とエントリ単位で分ける仕様）。
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"ok": False, "reason": f"stat_failed: {exc}"}
    if size > WEB_EXPORT_MAX_BYTES:
        return {"ok": False, "reason": f"file_too_large({size}bytes)"}

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "reason": f"read_failed: {exc}"}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"ok": False, "reason": f"invalid_json: {exc}"}

    if not isinstance(data, dict) or data.get("schema") != WEB_EXPORT_SCHEMA:
        return {"ok": False, "reason": "schema_mismatch"}

    exported_at = _parse_export_exported_at(data.get("exported_at"))
    if exported_at is None:
        return {"ok": False, "reason": "exported_at_missing_or_invalid"}

    evals = data.get("evals")
    if not isinstance(evals, list):
        return {"ok": False, "reason": "evals_not_list"}
    if len(evals) > WEB_EXPORT_MAX_EVALS:
        return {"ok": False, "reason": f"too_many_evals({len(evals)})"}

    sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    entries = []
    skipped = 0
    for entry in evals:
        validated = _validate_export_entry(entry, known_ids)
        if validated is None:
            skipped += 1
            continue
        entries.append(validated)

    return {"ok": True, "exported_at": exported_at, "sha256": sha256, "entries": entries, "skipped": skipped}


def _move_export_file(src: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst_dir / src.name))


def harvest_web_exports(downloads_dir, ledger_path, events, replay_state, run_id):
    """~/Downloads直下（再帰なし）のx-keywords-evals-*.jsonを回収し、台帳へ
    evaluation_batch（source=web_export）イベントを追記する。

    処理: 候補ファイルをexported_at昇順に確定させ、同一(id_type,id)は後勝ちでマージする
    （エクスポートはlocalStorageの累積フルダンプのため、最新ファイルが優先される）。
    マージ後に採用されたエントリは「勝った側のファイル」単位でグルーピングし、
    ファイルごとに1つのevaluation_batchイベント（note_revision=file sha256）を追記する
    （冪等: 既にconsumed/pending済みの(id_type,id,mark,file_sha256)は再追記しない）。
    file-level不正のファイルはevals_rejected/へ隔離（ログ+Mac通知）、正常ファイルは
    寄与の有無に関わらずevals_processed/へmoveする（次回harvestで再読込されないため、
    ファイルの再出現＝再処理が自然にno-opになる＝冪等性の主機構）。

    戻り値: {"processed": int, "rejected": int, "appended_events": int}
    """
    result = {"processed": 0, "rejected": 0, "appended_events": 0}
    downloads_root = Path(downloads_dir)
    if not downloads_root.is_dir():
        return result

    ledger_dir = Path(ledger_path).parent
    processed_dir = ledger_dir / WEB_EXPORT_PROCESSED_DIRNAME
    rejected_dir = ledger_dir / WEB_EXPORT_REJECTED_DIRNAME
    known_ids = _collect_known_eval_ids(events)

    valid_files = []
    for path in sorted(downloads_root.glob(WEB_EXPORT_GLOB)):
        if not _is_safe_export_file(path, downloads_root):
            _move_export_file(path, rejected_dir)
            result["rejected"] += 1
            print(f"[harvest_web_exports] REJECT {path.name}: unsafe_path_or_symlink", file=sys.stderr)
            _notify_mac(f"評価エクスポート拒否: {path.name} (不正パス)")
            continue

        validation = load_web_export_file(path, known_ids)
        if not validation["ok"]:
            _move_export_file(path, rejected_dir)
            result["rejected"] += 1
            print(f"[harvest_web_exports] REJECT {path.name}: {validation['reason']}", file=sys.stderr)
            _notify_mac(f"評価エクスポート拒否: {path.name} ({validation['reason']})")
            continue

        if validation["skipped"]:
            print(
                f"[harvest_web_exports] {path.name}: 未知id/不正mark/不正noteのエントリ"
                f"{validation['skipped']}件を無視",
                file=sys.stderr,
            )
        valid_files.append({
            "path": path, "exported_at": validation["exported_at"],
            "sha256": validation["sha256"], "entries": validation["entries"],
        })

    if not valid_files:
        return result

    valid_files.sort(key=lambda f: f["exported_at"])

    # 同一(id_type, id)はexported_at昇順で後勝ち。勝者のfile indexを記録する。
    final_by_id = {}
    for idx, f in enumerate(valid_files):
        for entry in f["entries"]:
            final_by_id[(entry["id_type"], entry["id"])] = (idx, entry)

    by_file_idx: dict = {}
    for idx, entry in final_by_id.values():
        by_file_idx.setdefault(idx, []).append(entry)

    already = set(replay_state["consumed_evaluation_keys"])
    already.update(
        (e["id_type"], e["id"], e["mark"], e["note_revision"])
        for e in replay_state["pending_evaluations"]
    )

    for idx, f in enumerate(valid_files):
        new_items = []
        for entry in by_file_idx.get(idx, []):
            key = (entry["id_type"], entry["id"], entry["mark"], f["sha256"])
            if key in already:
                continue
            already.add(key)
            new_items.append(entry)

        if new_items:
            common.append_event(ledger_path, {
                "type": "evaluation_batch",
                "note_revision": f["sha256"],
                "evaluations": new_items,
                "run_id": run_id,
                "source": "web_export",
                "file": f["path"].name,
            })
            result["appended_events"] += 1

        _move_export_file(f["path"], processed_dir)
        result["processed"] += 1

    return result


def build_previous_generation_summary(replay_state):
    """worklist.previous_generationを構築する。generation/revisionは、ingestが
    このworklistを消費する時点で台帳末尾の受理世代と一致しているかを検証する
    参照値として使う（P0-3d: ledger_advanced_since_worklist検出）。"""
    active = replay_state.get("active_clusters", [])
    dormant = replay_state.get("dormant_clusters", [])
    last_generation = replay_state.get("last_generation")
    queries = []
    for cluster in active + dormant:
        for q in cluster.get("queries", []):
            queries.append({
                "query_id": q.get("query_id"),
                "q": q.get("q"),
                "q_simple": q.get("q_simple"),
                "intent": q.get("intent"),
                "eval_history": q.get("eval_history", []),
            })
    return {
        "generation": last_generation.get("generation") if last_generation else None,
        "revision": last_generation.get("revision", 0) if last_generation else None,
        "active": [{"cluster_id": c.get("cluster_id"), "name": c.get("name")} for c in active],
        "dormant": [{"cluster_id": c.get("cluster_id"), "name": c.get("name")} for c in dormant],
        "queries": queries,
    }


def determine_trigger(
    fetch_status, eligible_nonempty_delta, replay_state, raw_stats, current_url_count, force, reason
):
    """§A-2dのトリガー判定。台帳なし(初回)は常にfired=True(reason=baseline)。

    SKIPPED_MANUALの整合検証（件数が前回snapshot以上）はcanonical_url_key基準の
    ユニークURL数同士で比較する（current_url_count・baseline_urlsはいずれもcanonical基準）。
    """
    last_generation = replay_state["last_generation"]

    if last_generation is None:
        return {"fired": True, "reason": "baseline"}

    if force:
        if fetch_status == "SKIPPED_MANUAL":
            prev_count = len(replay_state["baseline_urls"])
            valid = (len(raw_stats["bad_lines"]) == 0) and (current_url_count >= prev_count)
            if not valid:
                return {"fired": False, "reason": "force_rejected_raw_integrity"}
        return {"fired": True, "reason": f"force: {reason}"}

    if fetch_status != "SUCCESS":
        return {"fired": False, "reason": "fetch_not_success"}

    if eligible_nonempty_delta >= DELTA_TRIGGER_THRESHOLD:
        return {"fired": True, "reason": "delta_threshold"}

    last_at = last_generation.get("at")
    if last_at and eligible_nonempty_delta > 0:
        if _elapsed_days(last_at) >= ELAPSED_TRIGGER_DAYS:
            return {"fired": True, "reason": "elapsed_28d"}

    return {"fired": False, "reason": "no_change"}


def build_worklist(args) -> tuple:
    """worklist.jsonの中身を構築する。戻り値: (payload_dict, exit_code)。

    exit_code != 0 の場合、payload_dictは既に {"abort": {...}} 形式である。
    """
    run_id = uuid.uuid4().hex

    records, raw_stats = common.load_bookmarks(args.raw)

    fetch_status = args.fetch_status
    if raw_stats["bad_lines"] and fetch_status == "SUCCESS":
        fetch_status = "DEGRADED"

    if getattr(args, "record_fetch_run", False):
        fetch_run_url_count = len({
            common.canonical_url_key(r["url"]) for r in records if r.get("url")
        })
        common.append_event(args.ledger, {
            "type": "fetch_run",
            "status": fetch_status,
            "run_id": run_id,
            "raw_line_count": raw_stats["total"],
            "url_count": fetch_run_url_count,
            "empty_text_count": raw_stats["empty_text"],
            "bad_lines": raw_stats["bad_lines"],
            "observed_url_count": getattr(args, "observed_url_count", 0),
        })

    # 台帳読込→評価収穫(evaluation_batch append)→再読込の区間はingestと排他する(P1-1)。
    with common.pipeline_lock(args.ledger):
        try:
            events = common.read_ledger(args.ledger)
        except common.LedgerTailCorruption as exc:
            return {"abort": {"reason": "ledger_corruption", "detail": str(exc), "tail": True}}, 5
        except common.LedgerCorruption as exc:
            return {"abort": {"reason": "ledger_corruption", "detail": str(exc), "tail": False}}, 5

        replay_state = common.replay(events)

        note_hash, unknown_values = harvest_evaluations(args.note, args.ledger, replay_state, run_id)
        if unknown_values:
            return {"abort": {"reason": "unknown_eval_values", "values": unknown_values}}, 3

        if note_hash is not None:
            # 評価収穫でイベントを追記した可能性があるため、最新状態を読み直す。
            events = common.read_ledger(args.ledger)
            replay_state = common.replay(events)

        downloads_dir = getattr(args, "downloads_dir", DEFAULT_DOWNLOADS_DIR)
        web_export_harvest = harvest_web_exports(downloads_dir, args.ledger, events, replay_state, run_id)
        if web_export_harvest["appended_events"]:
            events = common.read_ledger(args.ledger)
            replay_state = common.replay(events)

    delta_records, eligible_nonempty = compute_delta(records, replay_state["baseline_urls"])
    current_url_keys = {common.canonical_url_key(r["url"]) for r in records if r.get("url")}

    trigger = determine_trigger(
        fetch_status, len(eligible_nonempty), replay_state, raw_stats,
        len(current_url_keys), args.force, args.reason,
    )

    if args.force and args.fetch_status == "SKIPPED_MANUAL" and trigger["reason"] == "force_rejected_raw_integrity":
        return {
            "abort": {
                "reason": "raw_integrity_failed",
                "detail": "bad_linesが存在するか、件数が前回snapshotを下回っています",
            }
        }, 4

    clusters = replay_state["active_clusters"] + replay_state["dormant_clusters"]
    continuity_stats = compute_continuity_stats(eligible_nonempty, clusters)
    digest_hits_by_cluster = compute_digest_hits(delta_records, replay_state["digest_items"])
    for stat in continuity_stats:
        stat["digest_hits"] = digest_hits_by_cluster.get(stat["cluster_id"], _empty_digest_hits())
    stats_candidates = compute_stats_candidates(records)

    payload = {
        "run_id": run_id,
        "worklist_version": "1",
        "fetch_status": fetch_status,
        "current_snapshot": {
            "url_count": len(current_url_keys),
            "raw_line_count": raw_stats["total"],
            "urls_sha256": common.sha256_of_urls(current_url_keys),
        },
        "excluded": {
            "empty_text_count": raw_stats["empty_text"],
            "bad_lines": raw_stats["bad_lines"],
        },
        "delta": {
            "count": len(delta_records),
            "eligible_nonempty_count": len(eligible_nonempty),
            "urls": [r["url"] for r in delta_records],
            "samples": build_delta_samples(delta_records),
        },
        "continuity_stats": continuity_stats,
        "previous_generation": build_previous_generation_summary(replay_state),
        "pending_evaluations": replay_state["pending_evaluations"],
        "pending_proposals": replay_state["pending_proposals"],
        "free_slots": max(0, ACTIVE_CLUSTER_LIMIT - len(replay_state["active_clusters"])),
        "trigger": trigger,
        "stats_candidates": stats_candidates,
        "note_hash": note_hash,
        "baseline_url_count": len(replay_state["baseline_urls"]),
        "web_export_harvest": web_export_harvest,
    }
    return payload, 0


def main() -> int:
    args = parse_args()
    payload, exit_code = build_worklist(args)
    atomic_write_json(args.out, payload)

    if exit_code != 0:
        print(f"ABORT: {payload['abort']['reason']} -> {args.out}", file=sys.stderr)
        return exit_code

    trigger = payload["trigger"]
    web_harvest = payload.get("web_export_harvest", {})
    print(
        f"DONE: fetch_status={payload['fetch_status']} "
        f"url_count={payload['current_snapshot']['url_count']} "
        f"delta={payload['delta']['count']}(eligible={payload['delta']['eligible_nonempty_count']}) "
        f"trigger.fired={trigger['fired']}({trigger['reason']}) "
        f"web_export(processed={web_harvest.get('processed', 0)} "
        f"rejected={web_harvest.get('rejected', 0)} events={web_harvest.get('appended_events', 0)}) "
        f"-> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
