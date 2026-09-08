#!/usr/bin/env bash
# Dev-only: kill any running bot process and start a fresh one, logging to
# /tmp/vo-ad-bot.log. Run from the project root: ./restart.sh
set -euo pipefail

cd "$(dirname "$0")"

if pgrep -f "app.bot" > /dev/null; then
    echo "Stopping existing bot process..."
    pkill -f "app.bot"
    sleep 1
fi

set -a
source .env
set +a

nohup .venv/bin/python -m app.bot > /tmp/vo-ad-bot.log 2>&1 &
disown

sleep 2
echo "Started. PID: $(pgrep -f app.bot)"
echo "Logs: tail -f /tmp/vo-ad-bot.log"
tail -5 /tmp/vo-ad-bot.log
