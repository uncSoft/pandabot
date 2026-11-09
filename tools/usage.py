"""Print how PandaBot is being used across every server, straight from its database.

On Render: open the pandabot service → Shell, then run

    python tools/usage.py

Locally it reads data/jockie_companion.db (or DB_PATH). It only reads; it never changes anything.
Servers are shown by ID because the database doesn't store server names.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

DAY = 86400


def when(ts: float | None) -> str:
    if not ts:
        return "never"
    ago = time.time() - ts
    if ago < 3600:
        return f"{int(ago // 60)}m ago"
    if ago < DAY:
        return f"{int(ago // 3600)}h ago"
    if ago < 60 * DAY:
        return f"{int(ago // DAY)}d ago"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DB_PATH", "data/jockie_companion.db")
    if not os.path.exists(path):
        sys.exit(f"No database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, cols in [("plays", "guild_id INTEGER, played_at REAL, requester_id INTEGER, title TEXT, artist TEXT"),
                        ("likes", "guild_id INTEGER, user_id INTEGER, liked_at REAL"),
                        ("listeners", "user_id INTEGER"), ("user_settings", "dm_on_save INTEGER"),
                        ("guild_settings", "guild_id INTEGER, announce_mode TEXT"), ("music_bots", "guild_id INTEGER")]:
        if table not in have:  # an older database: treat the missing table as empty
            conn.execute(f"CREATE TEMP TABLE {table} ({cols})")
    now = time.time()
    week, day = now - 7 * DAY, now - DAY

    def one(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0] or 0

    print(f"PandaBot usage  ({path}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)\n")
    rows = [
        ("Servers with any activity", one(
            "SELECT COUNT(*) FROM (SELECT guild_id FROM plays UNION SELECT guild_id FROM likes"
            " UNION SELECT guild_id FROM guild_settings UNION SELECT guild_id FROM music_bots)")),
        ("Servers with a song in the last 7 days", one(
            "SELECT COUNT(DISTINCT guild_id) FROM plays WHERE played_at > ?", week)),
        ("Songs played (all time / 7 days / 24h)", "{} / {} / {}".format(
            one("SELECT COUNT(*) FROM plays"),
            one("SELECT COUNT(*) FROM plays WHERE played_at > ?", week),
            one("SELECT COUNT(*) FROM plays WHERE played_at > ?", day))),
        ("Songs saved (all time / 7 days)", "{} / {}".format(
            one("SELECT COUNT(*) FROM likes"),
            one("SELECT COUNT(*) FROM likes WHERE liked_at > ?", week))),
        ("People who saved a song", one("SELECT COUNT(DISTINCT user_id) FROM likes")),
        ("People who requested a song", one(
            "SELECT COUNT(DISTINCT requester_id) FROM plays WHERE requester_id IS NOT NULL")),
        ("People who heard a song in voice", one("SELECT COUNT(DISTINCT user_id) FROM listeners")),
        ("People who turned save DMs off", one("SELECT COUNT(*) FROM user_settings WHERE dm_on_save = 0")),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label:<{width}}  {value}")

    servers = conn.execute(
        """
        SELECT g.guild_id,
               (SELECT COUNT(*) FROM plays p WHERE p.guild_id = g.guild_id),
               (SELECT COUNT(*) FROM plays p WHERE p.guild_id = g.guild_id AND p.played_at > ?),
               (SELECT COUNT(*) FROM likes l WHERE l.guild_id = g.guild_id),
               (SELECT COUNT(DISTINCT user_id) FROM likes l WHERE l.guild_id = g.guild_id),
               (SELECT MAX(played_at) FROM plays p WHERE p.guild_id = g.guild_id),
               (SELECT announce_mode FROM guild_settings s WHERE s.guild_id = g.guild_id),
               (SELECT COUNT(*) FROM music_bots m WHERE m.guild_id = g.guild_id)
        FROM (SELECT guild_id FROM plays UNION SELECT guild_id FROM likes
              UNION SELECT guild_id FROM guild_settings UNION SELECT guild_id FROM music_bots) g
        ORDER BY 6 DESC
        """,
        (week,),
    ).fetchall()
    print(f"\n  {'Server ID':<20} {'Plays':>6} {'7d':>5} {'Saves':>6} {'Savers':>6}  {'Last song':<11} Announce  Extra bots")
    for gid, plays, plays7, saves, savers, last, mode, bots in servers:
        print(f"  {gid:<20} {plays:>6} {plays7:>5} {saves:>6} {savers:>6}  {when(last):<11} {mode or 'default':<9} {bots}")

    top = conn.execute(
        "SELECT title, artist, COUNT(*) n FROM plays WHERE played_at > ? GROUP BY lower(title), lower(artist)"
        " ORDER BY n DESC LIMIT 5",
        (week,),
    ).fetchall()
    if top:
        print("\n  Most played this week, all servers:")
        for title, artist, n in top:
            print(f"    {n:>3}×  {title}" + (f" — {artist}" if artist else ""))


if __name__ == "__main__":
    main()
