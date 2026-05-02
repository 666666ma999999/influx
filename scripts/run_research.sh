#!/usr/bin/env bash
# scripts/research_influencers.py を xstock-vnc コンテナ内で実行するラッパー。
# .envrc から XAI_API_KEY / ANTHROPIC_API_KEY を読み込んでから docker exec する。
#
# Usage:
#   scripts/run_research.sh --phase evaluate
#   scripts/run_research.sh --phase report
#   scripts/run_research.sh <その他 research_influencers.py のオプション>

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR/.."

# shellcheck disable=SC1091
source .envrc 2>/dev/null || true

: "${XAI_API_KEY:?XAI_API_KEY が未設定（.envrc を確認）}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY が未設定（.envrc を確認）}"

exec docker exec \
  -e XAI_API_KEY="$XAI_API_KEY" \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  xstock-vnc python scripts/research_influencers.py "$@"
