#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプライン: digest確定CLI（host柵・stdlibのみ）。

設計正本: ~/.claude/docs/x-keywords-plan.md 末尾セクション
「Plan: x-keywords v2 — AI 検索代行ダイジェスト」§詳細設計v2/§アーキテクチャ（C1バッチ）。

inbox内の最新digest_raw/digest_statusペアを検証し、台帳（keywords_ledger.jsonl）へ
digestイベントを追記する「柵」。bookmarks_keyword_digest_collect.pyが書いたJSONL/JSONを
一切信用せず、URL形式・Snowflake ID時刻整合・likes下限をコード側で再検証する。
DIGEST_APPLIED時は台帳append+processed移動の直後にrender_htmlを再生成する（C2で配線。
render失敗はWARNログのみでexit 0を維持=台帳は正・表示は次回修復）。

## 処理順

1. --run-id指定時はそのrun_idのdigest_status-<run_id>.json/digest_raw-<run_id>.jsonl
   ペアのみを対象にする（run固定・他run_idの新旧ペアは一切見ない）。未指定時（手動用）は
   inbox内でrun_idが一致するペアのうち最新（generated_at基準）を発見。
   いずれの経路でもペアが無ければ何もせずDIGEST_SKIPPED・exit 0
2. .pipeline.lockのflock(LOCK_EX・ノンブロッキング)で排他取得（取得失敗はexit 7）
3. 台帳read + 整合検証: status.complete==true / generated_atが24h以内 /
   source.generation・revisionが現在の台帳末尾世代と一致 / source.urls_sha256が
   現在のlatest.json側snapshot（無ければ台帳末尾generationのsource_snapshot）と一致
   （None同士は許容）。いずれか不合格なら台帳に書かずSKIPPED_STALE・exit 0
   （run_id不一致は手順1の発見時点で「ペアなし」扱いになるためDIGEST_SKIPPEDとして表出する）
4. 各item検証（不合格はfatalにせずdrop）: cluster_idがlatest.jsonのactive_clustersに
   実在（無ければunknown_cluster）+ seed_query_idが該当クラスタのクエリに実在
   （指定時のみ・無ければunknown_seed）+ URL形式(https+x.com/twitter.com完全一致+
   /<handle>/status/<digits>)+ URL/ID整合 + Snowflake ID→時刻が収集窓+スラック内 +
   likes型 + content型/長さ(<=2000字) + スパム/懸賞キャンペーン系キーワード除外
   （SPAM_KEYWORDS・reason=spam_filter）+ likes下限（min-likes索引が引けない場合は
   MIN_LIKES_INDEX_FALLBACK=80を一律下限として適用）。既ブックマーク・既掲載URLも除外
5. 選定: クラスタ内likes降順 <= --per-cluster、全体likes降順 <= --total（LLMなし）
6. 選定件数 < --min-publish なら台帳に書かずDIGEST_DEGRADED・exit 0
7. digestイベントをpipeline_lock下でappend → raw/statusをinbox/processed/へ移動 →
   render_html.render_to_file()を呼びHTMLを再生成（失敗してもWARNログのみでexit 0は
   変えない） → DIGEST_APPLIED n=<件数>・exit 0

## CLI

    python3 scripts/bookmarks_keyword_digest_apply.py [--inbox PATH] [--ledger PATH] \\
        [--raw PATH] [--latest PATH] [--min-publish N] [--per-cluster N] [--total N] \\
        [--html-out PATH] [--run-id RUN_ID]

exit code: 0=完走（APPLIED/SKIPPED/SKIPPED_STALE/DEGRADEDいずれもexit 0の
    fail-soft方針。stdoutのトークンで結果を判別する）/ 5=台帳破損 / 7=別プロセス実行中
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bookmarks_keyword_common as common  # noqa: E402
import bookmarks_keyword_ingest as ingest  # noqa: E402 (_sanitize_md_text再利用)
import bookmarks_keyword_render_html as render_html  # noqa: E402 (DIGEST_APPLIED後の再生成用)

