"""日次 1 投稿運用パイプライン（plan.md M4 T4.1）。

collect → classify → compose → viewer 更新を 1 コマンドで実行する。
cron/launchd で 1 日 1 回呼ばれることを想定。途中で失敗したら stderr に
ステップ名と exit code を出力して停止し、以降のステップは飛ばす。

plan.md M1 T1.9 時間計測:
    - 開始時に `pipeline_start` レコードで `collect_start_at` を即時記録
    - classify 完了時に `pipeline_metric` レコードで `classify_done_at` と
      `collect_to_classify_sec` を保存
    - 40 分超過で stderr 警告、ステップ別所要秒を stderr へ出力
    - 全レコードに `run_id` を付与し、同日再実行時の集計汚染を防ぐ

Usage:
    python scripts/daily_pipeline.py                     # 全ステップ
    python scripts/daily_pipeline.py --skip-collect      # 収集スキップ（分類だけ再実行）
    python scripts/daily_pipeline.py --skip-compose      # compose スキップ
    python scripts/daily_pipeline.py --no-llm-compose    # compose 時に LLM 呼ばない
    python scripts/daily_pipeline.py --scrolls 8         # 収集スクロール数
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict


class PipelineSummary(TypedDict):
    """`_summarize_log` の戻り値契約。"""

    steps: Dict[str, float]
    collect_to_classify_sec: Optional[float]
    total_sec: float

JST = timezone(timedelta(hours=9))
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# plan.md M4 ゲート: collect→classify が 40 分超過で stderr 警告
COLLECT_TO_CLASSIFY_WARN_SEC = 40 * 60


def _run(cmd: List[str], step: str, allow_nonzero: bool = False, cwd: Optional[str] = None) -> int:
    """サブコマンド実行。失敗時は stderr にログを残して呼び出し元へ返す。"""
    print(f"\n===== [{step}] =====")
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=cwd or str(PROJECT_ROOT))
    if proc.returncode != 0 and not allow_nonzero:
        print(f"[ERROR] {step} が exit code {proc.returncode} で失敗", file=sys.stderr)
    return proc.returncode


def _append_log(log_path: Path, entry: Dict[str, Any]) -> None:
    """pipeline_log/YYYY-MM-DD.jsonl に 1 レコード追記する汎用ヘルパ。

    監視ログの書き込み失敗（permission / disk full 等）はパイプライン本処理を
    止めない方針で、OSError は stderr に degraded warning を出して飲み込む。
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[pipeline] log 書き込み失敗 ({log_path}): {e}", file=sys.stderr)


def _log_run(log_path: Path, run_id: str, step: str, started_at: str, rc: int, duration_sec: float) -> None:
    """ステップ実行結果を pipeline_log に追記する。"""
    _append_log(log_path, {
        "step": step,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(JST).isoformat(),
        "exit_code": rc,
        "duration_sec": round(duration_sec, 2),
    })


def _latest_classified(output_dir: Path) -> Optional[Path]:
    """最新の classified_llm*.json を探す。"""
    cands = sorted(output_dir.glob("*/classified_llm*.json"))
    if cands:
        return cands[-1]
    cands = sorted(output_dir.glob("*/tweets.json"))
    return cands[-1] if cands else None


def main() -> int:
    parser = argparse.ArgumentParser(description="plan.md M4 T4.1: 日次 1 投稿運用パイプライン")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-classify", action="store_true")
    parser.add_argument("--skip-compose", action="store_true")
    parser.add_argument("--no-llm-compose", action="store_true", help="compose で LLM を使わない")
    parser.add_argument("--scrolls", type=int, default=10)
    parser.add_argument("--groups", default="all")
    parser.add_argument("--output", default="output")
    parser.add_argument("--posting-output", default="output/posting")
    args = parser.parse_args()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    log_path = PROJECT_ROOT / "output" / "pipeline_log" / f"{today}.jsonl"
    run_id = uuid.uuid4().hex
    collect_start_at = datetime.now(JST).isoformat()

    # plan.md M1 T1.9: 開始時点で collect_start_at を即時記録（collect 途中クラッシュでも残す）
    _append_log(log_path, {
        "step": "pipeline_start",
        "run_id": run_id,
        "collect_start_at": collect_start_at,
    })

    print(f"[pipeline] day={today} run_id={run_id} log={log_path}")

    failures: List[str] = []

    # Step 1: collect_tweets
    if not args.skip_collect:
        start = datetime.now(JST).isoformat()
        t0 = time.time()
        rc = _run(
            ["python3", "scripts/collect_tweets.py",
             "--groups", args.groups, "--scrolls", str(args.scrolls), "--output", args.output],
            "collect",
        )
        _log_run(log_path, run_id, "collect", start, rc, time.time() - t0)
        if rc != 0:
            failures.append("collect")
            print("[pipeline] collect 失敗のため以降スキップ", file=sys.stderr)
            return _finish(log_path, run_id, failures, today)

    # Step 2: classify_tweets
    if not args.skip_classify and "collect" not in failures:
        start = datetime.now(JST).isoformat()
        t0 = time.time()
        rc = _run(["python3", "scripts/classify_tweets.py"], "classify")
        _log_run(log_path, run_id, "classify", start, rc, time.time() - t0)

        # plan.md M1 T1.9: classify 完了時に classify_done_at と collect_to_classify_sec を保存
        if rc == 0:
            classify_done_at = datetime.now(JST).isoformat()
            try:
                c2c_sec = (
                    datetime.fromisoformat(classify_done_at)
                    - datetime.fromisoformat(collect_start_at)
                ).total_seconds()
            except ValueError:
                c2c_sec = None
            _append_log(log_path, {
                "step": "pipeline_metric",
                "run_id": run_id,
                "classify_done_at": classify_done_at,
                "collect_to_classify_sec": round(c2c_sec, 2) if c2c_sec is not None else None,
            })

        if rc != 0:
            failures.append("classify")
            # classify 失敗でも compose は既存データで続行可能
            print("[pipeline] classify 失敗、compose は前日データで継続", file=sys.stderr)

    # Step 3: compose
    if not args.skip_compose:
        latest = _latest_classified(Path(args.output))
        if not latest:
            print("[ERROR] 分類済みファイルが見つかりません", file=sys.stderr)
            failures.append("compose-precheck")
            return _finish(log_path, run_id, failures, today)

        start = datetime.now(JST).isoformat()
        t0 = time.time()
        # 2026-05-01 Phase 3: tier3_posting は別リポ（autopost）へ。subprocess の cwd を切替
        tier3_root = os.environ.get("TIER3_REPO", str(Path.home() / "Desktop" / "biz" / "autopost"))
        cmd = [
            "python3", "-m", "tier3_posting.cli.compose",
            "--input", str(latest),
            "--output-dir", args.posting_output,
        ]
        if args.no_llm_compose:
            cmd.append("--no-llm")
        rc = _run(cmd, "compose", cwd=tier3_root)
        _log_run(log_path, run_id, "compose", start, rc, time.time() - t0)
        if rc != 0:
            failures.append("compose")

    # Step 4: 承認待ちドラフト数を stderr に通知
    _notify_pending(Path(args.posting_output))

    return _finish(log_path, run_id, failures, today)


