#!/usr/bin/env python3
"""日証金日次アーカイブ(scripts/jsf_daily_archive.py)の欠損検知（監査O-7）。

data/jsf/archive_log.jsonl を直近5営業日走査し、4データセット
（zandaka/shina/meigara/seigenichiran・jsf_daily_archive.DATASETSをCanonical Moduleとして再利用）
それぞれについて、その日に saved/skipped_dup のいずれかの記録が最低1件あるかを検査する
（launchdは平日1日2回実行のため、いずれか1回が成功していれば充足とみなす。1日とも
error のみだった日を欠損として扱う）。

「営業日」は日証金アーカイバのlaunchd実行スケジュール（平日Weekday1-5・com.influx.jsf-archive）
に合わせた暦週平日であり、J-Quants側のJPX営業日カレンダー（祝日除外）とは別の定義
（本スクリプトはjsf archive監視専用でJ-Quants依存を持たせないための意図的な単純化）。

Usage:
    python3 scripts/check_jsf_gaps.py
    python3 scripts/check_jsf_gaps.py --lookback-weekdays 10
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jsf_daily_archive  # noqa: E402  (Canonical Module: DATASETS/LOG_PATH/now_jstを再利用)

OK_STATUSES = {"saved", "skipped_dup"}


def recent_weekdays(n: int, before: datetime.date) -> list[str]:
    """before（当日）より前のn個の平日日付をYYYY-MM-DD文字列・昇順で返す（当日は含めない）。

    launchd実行(平日12:30/19:30 JST)が完了する前の当日を対象に含めると、朝実行の
    daily_screen.py(07:30)から呼んだ際に常に「当日分欠損」の偽陽性WARNになるため、
    意図的に当日を除外する。
    """
    days: list[str] = []
    d = before - datetime.timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:  # Mon=0..Fri=4
            days.append(d.isoformat())
        d -= datetime.timedelta(days=1)
    return sorted(days)


def _read_log_records(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    records: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def find_gaps(log_records: list[dict], target_days: list[str]) -> list[tuple[str, str]]:
    """(day, dataset) の欠損一覧を返す（欠損なしなら空リスト）。"""
    status_by_day_dataset: dict[tuple[str, str], set[str]] = {}
    for r in log_records:
        dataset = r.get("dataset")
        ts = r.get("ts")
        if not dataset or not ts:
            continue
        day = ts[:10]
        status_by_day_dataset.setdefault((day, dataset), set()).add(r.get("status", ""))

    gaps = []
    for day in target_days:
        for dataset in sorted(jsf_daily_archive.DATASETS):
            statuses = status_by_day_dataset.get((day, dataset), set())
            if not statuses & OK_STATUSES:
                gaps.append((day, dataset))
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description="jsf_daily_archive.py の欠損検知（直近営業日走査）")
    parser.add_argument("--lookback-weekdays", type=int, default=5)
    parser.add_argument("--log-path", default=str(jsf_daily_archive.LOG_PATH))
    args = parser.parse_args()

    log_path = Path(args.log_path)
    records = _read_log_records(log_path)
    if not records:
        print(f"WARN: archive_log.jsonlが見つからないか空です: {log_path}", file=sys.stderr)
        print("archive_log.jsonlなし（未実行の可能性）")
        return 1

    today = jsf_daily_archive.now_jst().date()
    target_days = recent_weekdays(args.lookback_weekdays, today)
    gaps = find_gaps(records, target_days)

    if gaps:
        for day, dataset in gaps:
            print(f"WARN: 欠損 {day} [{dataset}]（saved/skipped_dupの記録なし）", file=sys.stderr)
        print(f"⚠️欠損{len(gaps)}件（直近{args.lookback_weekdays}営業日・詳細はSTDERR）")
        return 1

    print(f"OK（直近{args.lookback_weekdays}営業日・{len(jsf_daily_archive.DATASETS)}データセットとも欠損なし）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
