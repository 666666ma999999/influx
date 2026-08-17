#!/bin/bash
# R2「時計」対照レーンのランナー（launchd com.influx.news-shock-probe から2時間毎に呼ばれる）
# 2026-08-17 新設（第3R一致#6: 本線07:20/19:00より早く拾えるかを無料の高頻度ポーリングで実測）。
# 指標の正本= tasks/news_shock_preregister.md §7。失敗のみ通知（家訓）・通知やたら鳴らさない。
set -u

INFLUX="$HOME/Desktop/biz/influx"
OUT=$(mktemp -t news_shock_probe)
cleanup() { rm -f "$OUT"; }
trap cleanup EXIT

for L in "$HOME/.claude/state/news-shock-probe.out.log" "$HOME/.claude/state/news-shock-probe.err.log"; do
  if [ -f "$L" ] && [ "$(stat -f%z "$L" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -c 1048576 "$L" > "$L.tmp" && mv "$L.tmp" "$L"
  fi
done

if /usr/bin/python3 "$INFLUX/scripts/news_shock_collect.py" --first-seen-probe >"$OUT" 2>&1; then
  tail -2 "$OUT"
else
  rc=$?
  tail -10 "$OUT" >&2
  osascript -e 'display notification "probe収集が失敗（詳細は news-shock-probe ログ）" with title "⚠️ news_shock probe 失敗"' 2>/dev/null \
    || echo "WARN: 失敗通知も送れず" >&2
  exit "$rc"
fi
