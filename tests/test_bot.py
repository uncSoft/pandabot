"""End-to-end handler tests with fake Discord objects (no network)."""
import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import jockie_companion.bot as botmod
from jockie_companion.bot import CompanionBot, Config
from jockie_companion.db import Store

_ids = itertools.count(10_000)


class FakeUser:
    def __init__(self, uid, name, display_name, bot=False):
        self.id, self.name, self.display_name, self.bot, self.voice = uid, name, display_name, bot, None
        self.dms = []

    async def send(self, **kw):
        m = SimpleNamespace(id=next(_ids), channel=SimpleNamespace(id=500 + self.id), **kw)
        self.dms.append(m)
        return m


JOCKIE = FakeUser(5, "Jockie Music (1)", "Jockie Music (1)", bot=True)
JOCKIE2 = FakeUser(6, "Jockie Music (2)", "Jockie Music (2)", bot=True)
PANDA = FakeUser(7, "pandafan42", "Panda")
LISTENER = FakeUser(8, "listener", "Listener")


class FakeVoice:
    """Stands in for discord.VoiceChannel (its text chat can receive messages)."""
    def __init__(self, cid, name, members=()):
        self.id, self.name = cid, name
        self.voice_states = {m: object() for m in members}
        self.members = [SimpleNamespace(id=m, bot=m in (JOCKIE.id, JOCKIE2.id)) for m in members]
        self.sent = []

    async def send(self, **kw):
        m = SimpleNamespace(id=next(_ids), add_reaction=AsyncMock(), **kw)
        self.sent.append(m)
        return m