def _notify_pending(posting_dir: Path) -> None:
    """draft 状態のドラフト数を stderr に出す（運用者アラート用）。

    I/O 失敗はパイプライン本処理を止めない方針で、OSError は警告扱い。
    """
    drafts_file = posting_dir / "drafts.jsonl"
    if not drafts_file.exists():
        print("[pipeline] drafts.jsonl 不在", file=sys.stderr)
        return
    pending = 0
    try:
        with drafts_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") == "draft":
                    pending += 1
    except OSError as e:
        print(f"[pipeline] drafts.jsonl 読み取り失敗: {e}", file=sys.stderr)
        return
    print(f"[pipeline] 承認待ちドラフト: {pending} 件 (review.html で確認)", file=sys.stderr)


def _summarize_log(log_path: Path, run_id: str) -> PipelineSummary:
    """指定 run_id の pipeline_log レコードを集計する。

    同日再実行時に過去 run のレコードを拾わないよう、run_id で明示的にフィルタする。
    不正な JSON 行は個別に skip し、集計全体は継続する。log_path 不在・
    OSError 時は空サマリを返す（監視集計のためパイプライン本処理を止めない方針）。

    Args:
        log_path: pipeline_log/YYYY-MM-DD.jsonl へのパス。
        run_id: 対象 run の識別子。

    Returns:
        PipelineSummary: ステップ別 duration、collect→classify 間隔、総所要秒。
    """
    steps: Dict[str, float] = {}
    c2c_sec: Optional[float] = None
    total = 0.0
    if not log_path.exists():
        return {"steps": steps, "collect_to_classify_sec": c2c_sec, "total_sec": total}

    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("run_id") != run_id:
                    continue
                step = rec.get("step")
                if step == "pipeline_metric":
                    if rec.get("collect_to_classify_sec") is not None:
                        c2c_sec = rec["collect_to_classify_sec"]
                    continue
                if step == "pipeline_start":
                    continue
                dur = rec.get("duration_sec", 0.0)
                if step:
                    steps[step] = dur
                    total += dur
    except OSError as e:
        print(f"[pipeline] log 集計読み取り失敗 ({log_path}): {e}", file=sys.stderr)

    return {"steps": steps, "collect_to_classify_sec": c2c_sec, "total_sec": total}


def _finish(log_path: Path, run_id: str, failures: List[str], today: str) -> int:
    print(f"\n===== pipeline 終了 =====")

    # plan.md M1 T1.9: ステップ別所要秒は stderr へ（M4 ゲート監視用）
    summary = _summarize_log(log_path, run_id)
    if summary["steps"]:
        print("ステップ別所要秒:", file=sys.stderr)
        for step, dur in summary["steps"].items():
            print(f"  {step:<10s} {dur:7.2f}s", file=sys.stderr)
        print(f"  {'total':<10s} {summary['total_sec']:7.2f}s", file=sys.stderr)

    c2c = summary["collect_to_classify_sec"]
    if c2c is not None:
        print(f"collect→classify: {c2c:.2f}s", file=sys.stderr)
        if c2c >= COLLECT_TO_CLASSIFY_WARN_SEC:
            print(
                f"[WARN] collect→classify が {c2c:.0f}s (>= {COLLECT_TO_CLASSIFY_WARN_SEC}s / M4 ゲート超過)",
                file=sys.stderr,
            )

    if failures:
        print(f"失敗ステップ: {failures}", file=sys.stderr)
        print(f"ログ: {log_path}", file=sys.stderr)
        return 1
    print(f"全ステップ成功 ({today})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
