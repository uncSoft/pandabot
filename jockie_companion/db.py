"""SQLite storage: play history, who heard what, per-user saved songs, and stats queries."""
from __future__ import annotations

import math
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .parser import Track, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id               INTEGER PRIMARY KEY,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,       -- text channel the music bot posted in
    voice_channel_id INTEGER,                -- voice channel the music bot was playing in
    message_id       INTEGER NOT NULL,
    title            TEXT NOT NULL,
    artist           TEXT,
    url              TEXT,
    requester_id     INTEGER,
    requester_name   TEXT,
    played_at        REAL NOT NULL,
    length_seconds   INTEGER,                -- from the "Added Track" message, when we saw one
    -- Not UNIQUE(message_id) alone: some bots edit one "player" message for each new song.
    UNIQUE (message_id, title)
);
CREATE INDEX IF NOT EXISTS plays_guild_time ON plays (guild_id, played_at DESC);
CREATE INDEX IF NOT EXISTS plays_message ON plays (message_id);
CREATE INDEX IF NOT EXISTS plays_requester ON plays (guild_id, requester_id, played_at DESC);

-- Who was in the voice channel when each song started ("songs I heard").
CREATE TABLE IF NOT EXISTS listeners (
    play_id INTEGER NOT NULL REFERENCES plays (id),
    user_id INTEGER NOT NULL,
    PRIMARY KEY (play_id, user_id)
);
CREATE INDEX IF NOT EXISTS listeners_user ON listeners (user_id);

