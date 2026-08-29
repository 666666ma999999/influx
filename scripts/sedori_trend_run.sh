#!/bin/bash
# せどりトレンド定点観測 週次ランナー（launchd com.influx.sedori-trend 毎週月曜09:00・2026-08-09新設）
# 流れ: Docker待機 → 収集(configs/sedori_trend.json・専用texts) → ダイジェスト生成 →
#       供給側反応があればMac通知。観測専用＝本番50本・map・台帳・αに不干渉。
# 型は price_universe_run.sh / xprice_watch_run.sh の合成（既存パターン踏襲）。
set -u

MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}
INFLUX="$HOME/Desktop/biz/influx"
TARGET_DATE=${TARGET_DATE:-$(date -u -v-1d +%Y-%m-%d)}

# --- Docker daemon 待機＋コンテナ復旧（共通部品・2026-08-29 に手書きから移行） ---
# 変更点: 以前は「xstock-vnc が停止していたら即 exit 1」だったが、共通部品に寄せたことで
# **自分で起こしてから続行**するようになった（収集・価格監視と同じ挙動＝週次が自力復旧できる）。
# ⚠️ ここに復旧処理を手書きしないこと。
if [ -z "${XSTOCK_SKIP_ENSURE:-}" ]; then
  . "$(dirname "$0")/lib/xstock_vnc.sh"
  XSTOCK_NOTIFY_TITLE="⚠️ せどりトレンド観測 失敗"
  XSTOCK_DAEMON_WAIT="$MAX_WAIT_SEC"
  XSTOCK_DAEMON_INTERVAL="$INTERVAL_SEC"
  XSTOCK_INFLUX="$INFLUX"
  xstock_ensure_ready || exit 1
fi

# 週次実行のため直近7日分（UTC日）を順に収集（既収集日はcollector側のtexts上書きで冪等）
fail=0
for i in 1 2 3 4 5 6 7; do
  D=$(date -u -v-"${i}"d +%Y-%m-%d)
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_collect.py \
    --config /app/configs/sedori_trend.json \
    --ledger /app/data/sedori_trend/ledger.jsonl \
    --texts-dir /app/data/sedori_trend/texts \
    --date "$D" || fail=$((fail+1))
done
if [ "$fail" -ge 7 ]; then
  osascript -e "display notification \"収集が全日失敗\" with title \"⚠️ せどりトレンド観測 失敗\"" 2>/dev/null || true
  exit 1
fi

OUT=$(/usr/bin/python3 "$INFLUX/scripts/sedori_trend_digest.py" 2>&1)
drc=$?
echo "$OUT"
if [ "$drc" -ne 0 ]; then
  osascript -e "display notification \"digest生成が失敗 (rc=$drc)\" with title \"⚠️ せどりトレンド観測 失敗\"" 2>/dev/null || true
  exit "$drc"
fi
# 供給側反応が1件以上あれば通知（機械可読行 SUPPLY_COUNT= を完全一致で読む）
SUPPLY=$(echo "$OUT" | sed -n 's/^SUPPLY_COUNT=\([0-9][0-9]*\)$/\1/p' | tail -1)
if [ -z "$SUPPLY" ]; then
  osascript -e 'display notification "SUPPLY_COUNT行を検出できず（出力形式の変化を疑う）" with title "⚠️ せどりトレンド観測 要確認"' 2>/dev/null || true
  exit 1
fi
if [ "$SUPPLY" -gt 0 ]; then
  osascript -e "display notification \"供給側反応 ${SUPPLY}件（再販・増産・受注生産）\" with title \"🛒 せどりトレンド週次\" subtitle \"digestを確認\"" 2>/dev/null || true
fi
exit 0
