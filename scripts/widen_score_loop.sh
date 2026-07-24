#!/bin/bash
# 拡張バッチの収集完了アカウントを逐次自動採点し、頑健性ゲート結果をTSVへ追記する
# （コンテナ内 detached 実行・無人運用）。widen_collect_batch.sh と対で使う。
set -u
LOG=/app/output/recollect_logs/widen.log
SCORELOG=/app/output/recollect_logs/score.log
RESULTS=/app/output/influencer_candidates/widen_results.tsv
ACCOUNTS="kazzn_blog noatake1127 tkmr_kato drdebuneko u___a___53 kabumoto_kabu purazumakoi gihuboy"
[ -f "$RESULTS" ] || printf "account\tn_scored\tEV\tex_outlier_EV\tclose20\tgate\n" > "$RESULTS"
DONE_SET=" "
for _ in $(seq 1 240); do   # 最大 240*60s = 4h
  ALL=1
  for acc in $ACCOUNTS; do
    case "$DONE_SET" in *" $acc "*) continue;; esac
    j="/app/data/influencer_candidates/recollect/$acc.json"
    if grep -q "done @$acc" "$LOG" 2>/dev/null && [ -f "$j" ]; then
      PYTHONPYCACHEPREFIX=/tmp/pc python3 /app/scripts/influencer_candidate_score.py \
        --input "$j" --output-dir "/app/output/influencer_candidates/recollect_$acc" >> "$SCORELOG" 2>&1
      PYTHONPYCACHEPREFIX=/tmp/pc python3 - "$acc" >> "$RESULTS" 2>> "$SCORELOG" <<'PY'
import sys, csv
from collections import defaultdict
acc = sys.argv[1]
p = f"/app/output/influencer_candidates/recollect_{acc}/mentions.csv"
try:
    rows = [r for r in csv.DictReader(open(p)) if r['status'] == 'scored']
except Exception as e:
    print(f"{acc}\tERR\t-\t-\t-\t{e}"); sys.exit()
if not rows:
    print(f"{acc}\t0\t-\t-\t-\tNO_SCORE"); sys.exit()
f = lambda r: float(r['net_return']); n = len(rows)
ev = sum(f(r) for r in rows) / n
close = sum(1 for r in rows if r['close_20pct'] in ('True', 'true', '1'))
byc = defaultdict(list)
for r in rows: byc[r['code']].append(f(r))
c = {k: sum(v) for k, v in byc.items()}; w = max(c, key=lambda k: c[k])
ex = [f(r) for r in rows if r['code'] != w]; exev = sum(ex) / len(ex)
gate = "PASS" if exev > 0 else "FAIL"
print(f"{acc}\t{n}\t{ev*100:+.1f}%\t{exev*100:+.1f}%\t{close}/{n}={close/n*100:.0f}%\t{gate}")
PY
      DONE_SET="$DONE_SET$acc "
    else
      ALL=0
    fi
  done
  if grep -q "widen batch DONE" "$LOG" 2>/dev/null && [ "$ALL" = "1" ]; then break; fi
  if ! pgrep -f widen_collect_batch >/dev/null 2>&1 && [ "$ALL" = "1" ]; then break; fi
  sleep 60
done
printf "SCORE_LOOP_DONE\n" >> "$RESULTS"
