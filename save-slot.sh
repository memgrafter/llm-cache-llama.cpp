#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8081}"
SLOT="${SLOT:-0}"
FILENAME="${1:-${FILENAME:-slot_0_current.bin}}"

curl -sS -X POST "http://$HOST:$PORT/slots/$SLOT?action=save" \
  -H 'Content-Type: application/json' \
  -d "{\"filename\":\"$FILENAME\"}"
echo
