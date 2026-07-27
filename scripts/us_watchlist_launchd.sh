#!/bin/bash
# 米国株ウォッチリスト前向き保存（第21R裁定・毎週火曜10:30・launchd）。
# 仕様正本: tasks/us_watchlist_preregister.md §2。
# rolling 14日窓・日付スナップショット（上書きしない）・sha256をreceiptsへappend。
set -uo pipefail
PROJECT_ROOT="$HOME/Desktop/biz/influx"
cd "$PROJECT_ROOT" || exit 1
ACCOUNTS="yukimamax paurooteri investramza Biz_zatukora tomoyaasakura kakatothecat Drdebuneko YasLovesTech"
RUN_DAY=$(date +%Y%m%d)
SINCE=$(date -v-14d +%Y-%m-%d)
UNTIL=$(date -v+1d +%Y-%m-%d)
OUT_REL="data/influencer_candidates/us_forward/$RUN_DAY"
RECEIPTS="data/influencer_candidates/us_forward/receipts.jsonl"
mkdir -p "$OUT_REL" "$(dirname "$RECEIPTS")"

load_key_from_zshrc() {
    local v="$1"; [ -n "${!v:-}" ] && return 0
    local line; line=$(grep -m1 "^export ${v}=" "$HOME/.zshrc" 2>/dev/null || true)
    [ -n "$line" ] && { eval "$line"; export "$v"; }
}
load_key_from_zshrc COOKIE_ENCRYPTION_KEY
export COOKIE_ENCRYPTION_KEY

docker compose -f docker-compose.vnc.yml up -d || { echo "FATAL: docker up 失敗" >&2; exit 3; }
for _ in $(seq 1 15); do
    docker exec xstock-vnc test -S /tmp/.X11-unix/X99 2>/dev/null && break
    sleep 2
done

for acc in $ACCOUNTS; do
    docker exec -e DISPLAY=:99 -e PYTHONPYCACHEPREFIX=/tmp/pc xstock-vnc \
        python3 /app/scripts/recollect_account.py \
        --account "$acc" --since "$SINCE" --until "$UNTIL" --max-scrolls 40 \
        --output-dir "/app/$OUT_REL" || echo "警告: @$acc capture失敗" >&2
    f="$OUT_REL/$acc.json"
    if [ -f "$f" ]; then
        SHA=$(shasum -a 256 "$f" | awk '{print $1}')
        N=$(python3 -c "import json;print(json.load(open('$f'))['_collection']['own_posts'])" 2>/dev/null || echo "?")
        printf '{"run_day":"%s","account":"%s","file":"%s","sha256":"%s","own_posts":%s,"ts":"%s"}\n' \
            "$RUN_DAY" "$acc" "$f" "$SHA" "${N:-null}" "$(date -Iseconds)" >> "$RECEIPTS"
    fi
    sleep 60
done
docker compose -f docker-compose.vnc.yml down
echo "[us-watchlist] $RUN_DAY 完了"
