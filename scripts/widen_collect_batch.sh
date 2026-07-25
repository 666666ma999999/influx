#!/bin/bash
# 発掘拡張バッチ: 未検証アカウントを中立全収集（コンテナ内 detached 実行用）。
# 第17R Batch1 の拡張。各収集の間に PACING 秒あけてアカウント保護（SafeXCollectorは低頻度想定）。
# 採点は収集完了後にホスト側でまとめて実施する（本スクリプトは収集のみ）。
set -u
LOG=/app/output/recollect_logs/widen.log
PACING=90
# 対象は環境変数 ACCOUNTS で上書き可（既定=第17R拡張バッチの8人・完了済み）。
# 例: ACCOUNTS="kabudev_gc bahbi_76" bash /app/scripts/widen_collect_batch.sh
ACCOUNTS="${ACCOUNTS:-kazzn_blog noatake1127 tkmr_kato drdebuneko u___a___53 kabumoto_kabu purazumakoi gihuboy}"
echo "=== widen batch start $(date -u +%H:%M:%S) accounts=[$ACCOUNTS] ===" >> "$LOG"
for acc in $ACCOUNTS; do
  echo "--- collect @$acc $(date -u +%H:%M:%S) ---" >> "$LOG"
  PYTHONPYCACHEPREFIX=/tmp/pc DISPLAY=:99 python3 -u /app/scripts/recollect_account.py \
    --account "$acc" --since 2025-08-01 --until 2026-06-15 --max-scrolls 200 >> "$LOG" 2>&1
  echo "--- done @$acc, sleep ${PACING}s ---" >> "$LOG"
  sleep "$PACING"
done
echo "=== widen batch DONE $(date -u +%H:%M:%S) ===" >> "$LOG"
