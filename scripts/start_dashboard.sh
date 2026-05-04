#!/bin/bash
# Start the dashboard in the background, logging output to nohup.out.
# Usage: ./scripts/start_dashboard.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR" || exit

# Kill any existing dashboard process
if [ -f .dashboard.pid ]; then
    OLD_PID=$(cat .dashboard.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing dashboard (PID $OLD_PID)..."
        kill "$OLD_PID"
    fi
    rm .dashboard.pid
fi

# Start the dashboard
nohup uv run streamlit run scripts/dashboard.py \
    --server.address 0.0.0.0 \
    --server.headless true \
    > nohup.out 2>&1 &

echo $! > .dashboard.pid
echo "Dashboard started (PID $(cat .dashboard.pid)) — logs in nohup.out"
