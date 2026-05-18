#!/bin/bash
# Start the Dash dashboard in the background, logging output to nohup_dash.out.
# Usage: ./scripts/start_dash_dashboard.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR" || exit

# Kill any existing Dash dashboard process
if [ -f .dash_dashboard.pid ]; then
    OLD_PID=$(cat .dash_dashboard.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing Dash dashboard (PID $OLD_PID)..."
        kill "$OLD_PID"
    fi
    rm .dash_dashboard.pid
fi

# Start the Dash dashboard
nohup uv run python scripts/dash_dashboard.py \
    > nohup_dash.out 2>&1 &

echo $! > .dash_dashboard.pid
echo "Dash dashboard started (PID $(cat .dash_dashboard.pid)) — logs in nohup_dash.out"
