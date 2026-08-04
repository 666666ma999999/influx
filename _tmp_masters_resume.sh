#!/bin/bash
# 遡及収集の再開（窓2の退避待ち + 窓3・4実行）2026-07-17
set -uo pipefail
cd /Users/masaaki_nagasawa/Desktop/biz/influx

DEST="output/research/masters_20260717"
CAND="output/research/candidates_masters_20260717.json"
ACCOUNTS="noatake1127 Drdebuneko Biz_zatukora kakatothecat tomoyaasakura"

# --- 窓2（コンテナ内で実行中）の完了待ち ---
echo "窓2完了待ち開始 $(date '+%H:%M:%S')"
for i in $(seq 1 80); do
  if ! docker exec xstock-vnc ps aux | grep research_influencers | grep -v grep > /dev/null; then
    break
  fi
  sleep 30
done
echo "窓2プロセス終了確認 $(date '+%H:%M:%S')"

# 窓2成果物を退避
TAG="2026-01-17_2026-04-17"
for u in $ACCOUNTS; do
  if [ -f "output/research/tweets_${u}.json" ]; then
    mv "output/research/tweets_${u}.json" "$DEST/tweets_${u}__${TAG}.json"
  fi
done
echo "===== 窓 $TAG 退避完了 ====="
sleep 60

# --- 窓3・4 ---
WINDOWS=(
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
  for u in $ACCOUNTS; do
    if [ -f "output/research/tweets_${u}.json" ]; then
      mv "output/research/tweets_${u}.json" "$DEST/tweets_${u}__${TAG}.json"
    fi
  done
  echo "===== 窓 $TAG 完了 ====="
  sleep 60
done

# 元の3ファイルを復元
cp output/research/backup-20260717/*.json output/research/ 2>/dev/null

echo "===== 全窓完了。収集サマリー ====="
python3 - <<'EOF'
import json, glob, os
files = sorted(glob.glob('output/research/masters_20260717/tweets_*.json'))
total = 0
by_acct = {}
for f in files:
    n = len(json.load(open(f)))
    total += n
    acct = os.path.basename(f).split('__')[0].replace('tweets_','')
    by_acct[acct] = by_acct.get(acct, 0) + n
    print(f"{os.path.basename(f)}: {n}件")
print("--- アカウント別合計 ---")
for a, n in sorted(by_acct.items(), key=lambda x: -x[1]):
    print(f"{a}: {n}件")
print(f"総合計: {total}件 / {len(files)}ファイル")
EOF
