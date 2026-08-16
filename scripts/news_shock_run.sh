#!/bin/bash
# news_shock レーンのランナー（launchd com.influx.news-shock から毎日 07:20 / 19:00 に呼ばれる）
# 2026-08-16 新設（オーナー裁定=作る・入場条件= docs/price-watch-universe.md §16u）。
# Docker 不要（収集は標準ライブラリのみ・ホスト /usr/bin/python3 で完結）。
# 流れ: 収集→判定→発火があれば collector 自身が通知。ここでは「失敗の通知」だけを担う（家訓: 成功でなく失敗を通知）。
# 手動実行も同じ経路で: bash scripts/news_shock_run.sh
set -u

INFLUX="$HOME/Desktop/biz/influx"
OUT=$(mktemp -t news_shock_run)
cleanup() { rm -f "$OUT"; }
trap cleanup EXIT

# ログ肥大ガード（launchd の追記ログはローテーションが無いため・5MB超で末尾1MBだけ残す）
for L in "$HOME/.claude/state/news-shock.out.log" "$HOME/.claude/state/news-shock.err.log"; do
  if [ -f "$L" ] && [ "$(stat -f%z "$L" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -c 1048576 "$L" > "$L.tmp" && mv "$L.tmp" "$L"
  fi
done

if /usr/bin/python3 "$INFLUX/scripts/news_shock_collect.py" >"$OUT" 2>&1; then
  tail -5 "$OUT"
else
  rc=$?
  tail -10 "$OUT" >&2
  osascript -e 'display notification "収集が失敗（詳細は news-shock ログ）" with title "⚠️ news_shock 失敗"' 2>/dev/null \
    || echo "WARN: 失敗通知も送れず" >&2
  exit "$rc"
fi

# 固定窓評価（+5/+20営業日・冪等）。失敗しても収集の成否には混ぜないが、沈黙もさせない
# （fail-soft＋失敗通知・翌run で再試行）
if ! /usr/bin/python3 "$INFLUX/scripts/news_shock_eval.py"; then
  echo "WARN: 評価が失敗（冪等・次回再試行）" >&2
  osascript -e 'display notification "評価が失敗（次回再試行・詳細は news-shock ログ）" with title "⚠️ news_shock 評価失敗"' 2>/dev/null || true
fi
