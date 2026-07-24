#!/bin/bash
# @fxnia_kabu 前向き監視の週次capture+採点+台帳追記（Batch3事前登録・launchdラッパー）。
# research_weekly_launchd.sh と同型: キー解決→vnc up→Xvfb待ち→docker exec→down。
# since=監視開始日(2026-07-24 固定=out-of-sample境界)。until=recollect既定(35日前・成熟)。
# 各週フル窓を採り直すため、Docker停止で走らなかった週があっても次回成功で完全回復する。
set -uo pipefail

PROJECT_ROOT="/Users/masaaki_nagasawa/Desktop/biz/influx"
cd "$PROJECT_ROOT" || exit 1
START=20260724

load_key_from_zshrc() {
    local var_name="$1"
    [ -n "${!var_name:-}" ] && return 0
    local line
    line=$(grep -m1 "^export ${var_name}=" "$HOME/.zshrc" 2>/dev/null || true)
    [ -n "$line" ] && { eval "$line"; export "$var_name"; }
}
load_key_from_zshrc ANTHROPIC_API_KEY
load_key_from_zshrc XAI_API_KEY
load_key_from_zshrc COOKIE_ENCRYPTION_KEY
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "FATAL: ANTHROPIC_API_KEY 未解決" >&2; exit 2
fi
export ANTHROPIC_API_KEY XAI_API_KEY COOKIE_ENCRYPTION_KEY

docker compose -f docker-compose.vnc.yml up -d || { echo "FATAL: docker compose up 失敗（Docker未起動?）" >&2; exit 3; }

READY=0
for _ in $(seq 1 15); do
    docker exec xstock-vnc test -S /tmp/.X11-unix/X99 2>/dev/null && { READY=1; break; }
    sleep 2
done
[ "$READY" -ne 1 ] && echo "警告: Xvfb起動確認タイムアウト・続行" >&2

ASOF=$(date +%Y%m%d)
SINCE_ISO="2026-07-24"

# capture（forward専用dir・in-sample recollect/ を汚さない）
docker exec -e DISPLAY=:99 -e PYTHONPYCACHEPREFIX=/tmp/pc xstock-vnc \
    python3 /app/scripts/recollect_account.py \
    --account fxnia_kabu --since "$SINCE_ISO" \
    --output-dir /app/data/influencer_candidates/forward || echo "警告: capture失敗" >&2

# score
docker exec -e PYTHONPYCACHEPREFIX=/tmp/pc xstock-vnc \
    python3 /app/scripts/influencer_candidate_score.py \
    --input /app/data/influencer_candidates/forward/fxnia_kabu.json \
    --output-dir /app/output/influencer_candidates/fxnia_forward || echo "警告: score失敗（成熟コール0の可能性）" >&2

# 評価＋台帳追記
docker exec -e PYTHONPYCACHEPREFIX=/tmp/pc xstock-vnc \
    python3 /app/scripts/fxnia_forward_eval.py \
    --mentions /app/output/influencer_candidates/fxnia_forward/mentions.csv \
    --ledger /app/output/influencer_candidates/fxnia_forward_ledger.tsv \
    --asof "$ASOF" --start "$START"

docker compose -f docker-compose.vnc.yml down
exit 0
