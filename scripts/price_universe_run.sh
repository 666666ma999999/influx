#!/bin/bash
# B2B価格週次チェッカーのランナー（launchd com.influx.price-universe から毎週月曜 08:30 に呼ばれる）
# 2026-07-28 新設（P0-③）。xprice_watch_run.sh（日次X版）の週次B2B版で、構成を意図的に揃えている。
# 流れ: Docker daemon 待機 → チェッカー実行 → 発火 or 異常があれば macOS 通知。
# 手動実行も同じ経路で: bash scripts/price_universe_run.sh
set -u

MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}
INTERVAL_SEC=${INTERVAL_SEC:-30}
INFLUX="$HOME/Desktop/biz/influx"
OUT=$(mktemp -t price_universe_run)
# 系列の健全性しきい値: 52系列中これを下回る ok 数ならソース側の異常として通知する
# （2026-08-02 30カテゴリ拡張で 34→52系列。週次の stale 1〜2件＋単発失敗を許容しつつ
#   TE一覧レイアウト変更のような広域破損は確実に鳴る水準）
MIN_OK=${MIN_OK:-42}

cleanup() { rm -f "$OUT"; }
trap cleanup EXIT

# --- Docker daemon 待機（共通部品・2026-08-29 に手書きから移行） ---
# ここは `docker compose run --rm xstock` で使い捨てコンテナを立てるため、
# ブラウザ用の常設コンテナ xstock-vnc は不要＝ ensure_ready ではなく **daemon 待機だけ** を呼ぶ
# （ensure_ready を呼ぶと不要な xstock-vnc を起こしてしまう。実測 1.79GiB）。
# ⚠️ ここに待機処理を手書きしないこと。
if [ -z "${XSTOCK_SKIP_ENSURE:-}" ]; then
  . "$(dirname "$0")/lib/xstock_vnc.sh"
  XSTOCK_NOTIFY_TITLE="⚠️ B2B価格チェッカー失敗"
  XSTOCK_DAEMON_WAIT="$MAX_WAIT_SEC"
  XSTOCK_DAEMON_INTERVAL="$INTERVAL_SEC"
  XSTOCK_INFLUX="$INFLUX"
  xstock_wait_daemon || exit 1
fi

cd "$INFLUX" || exit 1
docker compose run --rm xstock python scripts/price_universe_check.py 2>&1 | tee "$OUT"
rc=${PIPESTATUS[0]}

# --- 仕込み候補リストについて（2026-08-16 一本化・ここでは生成しない） ---
# ユーザー指摘「同じ役割なら1ファイルにまとめて」により、仕込み型は
# **output/daily_reco.md の「🌱 仕込み型」節に合流**した（毎朝 daily_screen 経由で再生成）。
# 週次でも別ファイルを作ると更新時刻差で内容が食い違うため、ここでの生成は廃止する
# （Codex 指摘: 週次別ファイルの残存は統合の趣旨に反し不整合の火種）。
# 本ジョブが更新した forward_log は、翌営業日の朝ジョブが自動で拾う。

# --- ヘルス判定と通知 ---
# 「N/M ok」行を実出力から拾う（取れなければ健全性判定は行わない＝黙って正常扱いにしない）
ok_line=$(grep -oE '\[done\] [0-9]+/[0-9]+ ok' "$OUT" | tail -1)
n_ok=$(echo "$ok_line" | grep -oE '[0-9]+' | head -1)
n_all=$(echo "$ok_line" | grep -oE '[0-9]+' | sed -n 2p)
# 発火件数はチェッカーの集計行「🚨 閾値超え N 系列」から採る（本文行の grep -c は
# 無一致時に「0」と `|| echo 0` の両方が出て "0\n0" になり数値比較が落ちる・初回実走で実測）
n_alert=$(grep -oE '閾値超え [0-9]+ 系列' "$OUT" | grep -oE '[0-9]+' | head -1)
n_alert=${n_alert:-0}

if [ "$rc" -ne 0 ]; then
  osascript -e "display notification \"チェッカーが異常終了 (rc=$rc)\" with title \"⚠️ B2B価格チェッカー失敗\"" 2>/dev/null || true
elif [ -z "$n_ok" ]; then
  osascript -e 'display notification "実行はしたが集計行を検出できず（出力形式の変化を疑う）" with title "⚠️ B2B価格チェッカー要確認"' 2>/dev/null || true
elif [ "$n_ok" -lt "$MIN_OK" ]; then
  osascript -e "display notification \"取得成功が ${n_ok}/${n_all} 系列（下限${MIN_OK}）。ソース側の変化を疑う\" with title \"⚠️ B2B価格 取得低下\"" 2>/dev/null || true
fi

if [ "$n_alert" -gt 0 ]; then
  head_txt=$(grep -E '^  .*（.*）→ 受益' "$OUT" | head -1 | tr -d '"\\' | cut -c1-100)
  osascript -e "display notification \"${head_txt}\" with title \"📈 商品価格の閾値超え ${n_alert}系列\"" 2>/dev/null || true
fi

exit "$rc"
