#!/bin/bash
# 共通部品: ブラウザ用コンテナ xstock-vnc を「使える状態」にしてから返す。
#
# 使い方（呼び出し側は bash・source して関数を呼ぶ）:
#     . "$(dirname "$0")/lib/xstock_vnc.sh"
#     XSTOCK_NOTIFY_TITLE="x-buzz トレーサー"
#     xstock_ensure_ready || exit 1
#     # 呼び出し後 $XSTOCK_STARTED が 1 なら、この実行でコンテナを起こした
#
# 背景（2026-08-11 新設）:
#   同じ「daemon を待つ → コンテナが無ければ起こす → 暖機する」処理が
#   xbuzz_collect_run.sh と xprice_watch_run.sh に**別々に手書き**されていて、
#   xbuzz_tracer_run.sh にだけ**無かった**。そのため 2026-08-11 17:08 に
#   コンテナが落ちたとき、収集と価格監視は自力で復旧できたのに tracer だけが
#   `No such container: xstock-vnc` で失敗して止まった（見張り先の巡回が停止）。
#   ~/.claude/rules/20-code-quality.md の Dual-Path 禁止に従い、ここに集約する。
#   **この処理を新しく手書きしないこと**（増やすとまた片側だけ直る事故になる）。
#
# 常駐化（compose の restart: unless-stopped）を選ばない理由:
#   実測でメモリ 1.79GiB を常時占有する。1日数分しか使わないプロセスに
#   常時2GBは割に合わない（xprice_watch_run.sh 2026-07-29 の判断を踏襲）。

XSTOCK_CONTAINER=${XSTOCK_CONTAINER:-xstock-vnc}
XSTOCK_INFLUX=${XSTOCK_INFLUX:-$HOME/Desktop/biz/influx}
XSTOCK_COMPOSE_CMD=${XSTOCK_COMPOSE_CMD:-"docker compose -f docker-compose.vnc.yml up -d"}
XSTOCK_DAEMON_WAIT=${XSTOCK_DAEMON_WAIT:-300}      # Docker daemon 起動待ちの上限（秒）
XSTOCK_DAEMON_INTERVAL=${XSTOCK_DAEMON_INTERVAL:-30}
XSTOCK_UP_WAIT=${XSTOCK_UP_WAIT:-120}              # コンテナ起動待ちの上限（秒）
XSTOCK_WARMUP=${XSTOCK_WARMUP:-15}                 # 起こした直後の暖機（秒）
XSTOCK_DISPLAY_WAIT=${XSTOCK_DISPLAY_WAIT:-60}     # Xvfb(:99) のソケット待ちの上限（秒）
XSTOCK_NOTIFY_TITLE=${XSTOCK_NOTIFY_TITLE:-x-buzz}

# 呼び出し後に参照する: 1 = この実行でコンテナを起こした（0 = 元から動いていた）
XSTOCK_STARTED=0

# 待機値の健全性チェック（2026-08-29 追加・Codex レビュー指摘）。
# XSTOCK_DAEMON_INTERVAL=0 を渡すと待機ループのカウンタが増えず**無限ループ**になる。
# 共通部品なので設定事故が5本の runner へ波及する＝ここで弾く。
xstock_validate_waits() {
  local name value
  # 10進で正規化して検証する。先頭ゼロ（08/09）は算術展開で八進として扱われ
  # `value too great for base` になるため、ここで弾く（2026-08-29 実測で再現）。
  # 上限を置くのは、桁の打ち間違いで「事実上止まらない待機」になるのを防ぐため。
  for name in XSTOCK_DAEMON_WAIT:3600 XSTOCK_DAEMON_INTERVAL:300 \
              XSTOCK_UP_WAIT:3600 XSTOCK_DISPLAY_WAIT:3600 XSTOCK_WARMUP:600; do
    eval "value=\${${name%%:*}}"
    case "$value" in
      ''|*[!0-9]*|0[0-9]*)
        echo "ERROR: ${name%%:*} は0〜${name##*:}の10進整数（先頭ゼロ不可・現在: '${value}'）" >&2
        return 1 ;;
    esac
    if [ "$value" -gt "${name##*:}" ]; then
      echo "ERROR: ${name%%:*} が上限 ${name##*:} を超えている（現在: '${value}'）" >&2
      return 1
    fi
  done
  # interval だけは 0 だと待機カウンタが増えず無限ループになるため 1 以上
  if [ "$XSTOCK_DAEMON_INTERVAL" -lt 1 ]; then
    echo "ERROR: XSTOCK_DAEMON_INTERVAL は1以上（現在: '${XSTOCK_DAEMON_INTERVAL}'）" >&2
    return 1
  fi
  return 0
}

# 通知（AppleScript のリテラルに壊れる文字を落としてから埋め込む。素で入れると
# `"` や `\` で構文エラーになり、`|| true` のせいで**黙って消える**・2026-08-29 Codex 指摘）
xstock_notify() {
  local msg title
  msg=$(printf '%s' "$1" | tr -d '"\\')
  title=$(printf '%s' "${XSTOCK_NOTIFY_TITLE}" | tr -d '"\\')
  osascript -e "display notification \"${msg}\" with title \"${title}\"" 2>/dev/null || true
}

