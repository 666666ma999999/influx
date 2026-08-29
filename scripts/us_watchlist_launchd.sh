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
# 選定=config記載順の先頭 JP_SEARCH_N（結果で選ばない・2026-07-28追加）。
# 名簿の正本は collector/config.py INFLUENCER_GROUPS の1箇所（2026-08-29 統合。以前はここへ
# 手書きコピーしており、config を触った時に片側だけ変わる状態だった）。増員は N を上げるだけ。
JP_SEARCH_N=10
ACCOUNTS_JP_SEARCH=$(cd "$PROJECT_ROOT" && JP_SEARCH_N="$JP_SEARCH_N" python3 - <<'PYROSTER'
import json, os, sys
sys.path.insert(0, ".")
from collector.config import INFLUENCER_GROUPS

n = int(os.environ["JP_SEARCH_N"])
handles = [a["username"] for g in INFLUENCER_GROUPS.values()
           for a in (g.get("accounts", []) if isinstance(g, dict) else g)
           if isinstance(a, dict) and "username" in a]
if len(handles) < n:
    sys.exit(f"FATAL: config の名簿が {len(handles)} 人で N={n} に届かない")
top = handles[:n]
# fail-closed: 既に前向き観察に入っている口が先頭 N から落ちたら中断（コホートを黙って変えない）
try:
    with open("data/influencer_candidates/jp_forward/receipts.jsonl", encoding="utf-8") as f:
        seen = {json.loads(l)["account"] for l in f if l.strip()}
except FileNotFoundError:
    seen = set()
dropped = sorted((seen & set(handles)) - set(top))
if dropped:
    sys.exit("FATAL: 観察中の口が名簿の先頭から外れた: " + " ".join(dropped))
print(" ".join(top))
PYROSTER
) || { echo "FATAL: jp_search 名簿の導出に失敗（collector/config.py を確認）" >&2; exit 4; }
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
# fxnia新旧YouTubeチャンネルの動画タイトル台帳（第23R③(b)・公式RSS・軽量）
python3 "$PROJECT_ROOT/scripts/nia_youtube_rss.py" || echo "警告: nia-rss 失敗" >&2
docker compose -f docker-compose.vnc.yml down
echo "[us-watchlist] $RUN_DAY 完了 (us+jp)"
