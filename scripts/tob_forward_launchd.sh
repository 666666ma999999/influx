#!/bin/bash
# tob_drift_v1 前向きペーパー 日次ラン（毎朝07:15・launchd）。Docker不要（標準ライブラリ＋ローカルbars）。
# 手順: TDnetインデックス直近7日更新 → ランナー（observe→order→fill→exit）。
set -uo pipefail
PROJECT_ROOT="/Users/masaaki_nagasawa/Desktop/biz/influx"
cd "$PROJECT_ROOT" || exit 1
python3 scripts/tdnet_index_fetch.py --recent 7 || echo "警告: index更新失敗（欠測日として記録される）" >&2
python3 scripts/tob_forward_runner.py
