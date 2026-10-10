#!/bin/bash
# Wikipedia 閲覧数の週次見張りランナー（launchd com.influx.wiki-pv-watch から毎週火曜 09:30 に呼ばれる）
# 2026-10-10 新設（P-INF-43 b・事前登録= tasks/shortage_presign_forward.md §A）。
# Docker 不要（収集は標準ライブラリのみ・ホスト /usr/bin/python3 で完結）。
# 流れ: 取得→判定→台帳→発火があれば collector 自身が通知。ここでは「失敗の通知」だけを担う（家訓: 成功でなく失敗を通知）。
# 末尾で前向き採点 presign_forward_eval.py を fail-soft で回す（無ければ WARN のみ・採点失敗でも収集の成否には混ぜない）。
# 収集が失敗しても採点器は走らせ、最後に収集の rc で exit する（Codex 統合レビュー Warning・RSS 側の発火の採点を止めない）。
# 手動実行も同じ経路で: bash scripts/wiki_pv_watch_run.sh
# 疎通（台帳に書かない・通知も出ない）: PRESIGN_DRY_RUN=1 bash scripts/wiki_pv_watch_run.sh
set -u

INFLUX="$HOME/Desktop/biz/influx"
OUT=$(mktemp -t wiki_pv_watch_run)
cleanup() { rm -f "$OUT"; }
trap cleanup EXIT

# 疎通用: PRESIGN_DRY_RUN=1 なら収集器・採点器の両方に --dry-run を渡す（既定= 本番）
DRY=()
if [ "${PRESIGN_DRY_RUN:-0}" = "1" ]; then
  DRY=(--dry-run)
  echo "[runner] PRESIGN_DRY_RUN=1: 収集器・採点器とも --dry-run（台帳に書かない・通知なし）"
fi

# ログ肥大ガード（launchd の追記ログはローテーションが無いため・5MB超で末尾1MBだけ残す）
for L in "$HOME/.claude/state/wiki-pv-watch.out.log" "$HOME/.claude/state/wiki-pv-watch.err.log"; do
  if [ -f "$L" ] && [ "$(stat -f%z "$L" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -c 1048576 "$L" > "$L.tmp" && mv "$L.tmp" "$L"
  fi
done

# 1) 収集（失敗しても exit せず rc を保存→採点器へ進む）
rc=0
if /usr/bin/python3 "$INFLUX/scripts/wiki_pv_watch.py" ${DRY[@]+"${DRY[@]}"} >"$OUT" 2>&1; then
  tail -5 "$OUT"
else
  rc=$?
  tail -10 "$OUT" >&2
  if [ ${#DRY[@]} -eq 0 ]; then
    osascript -e 'display notification "収集が失敗（詳細は wiki-pv-watch ログ）" with title "⚠️ wiki_pv_watch 失敗"' 2>/dev/null \
      || echo "WARN: 失敗通知も送れず" >&2
  fi
  echo "WARN: 収集 rc=$rc（採点器は続行・最後にこの rc で exit）" >&2
fi

# 2) 前向き採点（60 日・冪等・週 1 はこの便の末尾で回す＝task §D）。fail-soft:
# スクリプトが無ければ WARN のみ・失敗しても収集の成否には混ぜないが沈黙もさせない（失敗通知・翌便で再試行）
EVAL="$INFLUX/scripts/presign_forward_eval.py"
if [ ! -f "$EVAL" ]; then
  echo "WARN: $EVAL が無いため採点をスキップ" >&2
elif ! /usr/bin/python3 "$EVAL" ${DRY[@]+"${DRY[@]}"}; then
  echo "WARN: 前向き採点が失敗（冪等・次回再試行）" >&2
  if [ ${#DRY[@]} -eq 0 ]; then
    osascript -e 'display notification "前向き採点が失敗（次回再試行・詳細は wiki-pv-watch ログ）" with title "⚠️ presign 採点失敗"' 2>/dev/null || true
  fi
fi

exit "$rc"
