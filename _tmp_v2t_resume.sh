#!/bin/bash
# batch_v2t 本番再開: シグナル生成コンテナ完了待ち→screen+confirm実行（使い捨て・2026-07-17）
set -uo pipefail
cd /Users/masaaki_nagasawa/Desktop/biz/influx

CONT="influx-xstock-run-937d5e184880"
CELLS_DIR="output/kpi_screening/batch_v2t/cells"

echo "[$(date '+%H:%M:%S')] シグナル生成コンテナ($CONT)の完了待ち開始"
docker wait "$CONT" 2>/dev/null || echo "コンテナは既に終了済みか不存在"
echo "[$(date '+%H:%M:%S')] コンテナ終了検知"

# 4セルCSVの実在確認（最大10分待つ: 書き込みフラッシュ猶予）
for i in $(seq 1 20); do
  n=$(ls "$CELLS_DIR"/v2t-F03-0*.csv 2>/dev/null | wc -l | tr -d ' ')
  if [ "$n" = "4" ]; then break; fi
  sleep 30
done
n=$(ls "$CELLS_DIR"/v2t-F03-0*.csv 2>/dev/null | wc -l | tr -d ' ')
if [ "$n" != "4" ]; then
  echo "FATAL: セルCSVが4本揃っていない (n=$n)。screen実行を中止"
  touch _tmp_v2t_failed.marker
  exit 2
fi
echo "[$(date '+%H:%M:%S')] 4セルCSV確認OK。screen+confirm開始"

docker compose run --rm xstock python3 scripts/kpi_screen_batch.py \
  --grid config/screening_grid_v2t.json --batch-id batch_v2t --phase both
rc=$?
echo "[$(date '+%H:%M:%S')] screen+confirm exit=$rc"
if [ "$rc" = "0" ]; then
  touch _tmp_v2t_done.marker
else
  touch _tmp_v2t_failed.marker
fi
exit $rc