xstock_container_up() {
  docker ps --format '{{.Names}}' | grep -q "^${XSTOCK_CONTAINER}\$"
}

# Xvfb の UNIX ソケット（:99）が出来るまで待つ。成功 0 / タイムアウト 1。
xstock_wait_display() {
  local waited=0
  until docker exec "$XSTOCK_CONTAINER" test -S /tmp/.X11-unix/X99 2>/dev/null; do
    if [ "$waited" -ge "$XSTOCK_DISPLAY_WAIT" ]; then
      echo "ERROR: ${XSTOCK_CONTAINER} の DISPLAY=:99 が${XSTOCK_DISPLAY_WAIT}秒待っても準備できない" >&2
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 0
}

# Docker daemon だけを待つ（コンテナは起こさない）。
# `docker compose run --rm` のようにブラウザ用コンテナを必要としない呼び出し側はこちらを使う
# ＝ ensure_ready を呼ぶと不要な xstock-vnc（実測 1.79GiB）まで起こしてしまうため。
# 成功 0 / 失敗 1（呼び出し側で exit する）。失敗時は必ず通知を出す。
xstock_wait_daemon() {
  xstock_validate_waits || { xstock_notify "待機設定が不正で実行できませんでした"; return 1; }

  if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon 未起動 → Docker Desktop をバックグラウンド起動 (open -ga Docker)"
    open -ga Docker 2>/dev/null || true
  fi

  local waited=0
  until docker info >/dev/null 2>&1; do
    if [ "$waited" -ge "$XSTOCK_DAEMON_WAIT" ]; then
      echo "ERROR: Docker daemon起動待ちタイムアウト(${XSTOCK_DAEMON_WAIT}秒経過)" >&2
      xstock_notify "Docker が起動せず実行できませんでした"
      return 1
    fi
    echo "Docker daemon起動待ち... (${waited}/${XSTOCK_DAEMON_WAIT}秒)"
    sleep "$XSTOCK_DAEMON_INTERVAL"
    waited=$((waited + XSTOCK_DAEMON_INTERVAL))
  done
  return 0
}

# 成功 0 / 失敗 1（呼び出し側で exit する）。失敗時は必ず通知を出す
# ＝「止まっても気づかない」を作らない（改善レーン G4）。
xstock_ensure_ready() {
  XSTOCK_STARTED=0

  xstock_wait_daemon || return 1

  local waited=0
  if xstock_container_up; then
    # 名前が docker ps に出ていても、Xvfb(:99) がまだ上がっていない場合がある
    # （別ジョブが起こした直後に相乗りするとこの窓に入る）。ソケットの実在まで確認する
    # ＝ us_watchlist_launchd.sh が以前から使っている判定と同じ（2026-08-29 に共通化）。
    xstock_wait_display && return 0
    # ここで compose up をやり直さない（2026-08-29 Codex 2巡目の指摘）。
    # 設定に差分があるとコンテナが作り直され、同じコンテナを使っている**別ジョブを巻き込んで落とす**。
    # 差分が無ければ no-op で Xvfb の固着は直らず、待ち時間だけ伸びる。どちらも損なので、
    # 「稼働中なのに DISPLAY が来ない」は人が見るべき異常として通知して失敗させる。
    echo "ERROR: ${XSTOCK_CONTAINER} は稼働中だが DISPLAY=:99 が準備できない（別ジョブが起動中か Xvfb の固着）" >&2
    xstock_notify "${XSTOCK_CONTAINER} は稼働中だが DISPLAY=:99 が準備できませんでした"
    return 1
  fi

  echo "${XSTOCK_CONTAINER} 停止 → ${XSTOCK_COMPOSE_CMD} を試行"
  # shellcheck disable=SC2086  # 単語分割は意図（テストで COMPOSE_CMD を差し替えるため）
  ( cd "$XSTOCK_INFLUX" && $XSTOCK_COMPOSE_CMD ) || true

  waited=0
  until xstock_container_up; do
    if [ "$waited" -ge "$XSTOCK_UP_WAIT" ]; then
      echo "ERROR: ${XSTOCK_CONTAINER} を${XSTOCK_UP_WAIT}秒待っても起動できなかった" >&2
      xstock_notify "${XSTOCK_CONTAINER} を起動できず失敗しました"
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
  done

  XSTOCK_STARTED=1
  # Xvfb(:99)/supervisord の立ち上がり待ち。ここを待たずに docker exec すると
  # DISPLAY 無しで Playwright が落ちる（TargetClosedError・2026-08-03 実測）
  sleep "$XSTOCK_WARMUP"
  xstock_wait_display || { xstock_notify "${XSTOCK_CONTAINER} の DISPLAY=:99 が準備できませんでした"; return 1; }
  echo "${XSTOCK_CONTAINER} を起動した（起動待ち${waited}秒 + 暖機${XSTOCK_WARMUP}秒）"
  return 0
}