def make_guild(*voice_channels, text_channels=()):
    chans = {c.id: c for c in (*voice_channels, *text_channels)}
    return SimpleNamespace(id=1, name="g", voice_channels=list(voice_channels), stage_channels=[],
                           get_channel=chans.get)


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    for var in ("MUSIC_BOT_IDS", "ANNOUNCE_MODE", "ANNOUNCE_CHANNEL_ID", "WATCH_CHANNEL_IDS", "DM_ON_SAVE",
                "STATS_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(botmod, "VoiceLike", (FakeVoice,))
    b = CompanionBot(Config(), Store(":memory:"))
    b._connection.user = SimpleNamespace(id=999)
    for u in (PANDA, LISTENER):
        u.dms.clear()
    b.get_user = {u.id: u for u in (PANDA, LISTENER)}.get
    b.deleted_dms = []
    b.get_partial_messageable = lambda cid: SimpleNamespace(
        get_partial_message=lambda mid: SimpleNamespace(delete=AsyncMock(
            side_effect=lambda: b.deleted_dms.append((cid, mid))))
    )
    return b


def msg(guild, channel, author, content="", embeds=()):
    return SimpleNamespace(
        id=next(_ids), guild=guild, channel=channel, author=author, content=content,
        embeds=list(embeds), components=[], interaction_metadata=None, reference=None,
        add_reaction=AsyncMock(), reply=AsyncMock(), mentions=[],
    )


def now_playing(title, artist):
    e = discord.Embed()
    e.set_author(name="Now Playing")
    e.add_field(name="Track", value=f"{title} by {artist}")
    return e


def added_track(title, artist, requested_by):
    """Shape of the real Jockie 'Added Track' message."""
    e = discord.Embed()
    e.set_author(name="Added Track")
    e.add_field(name="Track", value=f"[{title}](https://open.spotify.com/track/abc) by {artist}")
    e.add_field(name="Estimated time until played", value="04:04")
    e.add_field(name="Track Length", value="02:21")
    e.add_field(name="Position in queue", value="32")
    e.set_footer(text=f"Requested by {requested_by}")
    return e


def heart(user_id, message_id):
    return SimpleNamespace(emoji=SimpleNamespace(name="❤️"), user_id=user_id, member=None, message_id=message_id)


def test_jockie_flow_requester_announce_and_heart(bot):
    text = SimpleNamespace(id=20, name="music")
    vc = FakeVoice(77, "Chill VC", members=[JOCKIE.id, PANDA.id])
    guild = make_guild(vc, text_channels=[text])

    async def run():
        await bot.on_message(msg(guild, text, PANDA, content="M!p https://open.spotify.com/track/abc"))
        await bot.on_message(msg(guild, text, JOCKIE, embeds=[added_track("GRAILED", "1nonly", "pandafan42")]))
        np = msg(guild, text, JOCKIE, embeds=[now_playing("GRAILED", "1nonly")])
        await bot.on_message(np)

        play = bot.store.play_by_message(np.id)
        assert (play.title, play.artist, play.voice_channel_id) == ("GRAILED", "1nonly", 77)
        assert play.requester_id == PANDA.id  # name in Jockie's footer matched the m! command author
        np.add_reaction.assert_awaited_once_with("❤️")

        # Announced in the voice channel's chat, with a heart and a Save button
        [announcement] = vc.sent
        assert announcement.embed.title == "GRAILED"
        announcement.add_reaction.assert_awaited_once_with("❤️")

        await bot.on_raw_reaction_add(heart(8, announcement.id))
        assert [l.title for l in bot.store.likes(1, 8)] == ["GRAILED"]
        await bot.on_raw_reaction_remove(heart(8, announcement.id))
        assert bot.store.likes(1, 8) == []

    asyncio.run(run())


def test_no_duplicate_announcement_when_jockie_posts_in_voice_chat(bot):
    vc = FakeVoice(77, "VC", members=[JOCKIE.id])
    guild = make_guild(vc)
    asyncio.run(bot.on_message(msg(guild, vc, JOCKIE, embeds=[now_playing("A", "B")])))
    assert vc.sent == []


def test_announce_off_and_fixed_channel(bot):
    vc = FakeVoice(77, "VC", members=[JOCKIE.id])
    fixed = FakeVoice(88, "now-playing")
    text = SimpleNamespace(id=20, name="music")
    guild = make_guild(vc, fixed, text_channels=[text])

    async def run():
        bot.store.set_announce_settings(1, "off", None)
        await bot.on_message(msg(guild, text, JOCKIE, embeds=[now_playing("A", "B")]))
        assert vc.sent == [] and fixed.sent == []

        bot.store.set_announce_settings(1, "channel", 88)
        await bot.on_message(msg(guild, text, JOCKIE, embeds=[now_playing("C", "D")]))
        assert vc.sent == [] and [m.embed.title for m in fixed.sent] == ["C"]

    asyncio.run(run())


def test_like_keyword_saves_song_in_that_voice_channel(bot):
    """Two Jockie instances in two voice channels: !like saves the one you're listening to."""
    vc1 = FakeVoice(71, "VC 1", members=[JOCKIE.id])
    vc2 = FakeVoice(72, "VC 2", members=[JOCKIE2.id])
    text = SimpleNamespace(id=20, name="music")
    general = SimpleNamespace(id=21, name="general")
    guild = make_guild(vc1, vc2, text_channels=[text, general])

    async def run():
        await bot.on_message(msg(guild, text, JOCKIE, embeds=[now_playing("Song One", "X")]))
        await bot.on_message(msg(guild, text, JOCKIE2, embeds=[now_playing("Song Two", "Y")]))

        await bot.on_message(msg(guild, vc1, PANDA, content="!like"))
        assert [l.title for l in bot.store.likes(1, PANDA.id)] == ["Song One"]

        # A lone heart in an unrelated channel is ignored
        await bot.on_message(msg(guild, general, PANDA, content="❤️"))
        assert len(bot.store.likes(1, PANDA.id)) == 1

        # ...but works in the channel Jockie posts in (falls back to the latest song)
        await bot.on_message(msg(guild, text, PANDA, content="❤️"))
        assert [l.title for l in bot.store.likes(1, PANDA.id)] == ["Song Two", "Song One"]

    asyncio.run(run())


def started_playing(title, artist):
    """Shape of the real Jockie announcement: ':spotify: Started playing <linked title> by <artist>'."""
    return discord.Embed(description=(
        f"<:spotify:1234> Started playing [{title}](https://open.spotify.com/track/xyz) by {artist}"))


def test_request_in_another_channel_still_credited_and_announced(bot):
    """Someone types m!p in #general; Jockie announces in #music; card goes to Jockie's voice chat."""
    music = SimpleNamespace(id=20, name="music")
    general = SimpleNamespace(id=21, name="general")
    vc = FakeVoice(77, "Hangout", members=[JOCKIE.id, PANDA.id])
    guild = make_guild(vc, text_channels=[music, general])

    async def run():
        await bot.on_message(msg(guild, general, PANDA, content="m!play 9mm kurt cobain"))
        np = msg(guild, music, JOCKIE, embeds=[started_playing("9MM (KURT COBAIN)", "GODHANDUSA")])
        await bot.on_message(np)
        play = bot.store.play_by_message(np.id)
        assert (play.title, play.artist, play.requester_id, play.voice_channel_id) == (
            "9MM (KURT COBAIN)", "GODHANDUSA", PANDA.id, 77)
        assert [m.embed.title for m in vc.sent] == ["9MM (KURT COBAIN)"]

    asyncio.run(run())


def test_non_play_commands_are_not_requests(bot):
    music = SimpleNamespace(id=20, name="music")
    guild = make_guild(text_channels=[music])

    async def run():
        await bot.on_message(msg(guild, music, PANDA, content="m!skip"))
        np = msg(guild, music, JOCKIE, embeds=[started_playing("Next Song", "Someone")])
        await bot.on_message(np)
        assert bot.store.play_by_message(np.id).requester_id is None

    asyncio.run(run())


def test_one_request_credits_one_song(bot):
    """Panda's m!p queues a song; the song that starts right after is someone else's, not Panda's."""
    music = SimpleNamespace(id=20, name="music")
    guild = make_guild(text_channels=[music])

    async def run():
        await bot.on_message(msg(guild, music, PANDA, content="M!p https://open.spotify.com/track/abc"))
        await bot.on_message(msg(guild, music, JOCKIE, embeds=[added_track("GRAILED", "1nonly", "pandafan42")]))
        np = msg(guild, music, JOCKIE, embeds=[started_playing("Older Queued Song", "X")])
        await bot.on_message(np)
        assert bot.store.play_by_message(np.id).requester_id is None
        later = msg(guild, music, JOCKIE, embeds=[started_playing("GRAILED", "1nonly")])
        await bot.on_message(later)
        assert bot.store.play_by_message(later.id).requester_id == PANDA.id

    asyncio.run(run())


def run(bot, coro):
    """Run a handler, then let background work (like DM sending) finish, in one event loop."""
    async def go():
        await coro
        await bot.wait_for_background()
    asyncio.run(go())


def _play_one(bot):
    """Jockie plays a song; returns (guild, voice channel, announcement message)."""
    text = SimpleNamespace(id=20, name="music")
    vc = FakeVoice(77, "VC", members=[JOCKIE.id])
    guild = make_guild(vc, text_channels=[text])
    asyncio.run(bot.on_message(msg(guild, text, JOCKIE, embeds=[now_playing("GRAILED", "1nonly")])))
    return guild, vc, vc.sent[0]


def test_saving_dms_a_card_and_unsaving_deletes_it(bot):
    _, _, announcement = _play_one(bot)

    run(bot, bot.on_raw_reaction_add(heart(LISTENER.id, announcement.id)))
    [card] = LISTENER.dms
    assert card.embed.title == "GRAILED"
    assert card.view.children[0].custom_id.startswith("jc:unsave:")
    like = bot.store.likes(1, LISTENER.id)[0]
    assert (like.dm_channel_id, like.dm_message_id) == (card.channel.id, card.id)

    # Hearting the same song again doesn't send a second card
    run(bot, bot.on_raw_reaction_add(heart(LISTENER.id, announcement.id)))
    assert len(LISTENER.dms) == 1

    run(bot, bot.on_raw_reaction_remove(heart(LISTENER.id, announcement.id)))
    assert bot.store.likes(1, LISTENER.id) == []
    assert bot.deleted_dms == [(card.channel.id, card.id)]


def test_remove_button_on_dm_card(bot):
    _, _, announcement = _play_one(bot)
    run(bot, bot.on_raw_reaction_add(heart(LISTENER.id, announcement.id)))
    [card] = LISTENER.dms

    def click(user):
        return SimpleNamespace(
            type=discord.InteractionType.component, data={"custom_id": card.view.children[0].custom_id},
            user=user, message=SimpleNamespace(delete=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()))

    intruder = click(PANDA)
    run(bot, bot.on_interaction(intruder))
    assert len(bot.store.likes(1, LISTENER.id)) == 1  # someone else can't remove it
    intruder.message.delete.assert_not_awaited()

    mine = click(LISTENER)
    run(bot, bot.on_interaction(mine))
    assert bot.store.likes(1, LISTENER.id) == []
    mine.message.delete.assert_awaited_once()


def test_dms_can_be_turned_off(bot):
    _, _, announcement = _play_one(bot)
    bot.store.set_dm_pref(LISTENER.id, False)
    run(bot, bot.on_raw_reaction_add(heart(LISTENER.id, announcement.id)))
    assert LISTENER.dms == []
    assert len(bot.store.likes(1, LISTENER.id)) == 1


def test_ignores_other_bots(bot):
    other = SimpleNamespace(bot=True, id=9, name="SomeOtherBot", display_name="x")
    guild = make_guild()
    m = msg(guild, SimpleNamespace(id=20), other, embeds=[now_playing("X", "Y")])
    asyncio.run(bot.on_message(m))
    assert bot.store.play_by_message(m.id) is None


def test_setup_problems_reports_hidden_and_voice_channels():
    from jockie_companion.bot import setup_problems

    def perms(**granted):
        names = ("view_channel", "read_message_history", "send_messages", "add_reactions", "embed_links")
        return SimpleNamespace(**{n: granted.get(n, True) for n in names})

    def chan(name, p):
        return SimpleNamespace(name=name, permissions_for=lambda me: p)

    guild = SimpleNamespace(
        text_channels=[chan("music", perms()), chan("secret", perms(view_channel=False)),
                       chan("general", perms(add_reactions=False))],
        voice_channels=[chan("Hangout", perms(send_messages=False))], stage_channels=[])
    problems = setup_problems(guild, me=None)
    assert any("#secret" in p for p in problems)
    assert any("#general: missing add_reactions" in p for p in problems)
    assert any("Hangout" in p and "send_messages" in p for p in problems)
    assert not any("#music" in p for p in problems)


def test_save_button_answers_discord_before_sending_dm(bot):
    """Discord requires a reply within 3s; the DM must not hold it up (was '404 Unknown interaction')."""
    _, _, announcement = _play_one(bot)
    order = []
    original_send = LISTENER.send

    async def slow_send(**kw):
        order.append("dm")
        return await original_send(**kw)

    LISTENER.send = slow_send
    try:
        response = SimpleNamespace(send_message=AsyncMock(side_effect=lambda *a, **k: order.append("reply")))
        click = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": announcement.view.children[0].custom_id},
            user=LISTENER, response=response)
        run(bot, bot.on_interaction(click))
    finally:
        del LISTENER.send
    assert order == ["reply", "dm"]
    assert len(LISTENER.dms) == 1



