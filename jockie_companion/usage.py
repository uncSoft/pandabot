"""How PandaBot is used across every server: totals, per-server activity and this week's top songs.

Read-only. Shared by the owner-only `p!usage` command and `tools/usage.py` (run in Render's Shell).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

DAY = 86400

# Older databases may lack some tables; missing ones are treated as empty.
_TABLES = {
    "plays": "guild_id INTEGER, played_at REAL, requester_id INTEGER, title TEXT, artist TEXT",
    "likes": "guild_id INTEGER, user_id INTEGER, liked_at REAL",
    "listeners": "user_id INTEGER",
    "user_settings": "dm_on_save INTEGER",
    "guild_settings": "guild_id INTEGER, announce_mode TEXT",
    "music_bots": "guild_id INTEGER",
}
_ALL_GUILDS = ("SELECT guild_id FROM plays UNION SELECT guild_id FROM likes"
               " UNION SELECT guild_id FROM guild_settings UNION SELECT guild_id FROM music_bots")


@dataclass
class ServerUsage:
    guild_id: int
    plays: int = 0
    plays_7d: int = 0
    saves: int = 0
    savers: int = 0
    last_play: float | None = None
    announce_mode: str | None = None
    extra_bots: int = 0


@dataclass
class Usage:
    servers_active: int
    servers_7d: int
    plays: int
    plays_7d: int
    plays_24h: int
    saves: int
    saves_7d: int
    savers: int
    requesters: int
    listeners: int
    dms_off: int
    servers: dict[int, ServerUsage] = field(default_factory=dict)
    top_week: list[tuple[str, str | None, int]] = field(default_factory=list)


def usage(conn: sqlite3.Connection, now: float | None = None) -> Usage:
    now = now or time.time()
    week, day = now - 7 * DAY, now - DAY
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, cols in _TABLES.items():
        if table not in have:
            conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {table} ({cols})")

    def one(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0] or 0

    result = Usage(
        servers_active=one(f"SELECT COUNT(*) FROM ({_ALL_GUILDS})"),
        servers_7d=one("SELECT COUNT(DISTINCT guild_id) FROM plays WHERE played_at > ?", week),
        plays=one("SELECT COUNT(*) FROM plays"),
        plays_7d=one("SELECT COUNT(*) FROM plays WHERE played_at > ?", week),
        plays_24h=one("SELECT COUNT(*) FROM plays WHERE played_at > ?", day),
        saves=one("SELECT COUNT(*) FROM likes"),
        saves_7d=one("SELECT COUNT(*) FROM likes WHERE liked_at > ?", week),
        savers=one("SELECT COUNT(DISTINCT user_id) FROM likes"),
        requesters=one("SELECT COUNT(DISTINCT requester_id) FROM plays WHERE requester_id IS NOT NULL"),
        listeners=one("SELECT COUNT(DISTINCT user_id) FROM listeners"),
        dms_off=one("SELECT COUNT(*) FROM user_settings WHERE dm_on_save = 0"),
    )
    for row in conn.execute(
        f"""
        SELECT g.guild_id,
               (SELECT COUNT(*) FROM plays p WHERE p.guild_id = g.guild_id),
               (SELECT COUNT(*) FROM plays p WHERE p.guild_id = g.guild_id AND p.played_at > ?),
               (SELECT COUNT(*) FROM likes l WHERE l.guild_id = g.guild_id),
               (SELECT COUNT(DISTINCT user_id) FROM likes l WHERE l.guild_id = g.guild_id),
               (SELECT MAX(played_at) FROM plays p WHERE p.guild_id = g.guild_id),
               (SELECT announce_mode FROM guild_settings s WHERE s.guild_id = g.guild_id),
               (SELECT COUNT(*) FROM music_bots m WHERE m.guild_id = g.guild_id)
        FROM ({_ALL_GUILDS}) g
        """,
        (week,),
    ):
        server = ServerUsage(*tuple(row))
        result.servers[server.guild_id] = server
    result.top_week = [tuple(r) for r in conn.execute(
        "SELECT title, artist, COUNT(*) n FROM plays WHERE played_at > ?"
        " GROUP BY lower(title), lower(artist) ORDER BY n DESC LIMIT 5", (week,))]
    return result


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "never"
    secs = (now or time.time()) - ts
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < DAY:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // DAY)}d ago"
