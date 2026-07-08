#!/bin/bash
# 週次インフルエンサー勝率リサーチ（凍結34アカウント・回収案R3）launchdラッパー。
#
# launchdはdirenvフックを持たないため .envrc は使えない。ANTHROPIC_API_KEY /
# XAI_API_KEY / COOKIE_ENCRYPTION_KEY は環境変数優先、無ければ ~/.zshrc の
# export 行から取得する（scripts/jq_fetch.py の get_api_key() と同一パターン）。
#
# 実機検証済み(2026-07-08): `docker compose -f docker-compose.vnc.yml up -d`
# はプロジェクトルートの .env からXAI_API_KEY/COOKIE_ENCRYPTION_KEYを自動解決
# する（本ラッパーの呼び出し元シェルにこれらが無くてもコンテナ内には入る）。
# 一方 ANTHROPIC_API_KEY は .env に無いため必ず本ラッパー側での解決が必要
# （FATAL対象は ANTHROPIC_API_KEY のみ。他2つは無くても続行を試みる）。
#
# docker-compose.vnc.yml の xstock-vnc サービスは headless=False な
# Playwrightブラウザ用にXvfb(仮想ディスプレイ)をsupervisord経由で必要とする。
# `docker compose run` はCMD(supervisord)を上書きしてしまいXvfbが起動しない
# ため、`up -d` でsupervisord/Xvfbを起動した同一コンテナに対して
# `docker exec` で実行する（tasks/research_pipeline.md 記載のCookie再取得
# 手順と同型）。
set -uo pipefail

PROJECT_ROOT="/Users/masaaki_nagasawa/Desktop/biz/influx"
cd "$PROJECT_ROOT" || exit 1

load_key_from_zshrc() {
    local var_name="$1"
    if [ -n "${!var_name:-}" ]; then
        return 0
    fi
    local line
    line=$(grep -m1 "^export ${var_name}=" "$HOME/.zshrc" 2>/dev/null || true)
    if [ -n "$line" ]; then
        eval "$line"
        export "$var_name"
    fi
}

load_key_from_zshrc ANTHROPIC_API_KEY
load_key_from_zshrc XAI_API_KEY
load_key_from_zshrc COOKIE_ENCRYPTION_KEY

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "FATAL: ANTHROPIC_API_KEY が環境変数にも ~/.zshrc にも見つかりません（.envはdocker composeが自動解決するがANTHROPIC_API_KEYは含まれていない）" >&2
    exit 2
fi
if [ -z "${COOKIE_ENCRYPTION_KEY:-}" ]; then
    echo "警告: COOKIE_ENCRYPTION_KEY が環境変数にも ~/.zshrc にも見つかりません。docker composeの.env解決に委ねて続行します" >&2
fi

export ANTHROPIC_API_KEY XAI_API_KEY COOKIE_ENCRYPTION_KEY

docker compose -f docker-compose.vnc.yml up -d

# Xvfb(supervisord経由)起動待ち（最大30秒）
READY=0
for _ in $(seq 1 15); do
    if docker exec xstock-vnc test -S /tmp/.X11-unix/X99 2>/dev/null; then
        READY=1
        break
    fi
    sleep 2
done
if [ "$READY" -ne 1 ]; then
    echo "警告: Xvfb起動確認がタイムアウトしました。実行を試みます" >&2
fi

docker exec xstock-vnc python scripts/research_weekly.py
RC=$?

docker compose -f docker-compose.vnc.yml down

exit $RC