# --- stats & p! commands ------------------------------------------------
def _session(bot):
    """Panda requests two songs while Panda and Listener are in voice; Listener hearts one."""
    text = SimpleNamespace(id=20, name="music")
    vc = FakeVoice(77, "VC", members=[JOCKIE.id, PANDA.id, LISTENER.id])
    guild = make_guild(vc, text_channels=[text])

    async def go():
        for title, artist in (("9MM (KURT COBAIN)", "GODHANDUSA"), ("GRAILED", "1nonly")):
            await bot.on_message(msg(guild, text, PANDA, content="m!p " + title))
            await bot.on_message(msg(guild, text, JOCKIE, embeds=[started_playing(title, artist)]))
        await bot.on_raw_reaction_add(heart(LISTENER.id, vc.sent[0].id))
        await bot.wait_for_background()
    asyncio.run(go())
    return guild, text


def command(bot, guild, channel, author, content, mentions=()):
    m = msg(guild, channel, author, content=content)
    m.mentions = list(mentions)
    m.reply = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    asyncio.run(bot.on_message(m))
    return m.reply


def test_listeners_are_recorded_without_bots(bot):
    _session(bot)
    heard = bot.store.heard_by(1, LISTENER.id)
    assert [p.title for p in heard] == ["GRAILED", "9MM (KURT COBAIN)"]
    assert bot.store.heard_by(1, JOCKIE.id) == []


