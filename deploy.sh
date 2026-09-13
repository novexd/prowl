#!/usr/bin/env bash
#
# Prowl bot auto-updater.
# Pulls the latest code from the GitHub repo (website branch), reinstalls any
# new dependencies, and restarts the bot - but only when something changed.
#
# Setup (one time):
#   git clone --depth 1 --filter=blob:none --sparse -b website https://github.com/novexd/prowl.git /opt/prowl
#   cd /opt/prowl && git sparse-checkout set cli
#   cd cli && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
#   cp .env.example .env.local   # then fill in your real values
#
# Then either run it manually, or drop this line in `crontab -e` to auto-update:
#   */5 * * * * cd /opt/prowl/cli && ./deploy.sh >> /opt/prowl/cli/deploy.log 2>&1

set -e
cd "$(dirname "$0")"

BOT_SERVICE="${BOT_SERVICE:-prowl}"

before=$(git rev-parse HEAD 2>/dev/null || echo none)
git pull --ff-only origin website 2>&1 | grep -v 'Already up to date' || true
after=$(git rev-parse HEAD 2>/dev/null || echo none)

if [ "$before" = "$after" ]; then
  echo "$(date '+%F %T') - no changes"
  exit 0
fi

echo "$(date '+%F %T') - updated $before -> $after"

if [ -d venv ]; then
  source venv/bin/activate
fi
pip install -r requirements.txt --quiet

# Restart the bot through whatever process manager exists.
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q "^${BOT_SERVICE}\."; then
  sudo systemctl restart "$BOT_SERVICE"
elif command -v pm2 >/dev/null 2>&1; then
  pm2 restart "$BOT_SERVICE"
else
  echo "No process manager found - restart the bot manually (python3 start.py)."
fi

echo "$(date '+%F %T') - restart requested"
