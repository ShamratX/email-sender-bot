#!/bin/bash
# Run on the VPS: ./update.sh
# Pulls latest code, restarts the bot in the background, survives SSH disconnect.
set -e
cd "$(dirname "$0")"
pkill -f "run.py" 2>/dev/null || true
sleep 1
git pull
nohup env SENDER_HOST=0.0.0.0 .venv/bin/python run.py > log.txt 2>&1 &
disown
sleep 1
echo "Updated and running. PID: $(pgrep -f run.py)"
