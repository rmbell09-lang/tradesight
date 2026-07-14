#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$SCRIPT_DIR/.runtime/bin/python"

if [[ ! -x "$RUNTIME" ]]; then
  echo "TradeSight runtime is not installed. Run: python3 install_tradesight.py" >&2
  exit 2
fi

export TRADESIGHT_HOME="$SCRIPT_DIR"
export TRADESIGHT_PORT="${TRADESIGHT_PORT:-5001}"
exec "$RUNTIME" -m tradesight "$@"