DEFAULT_INBOX = "output/bookmarks/digest_inbox"
DEFAULT_LEDGER = "output/bookmarks/keywords_ledger.jsonl"
DEFAULT_RAW = "output/bookmarks.jsonl"
DEFAULT_LATEST = "output/bookmarks/keywords_latest.json"
DEFAULT_MIN_PUBLISH = 8
DEFAULT_PER_CLUSTER = 3
DEFAULT_TOTAL = 20
# bookmarks_keyword_digest_collect.py:DEFAULT_DAYSと同値。apply側はpydantic非依存を保つため
# collectをimportせずこの値を複製する（値を変える場合は両側を揃えること）。
DEFAULT_COLLECT_DAYS = 7
MIN_LIKES_INDEX_FALLBACK = 80

FRESHNESS_HOURS = 24
SNOWFLAKE_PAST_SLACK_DAYS = 2
SNOWFLAKE_FUTURE_SLACK_DAYS = 1
LIKES_FLOOR_RATIO = 0.8
EXCERPT_MAX = 120
CONTENT_MAX = 2000
RAW_ITEM_CAP = 200
X_EPOCH_MS = 1288834974657  # Snowflake ID の基準エポック(ms)
REQUIRED_ITEM_KEYS = {"url", "id", "author", "content", "likes", "cluster_id"}

# キャンペーン/懸賞スパム除外キーワード（content正規化テキストの部分一致で判定。
# 誤検知/検知漏れが増えた場合はこのリストを編集する）。
SPAM_KEYWORDS = [
    "キャンペーン",
    "懸賞",
    "プレゼント企画",
    "フォロー&RT",
    "フォロー＆RT",
    "リポストで",
    "抽選で",
    "当選",
    "配布します",
]

_STATUS_FILENAME_RE = re.compile(r"^digest_status-(.+)\.json$")
_STATUS_PATH_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)$")
_STATUS_HOSTS = {"x.com", "twitter.com"}


class LedgerFailure(Exception):
    """台帳破損。exit 5で即終了すべき場合に送出する。"""


# --- pure ヘルパー -------------------------------------------------------------

def _parse_iso(ts) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def snowflake_to_ms(id_str: str) -> int | None:
    if not id_str or not id_str.isdigit():
        return None
    return (int(id_str) >> 22) + X_EPOCH_MS


def is_spam_content(content: str) -> bool:
    """contentの正規化テキスト（common.normalize_text）にSPAM_KEYWORDSのいずれかを
    部分一致で含むか。common.keyword_matches(mode="normalized_substring")を再利用する。"""
    normalized = common.normalize_text(content)
    if not normalized:
        return False
    return any(common.keyword_matches(kw, "normalized_substring", normalized) for kw in SPAM_KEYWORDS)


def validate_url_and_id(url, item_id) -> bool:
    """https + x.com/twitter.com完全一致 + /<handle>/status/<digits> + URL/ID整合。"""
    if not isinstance(url, str) or not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _STATUS_HOSTS:
        return False
    m = _STATUS_PATH_RE.match(parsed.path or "")
    if not m:
        return False
    id_str = str(item_id) if item_id is not None else ""
    if not id_str.isdigit():
        return False
    return m.group(2) == id_str


def build_min_likes_index(latest_data: dict) -> dict:
    """(cluster_id, query_id) -> そのクエリのmin_faves数値。latest.jsonのactive_clustersから。"""
    index = {}
    for cluster in (latest_data or {}).get("active_clusters", []):
        cid = cluster.get("cluster_id")
        for q in cluster.get("queries", []):
            mf = common.extract_min_faves(q.get("q", ""))
            if mf is not None:
                index[(cid, q.get("query_id"))] = mf
    return index


def build_cluster_query_index(latest_data: dict) -> dict:
    """cluster_id -> {query_id, ...}（そのクラスタが持つ全クエリのquery_id集合）。

    validate_itemのunknown_cluster/unknown_seed検証に使う。min_faves未指定の
    クエリも含む（build_min_likes_indexより広い集合。存在確認のみが目的）。
    """
    index: dict = {}
    for cluster in (latest_data or {}).get("active_clusters", []):
        cid = cluster.get("cluster_id")
        index[cid] = {q.get("query_id") for q in cluster.get("queries", [])}
    return index


