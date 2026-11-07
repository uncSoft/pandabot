"""Stats queries, periods, stat cards and the paged song list."""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jockie_companion.db import Store
from jockie_companion.parser import Kind, Track, parse_duration
from jockie_companion.stats import (SongPager, dj_embed, match_embed, parse_period, rows_from_plays,
                                    stats_embed, top_embed)

NOW = 1_800_000_000.0
DAY = 86400
G = 1
PANDA, PEPPA, LURKER = 7, 8, 9


def track(title, artist):
    return Track(kind=Kind.NOW_PLAYING, title=title, artist=artist)


def seeded():
    """Panda and Peppa request songs over two weeks; people save each other's songs."""
    s = Store(":memory:")
    ids = {}
    plays = [
        ("9MM (KURT COBAIN)", "GODHANDUSA", PANDA, "Panda", NOW - 1 * DAY, 151),
        ("GRAILED", "1nonly", PANDA, "Panda", NOW - 2 * DAY, 141),
        ("ZOMBIE", "GODHANDUSA", PANDA, "Panda", NOW - 3 * DAY, None),
        ("Nobody Speak", "DJ Shadow", PEPPA, "djmoss", NOW - 1 * DAY, 200),
        ("Money For Nothing", "Dire Straits", PEPPA, "djmoss", NOW - 10 * DAY, 300),
    ]
    for i, (t, a, rid, rname, at, length) in enumerate(plays):
        pid, _ = s.record_play(G, 10, 100 + i, track(t, a), rid, rname, voice_channel_id=77, at=at,
                               length_seconds=length)
        ids[t] = pid
    s.add_listeners(ids["9MM (KURT COBAIN)"], [PANDA, PEPPA, LURKER])
    s.add_listeners(ids["Nobody Speak"], [PEPPA, LURKER])
    # Peppa and the lurker love Panda's 9MM; Panda saves Peppa's DJ Shadow and their own GRAILED
    for user in (PEPPA, LURKER):
        s.like_play(user, s.play_by_id(ids["9MM (KURT COBAIN)"]))
    s.like_play(PANDA, s.play_by_id(ids["Nobody Speak"]))
    s.like_play(PANDA, s.play_by_id(ids["GRAILED"]))
    return s, ids


# --- periods ------------------------------------------------------------
def test_parse_period():
    assert parse_period("7d", NOW).since == NOW - 7 * DAY
    assert parse_period("24h", NOW).label == "last 24 hours"
    assert parse_period("1w", NOW).label == "last week"
    assert parse_period("3m", NOW).since == NOW - 90 * DAY  # m = months
    assert parse_period("month", NOW).since == NOW - 30 * DAY
    assert parse_period("all").since == 0
    assert parse_period("today", NOW).since <= NOW
    assert parse_period("panda") is None
    assert parse_period("0d") is None


def test_parse_duration():
    assert parse_duration("02:21") == 141
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("soon") is None


# --- queries ------------------------------------------------------------
def test_user_summary():
    s, _ = seeded()
    week = s.user_summary(G, PANDA, NOW - 7 * DAY)
    assert (week.requests, week.artists) == (3, 2)
    assert (week.known_length_seconds, week.songs_with_length) == (292, 2)
    assert week.top_artists[0] == ("GODHANDUSA", 2)
    assert week.most_loved == ("9MM (KURT COBAIN)", "GODHANDUSA", 2)
    assert week.requests_saved_by_others == 1  # their own save of GRAILED doesn't count
    assert round(week.hit_rate, 2) == 0.33
    assert (week.saves, week.heard) == (0, 1)  # saves are timed by real clock; heard 9MM


def test_requests_heard_and_name_lookup():
    s, _ = seeded()
    assert [p.title for p in s.requests_by(G, PANDA, NOW - 1.5 * DAY)] == ["9MM (KURT COBAIN)"]
    assert [p.title for p in s.heard_by(G, LURKER)] == ["9MM (KURT COBAIN)", "Nobody Speak"]
    assert s.find_requester(G, "panda") == (PANDA, "Panda")
    assert s.find_requester(G, "dj") == (PEPPA, "djmoss")  # prefix match
    assert s.find_requester(G, "nobody") is None


