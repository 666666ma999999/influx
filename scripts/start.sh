#!/bin/bash
# X投稿収集システム起動スクリプト

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# OS判定
OS_TYPE=$(uname -s)

echo "======================================"
echo "X投稿収集システム"
echo "======================================"

case "$OS_TYPE" in
    Darwin)
        echo "macOS環境を検出"
        echo ""
        echo "[注意] macOSでGUIブラウザを表示するには XQuartz が必要です"
        echo ""
        echo "XQuartzがインストールされていない場合:"
        echo "  brew install --cask xquartz"
        echo ""
        echo "XQuartzを起動し、以下を設定:"
        echo "  環境設定 → セキュリティ → 「ネットワーク・クライアントからの接続を許可」にチェック"
        echo ""
        echo "その後、ターミナルで以下を実行:"
        echo "  xhost +localhost"
        echo ""

        # XQuartz接続許可
        if command -v xhost &> /dev/null; then
            xhost +localhost 2>/dev/null || true
        fi

        COMPOSE_FILE="docker-compose.mac.yml"
        ;;
    Linux)
        echo "Linux環境を検出"
        xhost +local:docker 2>/dev/null || true
        COMPOSE_FILE="docker-compose.yml"
        ;;
    *)
        echo "未対応のOS: $OS_TYPE"
        exit 1
        ;;
esac

# 引数処理
case "${1:-run}" in
    setup)
        echo ""
        echo "初回セットアップを開始します..."
        echo "ブラウザが開いたらXにログインしてください"
        echo ""
        docker-compose -f "$COMPOSE_FILE" --profile setup run --rm setup
        ;;
    run)
        echo ""
        echo "ツイート収集を開始します..."
        echo ""
        docker-compose -f "$COMPOSE_FILE" run --rm xstock
        ;;
    build)
        echo ""
        echo "Dockerイメージをビルドします..."
        echo ""
        docker-compose -f "$COMPOSE_FILE" build
        ;;
    *)
        echo "使用方法: $0 {setup|run|build}"
        echo ""
        echo "  setup  - 初回セットアップ（Xにログイン）"
        echo "  run    - ツイート収集を実行"
        echo "  build  - Dockerイメージをビルド"
        exit 1
        ;;
esac
