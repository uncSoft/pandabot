"""Print how PandaBot is being used across every server, straight from its database.

On Render: open the pandabot service → Shell, then run

    python tools/usage.py

Locally it reads data/jockie_companion.db (or DB_PATH). It only reads; it never changes anything.
Servers are shown by ID because the database doesn't store server names; `p!usage` (bot owner only,
answered in DMs) shows the same report with names and member counts.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jockie_companion.usage import ago, usage  # noqa: E402


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DB_PATH", "data/jockie_companion.db")
    if not os.path.exists(path):
        sys.exit(f"No database at {path}")
    u = usage(sqlite3.connect(f"file:{path}?mode=ro", uri=True))

    print(f"PandaBot usage  ({path}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)\n")
    rows = [
        ("Servers with any activity", u.servers_active),
        ("Servers with a song in the last 7 days", u.servers_7d),
        ("Songs played (all time / 7 days / 24h)", f"{u.plays} / {u.plays_7d} / {u.plays_24h}"),
        ("Songs saved (all time / 7 days)", f"{u.saves} / {u.saves_7d}"),
        ("People who saved a song", u.savers),
        ("People who requested a song", u.requesters),
        ("People who heard a song in voice", u.listeners),
        ("People who turned save DMs off", u.dms_off),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label:<{width}}  {value}")

    print(f"\n  {'Server ID':<20} {'Plays':>6} {'7d':>5} {'Saves':>6} {'Savers':>6}  {'Last song':<11} Announce  Extra bots")
    for s in sorted(u.servers.values(), key=lambda s: s.last_play or 0, reverse=True):
        print(f"  {s.guild_id:<20} {s.plays:>6} {s.plays_7d:>5} {s.saves:>6} {s.savers:>6}  "
              f"{ago(s.last_play):<11} {s.announce_mode or 'default':<9} {s.extra_bots}")

    if u.top_week:
        print("\n  Most played this week, all servers:")
        for title, artist, n in u.top_week:
            print(f"    {n:>3}×  {title}" + (f" — {artist}" if artist else ""))


if __name__ == "__main__":
    main()
