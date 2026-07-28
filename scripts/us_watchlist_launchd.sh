#!/bin/bash
# インフルエンサー watchlist 前向き保存（毎週火曜10:30・launchd）。
# us_forward: 第21R裁定・仕様正本 tasks/us_watchlist_preregister.md §2。
# jp_forward: 第22R裁定・仕様正本 tasks/influencer_discovery_preregister.md §第22R。
# rolling 14日窓・日付スナップショット（上書きしない）・sha256をreceiptsへappend。
set -uo pipefail
PROJECT_ROOT="$HOME/Desktop/biz/influx"
cd "$PROJECT_ROOT" || exit 1
ACCOUNTS_US="yukimamax paurooteri investramza Biz_zatukora tomoyaasakura kakatothecat Drdebuneko YasLovesTech"
ACCOUNTS_JP="ShinjukuSokai"
# 第23R裁定②: 前向き勝率台帳の段階拡大（月+10人上限・bot徴候で即停止）。
# 選定=config記載順の先頭10（結果で選ばない・2026-07-28追加）。
ACCOUNTS_JP_SEARCH="goto_finance cissan_9984 tesuta001 yurumazu 2okutameo tapazou29 kanpo_blog haru_tachibana8 heihachiro888 uehara_sato4"
RUN_DAY=$(date +%Y%m%d)
SINCE=$(date -v-14d +%Y-%m-%d)
UNTIL=$(date -v+1d +%Y-%m-%d)

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

capture_group() {
    local base="$1" via="$2" scrolls="$3"; shift 3
    local out_rel="data/influencer_candidates/$base/$RUN_DAY"
    local receipts="data/influencer_candidates/$base/receipts.jsonl"
    mkdir -p "$out_rel" "$(dirname "$receipts")"
    for acc in "$@"; do
        docker exec -e DISPLAY=:99 -e PYTHONPYCACHEPREFIX=/tmp/pc xstock-vnc \
            python3 /app/scripts/recollect_account.py \
            --account "$acc" --since "$SINCE" --until "$UNTIL" \
            --via "$via" --max-scrolls "$scrolls" \
            --output-dir "/app/$out_rel" || echo "警告: @$acc capture失敗" >&2
        local f="$out_rel/$acc.json"
        if [ -f "$f" ]; then
            local SHA N
            SHA=$(shasum -a 256 "$f" | awk '{print $1}')
            N=$(python3 -c "import json;print(json.load(open('$f'))['_collection']['own_posts'])" 2>/dev/null || echo "?")
            printf '{"run_day":"%s","account":"%s","file":"%s","sha256":"%s","own_posts":%s,"ts":"%s"}\n' \
                "$RUN_DAY" "$acc" "$f" "$SHA" "${N:-null}" "$(date -Iseconds)" >> "$receipts"
        fi
        sleep 60
    done
}

# jp: ShinjukuSokai は from:検索が0件（検索インデックス除外・2026-07-28実測）のため
# profile直読み。超高頻度（16投稿/2日実測）なので週次でもスクロール多め。
capture_group us_forward search 40 $ACCOUNTS_US
capture_group jp_forward profile 80 $ACCOUNTS_JP
capture_group jp_forward search 25 $ACCOUNTS_JP_SEARCH
docker compose -f docker-compose.vnc.yml down
echo "[us-watchlist] $RUN_DAY 完了 (us+jp)"
