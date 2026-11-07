"""Parser tests. The sample messages are guesses at Jockie-style formats; once you
capture real ones with DEBUG_DUMP_PATH, add them here."""
from jockie_companion.parser import (
    EmbedSnapshot,
    Kind,
    MessageSnapshot,
    normalize,
    parse_message,
    youtube_link,
)


def embed(**kw):
    return MessageSnapshot(embeds=[EmbedSnapshot(**kw)])


def test_started_playing_with_link_and_artist():
    t = parse_message(embed(
        description="<:play:123> Started playing **[Blinding Lights](https://www.youtube.com/watch?v=4NRXx6U8ABQ)** by **The Weeknd**",
    ))
    assert t.kind == Kind.NOW_PLAYING
    assert t.title == "Blinding Lights"
    assert t.artist == "The Weeknd"
    assert t.url == "https://www.youtube.com/watch?v=4NRXx6U8ABQ"


def test_now_playing_title_in_embed_title_with_requester_field():
    t = parse_message(embed(
        author_name="Now Playing",
        title="Never Gonna Give You Up",
        url="https://youtu.be/dQw4w9WgXcQ",
        fields=[("Artist", "Rick Astley"), ("Requested by", "<@111222333>"), ("Duration", "3:33")],
    ))
    assert t.kind == Kind.NOW_PLAYING
    assert t.title == "Never Gonna Give You Up"
    assert t.artist == "Rick Astley"
    assert t.url == "https://youtu.be/dQw4w9WgXcQ"
    assert t.requester_id == 111222333


def test_trigger_only_embed_title_is_skipped():
    t = parse_message(embed(
        title="Now Playing",
        description="**Bohemian Rhapsody** `[5:55]`\nRequested by <@42>",
    ))
    assert t.title == "Bohemian Rhapsody"
    assert t.requester_id == 42
    assert t.artist is None


def test_added_to_queue():
    t = parse_message(embed(description="Added **[Levitating](https://open.spotify.com/track/abc)** to the queue"))
    assert t.kind == Kind.QUEUED
    assert t.title == "Levitating"


def test_requested_by_name_not_mistaken_for_artist():
    t = parse_message(embed(description="Started playing **Song A**\nRequested by **someuser**"))
    assert t.title == "Song A"
    assert t.artist is None
    assert t.requester_name == "someuser"


def test_plain_text_content():
    t = parse_message(MessageSnapshot(content="Now playing: Clocks by Coldplay"))
    assert t.kind == Kind.NOW_PLAYING
    assert t.title == "Clocks"
    assert t.artist == "Coldplay"


def test_playlist_add_ignored():
    assert parse_message(embed(description="Added **25 tracks** from **Chill Mix** to the queue")) is None


def test_unrelated_message_ignored():
    assert parse_message(embed(title="Queue", description="1. Song A\n2. Song B")) is None
    assert parse_message(MessageSnapshot(content="hello")) is None


BLANK = "‎"


def test_jockie_added_track_embed_fields():
    """Real Jockie 'Added Track' message (as pasted by the user), embed-field form."""
    t = parse_message(embed(
        author_name="Added Track",
        fields=[
            ("Track", "[GRAILED](https://open.spotify.com/track/0HfCBwaSDXXHVTNLdGoIRd) by [1nonly](https://open.spotify.com/artist/x)"),
            ("Estimated time until played", "04:04"),
            ("Track Length", "02:21"),
            (BLANK, BLANK),
            ("Position in upcoming", "2"),
            ("Position in queue", "32"),
            (BLANK, BLANK),
        ],
        footer_text="Requested by pandafan42",
    ))
    assert t.kind == Kind.QUEUED
    assert (t.title, t.artist) == ("GRAILED", "1nonly")
    assert t.url == "https://open.spotify.com/track/0HfCBwaSDXXHVTNLdGoIRd"
    assert t.requester_name == "pandafan42"


