#!/bin/bash
# VNC版 X投稿収集システム起動スクリプト

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "======================================"
echo "X投稿収集システム (VNC版)"
echo "======================================"

case "${1:-start}" in
    build)
        echo ""
        echo "Dockerイメージをビルドします..."
        docker-compose -f docker-compose.vnc.yml build
        echo ""
        echo "ビルド完了!"
        ;;
    start)
        echo ""
        echo "VNCサーバーを起動します..."
        docker-compose -f docker-compose.vnc.yml up -d
        echo ""
        echo "======================================"
        echo "起動完了!"
        echo ""
        echo "ブラウザで以下にアクセスしてください:"
        echo "  http://localhost:6080/vnc.html"
        echo ""
        echo "VNC画面が表示されたら、ターミナルで以下を実行:"
        echo "  docker exec -it xstock-vnc python scripts/setup_profile.py"
        echo ""
        echo "停止するには:"
        echo "  ./scripts/start_vnc.sh stop"
        echo "======================================"
        ;;
    stop)
        echo ""
        echo "VNCサーバーを停止します..."
        docker-compose -f docker-compose.vnc.yml down
        echo "停止完了!"
        ;;
    setup)
        echo ""
        echo "セットアップを実行します..."
        echo "ブラウザで http://localhost:6080/vnc.html を開いてください"
        echo ""
        docker exec -it xstock-vnc python scripts/setup_profile.py
        ;;
    collect)
        echo ""
        echo "ツイート収集を実行します..."
        docker exec -it xstock-vnc python scripts/collect_tweets.py
        ;;
    logs)
        docker-compose -f docker-compose.vnc.yml logs -f
        ;;
    *)
        echo "使用方法: $0 {build|start|stop|setup|collect|logs}"
        echo ""
        echo "  build   - Dockerイメージをビルド"
        echo "  start   - VNCサーバーを起動"
        echo "  stop    - VNCサーバーを停止"
        echo "  setup   - 初回セットアップ（Xにログイン）"
        echo "  collect - ツイート収集を実行"
        echo "  logs    - ログを表示"
        exit 1
        ;;
esac
