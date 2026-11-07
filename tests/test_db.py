from jockie_companion.db import Store
from jockie_companion.parser import Kind, Track


def store():
    return Store(":memory:")


def track(title="Song", artist="Band", url=None):
    return Track(kind=Kind.NOW_PLAYING, title=title, artist=artist, url=url)


def test_record_play_is_idempotent_per_message():
    s = store()
    pid, created = s.record_play(1, 10, 100, track())
    assert created
    assert s.record_play(1, 10, 100, track()) == (pid, False)
    assert s.play_by_message(100).title == "Song"


def test_edited_player_message_records_each_song():
    s = store()
    s.record_play(1, 10, 100, track("First"), at=1)
    s.record_play(1, 10, 100, track("Second"), at=2)
    assert s.play_by_message(100).title == "Second"
    assert len(s.history(1)) == 2


def test_latest_play_per_voice_channel():
    s = store()
    s.record_play(1, 10, 100, track("In VC 1"), voice_channel_id=71, at=1)
    s.record_play(1, 10, 101, track("In VC 2"), voice_channel_id=72, at=2)
    assert s.latest_play(1, 71).title == "In VC 1"
    assert s.latest_play(1).title == "In VC 2"
    assert s.latest_play(1, 99).title == "In VC 2"  # unknown channel falls back to server


def test_announcement_maps_to_play():
    s = store()
    pid, _ = s.record_play(1, 10, 100, track("A"))
    s.add_announcement(555, pid)
    assert s.play_by_message(555).title == "A"


def test_announce_settings_roundtrip():
    s = store()
    assert s.get_announce_settings(1) is None
    s.set_announce_settings(1, "channel", 88)
    s.set_announce_settings(1, "follow", 88)
    assert s.get_announce_settings(1) == ("follow", 88)


def test_latest_and_history_order():
    s = store()
    s.record_play(1, 10, 100, track("A"), at=1)
    s.record_play(1, 10, 101, track("B"), at=2)
    s.record_play(2, 20, 102, track("Other guild"), at=3)
    assert s.latest_play(1).title == "B"
    assert [p.title for p in s.history(1)] == ["B", "A"]


def test_likes_dedupe_across_urls_and_remove():
    s = store()
    p1 = s.play_by_id(s.record_play(1, 10, 100, track(url="https://youtu.be/x"))[0])
    p2 = s.play_by_id(s.record_play(1, 10, 101, track(url="https://open.spotify.com/y"))[0])
    assert s.like_play(7, p1)
    assert not s.like_play(7, p2)  # same song, different source
    assert len(s.likes(1, 7)) == 1
    assert s.remove_like_for_play(7, p2)
    assert s.likes(1, 7) == []


def test_remove_like_at_position():
    s = store()
    s.add_like(1, 7, "First", None, None)
    s.add_like(1, 7, "Second", None, None)
    assert s.remove_like_at(1, 7, 1).title == "Second"  # newest first
    assert s.remove_like_at(1, 7, 5) is None
    assert [l.title for l in s.likes(1, 7)] == ["First"]


def test_top_played_and_liked():
    s = store()
    for i, t in enumerate(["A", "B", "A", "a"]):
        s.record_play(1, 10, 100 + i, track(t, None), at=i)
    assert s.top_played(1)[0] == ("A", None, 3)
    s.add_like(1, 7, "B", None, None)
    s.add_like(1, 8, "B", None, None)
    assert s.top_liked(1) == [("B", None, 2)]


def test_pending_requests_match_by_title():
    s = store()
    s.add_pending(1, "Blinding Lights", 55, None)
    pending = s.pop_pending(1, "blinding lights!")
    assert (pending.requester_id, pending.requester_name) == (55, None)
    assert s.pop_pending(1, "Blinding Lights") is None
