#!/usr/bin/env bash
# Runs ON THE SERVER (Ubuntu/Debian) as root. Safe to run again: it updates in place.
set -euo pipefail
APP=/opt/pandabot

if ! python3 -m venv --help >/dev/null 2>&1 || ! command -v sqlite3 >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv sqlite3 >/dev/null
fi

id pandabot >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin pandabot

cd "$APP"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p data
if [ ! -f .env ]; then
  echo "!! $APP/.env is missing. Copy it up first (deploy/push.sh does this with --first)." >&2
  exit 1
fi
chown -R pandabot:pandabot "$APP"
chmod 600 .env

# Nightly database backup, keeping 14 days.
cat > /etc/cron.daily/pandabot-backup <<'EOF'
#!/bin/sh
mkdir -p /opt/pandabot/data/backups
sqlite3 /opt/pandabot/data/jockie_companion.db ".backup /opt/pandabot/data/backups/$(date +%F).db"
find /opt/pandabot/data/backups -name '*.db' -mtime +14 -delete
chown -R pandabot:pandabot /opt/pandabot/data/backups
EOF
chmod +x /etc/cron.daily/pandabot-backup

cp deploy/pandabot.service /etc/systemd/system/pandabot.service
systemctl daemon-reload
systemctl enable pandabot >/dev/null
systemctl restart pandabot
sleep 6
systemctl --no-pager --lines=0 status pandabot | head -3
echo "--- last log lines ---"
journalctl -u pandabot --no-pager -n 6 -o cat | grep -v "voice will NOT" || true
