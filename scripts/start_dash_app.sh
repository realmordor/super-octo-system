#!/bin/bash
# Start the phone-style dashboard (port 8051) in the background, logging output to nohup_app.out.
# Runs alongside the original dashboard started by start_dash_dashboard.sh.
# Usage: ./scripts/start_dash_app.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR" || exit

# Kill any existing phone-style dashboard process
pkill -f "scripts/dash_app.py" 2>/dev/null
rm -f .dash_app.pid

nohup uv run python scripts/dash_app.py \
    > nohup_app.out 2>&1 &

echo $! > .dash_app.pid
echo "Phone-style dashboard started (PID $(cat .dash_app.pid)) on port 8051 — logs in nohup_app.out"