def test_dj_board_counts_saves_by_others():
    s, _ = seeded()
    board = s.dj_board(G)
    assert [(r.user_id, r.loves, r.requests) for r in board] == [(PANDA, 2, 3), (PEPPA, 1, 2)]


def test_taste_match():
    s, _ = seeded()
    m = s.taste_match(G, PANDA, PEPPA)
    assert 0 < m.score <= 100
    assert "GODHANDUSA" in m.shared_artists
    assert s.taste_match(G, PANDA, 12345) is None


def test_old_database_is_upgraded(tmp_path):
    """A database from the first release (no length/listeners) opens and gains the new columns."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE plays (id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            voice_channel_id INTEGER, message_id INTEGER NOT NULL, title TEXT NOT NULL, artist TEXT, url TEXT,
            requester_id INTEGER, requester_name TEXT, played_at REAL NOT NULL, UNIQUE (message_id, title));
        CREATE TABLE pending_requests (id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, title_key TEXT NOT NULL,
            requester_id INTEGER, requester_name TEXT, created_at REAL NOT NULL);
        INSERT INTO plays VALUES (1, 1, 10, 77, 100, 'Old Song', 'Old Artist', NULL, 7, 'Panda', 1000);
    """)
    old.commit()
    old.close()
    s = Store(path)
    assert s.play_by_id(1).length_seconds is None
    s.record_play(G, 10, 101, track("New", "X"), length_seconds=99)
    s.add_listeners(1, [7])
    assert s.user_summary(G, 7).heard == 1


# --- cards --------------------------------------------------------------
def test_stats_card():
    s, _ = seeded()
    e = stats_embed(s, G, PANDA, "Panda", parse_period("7d", NOW))
    assert e.title == "🎧 Panda · last 7 days"
    assert "Requested **3** songs · **2** artists · ~5m of music" in e.description
    assert "Hit rate: **33%**" in e.description
    assert "GODHANDUSA (2)" in e.fields[0].value
    assert "9MM (KURT COBAIN)" in e.fields[1].value
    empty = stats_embed(s, G, 424242, "Nobody", parse_period("7d", NOW))
    assert "Nothing yet" in empty.description


def test_top_dj_match_cards():
    s, _ = seeded()
    top = top_embed(s, G, parse_period("all"))
    assert [f.name for f in top.fields] == ["Most played", "Top artists", "Top requesters", "Most saved"]
    assert "<@7>" in top.fields[2].value
    dj = dj_embed(s, G, parse_period("all"))
    assert dj.description.startswith("🥇 <@7>: ❤️ **2** from 3 requests (67%)")
    m = match_embed(s, G, (PANDA, "Panda"), (PEPPA, "djmoss"))
    assert "%" in m.description and m.fields[0].name == "You both like"


# --- paged list ---------------------------------------------------------
def test_song_pager_pages_and_saves():
    s, _ = seeded()
    plays = s.recent(G) * 5  # 25 rows → 3 pages
    saved = []

    async def on_save(interaction, row):
        saved.append(row.title)

    async def run():
        pager = SongPager("Test", rows_from_plays(plays, True), on_save)
        assert pager.pages == 3
        assert "Page 1/3 · 25 songs" in pager.embed().footer.text
        prev, nxt, select = pager.children
        assert prev.disabled and not nxt.disabled and len(select.options) == 10

        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        await nxt.callback(interaction)
        assert pager.page == 1
        assert pager.embed().description.startswith("`11.`")

        _, _, select = pager.children
        select._values = [select.options[0].value]  # what Discord would send when picking the first option
        await select.callback(SimpleNamespace())
        assert saved == [plays[10].title]

    asyncio.run(run())