def test_p_stats_for_self_and_mention(bot):
    guild, text = _session(bot)
    reply = command(bot, guild, text, PANDA, "p!stats")
    embed = reply.call_args.kwargs["embed"]
    assert embed.title == "🎧 Panda · last 30 days"
    assert "Requested **2** songs" in embed.description and "Hit rate: **50%**" in embed.description

    reply = command(bot, guild, text, LISTENER, "p!stats <@7> 7d", mentions=[PANDA])
    assert reply.call_args.kwargs["embed"].title == "🎧 Panda · last 7 days"


def test_p_name_shortcut_lists_their_requests(bot):
    guild, text = _session(bot)
    reply = command(bot, guild, text, LISTENER, "p!panda 7d")
    kw = reply.call_args.kwargs
    assert kw["embed"].title == "🎵 Requested by Panda · last 7 days"
    assert "GRAILED" in kw["embed"].description and "9MM" in kw["embed"].description
    assert isinstance(kw["view"], botmod.SongPager)


def test_p_commands_render(bot):
    guild, text = _session(bot)
    for content, expected in [
        ("p!help", "PandaBot commands"),
        ("P!TOP all", "Server charts"),
        ("p!dj", "DJ leaderboard"),
        ("p!heard", "Heard by Listener"),
        ("p!saved", "Saved by Listener"),
        ("p!history", "Played in g"),
        ("p!match <@7>", "Listener × Panda"),
        ("p!throwback", "Throwback"),
    ]:
        reply = command(bot, guild, text, LISTENER, content, mentions=[PANDA] if "<@7>" in content else [])
        embed = reply.call_args.kwargs["embed"]
        assert expected in (embed.title or "") + (embed.author.name or ""), content


