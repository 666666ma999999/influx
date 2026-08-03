#!/bin/bash
# x-buzz 週次収集ラッパー（launchd com.masa.xbuzz-buzz-collect から呼ぶ想定・2026-07-26 新設）
# 背景: 2026-07-20 20:30 実行が「failed to connect to the docker API ... daemon is running」で
#       失敗し、最新出力が grok-twittora-2026-07-19.jsonl のまま5日間停止した（Docker Desktop未起動）。
# 対策: docker info が通るまで最大5分・30秒間隔で待機リトライしてから本処理を実行。
#       タイムアウト時は明確なエラーで exit 1（launchd の err.log に残る）。
set -u

MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}

# 2026-08-03 追加: 失敗が err.log にしか残らず8日間気づかれなかったため通知を出す
notify() { osascript -e "display notification \"$1\" with title \"X収集 週次\"" 2>/dev/null || true; }

# 2026-07-26 ユーザー許可: daemon 未起動なら Docker Desktop をバックグラウンド起動してから待つ
# （-g=前面に出さない・-a=アプリ名指定。起動失敗しても従来どおり待機ループ→タイムアウト exit 1）
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon 未起動 → Docker Desktop をバックグラウンド起動 (open -ga Docker)"
  open -ga Docker 2>/dev/null || true
fi

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

# 2026-08-03: コンテナ停止時に「即失敗」していたため 7/27 の失敗が8日間放置され、
# 実効収集が平均18日に1回まで落ちていた（敵対レビュー実測）。up -d を1回試してから再判定する。
if ! docker ps --format '{{.Names}}' | grep -q '^xstock-vnc$'; then
  echo "xstock-vnc 停止 → docker compose up -d を試行"
  (cd "$HOME/Desktop/biz/influx" && docker compose -f docker-compose.vnc.yml up -d) || true
  for _ in $(seq 1 12); do
    docker ps --format '{{.Names}}' | grep -q '^xstock-vnc$' && break
    sleep 5
  done
fi
if ! docker ps --format '{{.Names}}' | grep -q '^xstock-vnc$'; then
  echo "ERROR: xstock-vnc コンテナを起動できなかった（influx で docker compose -f docker-compose.vnc.yml up -d）" >&2
  notify "X収集 失敗: xstock-vnc を起動できず"
  exit 1
fi

# 2026-08-03 22時台の実測: コンテナを自動起動した直後の docker exec は
# TargetClosedError（ブラウザ即死）で落ちる（20:30 実走 rows:0 crash・fxnia 11:04 と同型）。
# Xvfb/環境の温まり待ち＋失敗時1回だけ再試行（tracer の1リトライ前例と同型・有界）。
sleep 20
docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_search_collect_twittora.py --days 7
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "収集 exit $rc → 60秒待って1回だけ再試行（ブラウザ即死の一過性対策）"
  sleep 60
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_search_collect_twittora.py --days 7
  rc=$?
fi
[ "$rc" -ne 0 ] && notify "X収集 失敗: 収集スクリプトが exit $rc（再試行込み）"
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
