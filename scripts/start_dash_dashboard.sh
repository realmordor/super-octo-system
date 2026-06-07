#!/bin/bash
# Start the Dash dashboard in the background, logging output to nohup_dash.out.
# Usage: ./scripts/start_dash_dashboard.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR" || exit

# Kill any existing Dash dashboard process
pkill -f dash_dashboard.py 2>/dev/null
rm -f .dash_dashboard.pid

# Start the Dash dashboard
nohup uv run python scripts/dash_dashboard.py \
    > nohup_dash.out 2>&1 &

echo $! > .dash_dashboard.pid
echo "Dash dashboard started (PID $(cat .dash_dashboard.pid)) — logs in nohup_dash.out"
