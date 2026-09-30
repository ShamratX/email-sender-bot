#!/bin/bash
# Run on the VPS: ./update.sh
# Pulls latest code and restarts the systemd service. Survives SSH
# disconnect AND server reboots (systemd starts it automatically on boot).
set -e
cd "$(dirname "$0")"
git pull
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart email-sender
sleep 1
sudo systemctl status email-sender --no-pager -l | head -10
