#!/bin/sh
set -eu
mkdir -p /app/data
printf 'Starting Telegram Group Finder with Python: '
python --version
exec python /app/bot.py
