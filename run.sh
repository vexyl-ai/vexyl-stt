#!/bin/bash
# Start the VEXYL-STT server
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment config
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

source venv/bin/activate
python3 vexyl_stt_server.py
