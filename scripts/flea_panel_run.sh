#!/bin/bash
# Yahoo!フリマ 固定パネル 日次ランナー（launchd com.influx.flea-panel から毎日 21:40・P-INF-19 2026-09-22 新設）
# 流れ: Docker daemon 待機 → `docker compose run --rm xstock` で flea_panel_track.py → 失敗・件数不足を macOS 通知。
# 成功でなく失敗を通知する（機能マップ §5）。出力の全文は launchd の StandardOutPath（~/.claude/state/flea-panel.out.log）。
set -u
INFLUX="$HOME/Desktop/biz/influx"
MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}
OUT=$(mktemp -t flea-panel)

notify() {
  local msg title
  msg=$(printf '%s' "$1" | tr -d '"\\')
  title=$(printf '%s' "$2" | tr -d '"\\')
  osascript -e "display notification \"${msg}\" with title \"${title}\"" 2>/dev/null || true
}

# --- Docker daemon 待機（compose run は daemon さえ居ればよい・VNC コンテナは不要） ---
waited=0
until docker info >/dev/null 2>&1; do
  if [ "$waited" -ge "$MAX_WAIT_SEC" ]; then
    echo "ERROR: Docker daemon が ${MAX_WAIT_SEC}s 以内に上がらない" >&2
    notify "Docker daemon が起きていません" "⚠️ フリマパネル 失敗"
    exit 1
  fi
  sleep "$INTERVAL_SEC"; waited=$((waited + INTERVAL_SEC))
done

cd "$INFLUX" || exit 1
docker compose run --rm xstock python scripts/flea_panel_track.py 2>&1 | tee "$OUT"
rc=${PIPESTATUS[0]}

summary=$(grep -E '^--- flea_panel' "$OUT" | tail -1)
if [ "$rc" -ne 0 ]; then
  notify "取得に失敗したクエリあり (rc=$rc) ${summary}" "⚠️ フリマパネル 失敗"
  exit "$rc"
fi
if [ -z "$summary" ]; then
  notify "実行はしたが集計行を検出できず（出力形式の変化を疑う）" "⚠️ フリマパネル 要確認"
  exit 3
fi
planned=$(printf '%s' "$summary" | sed -n 's/.*queries_planned=\([0-9]*\).*/\1/p')
processed=$(printf '%s' "$summary" | sed -n 's/.*processed=\([0-9]*\).*/\1/p')
case "$planned:$processed" in
  *[!0-9:]*|:*|*:) notify "集計行から件数を読めない: ${summary}" "⚠️ フリマパネル 要確認"; exit 3 ;;
esac
if [ "$planned" != "$processed" ]; then
  notify "渡した ${planned} 件のうち処理 ${processed} 件" "⚠️ フリマパネル 未完"
  exit 4
fi
rm -f "$OUT"
exit 0
