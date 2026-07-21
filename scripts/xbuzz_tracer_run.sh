#!/bin/bash
# x-buzz ライブトレーサー実行ラッパー（launchd から呼ばれる・2026-07-21 裁定1+7）
# 役割: コンテナ内トレーサーを実行し、この回で増えたアラート（official_new/rising）が
#       あれば macOS 通知を出す（通知まで。書くかは毎回人間の判断＝裁定7）。
set -u
ALERTS="${ALERTS_FILE:-$HOME/Desktop/biz/influx/output/x_tracer/alerts-$(date +%Y-%m-%d).jsonl}"
TRACER_CMD=${TRACER_CMD:-"docker exec xstock-vnc python3 /app/scripts/x_watchlist_tracer.py"}

before=0
[ -f "$ALERTS" ] && before=$(wc -l < "$ALERTS" | tr -d ' ')

$TRACER_CMD
rc=$?

after=0
[ -f "$ALERTS" ] && after=$(wc -l < "$ALERTS" | tr -d ' ')

if [ "$after" -gt "$before" ]; then
  new_lines=$(tail -n $((after - before)) "$ALERTS")
  official=$(printf '%s\n' "$new_lines" | grep -c '"official_new"')
  rising=$(printf '%s\n' "$new_lines" | grep -c '"rising"')
  # 通知本文はクォート事故防止のため英数と日本語のみに掃除
  head_txt=$(printf '%s\n' "$new_lines" | head -1 \
    | /usr/bin/python3 -c 'import sys,json,re; d=json.loads(sys.stdin.readline()); s="@"+str(d.get("handle",""))+": "+str(d.get("text_head",""))[:60]; print(re.sub(r"[\"\\]", "", s))' 2>/dev/null || echo "詳細は alerts jsonl")
  osascript -e "display notification \"${head_txt}\" with title \"x-buzz 検知: 公式${official}件 / 急上昇${rising}件\"" 2>/dev/null || true
fi
exit $rc