def validate_item(item: dict, min_likes_index: dict, cluster_query_index: dict,
                   window_start_ms: int, window_end_ms: int) -> tuple:
    """(合格か, 不合格理由コード)を返す。不合格理由コードはdropped_countsの集計キー。"""
    if not isinstance(item, dict) or not REQUIRED_ITEM_KEYS.issubset(item.keys()):
        return False, "malformed_item"

    cluster_id = item.get("cluster_id")
    if cluster_id not in cluster_query_index:
        return False, "unknown_cluster"

    seed_query_id = item.get("seed_query_id")
    if seed_query_id is not None and seed_query_id not in cluster_query_index[cluster_id]:
        return False, "unknown_seed"

    if not validate_url_and_id(item.get("url"), item.get("id")):
        return False, "bad_url_or_id"

    ts_ms = snowflake_to_ms(str(item.get("id")))
    if ts_ms is None or not (window_start_ms <= ts_ms <= window_end_ms):
        return False, "snowflake_out_of_window"

    likes = item.get("likes")
    if not isinstance(likes, int) or isinstance(likes, bool):
        return False, "likes_type"

    content = item.get("content")
    if not isinstance(content, str) or not (0 < len(content) <= CONTENT_MAX):
        return False, "content_invalid"

    if is_spam_content(content):
        return False, "spam_filter"

    # min-likes索引が引けないケース(cluster/seedは既知だがそのクエリにmin_favesが
    # 無い等)には一律MIN_LIKES_INDEX_FALLBACKを下限として適用する(索引不在での
    # 無条件通過という迂回経路を塞ぐ)。
    min_likes = min_likes_index.get((cluster_id, seed_query_id))
    if min_likes is None:
        min_likes = MIN_LIKES_INDEX_FALLBACK
    if likes < min_likes * LIKES_FLOOR_RATIO:
        return False, "likes_below_floor"

    return True, ""


def select_items(accepted: list, per_cluster_cap: int, total_cap: int, dropped_counts: dict) -> list:
    """acceptedは[(canon_key, item), ...]。cluster_id別likes降順→per_cluster_cap、
    その後全体をlikes降順→total_capに切り詰める。"""
    by_cluster: dict = {}
    for canon_key, item in accepted:
        by_cluster.setdefault(item.get("cluster_id"), []).append((canon_key, item))

    capped: list = []
    for group in by_cluster.values():
        group.sort(key=lambda t: t[1].get("likes", 0), reverse=True)
        capped.extend(group[:per_cluster_cap])
        if len(group) > per_cluster_cap:
            _bump(dropped_counts, "cluster_cap_exceeded", len(group) - per_cluster_cap)

    capped.sort(key=lambda t: t[1].get("likes", 0), reverse=True)
    if len(capped) > total_cap:
        _bump(dropped_counts, "total_cap_exceeded", len(capped) - total_cap)
    return capped[:total_cap]


def _bump(counter: dict, key: str, n: int = 1) -> None:
    counter[key] = counter.get(key, 0) + n


def load_raw_items(path: Path) -> tuple:
    """digest_raw.jsonlを寛容にparseする。壊れた行はmalformed_jsonへ計上しdropする。
    件数上限RAW_ITEM_CAPを超えた分はraw_count_capへ計上しdropする。
    戻り値: (items, dropped_counts)
    """
    dropped_counts: dict = {}
    items: list = []
    if not path.exists():
        return items, dropped_counts
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            if len(items) >= RAW_ITEM_CAP:
                _bump(dropped_counts, "raw_count_cap")
                continue
            try:
                items.append(json.loads(stripped))
            except json.JSONDecodeError:
                _bump(dropped_counts, "malformed_json")
    return items, dropped_counts


def find_latest_pair(inbox: Path):
    """inbox直下のdigest_status-*.jsonのうちgenerated_atが最新のものを選び、
    ファイル名のrun_idと内容のrun_idが一致し対応するdigest_raw-<run_id>.jsonlが
    実在する場合のみ(raw_path, status_path, status_dict)を返す。無ければNone。"""
    candidates = []
    for sp in sorted(inbox.glob("digest_status-*.json")) if inbox.exists() else []:
        m = _STATUS_FILENAME_RE.match(sp.name)
        if not m:
            continue
        filename_run_id = m.group(1)
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        ts = _parse_iso(data.get("generated_at"))
        if ts is None:
            continue
        candidates.append((ts, filename_run_id, sp, data))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, filename_run_id, sp, data = candidates[-1]
    if data.get("run_id") != filename_run_id:
        return None
    rp = sp.parent / f"digest_raw-{filename_run_id}.jsonl"
    if not rp.exists():
        return None
    return rp, sp, data


