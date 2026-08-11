#!/bin/bash
# x-buzz 週次収集ラッパー（launchd com.masa.xbuzz-buzz-collect から呼ぶ想定・2026-07-26 新設）
# 背景: 2026-07-20 20:30 実行が「failed to connect to the docker API ... daemon is running」で
#       失敗し、最新出力が grok-twittora-2026-07-19.jsonl のまま5日間停止した（Docker Desktop未起動）。
# 対策: docker info が通るまで最大5分・30秒間隔で待機リトライしてから本処理を実行。
#       タイムアウト時は明確なエラーで exit 1（launchd の err.log に残る）。
set -u

MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}

# 2026-08-03 追加: 失敗が err.log にしか残らず8日間気づかれなかったため通知を出す
notify() { osascript -e "display notification \"$1\" with title \"X収集 週次\"" 2>/dev/null || true; }

# 2026-08-11: ここにあった「daemon待ち → コンテナが無ければ起こす」の手書き実装を
# scripts/lib/xstock_vnc.sh へ集約した（挙動は同じ）。理由は同処理が3本のシェルに
# 個別実装され、tracer にだけ無かったため 08-11 17:08 に tracer だけが復旧できず
# 止まったこと（rules/20 Dual-Path 禁止）。⚠️ ここに書き戻さないこと。
#   旧実装との差: コンテナ起動待ちの上限が「12回×5秒=60秒・超過しても続行して再判定」から
#   「120秒・超過で即エラー」に変わった（xprice_watch_run.sh 側の実装に揃えた）。
#   起動できなかった時に通知して exit 1 する結末は従来どおり。
. "$(dirname "$0")/lib/xstock_vnc.sh"
XSTOCK_NOTIFY_TITLE="X収集 週次"
XSTOCK_DAEMON_WAIT="$MAX_WAIT_SEC"
XSTOCK_DAEMON_INTERVAL="$INTERVAL_SEC"
xstock_ensure_ready || exit 1

# 2026-08-03 22時台の実測: コンテナを自動起動した直後の docker exec は
# TargetClosedError（ブラウザ即死）で落ちる（20:30 実走 rows:0 crash・fxnia 11:04 と同型）。
# Xvfb/環境の温まり待ち＋失敗時1回だけ再試行（tracer の1リトライ前例と同型・有界）。
sleep 20
# 2026-08-11 `--live` 追加（最新タブの併走）。人気順(f=top)だけだと X のランキングが
# バズらない投稿を落とし、いいね下限を0にしても低いいね帯が入ってこなかった（41件中2件=5%）。
# 試運転の実測: 壁エラー0・盲検で live 18/45(40%) vs top 12/38(32%)（劣化しない）・
# 使える記事が 12→30件(2.5倍)。所要は 49タスク36分 → 80タスク約60分の見込み。
# ⚠️ 失敗時は下の再試行で**全体をもう一度**引くため、壁が出ると検索回数が倍になる。
# 初回実行後は wall_errors と所要を必ず確認すること。戻すときはこの2行から --live を消すだけ。
# 経緯の正本= make_article docs/x-operation/research/grok-web-search-precision-2026-08-11.md
docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_search_collect_twittora.py --days 7 --live
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "収集 exit $rc → 60秒待って1回だけ再試行（ブラウザ即死の一過性対策）"
  sleep 60
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_search_collect_twittora.py --days 7 --live
  rc=$?
fi
[ "$rc" -ne 0 ] && notify "X収集 失敗: 収集スクリプトが exit $rc（再試行込み）"
TODAY=$(date +%Y-%m-%d)
VAULT_RAW="$HOME/Documents/Obsidian Vault/.raw"
SRC="$HOME/Desktop/biz/influx/output/grok_twittora/grok-twittora-$TODAY.jsonl"
DEST="$VAULT_RAW/grok-twittora-$TODAY.jsonl"
# 2026-07-26: vault側の原観測退避（同日再実行でのcp上書き消失対策）。
# 退避先は拡張子 .jsonl.bak（glob `grok-twittora-*.jsonl` にヒットしないため下流の二重読みなし）。
if [ -f "$DEST" ]; then
  STAMP=$(date +%H%M)
  mv "$DEST" "$VAULT_RAW/grok-twittora-$TODAY.pre-$STAMP.jsonl.bak" 2>/dev/null || true
fi
cp "$SRC" "$DEST" 2>/dev/null || true
exit $rc
