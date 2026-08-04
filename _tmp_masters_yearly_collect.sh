#!/bin/bash
# テクニカル名人5アカウント 過去1年 四半期4窓 遡及収集（2026-07-17・使い捨て）
set -uo pipefail
cd /Users/masaaki_nagasawa/Desktop/biz/influx

DEST="output/research/masters_20260717"
mkdir -p "$DEST"
CAND="output/research/candidates_masters_20260717.json"
ACCOUNTS="noatake1127 Drdebuneko Biz_zatukora kakatothecat tomoyaasakura"

# 窓: 新しい順（recall が高い順に確実に確保）
WINDOWS=(
  "2026-04-17 2026-07-18"
  "2026-01-17 2026-04-17"
  "2025-10-17 2026-01-17"
  "2025-07-17 2025-10-17"
)

for w in "${WINDOWS[@]}"; do
  SINCE=$(echo $w | cut -d' ' -f1)
  UNTIL=$(echo $w | cut -d' ' -f2)
  TAG="${SINCE}_${UNTIL}"
  echo "===== 窓 $TAG 開始 $(date '+%H:%M:%S') ====="
  docker exec -e DISPLAY=:99 xstock-vnc python scripts/research_influencers.py \
    --phase collect \
    --candidates "$CAND" \
    --screening "$CAND" \
    --max-collect 5 --scrolls 30 \
    --since "$SINCE" --until "$UNTIL" 2>&1 | grep -E "^---|収集完了|エラー|ツイートなし|ログイン"
  # 窓ごとに退避（次窓で上書きされるため）
  for u in $ACCOUNTS; do
    if [ -f "output/research/tweets_${u}.json" ]; then
      mv "output/research/tweets_${u}.json" "$DEST/tweets_${u}__${TAG}.json"
    fi
  done
  echo "===== 窓 $TAG 完了 ====="
  sleep 60
done

# 元の3ファイルを復元（研究パイプラインの既存成果物を保全）
cp output/research/backup-20260717/*.json output/research/ 2>/dev/null

echo "===== 全窓完了。収集サマリー ====="
python3 - <<'EOF'
import json, glob, os
files = sorted(glob.glob('output/research/masters_20260717/tweets_*.json'))
total = 0
for f in files:
    n = len(json.load(open(f)))
    total += n
    print(f"{os.path.basename(f)}: {n}件")
print(f"合計: {total}件 / {len(files)}ファイル")
EOF
