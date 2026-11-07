#!/usr/bin/env bash
# Runs ON YOUR MAC. Copies PandaBot to the server and (re)starts it there.
#
#   First time (also moves .env + the database, and stops the copy on this Mac):
#     deploy/push.sh root@YOUR_SERVER_IP --first
#   After code changes:
#     deploy/push.sh root@YOUR_SERVER_IP
set -euo pipefail
HOST="${1:?usage: deploy/push.sh root@SERVER_IP [--first]}"
FIRST="${2:-}"
cd "$(dirname "$0")/.."

ssh "$HOST" 'mkdir -p /opt/pandabot/data'
rsync -az --delete \
  --exclude .venv --exclude data --exclude .env --exclude __pycache__ --exclude .pytest_cache \
  --exclude '*.db' --exclude .DS_Store \
  ./ "$HOST:/opt/pandabot/"

if [ "$FIRST" = "--first" ]; then
  # Only one copy of the bot may run at a time, or every song gets announced twice.
  if pkill -f "python.* -m jockie_companion"; then echo "Stopped the copy running on this Mac."; fi
  sleep 1
  rsync -az .env "$HOST:/opt/pandabot/.env"
  if [ -f data/jockie_companion.db ]; then
    sqlite3 data/jockie_companion.db ".backup data/upload.db"
    rsync -az data/upload.db "$HOST:/opt/pandabot/data/jockie_companion.db"
    rm -f data/upload.db
    echo "Copied the database (songs, saves, settings)."
  fi
fi

ssh "$HOST" 'bash /opt/pandabot/deploy/server-setup.sh'
echo
echo "Done. Live logs:  ssh $HOST journalctl -u pandabot -f"
