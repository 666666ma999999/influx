"""日次 1 投稿運用パイプライン（plan.md M4 T4.1）。

collect → classify → compose → viewer 更新を 1 コマンドで実行する。
cron/launchd で 1 日 1 回呼ばれることを想定。途中で失敗したら stderr に
ステップ名と exit code を出力して停止し、以降のステップは飛ばす。

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

JST = timezone(timedelta(hours=9))
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: List[str], step: str, allow_nonzero: bool = False) -> int:
    """サブコマンド実行。失敗時は stderr にログを残して呼び出し元へ返す。"""
    print(f"\n===== [{step}] =====")
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if proc.returncode != 0 and not allow_nonzero:
        print(f"[ERROR] {step} が exit code {proc.returncode} で失敗", file=sys.stderr)
    return proc.returncode


def _log_run(log_path: Path, step: str, started_at: str, rc: int, duration_sec: float) -> None:
    """pipeline_log/YYYY-MM-DD.jsonl に追記。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "step": step,
        "started_at": started_at,
        "finished_at": datetime.now(JST).isoformat(),
        "exit_code": rc,
        "duration_sec": round(duration_sec, 2),
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
    print(f"[pipeline] day={today} log={log_path}")

    failures: List[str] = []

    # Step 1: collect_tweets
    if not args.skip_collect:
        start = datetime.now(JST).isoformat()
        import time
        t0 = time.time()
        rc = _run(
            ["python3", "scripts/collect_tweets.py",
             "--groups", args.groups, "--scrolls", str(args.scrolls), "--output", args.output],
            "collect",
        )
        _log_run(log_path, "collect", start, rc, time.time() - t0)
        if rc != 0:
            failures.append("collect")
            print("[pipeline] collect 失敗のため以降スキップ", file=sys.stderr)
            return _finish(log_path, failures, today)

    # Step 2: classify_tweets
    if not args.skip_classify and "collect" not in failures:
        start = datetime.now(JST).isoformat()
        import time
        t0 = time.time()
        rc = _run(["python3", "scripts/classify_tweets.py"], "classify")
        _log_run(log_path, "classify", start, rc, time.time() - t0)
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
            return _finish(log_path, failures, today)

        start = datetime.now(JST).isoformat()
        import time
        t0 = time.time()
        cmd = [
            "python3", "-m", "extensions.tier3_posting.cli.compose",
            "--input", str(latest),
            "--output-dir", args.posting_output,
        ]
        if args.no_llm_compose:
            cmd.append("--no-llm")
        rc = _run(cmd, "compose")
        _log_run(log_path, "compose", start, rc, time.time() - t0)
        if rc != 0:
            failures.append("compose")

    # Step 4: 承認待ちドラフト数を stderr に通知
    _notify_pending(Path(args.posting_output))

    return _finish(log_path, failures, today)


def _notify_pending(posting_dir: Path) -> None:
    """draft 状態のドラフト数を stderr に出す（運用者アラート用）。"""
    drafts_file = posting_dir / "drafts.jsonl"
    if not drafts_file.exists():
        print("[pipeline] drafts.jsonl 不在", file=sys.stderr)
        return
    pending = 0
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
    print(f"[pipeline] 承認待ちドラフト: {pending} 件 (review.html で確認)", file=sys.stderr)


def _finish(log_path: Path, failures: List[str], today: str) -> int:
    print(f"\n===== pipeline 終了 =====")
    if failures:
        print(f"失敗ステップ: {failures}", file=sys.stderr)
        print(f"ログ: {log_path}", file=sys.stderr)
        return 1
    print(f"全ステップ成功 ({today})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