def test_jockie_added_track_plain_values_and_title_header():
    t = parse_message(embed(
        title="Added Track",
        fields=[("Track", "GRAILED by 1nonly"), ("Track Length", "02:21"), ("Requested by", "<@111222333444>")],
    ))
    assert (t.kind, t.title, t.artist, t.requester_id) == (Kind.QUEUED, "GRAILED", "1nonly", 111222333444)


def test_jockie_style_as_text_components():
    """Same message if Jockie uses the newer text-component layout instead of an embed."""
    t = parse_message(embed(description=(
        "### Added Track\n**Track**\nGRAILED by 1nonly\n**Estimated time until played**\n04:04\n"
        "**Track Length**\n02:21\n-# Requested by pandafan42"
    )))
    assert (t.kind, t.title, t.artist, t.requester_name) == (Kind.QUEUED, "GRAILED", "1nonly", "pandafan42")


def test_jockie_started_playing_real_capture():
    """Captured verbatim from Jockie in the test server (zero-width space included)."""
    t = parse_message(embed(description=(
        "<:deezer:837244412492513312> ​ Started playing "
        "[**Nobody Speak** **by** **DJ Shadow**](https://www.deezer.com/track/1391399512)"
    )))
    assert (t.kind, t.title, t.artist) == (Kind.NOW_PLAYING, "Nobody Speak", "DJ Shadow")
    assert t.url == "https://www.deezer.com/track/1391399512"


def test_jockie_plain_by_inside_link():
    t = parse_message(embed(description="Started playing [Stand by Me by Ben E. King](https://x.test/1)"))
    assert (t.title, t.artist) == ("Stand by Me", "Ben E. King")


def test_jockie_skip_and_empty_queue_messages_ignored():
    """Also captured: these must not be logged as plays."""
    assert parse_message(embed(description=(
        "**[Tom Sawyer by Rush](https://www.deezer.com/track/10303625)** has been skipped by "
        "**<@123456789012345678>** (requester privilege)"))) is None
    assert parse_message(embed(description="There are no more tracks")) is None


def test_jockie_started_playing_linked_title():
    """Real Jockie announcement: ':spotify: Started playing 9MM (KURT COBAIN) by GODHANDUSA',
    with the title hyperlinked to its source."""
    t = parse_message(embed(description=(
        "<:spotify:1234567890> Started playing [9MM (KURT COBAIN)](https://open.spotify.com/track/xyz) by GODHANDUSA"
    )))
    assert (t.kind, t.title, t.artist) == (Kind.NOW_PLAYING, "9MM (KURT COBAIN)", "GODHANDUSA")
    assert t.url == "https://open.spotify.com/track/xyz"


def test_jockie_started_playing_bold_link_and_linked_artist():
    t = parse_message(embed(description=(
        "<:spotify:1> Started playing **[9MM (KURT COBAIN)](https://open.spotify.com/track/xyz)** "
        "by **[GODHANDUSA](https://open.spotify.com/artist/a)**"
    )))
    assert (t.title, t.artist) == ("9MM (KURT COBAIN)", "GODHANDUSA")


def test_jockie_started_playing_plain_message_content():
    t = parse_message(MessageSnapshot(content="<:spotify:1> Started playing 9MM (KURT COBAIN) by GODHANDUSA"))
    assert (t.kind, t.title, t.artist) == (Kind.NOW_PLAYING, "9MM (KURT COBAIN)", "GODHANDUSA")


def test_jockie_style_now_playing():
    t = parse_message(embed(author_name="Now Playing", fields=[("Track", "GRAILED by 1nonly")]))
    assert (t.kind, t.title, t.artist) == (Kind.NOW_PLAYING, "GRAILED", "1nonly")


def test_youtube_link():
    assert youtube_link("X", None, "https://youtu.be/abc") == "https://youtu.be/abc"
    assert youtube_link("Clocks", "Coldplay", "https://open.spotify.com/x") == (
        "https://www.youtube.com/results?search_query=Clocks+Coldplay"
    )


def test_normalize():
    assert normalize("Blinding Lights!") == normalize("blinding   lights")