def find_pair_by_run_id(inbox: Path, run_id: str):
    """--run-id指定時: そのrun_idのdigest_status/digest_rawペアのみを対象にする。

    ファイルが揃っていない、statusが壊れている、内容run_idがファイル名と不一致の
    いずれかの場合はNone（呼び出し側はDIGEST_SKIPPEDとして扱う。他run_idの新旧
    ペアが同じinbox内にあっても一切見ない=run固定）。
    """
    sp = inbox / f"digest_status-{run_id}.json"
    rp = inbox / f"digest_raw-{run_id}.jsonl"
    if not sp.exists() or not rp.exists():
        return None
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("run_id") != run_id:
        return None
    return rp, sp, data


def current_source_urls_sha256(latest_data: dict, events: list) -> str | None:
    """現在の「正」とみなすurls_sha256を返す。

    優先: latest.json側の source_snapshot.urls_sha256。それが無ければ台帳末尾の
    generationイベントのsource_snapshot.urls_sha256にフォールバックする。
    どちらにも無ければNone（statusのurls_sha256もNoneの場合のみ許容=同一視）。
    """
    snapshot = (latest_data or {}).get("source_snapshot")
    if snapshot:
        return snapshot.get("urls_sha256")
    for ev in reversed(events):
        if ev.get("type") == "generation":
            return (ev.get("source_snapshot") or {}).get("urls_sha256")
    return None


def validate_run_integrity(status: dict, replay_state: dict, now: datetime,
                            current_urls_sha256: str | None) -> tuple:
    """(合格か, 不合格理由)を返す。合格ならledgerへの書込を許可してよい。"""
    if not status.get("complete"):
        return False, "not_complete"

    generated_at = _parse_iso(status.get("generated_at"))
    if generated_at is None:
        return False, "bad_generated_at"
    age = now - generated_at
    if age > timedelta(hours=FRESHNESS_HOURS) or age < timedelta(0):
        return False, "stale_or_future"

    source = status.get("source") or {}
    last_gen = replay_state.get("last_generation")
    current_gen = last_gen.get("generation") if last_gen else None
    current_rev = last_gen.get("revision", 0) if last_gen else 0
    if source.get("generation") != current_gen or source.get("revision") != current_rev:
        return False, "generation_mismatch"

    # collect実行時点のsnapshotが今なお「正」と一致するかの照合(None同士は許容)。
    if source.get("urls_sha256") != current_urls_sha256:
        return False, "sha_mismatch"

    return True, ""


def build_digest_event(run_id: str, source: dict, selected: list, dropped_counts: dict, collect_errors: list) -> dict:
    items_field = []
    for _canon_key, item in selected:
        raw_excerpt = (item.get("content") or "")[:EXCERPT_MAX]
        excerpt = ingest._sanitize_md_text(raw_excerpt)
        canonical_url = f"https://x.com/{item.get('author')}/status/{item.get('id')}"
        items_field.append({
            "url": canonical_url,
            "cluster_id": item.get("cluster_id"),
            "seed_query_id": item.get("seed_query_id"),
            "excerpt": excerpt,
            "author": item.get("author"),
            "likes": item.get("likes"),
            "posted_at": item.get("posted_at", ""),
        })
    return {
        "type": "digest",
        "run_id": run_id,
        "source": source,
        "items": items_field,
        "dropped_counts": dropped_counts,
        "errors": collect_errors,
    }


# --- CLI本体 -------------------------------------------------------------------

def run_apply(args, now: datetime) -> int:
    inbox = Path(args.inbox)
    run_id = getattr(args, "run_id", None)
    pair = find_pair_by_run_id(inbox, run_id) if run_id else find_latest_pair(inbox)
    if pair is None:
        print("[bookmarks_keyword_digest_apply] DIGEST_SKIPPED (raw/statusペアなし)")
        return 0

    ledger_path = Path(args.ledger)
    try:
        with common.pipeline_lock(ledger_path, blocking=False):
            return _run_apply_locked(args, now, pair, ledger_path)
    except BlockingIOError:
        sys.stderr.write(
            "[bookmarks_keyword_digest_apply] LOCKED: 別プロセスが.pipeline.lockを保持中のため中止\n"
        )
        return 7


