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
# 系列の健全性しきい値: 42系列中これを下回る ok 数ならソース側の異常として通知する
# （2026-08-02 34→42系列化に追随して 20→34 へ。週次の stale 1〜2件＋単発失敗を許容しつつ
#   TE一覧レイアウト変更のような広域破損は確実に鳴る水準）
MIN_OK=${MIN_OK:-34}

cleanup() { rm -f "$OUT"; }
trap cleanup EXIT

# --- Docker daemon 待機（未起動ならバックグラウンド起動して最大5分待つ） ---
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon 未起動 → Docker Desktop をバックグラウンド起動 (open -ga Docker)"
  open -ga Docker 2>/dev/null || true
fi
waited=0
until docker info >/dev/null 2>&1; do
  if [ "$waited" -ge "$MAX_WAIT_SEC" ]; then
    echo "ERROR: Docker daemon起動待ちタイムアウト(${MAX_WAIT_SEC}秒経過)" >&2
    osascript -e 'display notification "Docker起動待ちタイムアウト" with title "⚠️ B2B価格チェッカー失敗"' 2>/dev/null || true
    exit 1
  fi
  echo "Docker daemon起動待ち... (${waited}/${MAX_WAIT_SEC}秒)"
  sleep "$INTERVAL_SEC"
  waited=$((waited + INTERVAL_SEC))
done

cd "$INFLUX" || exit 1
docker compose run --rm xstock python scripts/price_universe_check.py 2>&1 | tee "$OUT"
rc=${PIPESTATUS[0]}

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
