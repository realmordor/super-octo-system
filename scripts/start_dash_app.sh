#!/bin/bash
# Start the dashboard (port 8051) in the background, logging output to nohup_app.out.
# Usage: ./scripts/start_dash_app.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR" || exit

# Kill any existing dashboard process
pkill -f "scripts/dash_app.py" 2>/dev/null
rm -f .dash_app.pid

nohup uv run python scripts/dash_app.py \
    > nohup_app.out 2>&1 &

echo $! > .dash_app.pid
echo "Dashboard started (PID $(cat .dash_app.pid)) on port 8051 — logs in nohup_app.out"
