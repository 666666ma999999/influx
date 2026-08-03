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

# rc!=0 の主経路は「全ハンドル取得0件」（x_watchlist_tracer.py main() の明示 return 1）。
# ただし Cookie失効・config破損・playwright起動失敗などの未捕捉例外でも rc!=0 になる。
# その場合の再試行は決定論的に再失敗する無駄打ちだが、有界(1回・+5分)なので許容（2026-07-26 注記修正）。
# 一時的なDNS/ネットワーク障害を想定し、5分待って1回だけ再試行してから失敗確定
# （2026-07-25 実測: 全13ハンドル ERR_NAME_NOT_RESOLVED で rows=0・exit 1）
if [ "$rc" -ne 0 ]; then
  echo "全ハンドル取得失敗(rc=$rc)。5分待って1回だけ再試行..."
  sleep 300
  $TRACER_CMD
  rc=$?
fi

# 2026-08-03 追加（敵対レビュー2件一致）: tracer 出力を vault へ複製し、週次提案カードの
# 入力に載せる。tracer は日次2-3回・週272件・grok コーパスとの重複0.7%でほぼ全部が新規。
# 手口は xbuzz_collect_run.sh 末尾の cp と同一（runner の extra_dir が単一のため vault 経由が必要）。
TRACER_SRC="$HOME/Desktop/biz/influx/output/x_tracer/tracer-$(date +%Y-%m-%d).jsonl"
VAULT_RAW="$HOME/Documents/Obsidian Vault/.raw"
[ -s "$TRACER_SRC" ] && cp "$TRACER_SRC" "$VAULT_RAW/x-tracer-$(date +%Y-%m-%d).jsonl" 2>/dev/null || true

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