-- Our own "now playing" posts, so ❤️ on them saves the right song.
CREATE TABLE IF NOT EXISTS announcements (
    message_id INTEGER PRIMARY KEY,
    play_id    INTEGER NOT NULL REFERENCES plays (id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id            INTEGER PRIMARY KEY,
    announce_mode       TEXT NOT NULL,   -- follow | channel | off
    announce_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS likes (
    id        INTEGER PRIMARY KEY,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    track_key TEXT NOT NULL,
    title     TEXT NOT NULL,
    artist    TEXT,
    url       TEXT,
    play_id   INTEGER REFERENCES plays (id),
    liked_at  REAL NOT NULL,
    -- The DM card we sent for this save, so unsaving can delete it too.
    dm_channel_id INTEGER,
    dm_message_id INTEGER,
    UNIQUE (guild_id, user_id, track_key)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id     INTEGER PRIMARY KEY,
    dm_on_save  INTEGER NOT NULL
);

-- "Added to queue" messages carry the requester and length; "now playing" often doesn't.
CREATE TABLE IF NOT EXISTS pending_requests (
    id             INTEGER PRIMARY KEY,
    guild_id       INTEGER NOT NULL,
    title_key      TEXT NOT NULL,
    requester_id   INTEGER,
    requester_name TEXT,
    created_at     REAL NOT NULL,
    length_seconds INTEGER
);
"""

# Columns added after the first release; added to existing databases on startup.
MIGRATIONS = [("plays", "length_seconds", "INTEGER"), ("pending_requests", "length_seconds", "INTEGER")]

PENDING_TTL = 6 * 3600  # forget queue requests that never got played


@dataclass
class Play:
    id: int
    guild_id: int
    channel_id: int
    voice_channel_id: int | None
    message_id: int
    title: str
    artist: str | None
    url: str | None
    requester_id: int | None
    requester_name: str | None
    played_at: float
    length_seconds: int | None = None


@dataclass
class Like:
    id: int
    guild_id: int
    user_id: int
    title: str
    artist: str | None
    url: str | None
    liked_at: float
    dm_channel_id: int | None = None
    dm_message_id: int | None = None


@dataclass
class Pending:
    requester_id: int | None
    requester_name: str | None
    length_seconds: int | None


@dataclass
class UserSummary:
    requests: int
    artists: int
    known_length_seconds: int
    songs_with_length: int
    top_artists: list[tuple[str, int]]
    most_loved: tuple[str, str | None, int] | None  # (title, artist, people who saved it)
    requests_saved_by_others: int
    saves: int
    heard: int

    @property
    def hit_rate(self) -> float:
        return self.requests_saved_by_others / self.requests if self.requests else 0.0


@dataclass
class DJRow:
    user_id: int
    name: str | None
    requests: int
    loves: int  # saves of their requests by other people


@dataclass
class Match:
    score: int  # 0-100
    shared_artists: list[str]
    shared_songs: list[tuple[str, str | None]]


LIKE_COLUMNS = "id, guild_id, user_id, title, artist, url, liked_at, dm_channel_id, dm_message_id"


def track_key(title: str, artist: str | None) -> str:
    # Keyed on title+artist rather than URL: the same song gets different
    # Spotify/YouTube/SoundCloud URLs depending on how it was requested.
    return normalize(f"{title} {artist or ''}")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate_columns()
        self.conn.executescript(SCHEMA)

    def _migrate_columns(self) -> None:
        for table, column, sql_type in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:  # table exists from an older version
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        self.conn.commit()

    def _plays(self, sql: str, params: tuple) -> list[Play]:
        return [Play(**r) for r in self.conn.execute(sql, params).fetchall()]

    # --- plays -----------------------------------------------------------
    def record_play(self, guild_id: int, channel_id: int, message_id: int, track: Track,
                    requester_id: int | None = None, requester_name: str | None = None,
                    voice_channel_id: int | None = None, at: float | None = None,
                    length_seconds: int | None = None) -> tuple[int, bool]:
        """Returns (play_id, created). Re-recording the same message + song is a no-op."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO plays (guild_id, channel_id, voice_channel_id, message_id, title, artist,"
            " url, requester_id, requester_name, played_at, length_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (guild_id, channel_id, voice_channel_id, message_id, track.title, track.artist, track.url,
             requester_id, requester_name, at or time.time(), length_seconds),
        )
        self.conn.commit()
        if cur.rowcount:
            return cur.lastrowid, True
        row = self.conn.execute(
            "SELECT id FROM plays WHERE message_id = ? AND title = ?", (message_id, track.title)
        ).fetchone()
        return row["id"], False

    def add_listeners(self, play_id: int, user_ids: list[int]) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO listeners (play_id, user_id) VALUES (?, ?)",
                              [(play_id, u) for u in user_ids])
        self.conn.commit()

    def play_by_message(self, message_id: int) -> Play | None:
        """The song a message is about: a music-bot message (latest song if it was edited) or our announcement."""
        row = self.conn.execute(
            "SELECT * FROM plays WHERE message_id = ? ORDER BY id DESC LIMIT 1", (message_id,)
        ).fetchone()
        if not row:
            row = self.conn.execute(
                "SELECT plays.* FROM announcements JOIN plays ON plays.id = announcements.play_id"
                " WHERE announcements.message_id = ?", (message_id,)
            ).fetchone()
        return Play(**row) if row else None

    def play_by_id(self, play_id: int) -> Play | None:
        row = self.conn.execute("SELECT * FROM plays WHERE id = ?", (play_id,)).fetchone()
        return Play(**row) if row else None

    def latest_play(self, guild_id: int, voice_channel_id: int | None = None) -> Play | None:
        """Most recent song in the server, or in one voice channel if given (falls back to the server)."""
        if voice_channel_id:
            row = self.conn.execute(
                "SELECT * FROM plays WHERE guild_id = ? AND voice_channel_id = ? ORDER BY played_at DESC LIMIT 1",
                (guild_id, voice_channel_id),
            ).fetchone()
            if row:
                return Play(**row)
        row = self.conn.execute(
            "SELECT * FROM plays WHERE guild_id = ? ORDER BY played_at DESC LIMIT 1", (guild_id,)
        ).fetchone()
        return Play(**row) if row else None

    def count_plays(self, guild_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM plays WHERE guild_id = ?", (guild_id,)).fetchone()[0]

    def recent(self, guild_id: int, since: float = 0, limit: int = 500) -> list[Play]:
        return self._plays("SELECT * FROM plays WHERE guild_id = ? AND played_at >= ? ORDER BY played_at DESC LIMIT ?",
                           (guild_id, since, limit))

    def history(self, guild_id: int, limit: int = 10) -> list[Play]:
        return self._plays("SELECT * FROM plays WHERE guild_id = ? ORDER BY played_at DESC LIMIT ?",
                           (guild_id, limit))

    def requests_by(self, guild_id: int, user_id: int, since: float = 0, limit: int = 500) -> list[Play]:
        return self._plays(
            "SELECT * FROM plays WHERE guild_id = ? AND requester_id = ? AND played_at >= ?"
            " ORDER BY played_at DESC LIMIT ?", (guild_id, user_id, since, limit))

    def heard_by(self, guild_id: int, user_id: int, since: float = 0, limit: int = 500) -> list[Play]:
        return self._plays(
            "SELECT plays.* FROM plays JOIN listeners ON listeners.play_id = plays.id"
            " WHERE plays.guild_id = ? AND listeners.user_id = ? AND plays.played_at >= ?"
            " ORDER BY plays.played_at DESC LIMIT ?", (guild_id, user_id, since, limit))

    def random_play(self, guild_id: int, older_than: float = 3600) -> Play | None:
        """A random song from history, preferring ones that didn't just play."""
        for cutoff in (time.time() - older_than, float("inf")):
            row = self.conn.execute(
                "SELECT * FROM plays WHERE guild_id = ? AND played_at < ? ORDER BY RANDOM() LIMIT 1",
                (guild_id, cutoff)).fetchone()
            if row:
                return Play(**row)
        return None

    def find_requester(self, guild_id: int, name: str) -> tuple[int, str] | None:
        """Resolve a typed name ("panda") to a requester we've seen: exact name first, then prefix."""
        for pattern in (name, name + "%"):
            row = self.conn.execute(
                "SELECT requester_id, requester_name, COUNT(*) AS n FROM plays"
                " WHERE guild_id = ? AND requester_id IS NOT NULL AND requester_name LIKE ?"
                " GROUP BY requester_id ORDER BY n DESC LIMIT 1", (guild_id, pattern)).fetchone()
            if row:
                return row["requester_id"], row["requester_name"]
        return None

    def requester_name(self, guild_id: int, user_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT requester_name FROM plays WHERE guild_id = ? AND requester_id = ? AND requester_name IS NOT NULL"
            " ORDER BY played_at DESC LIMIT 1", (guild_id, user_id)).fetchone()
        return row["requester_name"] if row else None

    # --- stats -----------------------------------------------------------
    def user_summary(self, guild_id: int, user_id: int, since: float = 0) -> UserSummary:
        c = self.conn
        base = "FROM plays WHERE guild_id = ? AND requester_id = ? AND played_at >= ?"
        args = (guild_id, user_id, since)
        row = c.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT lower(artist)) AS artists,"
            f" coalesce(SUM(length_seconds), 0) AS secs, COUNT(length_seconds) AS with_len {base}", args).fetchone()
        top = c.execute(
            f"SELECT artist, COUNT(*) AS n {base} AND artist IS NOT NULL"
            f" GROUP BY lower(artist) ORDER BY n DESC, max(played_at) DESC LIMIT 3", args).fetchall()
        loved = c.execute(
            "SELECT p.title, p.artist, COUNT(DISTINCT l.user_id) AS n FROM plays p"
            " JOIN likes l ON l.play_id = p.id AND l.user_id != p.requester_id"
            " WHERE p.guild_id = ? AND p.requester_id = ? AND p.played_at >= ?"
            " GROUP BY p.title, p.artist ORDER BY n DESC LIMIT 1", args).fetchone()
        saved_by_others = c.execute(
            "SELECT COUNT(DISTINCT p.id) FROM plays p JOIN likes l ON l.play_id = p.id AND l.user_id != p.requester_id"
            " WHERE p.guild_id = ? AND p.requester_id = ? AND p.played_at >= ?", args).fetchone()[0]
        saves = c.execute("SELECT COUNT(*) FROM likes WHERE guild_id = ? AND user_id = ? AND liked_at >= ?",
                          args).fetchone()[0]
        heard = c.execute(
            "SELECT COUNT(*) FROM listeners JOIN plays ON plays.id = listeners.play_id"
            " WHERE plays.guild_id = ? AND listeners.user_id = ? AND plays.played_at >= ?", args).fetchone()[0]
        return UserSummary(
            requests=row["n"], artists=row["artists"], known_length_seconds=row["secs"],
            songs_with_length=row["with_len"], top_artists=[(r["artist"], r["n"]) for r in top],
            most_loved=(loved["title"], loved["artist"], loved["n"]) if loved else None,
            requests_saved_by_others=saved_by_others, saves=saves, heard=heard)

    def top_played(self, guild_id: int, since: float = 0, limit: int = 10) -> list[tuple[str, str | None, int]]:
        rows = self.conn.execute(
            "SELECT title, artist, COUNT(*) AS n FROM plays WHERE guild_id = ? AND played_at >= ?"
            " GROUP BY lower(title), lower(coalesce(artist, '')) ORDER BY n DESC, max(played_at) DESC LIMIT ?",
            (guild_id, since, limit),
        ).fetchall()
        return [(r["title"], r["artist"], r["n"]) for r in rows]

    def top_liked(self, guild_id: int, since: float = 0, limit: int = 10) -> list[tuple[str, str | None, int]]:
        rows = self.conn.execute(
            "SELECT title, artist, COUNT(*) AS n FROM likes WHERE guild_id = ? AND liked_at >= ?"
            " GROUP BY track_key ORDER BY n DESC, max(liked_at) DESC LIMIT ?",
            (guild_id, since, limit),
        ).fetchall()
        return [(r["title"], r["artist"], r["n"]) for r in rows]

    def top_artists(self, guild_id: int, since: float = 0, limit: int = 10) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT artist, COUNT(*) AS n FROM plays WHERE guild_id = ? AND played_at >= ? AND artist IS NOT NULL"
            " GROUP BY lower(artist) ORDER BY n DESC, max(played_at) DESC LIMIT ?", (guild_id, since, limit)
        ).fetchall()
        return [(r["artist"], r["n"]) for r in rows]

    def top_requesters(self, guild_id: int, since: float = 0, limit: int = 10) -> list[tuple[int, str | None, int]]:
        rows = self.conn.execute(
            "SELECT requester_id, max(requester_name) AS name, COUNT(*) AS n FROM plays"
            " WHERE guild_id = ? AND played_at >= ? AND requester_id IS NOT NULL"
            " GROUP BY requester_id ORDER BY n DESC LIMIT ?", (guild_id, since, limit)).fetchall()
        return [(r["requester_id"], r["name"], r["n"]) for r in rows]

    def dj_board(self, guild_id: int, since: float = 0, limit: int = 10) -> list[DJRow]:
        """Whose requests other people save most."""
        rows = self.conn.execute(
            "SELECT p.requester_id, max(p.requester_name) AS name, COUNT(*) AS requests,"
            " (SELECT COUNT(*) FROM likes l JOIN plays p2 ON l.play_id = p2.id"
            "   WHERE p2.guild_id = p.guild_id AND p2.requester_id = p.requester_id AND p2.played_at >= ?"
            "   AND l.user_id != p2.requester_id) AS loves"
            " FROM plays p WHERE p.guild_id = ? AND p.played_at >= ? AND p.requester_id IS NOT NULL"
            " GROUP BY p.requester_id ORDER BY loves DESC, requests DESC LIMIT ?",
            (since, guild_id, since, limit)).fetchall()
        return [DJRow(r["requester_id"], r["name"], r["requests"], r["loves"]) for r in rows]

    def _artist_weights(self, guild_id: int, user_id: int) -> Counter:
        """A user's taste: artists they requested (×1), heard (×0.5) and saved (×2)."""
        w: Counter = Counter()
        for sql, weight in (
            ("SELECT artist FROM plays WHERE guild_id = ? AND requester_id = ?", 1.0),
            ("SELECT plays.artist FROM plays JOIN listeners ON listeners.play_id = plays.id"
             " WHERE plays.guild_id = ? AND listeners.user_id = ?", 0.5),
            ("SELECT artist FROM likes WHERE guild_id = ? AND user_id = ?", 2.0),
        ):
            for (artist,) in self.conn.execute(sql, (guild_id, user_id)):
                if artist:
                    w[normalize(artist)] += weight
        return w

    def taste_match(self, guild_id: int, a: int, b: int) -> Match | None:
        wa, wb = self._artist_weights(guild_id, a), self._artist_weights(guild_id, b)
        if not wa or not wb:
            return None
        dot = sum(wa[k] * wb[k] for k in wa.keys() & wb.keys())
        norm = math.sqrt(sum(v * v for v in wa.values())) * math.sqrt(sum(v * v for v in wb.values()))
        shared = sorted(wa.keys() & wb.keys(), key=lambda k: -min(wa[k], wb[k]))[:3]
        names = {}
        for (artist,) in self.conn.execute("SELECT artist FROM plays WHERE guild_id = ? AND artist IS NOT NULL",
                                           (guild_id,)):
            names.setdefault(normalize(artist), artist)
        songs = self.conn.execute(
            "SELECT la.title, la.artist FROM likes la JOIN likes lb"
            " ON la.track_key = lb.track_key AND la.guild_id = lb.guild_id"
            " WHERE la.guild_id = ? AND la.user_id = ? AND lb.user_id = ? ORDER BY la.liked_at DESC LIMIT 3",
            (guild_id, a, b)).fetchall()
        return Match(score=round(100 * dot / norm) if norm else 0,
                     shared_artists=[names.get(k, k) for k in shared],
                     shared_songs=[(r["title"], r["artist"]) for r in songs])

    # --- announcements & settings ---------------------------------------
    def add_announcement(self, message_id: int, play_id: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO announcements (message_id, play_id) VALUES (?, ?)",
                          (message_id, play_id))
        self.conn.commit()

    def get_announce_settings(self, guild_id: int) -> tuple[str, int | None] | None:
        row = self.conn.execute(
            "SELECT announce_mode, announce_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return (row["announce_mode"], row["announce_channel_id"]) if row else None

    def set_announce_settings(self, guild_id: int, mode: str, channel_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO guild_settings (guild_id, announce_mode, announce_channel_id) VALUES (?, ?, ?)"
            " ON CONFLICT (guild_id) DO UPDATE SET announce_mode = excluded.announce_mode,"
            " announce_channel_id = excluded.announce_channel_id",
            (guild_id, mode, channel_id),
        )
        self.conn.commit()

    def has_plays_in_channel(self, channel_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM plays WHERE channel_id = ? LIMIT 1", (channel_id,)).fetchone() is not None

    # --- likes -----------------------------------------------------------
    def add_like(self, guild_id: int, user_id: int, title: str, artist: str | None,
                 url: str | None, play_id: int | None = None) -> int | None:
        """Returns the new like's id, or None if the user had already saved this song."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO likes (guild_id, user_id, track_key, title, artist, url, play_id, liked_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (guild_id, user_id, track_key(title, artist), title, artist, url, play_id, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def like_play(self, user_id: int, play: Play) -> int | None:
        return self.add_like(play.guild_id, user_id, play.title, play.artist, play.url, play.id)

    def get_like(self, like_id: int) -> Like | None:
        row = self.conn.execute(f"SELECT {LIKE_COLUMNS} FROM likes WHERE id = ?", (like_id,)).fetchone()
        return Like(**row) if row else None

    def remove_like(self, like_id: int) -> Like | None:
        like = self.get_like(like_id)
        if like:
            self.conn.execute("DELETE FROM likes WHERE id = ?", (like_id,))
            self.conn.commit()
        return like

    def remove_like_for_play(self, user_id: int, play: Play) -> Like | None:
        row = self.conn.execute(
            "SELECT id FROM likes WHERE guild_id = ? AND user_id = ? AND track_key = ?",
            (play.guild_id, user_id, track_key(play.title, play.artist)),
        ).fetchone()
        return self.remove_like(row["id"]) if row else None

    def set_like_dm(self, like_id: int, channel_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE likes SET dm_channel_id = ?, dm_message_id = ? WHERE id = ?",
                          (channel_id, message_id, like_id))
        self.conn.commit()

    def likes(self, guild_id: int, user_id: int, limit: int | None = None) -> list[Like]:
        sql = f"SELECT {LIKE_COLUMNS} FROM likes WHERE guild_id = ? AND user_id = ? ORDER BY id DESC"
        params: tuple = (guild_id, user_id)
        if limit:
            sql += " LIMIT ?"
            params += (limit,)
        return [Like(**r) for r in self.conn.execute(sql, params).fetchall()]

    def remove_like_at(self, guild_id: int, user_id: int, position: int) -> Like | None:
        """Remove by the 1-based position shown in /likes (newest first)."""
        likes = self.likes(guild_id, user_id)
        if not 1 <= position <= len(likes):
            return None
        return self.remove_like(likes[position - 1].id)

    # --- user settings ---------------------------------------------------
    def get_dm_pref(self, user_id: int) -> bool | None:
        row = self.conn.execute("SELECT dm_on_save FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row["dm_on_save"]) if row else None

    def set_dm_pref(self, user_id: int, enabled: bool) -> None:
        self.conn.execute(
            "INSERT INTO user_settings (user_id, dm_on_save) VALUES (?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET dm_on_save = excluded.dm_on_save",
            (user_id, int(enabled)),
        )
        self.conn.commit()

    # --- pending queue requests -----------------------------------------
    def add_pending(self, guild_id: int, title: str, requester_id: int | None, requester_name: str | None,
                    length_seconds: int | None = None) -> None:
        if requester_id is None and requester_name is None and length_seconds is None:
            return
        now = time.time()
        self.conn.execute("DELETE FROM pending_requests WHERE created_at < ?", (now - PENDING_TTL,))
        self.conn.execute(
            "INSERT INTO pending_requests (guild_id, title_key, requester_id, requester_name, created_at,"
            " length_seconds) VALUES (?,?,?,?,?,?)",
            (guild_id, normalize(title), requester_id, requester_name, now, length_seconds),
        )
        self.conn.commit()

    def pop_pending(self, guild_id: int, title: str) -> Pending | None:
        row = self.conn.execute(
            "SELECT id, requester_id, requester_name, length_seconds FROM pending_requests"
            " WHERE guild_id = ? AND title_key = ? AND created_at >= ? ORDER BY created_at LIMIT 1",
            (guild_id, normalize(title), time.time() - PENDING_TTL),
        ).fetchone()
        if not row:
            return None
        self.conn.execute("DELETE FROM pending_requests WHERE id = ?", (row["id"],))
        self.conn.commit()
        return Pending(row["requester_id"], row["requester_name"], row["length_seconds"])
