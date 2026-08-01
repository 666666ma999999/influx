#!/bin/bash
# price_watch 日次ランナー（launchd com.influx.price-watch から毎日22:10に呼ばれる・2026-07-26 新設）
# 流れ: Docker daemon 待機 → xstock-vnc 稼働チェック → 収集(失敗時5分後1回再試行) →
#       アラート判定(ホスト・stdlibのみ) → 新規アラートがあれば macOS 通知。
# 構成は xbuzz_collect_run.sh(Docker待機/コンテナ検査) + xbuzz_tracer_run.sh(再試行/通知差分) の合成。
set -u

MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}
INFLUX="$HOME/Desktop/biz/influx"
# 対象日 = 直前の完結UTC日（price_watch_collect.target_utc_day と同一定義・BSD date）
TARGET_DATE=${TARGET_DATE:-$(date -u -v-1d +%Y-%m-%d)}
ALERTS="${ALERTS_FILE:-$INFLUX/output/price_watch/alerts-$TARGET_DATE.jsonl}"
# --date を明示して collector と alert の対象日を一致させる（UTC日跨ぎ・Docker待機遅延対策）
COLLECT_CMD=${COLLECT_CMD:-"docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/price_watch_collect.py --date $TARGET_DATE"}
ALERT_CMD=${ALERT_CMD:-"/usr/bin/python3 $INFLUX/scripts/price_watch_alert.py"}

# --- Docker daemon 待機（未起動ならバックグラウンド起動して最大5分待つ） ---
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon 未起動 → Docker Desktop をバックグラウンド起動 (open -ga Docker)"
  open -ga Docker 2>/dev/null || true
fi
waited=0
until docker info >/dev/null 2>&1; do
  if [ "$waited" -ge "$MAX_WAIT_SEC" ]; then
    echo "ERROR: Docker daemon起動待ちタイムアウト(${MAX_WAIT_SEC}秒経過)" >&2
    exit 1
  fi
  echo "Docker daemon起動待ち... (${waited}/${MAX_WAIT_SEC}秒)"
  sleep "$INTERVAL_SEC"
  waited=$((waited + INTERVAL_SEC))
done

# コンテナが消えていたら自分で起こす（2026-07-29 追加）。
# 常駐化(compose の restart: unless-stopped)を選ばなかったのは、実測でメモリ 1.79GiB を
# 常時占有するため。日次の数分だけ必要なプロセスに常時2GBは割に合わない。
# 実害の記録: 2026-07-27 と 07-29 の2回コンテナが消え、22:10 の自動実行が
# 「ERROR: xstock-vnc コンテナが稼働していない」で失敗していた（price-watch.err.log に2行）。
if ! docker ps --format '{{.Names}}' | grep -q '^xstock-vnc$'; then
  echo "xstock-vnc が停止中 → 起動する"
  (cd "$INFLUX" && docker compose -f docker-compose.vnc.yml up -d) || true
  waited=0
  until docker ps --format '{{.Names}}' | grep -q '^xstock-vnc$'; do
    if [ "$waited" -ge 120 ]; then
      echo "ERROR: xstock-vnc を120秒待っても起動できなかった" >&2
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  # Xvfb(:99) と supervisord の立ち上がりを待つ。ここを待たずに docker exec すると
  # DISPLAY が無い状態で Playwright が落ちる
  sleep 15
  echo "xstock-vnc を起動した（${waited}秒待機）"
fi

# --- 収集（一時的なDNS/ネットワーク障害を想定し、失敗時は5分後に1回だけ再試行） ---
$COLLECT_CMD
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "収集失敗(rc=$rc)。5分待って1回だけ再試行..."
  sleep 300
  $COLLECT_CMD
  rc=$?
fi
if [ "$rc" -ne 0 ]; then
  echo "ERROR: 収集が2回失敗(rc=$rc)。アラート判定はスキップ" >&2
  exit "$rc"
fi

# --- アラート判定 + 新規行の macOS 通知 ---
before=0
[ -f "$ALERTS" ] && before=$(wc -l < "$ALERTS" | tr -d ' ')

$ALERT_CMD --date "$TARGET_DATE"
alert_rc=$?

after=0
[ -f "$ALERTS" ] && after=$(wc -l < "$ALERTS" | tr -d ' ')

if [ "$after" -gt "$before" ]; then
  new_count=$((after - before))
  head_txt=$(tail -n "$new_count" "$ALERTS" | head -1 \
    | /usr/bin/python3 -c 'import sys,json,re; d=json.loads(sys.stdin.readline()); s=str(d.get("query_id",""))+": "+str(d.get("count",""))+"件 (z="+str(d.get("z",""))+")"; print(re.sub(r"[\"\\\\]", "", s))' 2>/dev/null || echo "詳細は alerts jsonl")
  osascript -e "display notification \"${head_txt}\" with title \"📈 X値上がり検知: ${new_count}件\"" 2>/dev/null || true
fi

# --- 銘柄言及レーン（本文→銘柄名→zスコア→株価連動。2026-07-30 追加） ---
# ユーザー完了条件「リスト外の取引高TOP500銘柄で、品薄・値上がりの言及を拾い、株価が
# 連動して上がっている」の自動判定。件数レーンの成否とは独立の fail-soft（rc に混ぜない）。
MENTION_CMD=${MENTION_CMD:-"/usr/bin/python3 $INFLUX/scripts/x_mention_extract.py"}
GOAL_LOG="$INFLUX/data/x_price_watch/mention_alerts.jsonl"
goal_before=0
[ -f "$GOAL_LOG" ] && goal_before=$(wc -l < "$GOAL_LOG" | tr -d ' ')
if $MENTION_CMD --date "$TARGET_DATE"; then
  goal_after=0
  [ -f "$GOAL_LOG" ] && goal_after=$(wc -l < "$GOAL_LOG" | tr -d ' ')
  if [ "$goal_after" -gt "$goal_before" ]; then
    hit=$(tail -n $((goal_after - goal_before)) "$GOAL_LOG" \
      | /usr/bin/python3 -c 'import sys,json
rows=[json.loads(l) for l in sys.stdin if l.strip()]
g=[r for r in rows if r.get("goal_candidate")]
print(f"{len(g)}件が完了条件候補" if g else f"{len(rows)}件(リスト内/連動なし)")' 2>/dev/null || echo "詳細は mention_alerts.jsonl")
    osascript -e "display notification \"${hit}\" with title \"🎯 銘柄言及の急増を検知\"" 2>/dev/null || true
  fi
else
  echo "WARN: 銘柄言及レーンが失敗（件数レーンには影響なし）" >&2
fi

# 発火の固定窓評価（+5/+20営業日・冪等）。「株価が連動して上がっている」の再測定を毎晩自動化。
# 商品レーンの前向き記録(price_watch_forward)には混ぜない＝検定の分母を汚さない（2026-07-31）
$MENTION_CMD --eval || echo "WARN: 言及評価が失敗（翌晩に再試行される・冪等）" >&2
exit "$alert_rc"