def _run_apply_locked(args, now: datetime, pair, ledger_path: Path) -> int:
    rp, sp, status = pair

    try:
        events = common.read_ledger(ledger_path)
    except common.LedgerCorruption as exc:
        sys.stderr.write(f"[bookmarks_keyword_digest_apply] LEDGER CORRUPTION: {exc}\n")
        return 5

    replay_state = common.replay(events)

    latest_path = Path(args.latest)
    latest_data = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.exists() else {}
    current_urls_sha256 = current_source_urls_sha256(latest_data, events)

    ok, reason = validate_run_integrity(status, replay_state, now, current_urls_sha256)
    if not ok:
        print(f"[bookmarks_keyword_digest_apply] SKIPPED_STALE reason={reason}")
        return 0

    raw_items, dropped_counts = load_raw_items(rp)
    if not raw_items:
        print("[bookmarks_keyword_digest_apply] DIGEST_SKIPPED (rawが空)")
        return 0

    records, _raw_stats = common.load_bookmarks(args.raw)
    existing_url_keys = {common.canonical_url_key(r["url"]) for r in records if r.get("url")}
    already_published = replay_state["digest_urls"]

    min_likes_index = build_min_likes_index(latest_data)
    cluster_query_index = build_cluster_query_index(latest_data)

    generated_at = _parse_iso(status.get("generated_at")) or now
    collect_days = status.get("collect_days", DEFAULT_COLLECT_DAYS)
    window_start = generated_at - timedelta(days=collect_days) - timedelta(days=SNOWFLAKE_PAST_SLACK_DAYS)
    window_end = now + timedelta(days=SNOWFLAKE_FUTURE_SLACK_DAYS)
    window_start_ms = int(window_start.timestamp() * 1000)
    window_end_ms = int(window_end.timestamp() * 1000)

    seen_in_batch: set = set()
    accepted: list = []
    for item in raw_items:
        ok, reason = validate_item(item, min_likes_index, cluster_query_index, window_start_ms, window_end_ms)
        if not ok:
            _bump(dropped_counts, reason)
            continue

        canon_key = common.canonical_url_key(item["url"])
        if canon_key in existing_url_keys:
            _bump(dropped_counts, "already_bookmarked")
            continue
        if canon_key in already_published:
            _bump(dropped_counts, "already_published")
            continue
        if canon_key in seen_in_batch:
            _bump(dropped_counts, "duplicate_in_batch")
            continue
        seen_in_batch.add(canon_key)
        accepted.append((canon_key, item))

    selected = select_items(accepted, args.per_cluster, args.total, dropped_counts)

    if len(selected) < args.min_publish:
        print(f"[bookmarks_keyword_digest_apply] DIGEST_DEGRADED n={len(selected)} (min_publish={args.min_publish})")
        return 0

    event = build_digest_event(status["run_id"], status.get("source"), selected, dropped_counts, status.get("errors", []))
    common.append_event(ledger_path, event)

    processed_dir = inbox_processed_dir(rp.parent)
    shutil.move(str(rp), str(processed_dir / rp.name))
    shutil.move(str(sp), str(processed_dir / sp.name))

    html_out = getattr(args, "html_out", render_html.DEFAULT_OUT)
    try:
        render_html.render_to_file(latest_path=args.latest, ledger_path=str(ledger_path), out_path=html_out)
    except Exception as exc:
        sys.stderr.write(f"[bookmarks_keyword_digest_apply] WARN: render_html再生成に失敗しました: {exc}\n")

    print(f"[bookmarks_keyword_digest_apply] DIGEST_APPLIED n={len(selected)}")
    return 0


def inbox_processed_dir(inbox: Path) -> Path:
    processed_dir = inbox / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    return processed_dir


def parse_args():
    ap = argparse.ArgumentParser(description="Xブックマーク→キーワード抽出パイプライン: digest確定CLI（柵）")
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--raw", default=DEFAULT_RAW)
    ap.add_argument("--latest", default=DEFAULT_LATEST)
    ap.add_argument("--min-publish", type=int, default=DEFAULT_MIN_PUBLISH)
    ap.add_argument("--per-cluster", type=int, default=DEFAULT_PER_CLUSTER)
    ap.add_argument("--total", type=int, default=DEFAULT_TOTAL)
    ap.add_argument("--html-out", dest="html_out", default=render_html.DEFAULT_OUT)
    ap.add_argument(
        "--run-id", default=None,
        help="指定時はこのrun_idのdigest_raw/digest_statusペアのみを対象にする(collectが渡すrun固定用)。"
             "未指定時はinbox内の最新ペアを探す(手動実行向けの現行動作を維持)。",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    return run_apply(args, datetime.now(timezone.utc))


if __name__ == "__main__":
    sys.exit(main())
