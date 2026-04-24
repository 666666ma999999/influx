"""Gold Set ラベル付けを human_annotations.json から自動マッピング（plan.md M2 T2.0）。

`output/human_annotations.json` は人手アノテーション済みの教師データ（annotator: "human"）。
`data/gold_set/candidates.jsonl` の各候補を URL キーで突合し、一致するものを
`data/gold_set/gold_set.jsonl` に書き出す。一致しなかった候補は stderr に列挙し、
HTML UI（scripts/build_gold_set_labeler.py）で user が手動補完する。

中立性担保:
    - human_annotations.json は LLM 出力ではなく人手ラベル（annotator="human"）。
      Gold Set の参照元として使っても F1 自己評価バイアスを生じない。
    - annotator != "human" の入力は fail-fast で拒否する（中立性前提の保護）。
    - answer_key.jsonl は一切参照しない。

Usage:
    python3 scripts/apply_human_annotations.py
    python3 scripts/apply_human_annotations.py --labeler masaaki
    python3 scripts/apply_human_annotations.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

JST = timezone(timedelta(hours=9))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = PROJECT_ROOT / "data" / "gold_set" / "candidates.jsonl"
HUMAN_ANNOTATIONS = PROJECT_ROOT / "output" / "human_annotations.json"
GOLD_SET = PROJECT_ROOT / "data" / "gold_set" / "gold_set.jsonl"

ALLOWED = {
    "recommended_assets", "purchased_assets", "ipo", "market_trend",
    "bullish_assets", "bearish_assets", "warning_signals",
}

_TWITTER_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com", "www.twitter.com", "www.x.com"}
# `/<user>/status/<id>`、`/@<user>/status/<id>`、`/i/web/status/<id>` の 3 形式のみ吸収して tweet ID を抽出。
# fullmatch で縛り、許容する suffix は末尾スラッシュと `photo/<n>` / `video/<n>` のみ。
# `/settings/.../status/1`、`/foo/with_replies/status/1`、`/foo/status/1abc` 等の非正規パスは誤マッチしない。
_STATUS_ID = re.compile(
    r"^/(?:i/web|@?[^/]+)/status/(\d+)(?:/(?:photo|video)/\d+)?/?$"
)


def _norm_url(u: Any) -> str:
    """ツイート URL を tweet ID ベースの正規キーに畳み込む。

    Twitter/X ホストかつ status パスが取れれば `tid:<id>` を返す(URL はプロキシに過ぎないため)。
    それ以外はホスト + パスのみの最低限正規化。非文字列は ValueError を送出する。
    """
    if not isinstance(u, str):
        raise ValueError(f"URL is not a string: {type(u).__name__}={u!r}")
    s = u.strip()
    if not s:
        return ""
    try:
        p = urlparse(s)
    except ValueError:
        return s.rstrip("/")
    host = (p.netloc or "").lower()
    if host not in _TWITTER_HOSTS:
        return s.rstrip("/")
    m = _STATUS_ID.search(p.path or "")
    if m:
        return f"tid:{m.group(1)}"
    return f"https://twitter.com{(p.path or '').rstrip('/')}"


def _to_jst_iso(s: Any, field: str, news_id: str) -> str:
    """ISO 8601 → +09:00 JST。非文字列・空・パース失敗は ValueError（schema 逸脱拒否）。"""
    if not isinstance(s, str):
        raise ValueError(f"{field} が文字列でない: news_id={news_id} type={type(s).__name__}")
    if not s:
        raise ValueError(f"{field} が空: news_id={news_id}")
    src = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(src)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST).isoformat()


def _load_candidates(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"candidates.jsonl が見つかりません: {path}")
    out: List[Dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} の {i} 行目が JSON として不正: {e}") from e
    if not out:
        raise ValueError(f"{path} が空です。sample_gold_set_candidates.py を先に実行してください")
    return out


def _load_human_index(path: Path) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """URL → アノテ entry のマップと annotator ラベルを返す。

    annotator != "human" は fail-fast で拒否（中立性担保）。
    同一正規キーの重複は stderr に警告し最後の entry を採用する（サイレント上書き防止）。
    """
    if not path.exists():
        raise FileNotFoundError(f"human_annotations.json が見つかりません: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} が JSON として不正: {e}") from e
    annotator = data.get("annotator")
    if annotator != "human":
        raise ValueError(
            f"annotator='{annotator}' は受け付けません（'human' のみ許可）。"
            f" Gold Set の中立性前提を保護するため、LLM や自動アノテートの混入は拒否します。"
        )
    idx: Dict[str, Dict[str, Any]] = {}
    dup: List[str] = []
    for i, a in enumerate(data.get("annotations", []), start=1):
        raw = a.get("url", "")
        try:
            key = _norm_url(raw)
        except ValueError as e:
            raise ValueError(f"human_annotations.json の annotations[{i}].url 不正: {e}") from e
        if not key:
            continue
        if key in idx:
            dup.append(f"{key} <- {raw!r}")
        idx[key] = a
    if dup:
        print(
            f"[WARN] human_annotations.json に URL 重複 {len(dup)} 件（最後の entry を採用）:",
            file=sys.stderr,
        )
        for d in dup[:3]:
            print(f"  - {d}", file=sys.stderr)
        if len(dup) > 3:
            print(f"  ... 他 {len(dup) - 3} 件", file=sys.stderr)
    version = data.get("version", "?")
    return idx, f"human_annotations_v{version}"


def _ensure_unique_news_id(cands: List[Dict[str, Any]]) -> None:
    """news_id の空・重複を拒否する（measure_f1.py が news_id をキーに使うため）。"""
    seen: Dict[str, int] = {}
    for i, c in enumerate(cands, start=1):
        nid = c.get("news_id")
        if not nid or not isinstance(nid, str):
            raise ValueError(f"candidates.jsonl の {i} 行目に news_id がありません")
        if nid in seen:
            raise ValueError(
                f"candidates.jsonl に news_id 重複: {nid} (行 {seen[nid]} と {i})"
            )
        seen[nid] = i


def _coerce_labels(raw: Any) -> Tuple[List[str], List[Any]]:
    """human_categories を list[str] に矯正し、許可カテゴリだけ残す。許可外は dropped で返す。"""
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [raw]
    labels: List[str] = []
    dropped: List[Any] = []
    for item in raw:
        if isinstance(item, str) and item in ALLOWED:
            labels.append(item)
        else:
            dropped.append(item)
    return labels, dropped


def _atomic_write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    """途中失敗で既存ファイルを壊さないよう tempfile + os.replace で書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="human_annotations → gold_set.jsonl 自動マッピング")
    parser.add_argument("--labeler", default=None,
                        help="labeler 名（省略時は human_annotations.json の version を使う）")
    parser.add_argument("--dry-run", action="store_true", help="書き出しせず差分のみ表示")
    args = parser.parse_args()

    try:
        cands = _load_candidates(CANDIDATES)
        _ensure_unique_news_id(cands)
        idx, default_labeler = _load_human_index(HUMAN_ANNOTATIONS)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    labeler = args.labeler or default_labeler
    matched: List[Dict[str, Any]] = []
    unmatched: List[Tuple[str, str]] = []

    for c in cands:
        nid = c["news_id"]
        raw_url = c.get("tweet_url", "")
        try:
            key = _norm_url(raw_url)
        except ValueError as e:
            print(f"[ERROR] candidates.jsonl の tweet_url 不正: news_id={nid} {e}", file=sys.stderr)
            return 2
        hit = idx.get(key) if key else None
        if hit is None:
            unmatched.append((nid, raw_url if isinstance(raw_url, str) else repr(raw_url)))
            continue
        labels, dropped = _coerce_labels(hit.get("human_categories"))
        notes = ""
        if not labels:
            notes = "human_annotations 側で空配列（どのカテゴリにも該当しない）"
        if dropped:
            notes = (notes + " / " if notes else "") + f"非許可カテゴリ除外: {dropped}"
        try:
            posted_at = _to_jst_iso(c.get("posted_at", ""), "posted_at", nid)
            labeled_at = _to_jst_iso(hit.get("annotated_at", ""), "annotated_at", nid)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            return 2
        matched.append({
            "news_id": nid,
            "tweet_url": c.get("tweet_url", ""),
            "username": c.get("username", ""),
            "posted_at": posted_at,
            "text": c.get("text", ""),
            "labels": labels,
            "labeler": labeler,
            "labeled_at": labeled_at,
            "notes": notes,
        })

    print(f"candidates: {len(cands)} 件 / 一致: {len(matched)} / 未一致: {len(unmatched)}")
    if matched:
        empty = sum(1 for m in matched if not m["labels"])
        print(f"  うち実ラベル: {len(matched) - empty} 件 / 空ラベル: {empty} 件")
    if unmatched:
        print(f"\n未アノテ {len(unmatched)} 件（HTML UI で人手判定が必要）:", file=sys.stderr)
        for nid, url in unmatched[:5]:
            print(f"  - {nid}: {url}", file=sys.stderr)
        if len(unmatched) > 5:
            print(f"  ... 他 {len(unmatched) - 5} 件", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] 書き出しスキップ", file=sys.stderr)
        return 0

    _atomic_write_jsonl(GOLD_SET, matched)
    print(f"\n書き出し: {GOLD_SET}（{len(matched)} 件）")
    if unmatched:
        print(f"次: `open output/label_gold_set.html` で残 {len(unmatched)} 件を追加ラベル付け", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
