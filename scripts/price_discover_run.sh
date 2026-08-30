#!/bin/bash
# 発見器（X→新商品名の候補キュー）の週次ランナー
# （launchd com.influx.price-discover から毎週日曜 10:40 に呼ばれる。時刻の正本は config/launchd/com.influx.price-discover.plist）
# 2026-08-30 新設（tasks/shortage_goods_expansion.md B-2・オーナー裁定で 2026-08-01 以来休眠の発見器を再稼働）。
# 構成は xprice_watch_run.sh（日次X版・xstock-vnc 経由）と price_universe_run.sh（週次・通知）を意図的に揃えている。
# 流れ: Docker daemon 待機 → xstock-vnc 稼働チェック → 発見器実行（失敗時5分後1回再試行）→
#       候補キューの新規行を数えて macOS 通知（候補あり＝件数・ログイン壁/失敗＝失敗通知）。
# 手動実行も同じ経路で: bash scripts/price_discover_run.sh
# 候補は「週次レビュー用のキュー」＝売買判断には5チェック必須（docs/price-watch-universe.md §発見器運用）。
set -u

MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}
INFLUX="$HOME/Desktop/biz/influx"
QUEUE="$INFLUX/data/x_price_watch/discovery_queue.jsonl"
DISCOVER_CMD=${DISCOVER_CMD:-"docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_discover.py"}

# 失敗通知（lib を source しない XSTOCK_SKIP_ENSURE 経路でも鳴るよう runner 側に持つ）
discover_notify() {
  local msg title
  msg=$(printf '%s' "$1" | tr -d '"\\')
  title=$(printf '%s' "${2:-${XSTOCK_NOTIFY_TITLE:-⚠️ 発見器(X候補キュー) 失敗}}" | tr -d '"\\')
  osascript -e "display notification \"${msg}\" with title \"${title}\"" 2>/dev/null || true
}

# --- Docker daemon 待機＋コンテナ復旧（共通部品・ここに手書きしない） ---
if [ -z "${XSTOCK_SKIP_ENSURE:-}" ]; then
  # 変数は source の前に置く（lib は ${VAR:-既定} で初期化するため）
  XSTOCK_NOTIFY_TITLE=${XSTOCK_NOTIFY_TITLE:-"⚠️ 発見器(X候補キュー) 失敗"}
  XSTOCK_DAEMON_WAIT=${XSTOCK_DAEMON_WAIT:-$MAX_WAIT_SEC}
  XSTOCK_DAEMON_INTERVAL=${XSTOCK_DAEMON_INTERVAL:-$INTERVAL_SEC}
  XSTOCK_INFLUX=${XSTOCK_INFLUX:-$INFLUX}
  . "$(dirname "$0")/lib/xstock_vnc.sh"
  xstock_ensure_ready || exit 1
fi

before=0
[ -f "$QUEUE" ] && before=$(wc -l < "$QUEUE" | tr -d ' ')

# --- 実行（一時的なネットワーク障害を想定し、失敗時は5分後に1回だけ再試行。ログイン壁も rc=1 なので同じ扱い） ---
$DISCOVER_CMD
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "発見器失敗(rc=$rc)。5分待って1回だけ再試行..."
  sleep 300
  $DISCOVER_CMD
  rc=$?
fi

after=0
[ -f "$QUEUE" ] && after=$(wc -l < "$QUEUE" | tr -d ' ')

if [ "$rc" -ne 0 ]; then
  # 失敗こそ通知する（機能マップ §5「成功でなく失敗を通知」）。status 行は発見器自身が台帳に残す
  last_status=$(tail -n 1 "$QUEUE" 2>/dev/null | /usr/bin/python3 -c 'import sys,json; d=json.loads(sys.stdin.readline() or "{}"); print(d.get("status",""))' 2>/dev/null || echo "")
  discover_notify "発見器が2回失敗 (rc=$rc status=${last_status:-?})。ログイン壁なら Cookie 再取得（refresh-x-cookies）"
  exit "$rc"
fi

if [ "$after" -gt "$before" ]; then
  summary=$(tail -n 1 "$QUEUE" | /usr/bin/python3 -c 'import sys,json,re
d=json.loads(sys.stdin.readline() or "{}")
c=d.get("candidates") or []
head=", ".join(x.get("token","") for x in c[:3])
s=f"{len(c)}件 posts={d.get(\"n_posts\",\"?\")} status={d.get(\"status\",\"?\")}" + (" / "+head if head else "")
print(re.sub(r"[\"\\\\]", "", s))' 2>/dev/null || echo "詳細は discovery_queue.jsonl")
  n_cand=$(tail -n 1 "$QUEUE" | /usr/bin/python3 -c 'import sys,json; d=json.loads(sys.stdin.readline() or "{}"); print(len(d.get("candidates") or []))' 2>/dev/null || echo 0)
  if [ "${n_cand:-0}" -gt 0 ]; then
    discover_notify "$summary" "🔎 発見器: 新商品名の候補 ${n_cand}件（週次レビュー用）"
  else
    echo "[runner] 実行は成功したが候補 0 件（$summary）"
  fi
else
  # 発見器は成功でも失敗でも必ず1行残す設計＝増えていないのは異常
  discover_notify "実行は完了したが台帳に行が増えていない（出力形式の変化を疑う）" "⚠️ 発見器 要確認"
fi
exit "$rc"
