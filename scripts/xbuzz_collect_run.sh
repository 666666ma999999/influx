#!/bin/bash
# x-buzz 週次収集ラッパー（launchd com.masa.xbuzz-buzz-collect から呼ぶ想定・2026-07-26 新設）
# 背景: 2026-07-20 20:30 実行が「failed to connect to the docker API ... daemon is running」で
#       失敗し、最新出力が grok-twittora-2026-07-19.jsonl のまま5日間停止した（Docker Desktop未起動）。
# 対策: docker info が通るまで最大5分・30秒間隔で待機リトライしてから本処理を実行。
#       タイムアウト時は明確なエラーで exit 1（launchd の err.log に残る）。
set -u

MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}
waited=0
until docker info >/dev/null 2>&1; do
  if [ "$waited" -ge "$MAX_WAIT_SEC" ]; then
    echo "ERROR: Docker daemon起動待ちタイムアウト(${MAX_WAIT_SEC}秒経過)。daemon is not running." >&2
    exit 1
  fi
  echo "Docker daemon起動待ち... (${waited}/${MAX_WAIT_SEC}秒)"
  sleep "$INTERVAL_SEC"
  waited=$((waited + INTERVAL_SEC))
done

# 2026-07-26: daemonは生きているがコンテナが停止しているケースを即失敗で可視化
# （前例= make_article cron_metrics_snapshot.sh の xstock-vnc 稼働チェック）
if ! docker ps --format '{{.Names}}' | grep -q '^xstock-vnc$'; then
  echo "ERROR: xstock-vnc コンテナが稼働していない（influx で docker compose -f docker-compose.vnc.yml up -d）" >&2
  exit 1
fi

docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_search_collect_twittora.py --days 7
rc=$?
TODAY=$(date +%Y-%m-%d)
VAULT_RAW="$HOME/Documents/Obsidian Vault/.raw"
SRC="$HOME/Desktop/biz/influx/output/grok_twittora/grok-twittora-$TODAY.jsonl"
DEST="$VAULT_RAW/grok-twittora-$TODAY.jsonl"
# 2026-07-26: vault側の原観測退避（同日再実行でのcp上書き消失対策）。
# 退避先は拡張子 .jsonl.bak（glob `grok-twittora-*.jsonl` にヒットしないため下流の二重読みなし）。
if [ -f "$DEST" ]; then
  STAMP=$(date +%H%M)
  mv "$DEST" "$VAULT_RAW/grok-twittora-$TODAY.pre-$STAMP.jsonl.bak" 2>/dev/null || true
fi
cp "$SRC" "$DEST" 2>/dev/null || true
exit $rc