def test_p_unknown_name_and_command(bot):
    guild, text = _session(bot)
    reply = command(bot, guild, text, PANDA, "p!stats nobodyhere")
    assert "don't know anyone called **nobodyhere**" in reply.call_args.args[0]
    reply = command(bot, guild, text, PANDA, "p!zzz")
    assert "Unknown command" in reply.call_args.args[0]


def test_backfill_reads_old_messages(bot):
    from datetime import datetime, timezone

    me = SimpleNamespace(id=999)
    perms = SimpleNamespace(view_channel=True, read_message_history=True)

    def old(author, minutes_ago, content="", embeds=()):
        m = msg(None, None, author, content, embeds)
        m.created_at = datetime.fromtimestamp(1_700_000_000 - minutes_ago * 60, tz=timezone.utc)
        return m

    history = [  # newest first, as Discord returns it
        old(JOCKIE, 1, embeds=[started_playing("Older Song", "Someone")]),
        old(JOCKIE, 5, embeds=[started_playing("GRAILED", "1nonly")]),
        old(PANDA, 5.2, content="m!p grailed"),
        old(PANDA, 9, content="just chatting"),
    ]

    class Channel:
        id, name = 30, "old-music"
        def permissions_for(self, member):
            return perms
        def history(self, limit):
            async def gen():
                for m in history[:limit]:
                    yield m
            return gen()

    channel = Channel()
    guild = SimpleNamespace(id=1, name="g", me=me, text_channels=[channel], voice_channels=[],
                            stage_channels=[], get_channel=lambda cid: None)
    for m in history:
        m.guild, m.channel = guild, channel

    channels, added = asyncio.run(bot.backfill(guild))
    assert (channels, added) == (1, 2)
    plays = bot.store.history(1)
    assert [(p.title, p.requester_id) for p in plays] == [("Older Song", None), ("GRAILED", PANDA.id)]
    assert plays[1].played_at == 1_700_000_000 - 300  # message time, not "now"
    for m in history:
        m.add_reaction.assert_not_awaited()  # no reactions or announcements on old messages

    assert asyncio.run(bot.backfill(guild)) == (1, 0)  # running it again adds nothing


def test_commands_register_in_every_server_and_on_join(bot, monkeypatch):
    from unittest.mock import Mock

    def server(gid, name):
        return SimpleNamespace(id=gid, name=name, me=None, text_channels=[], voice_channels=[], stage_channels=[])

    test_server, real_server = server(1, "Test Server"), server(2, "//")
    monkeypatch.setattr(CompanionBot, "guilds", property(lambda self: [test_server, real_server]))
    bot.tree.sync = AsyncMock()
    bot.tree.copy_global_to = Mock()

    asyncio.run(bot.on_ready())
    assert [c.kwargs["guild"].id for c in bot.tree.sync.call_args_list] == [1, 2]

    asyncio.run(bot.on_ready())  # reconnect: no re-registration
    assert bot.tree.sync.await_count == 2

    newcomer = server(3, "Someone else's server")
    asyncio.run(bot.on_guild_join(newcomer))
    assert bot.tree.sync.call_args.kwargs["guild"] is newcomer

    bot.tree.sync = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
    assert asyncio.run(bot.register_commands_in(server(4, "x"))) is False  # logged, not raised
