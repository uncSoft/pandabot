#!/usr/bin/env python3
"""Screenshots of every PandaBot command, for the website, the App Directory and the support server.

Each command runs through PandaBot's real handlers against a made-up demo server (no token, no network,
no real people). The replies are drawn the way Discord shows them and photographed with Chrome, so the
pictures stay true to what the bot actually says. Re-run it whenever a reply changes.

    pip install -r requirements.txt playwright      # uses your installed Google Chrome
    python tools/screenshots.py                     # → site/img/commands/*.png and site/commands.html
"""
from __future__ import annotations

import asyncio
import base64
import html
import itertools
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Defaults only: nothing connects to Discord, and server-specific settings would change the replies.
os.environ["DISCORD_TOKEN"] = "demo"
for _var in ("MUSIC_BOT_IDS", "ANNOUNCE_MODE", "ANNOUNCE_CHANNEL_ID", "WATCH_CHANNEL_IDS", "DM_ON_SAVE",
             "STATS_PREFIX", "SUPPORT_URL", "SUPPORT_SERVER_URL", "MUSIC_COMMAND_PREFIXES", "LIKE_KEYWORDS"):
    os.environ.pop(_var, None)

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

import jockie_companion.bot as botmod  # noqa: E402
import jockie_companion.db as dbmod  # noqa: E402
from jockie_companion.bot import CompanionBot, Config  # noqa: E402
from jockie_companion.db import Store  # noqa: E402
from jockie_companion.parser import Kind, Track  # noqa: E402

OUT = ROOT / "site" / "img" / "commands"
GALLERY = ROOT / "site" / "commands.html"
AVATAR = ROOT / "site" / "img" / "pandabot.png"
NOW = time.time()
DAY = 86400
IDS = itertools.count(100_000)


# --- the demo server -----------------------------------------------------------
class Person:
    def __init__(self, uid: int, name: str, colour: str, bot: bool = False, display: str | None = None):
        self.id, self.name, self.colour, self.bot = uid, name, colour, bot
        self.display_name = display or name
        self.voice = None
        self.dms: list[dict] = []
        self.guild_permissions = SimpleNamespace(manage_guild=True)

    def __str__(self) -> str:
        return self.name

    async def send(self, content=None, **kw):  # a DM from PandaBot
        self.dms.append({"content": content, **kw})
        return SimpleNamespace(id=next(IDS), channel=SimpleNamespace(id=900), delete=AsyncMock())


ALLOWED = SimpleNamespace(view_channel=True, read_message_history=True, send_messages=True,
                          add_reactions=True, embed_links=True)


class Channel:
    def __init__(self, cid: int, name: str, members=()):
        self.id, self.name, self.members = cid, name, list(members)
        self.voice_states = {m.id: object() for m in members}
        self.sent: list[dict] = []

    def permissions_for(self, _member):
        return ALLOWED

    async def send(self, content=None, **kw):
        entry = {"content": content, **kw}
        self.sent.append(entry)

        async def edit(**changes):
            entry.update(changes, edited=True)
        return SimpleNamespace(id=next(IDS), edit=edit)


class Voice(Channel):
    """A voice channel (its text chat can receive messages)."""


PANDA = Person(7, "Panda", "#f47fff")
MOSS = Person(8, "djmoss", "#3ba55c")
JUNI = Person(9, "Juniper", "#faa61a")
KAI = Person(10, "Kai", "#00b0f4")
ROSA = Person(11, "Rosa", "#ed4245")
PEOPLE = [PANDA, MOSS, JUNI, KAI, ROSA]
JOCKIE = Person(5, "Jockie Music", "#ffffff", bot=True)
TUNES = Person(6, "Tunes", "#ffffff", bot=True)
PANDABOT = Person(999, "PandaBot", "#ffffff", bot=True)
EVERYONE = {p.id: p for p in (*PEOPLE, JOCKIE, TUNES, PANDABOT)}

MUSIC = Channel(20, "music")
GENERAL = Channel(21, "general")
CHILL = Voice(77, "Chill VC", members=[JOCKIE, PANDA, JUNI, KAI, ROSA])
GAMING = Voice(78, "Gaming", members=[])
CHANNELS = {c.id: c for c in (MUSIC, GENERAL, CHILL, GAMING)}
GUILD = SimpleNamespace(id=1, name="Lo-Fi Lounge", text_channels=[MUSIC, GENERAL], voice_channels=[CHILL, GAMING],
                        stage_channels=[], me=PANDABOT, get_channel=CHANNELS.get)

# The song that plays "live" in the screenshots (Midnight City) is kept out of the history on purpose,
# so everyone who saves it gets "Saved" rather than "Already in your list".
SONGS = [  # (title, artist, seconds)
    ("Wait", "M83", 343), ("Outro", "M83", 248), ("Redbone", "Childish Gambino", 327),
    ("Blinding Lights", "The Weeknd", 200), ("Starboy", "The Weeknd", 230), ("Nights", "Frank Ocean", 307),
    ("Pink + White", "Frank Ocean", 184), ("Ivy", "Frank Ocean", 249), ("Self Control", "Frank Ocean", 249),
    ("Electric Feel", "MGMT", 229), ("Kids", "MGMT", 302), ("Little Dark Age", "MGMT", 299),
    ("The Less I Know the Better", "Tame Impala", 216), ("Let It Happen", "Tame Impala", 467),
    ("Feels Like We Only Go Backwards", "Tame Impala", 192), ("Dreams", "Fleetwood Mac", 257),
    ("Everywhere", "Fleetwood Mac", 228), ("Nobody Speak", "DJ Shadow", 200), ("Get Lucky", "Daft Punk", 369),
    ("Instant Crush", "Daft Punk", 337), ("Digital Love", "Daft Punk", 301), ("Space Song", "Beach House", 320),
    ("Myth", "Beach House", 258), ("Everybody Wants to Rule the World", "Tears for Fears", 251),
]
FAVOURITES = {
    PANDA.id: {"Frank Ocean", "Tame Impala", "MGMT"},
    MOSS.id: {"Daft Punk", "DJ Shadow", "M83"},
    JUNI.id: {"Beach House", "Fleetwood Mac", "Frank Ocean"},
    KAI.id: {"The Weeknd", "Daft Punk", "Childish Gambino"},
    ROSA.id: {"Tears for Fears", "Fleetwood Mac", "Tame Impala"},
}


def url_for(title: str) -> str:
    return "https://open.spotify.com/track/demo" + re.sub(r"\W+", "", title.lower())


def make_bot() -> CompanionBot:
    botmod.VoiceLike = (Voice,)
    bot = CompanionBot(Config(), Store(":memory:"))
    bot._connection.user = SimpleNamespace(id=PANDABOT.id)
    bot.get_user = EVERYONE.get
    bot.get_guild = lambda _gid: GUILD
    bot.get_partial_messageable = lambda cid: SimpleNamespace(
        send=AsyncMock(), get_partial_message=lambda mid: SimpleNamespace(delete=AsyncMock()))
    return bot


def seed(store: Store) -> None:
    """A month of listening in the demo server: requests, who was in voice, and saves."""
    clock = [NOW]
    real_time, dbmod.time = dbmod.time, SimpleNamespace(time=lambda: clock[0])
    rng = random.Random(42)
    weights = [5, 4, 3, 2, 2]
    times = sorted([NOW - rng.uniform(0.05, 0.9) * DAY for _ in range(14)] +
                   [NOW - rng.uniform(1, 29) * DAY for _ in range(56)])
    for i, at in enumerate(times):
        requester = rng.choices(PEOPLE, weights)[0]
        liked_songs = [s for s in SONGS if s[1] in FAVOURITES[requester.id]]
        title, artist, length = rng.choice(liked_songs if rng.random() < 0.75 else SONGS)
        play_id, _ = store.record_play(GUILD.id, MUSIC.id, 50_000 + i,
                                       Track(Kind.NOW_PLAYING, title, artist, url_for(title)),
                                       requester.id, requester.display_name, voice_channel_id=CHILL.id,
                                       at=at, length_seconds=length)
        listeners = [requester, *rng.sample([p for p in PEOPLE if p is not requester], rng.randint(1, 3))]
        if at > NOW - DAY and PANDA not in listeners:
            listeners.append(PANDA)  # Panda was around last night, for /heard
        store.add_listeners(play_id, [p.id for p in listeners])
        play = store.play_by_id(play_id)
        for p in listeners:
            if rng.random() < (0.55 if artist in FAVOURITES[p.id] else 0.12):
                clock[0] = at + rng.uniform(30, 600)
                store.like_play(p.id, play)
    dbmod.time = real_time


# --- running commands ------------------------------------------------------------
def interaction(user: Person, channel: Channel, sink: list, data: dict | None = None):
    async def send_message(content=None, **kw):
        sink.append({"content": content, **kw})

    async def edit_message(**kw):
        sink.append({"edited": True, **kw})

    async def followup(content=None, **kw):
        sink.append({"content": content, **kw})

    return SimpleNamespace(
        user=user, guild=GUILD, guild_id=GUILD.id, channel=channel, type=discord.InteractionType.component,
        data=data or {}, message=None, followup=SimpleNamespace(send=followup),
        response=SimpleNamespace(send_message=send_message, edit_message=edit_message, defer=AsyncMock()),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))


def message(author: Person, channel: Channel, content: str = "", embeds=(), sink: list | None = None, mentions=()):
    async def reply(content=None, **kw):
        entry = {"content": content, **kw}
        if sink is not None:
            sink.append(entry)

        async def edit(**changes):
            entry.update(changes, edited=True)
        return SimpleNamespace(id=next(IDS), edit=edit)

    return SimpleNamespace(id=next(IDS), guild=GUILD, channel=channel, author=author, content=content,
                           embeds=list(embeds), components=[], interaction_metadata=None, reference=None,
                           mentions=list(mentions), reply=reply, add_reaction=AsyncMock())


def choice(value: str) -> app_commands.Choice:
    return app_commands.Choice(name=value, value=value)


# --- what a screenshot contains ----------------------------------------------------
@dataclass
class Msg:
    author: Person
    content: str = ""
    embeds: list = field(default_factory=list)
    view: object = None
    ephemeral: bool = False
    files: list = field(default_factory=list)
    used: str | None = None              # "/stats" → "Panda used /stats" above the reply
    used_by: Person | None = None
    reply_to: tuple | None = None        # (person, text) of the message being answered
    reactions: list = field(default_factory=list)  # (emoji, count, mine)
    edited: bool = False


@dataclass
class Scene:
    slug: str
    group: str
    command: str
    blurb: str
    channel: str
    messages: list
    overlay: str = ""
    width: int = 0
    height: int = 0


def from_reply(entry: dict, used: str | None = None, used_by: Person | None = None,
               reply_to: tuple | None = None) -> Msg:
    view = entry.get("view")
    if view is discord.utils.MISSING:
        view = None
    embeds = [entry["embed"]] if entry.get("embed") else list(entry.get("embeds") or [])
    file = entry.get("file")
    return Msg(PANDABOT, entry.get("content") or "", embeds, view, bool(entry.get("ephemeral")),
               [file.filename] if file else [], used, used_by, reply_to, edited=bool(entry.get("edited")))


async def build_scenes() -> list[Scene]:
    bot = make_bot()
    seed(bot.store)
    scenes: list[Scene] = []

    def add(slug, group, command, blurb, channel, messages, overlay=""):
        scenes.append(Scene(slug, group, command, blurb, channel, messages, overlay))

    async def slash(user, name, *args, sub=None, channel=MUSIC, label=None, **kwargs) -> Msg:
        cmd = bot.tree.get_command(name)
        if sub:
            cmd = cmd.get_command(sub)
        sink: list = []
        await cmd.callback(interaction(user, channel, sink), *args, **kwargs)
        await bot.wait_for_background()
        return from_reply(sink[0], used=label or f"/{name}" + (f" {sub}" if sub else ""), used_by=user)

    async def typed(user, text, channel=MUSIC, mentions=()) -> list[Msg]:
        sink: list = []
        await bot.on_message(message(user, channel, text, sink=sink, mentions=mentions))
        await bot.wait_for_background()
        return [Msg(user, text), *(from_reply(e, reply_to=(user, text)) for e in sink)]

    # A song starts: Panda asks Jockie in #music, Jockie announces it, PandaBot posts the card in voice chat.
    jockie_text = ("<:spotify:1> ​ Started playing "
                   f"[**Midnight City** **by** **M83**]({url_for('Midnight City')})")
    await bot.on_message(message(PANDA, MUSIC, "m!p midnight city m83"))
    jockie_msg = message(JOCKIE, MUSIC, embeds=[discord.Embed(description=jockie_text)])
    await bot.on_message(jockie_msg)
    play = bot.store.play_by_message(jockie_msg.id)
    jockie = Msg(JOCKIE, embeds=[discord.Embed(description=jockie_text)], reactions=[("❤️", 3, False)])

    add("now-playing", "Saving songs", "Now-playing cards",
        "When your music bot starts a song, PandaBot posts a card in that voice channel's chat with who "
        "requested it and a YouTube link.", "🔊 Chill VC", [from_reply(CHILL.sent[0])])

    # Hearts on the music bot's own message
    await bot.on_raw_reaction_add(SimpleNamespace(emoji=SimpleNamespace(name="❤️"), user_id=JUNI.id, member=None,
                                                  message_id=jockie_msg.id, channel_id=MUSIC.id))
    await bot.wait_for_background()
    add("heart", "Saving songs", "❤️ to save",
        "PandaBot adds a ❤️ under each song your music bot posts. Tap it to save the song.", "# music", [jockie])

    # "!like" in the voice chat
    add("like", "Saving songs", "!like",
        "Type <code>!like</code> (or just ❤️ in a music channel) to save whatever is playing.", "🔊 Chill VC",
        await typed(KAI, "!like", channel=CHILL))

    # The Save button → DM card
    sink: list = []
    await bot.on_interaction(interaction(PANDA, CHILL, sink, {"custom_id": f"{botmod.SAVE_PREFIX}{play.id}"}))
    await bot.wait_for_background()
    add("dm-card", "Saving songs", "Your saved songs, in your DMs",
        "Every song you save arrives in your DMs with its links, so your DMs become a running playlist. "
        "<b>Remove from list</b> takes it back out.", "@ PandaBot", [from_reply(PANDA.dms[-1])])

    add("save", "Saving songs", "/save",
        "The slash-command version of <code>!like</code>: saves the song that's playing.", "# music",
        [await slash(ROSA, "save")])

    sink = []
    await bot.tree.get_command("Save song", type=discord.AppCommandType.message).callback(
        interaction(MOSS, MUSIC, sink), jockie_msg)
    await bot.wait_for_background()
    menu = ('<div class="menu"><div class="item">Reply</div><div class="item">Copy Text</div>'
            '<div class="item sel">Apps <span>›</span></div><div class="submenu"><div class="item sel">'
            f'<img src="{{avatar}}"> Save song</div></div></div>')
    add("save-song-menu", "Saving songs", "Apps → Save song",
        "Right-click any music-bot message, even an old one, and pick <b>Apps → Save song</b>.", "# music",
        [jockie, from_reply(sink[0], used="Save song", used_by=MOSS)], overlay=menu)

    add("likes", "Saving songs", "p!saved  ·  /likes",
        "Page through your saved songs (or someone else's: <code>p!saved @name</code>), and save from their "
        "list straight into yours.", "# music", await typed(PANDA, "p!saved"))

    add("unlike", "Saving songs", "/unlike",
        "Remove a song by its number in <code>p!saved</code>. Its DM card disappears too.", "# music",
        [await slash(PANDA, "unlike", number=3)])
    add("export", "Saving songs", "/export",
        "Download your saved songs as a spreadsheet (CSV) with Spotify and YouTube links.", "# music",
        [await slash(PANDA, "export")])
    add("dms", "Saving songs", "/dms",
        "Turn the DM cards on or off for yourself.", "# music", [await slash(PANDA, "dms", enabled=True)])

    # Stats
    add("stats", "Stats", "p!stats  ·  /stats",
        "Someone's music profile: requests, top artists, how many saves, and how often other people "
        "save the songs they pick. Add <code>@name</code> or a period like <code>7d</code>.", "# music",
        await typed(PANDA, "p!stats"))
    add("played", "Stats", "p!played  ·  /played",
        "Everything someone requested in a time period, with paging and a menu to save any of it.",
        "# music", await typed(PANDA, f"p!played <@{JUNI.id}> 30d", mentions=[JUNI]))
    add("name-shortcut", "Stats", "p!<name>",
        "Type someone's name as the command, like <code>p!juniper 30d</code>, to see what they played.",
        "# music", await typed(KAI, "p!juniper 30d"))
    add("heard", "Stats", "p!heard  ·  /heard",
        "\"What was that song?\" Everything that played while you were in voice.", "# music",
        await typed(PANDA, "p!heard"))
    add("history", "Stats", "p!history  ·  /history",
        "Everything played in the server, newest first, with who asked for it.", "# music",
        await typed(PANDA, "p!history"))
    add("top", "Stats", "p!top  ·  /top",
        "Server charts: most played songs, top artists, top requesters and the most-saved songs.", "# music",
        await typed(PANDA, "p!top 30d"))
    add("dj", "Stats", "p!dj  ·  /dj",
        "Whose requests other people save the most.", "# music", await typed(PANDA, "p!dj"))
    add("match", "Stats", "p!match  ·  /match",
        "How well two people's taste lines up, and the artists and songs you share. "
        "<code>p!match @a @b</code> compares two other people.", "# music",
        await typed(PANDA, f"p!match <@{JUNI.id}>", mentions=[JUNI]))
    add("throwback", "Stats", "p!throwback  ·  /throwback",
        "A random song from the server's history, with a Save button.", "# music", await typed(KAI, "p!throwback"))
    add("nowplaying", "Stats", "/nowplaying",
        "Show the song that's playing in your voice channel, with a Save button.", "🔊 Chill VC",
        [await slash(JUNI, "nowplaying", channel=CHILL)])

    # Server setup (Manage Server)
    GUILD.system_channel = GENERAL
    await bot.on_guild_join(GUILD)
    add("welcome", "Server setup", "Welcome post",
        "When PandaBot joins a server it posts this once: there's nothing to set up for Jockie.",
        "# general", [from_reply(GENERAL.sent[-1])])
    add("announce", "Server setup", "/announce",
        "Choose where song cards go: the voice chat the music bot is in, one fixed channel, or nowhere.",
        "# music", [await slash(PANDA, "announce", mode=choice("follow"), channel=MUSIC)])
    add("musicbot-add", "Server setup", "/musicbot add",
        "Jockie Music works out of the box. Follow other music bots, and their text-command prefix, here.",
        "# music", [await slash(PANDA, "musicbot", sub="add", music_bot=TUNES, prefix="!")])
    add("musicbot-list", "Server setup", "/musicbot list",
        "The music bots PandaBot follows in this server.", "# music",
        [await slash(PANDA, "musicbot", sub="list")])
    add("musicbot-check", "Server setup", "/musicbot check",
        "See exactly how PandaBot read a music bot's last few messages, to check a new bot works.",
        "# music", [await slash(PANDA, "musicbot", sub="check")])
    add("setupcheck", "Server setup", "/setupcheck",
        "Finds channels PandaBot can't see or post in, since missing permissions otherwise fail silently.",
        "# music", [await slash(PANDA, "setupcheck")])
    bot.backfill = AsyncMock(return_value=(6, 412))
    add("backfill", "Server setup", "p!backfill  ·  /backfill",
        "Reads back through old messages so the server's past songs show up in stats from day one.",
        "# music", await typed(PANDA, "p!backfill 10000"))

    # Everything else
    add("help", "Everything else", "p!help",
        "Every text command on one card. They all exist as slash commands too.", "# music",
        await typed(ROSA, "p!help"))
    add("invite", "Everything else", "p!invite  ·  /invite",
        "A link to add PandaBot to another server.", "# music", await typed(KAI, "p!invite"))
    add("support", "Everything else", "p!support  ·  /support",
        "PandaBot is free. If you'd like to help with the hosting bill, there's a tip jar.", "# music",
        await typed(KAI, "p!support"))
    add("forgetme", "Everything else", "/forgetme",
        "Deletes everything PandaBot stores about you, in every server, after you confirm.", "# music",
        [await slash(MOSS, "forgetme")])
    return scenes


# --- drawing it the way Discord does ---------------------------------------------------
def fmt_ts(ts: int, style: str) -> str:
    t = time.localtime(ts)
    if style == "t":
        return time.strftime("%-I:%M %p", t)
    if style == "R":
        d = NOW - ts
        for limit, size, unit in ((60, 1, "second"), (3600, 60, "minute"), (DAY, 3600, "hour"),
                                  (30 * DAY, DAY, "day"), (365 * DAY, 30 * DAY, "month")):
            if d < limit:
                n = max(1, round(d / size))
                if unit == "second":
                    return "a few seconds ago"
                return f"{'a' if n == 1 else n} {unit}{'s' * (n != 1)} ago".replace("a hour", "an hour")
        return f"{round(d / 365 / DAY)} years ago"
    return time.strftime("%B %-d, %Y %-I:%M %p", t)


def name_of(uid: int) -> str:
    return EVERYONE[uid].display_name if uid in EVERYONE else "someone"


def channel_label(cid: int) -> str:
    c = CHANNELS.get(cid)
    if not c:
        return "#unknown"
    return f"🔊 {c.name}" if isinstance(c, Voice) else f"#{c.name}"


def md(text: str) -> str:
    """The bits of Discord markdown PandaBot uses, as HTML."""
    stash: list[str] = []

    def keep(s: str) -> str:
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    def inline(t: str) -> str:
        t = html.escape(t, quote=False)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)

    t = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    t = re.sub(r"<t:(\d+)(?::([tTdDfFR]))?>",
               lambda m: keep(f'<span class="ts">{fmt_ts(int(m.group(1)), m.group(2) or "f")}</span>'), t)
    t = re.sub(r"<@!?(\d+)>", lambda m: keep(f'<span class="mention">@{html.escape(name_of(int(m.group(1))))}</span>'), t)
    t = re.sub(r"<#(\d+)>", lambda m: keep(f'<span class="mention">{channel_label(int(m.group(1)))}</span>'), t)
    t = re.sub(r"<a?:(\w+):\d+>", lambda m: keep(f'<span class="cemoji {m.group(1)}"></span>'), t)
    t = re.sub(r"\[([^\]\n]+)\]\(<?(https?://[^\s)>]+)>?\)", lambda m: keep(f"<a>{inline(m.group(1))}</a>"), t)
    t = re.sub(r"<(https?://[^\s>]+)>", lambda m: keep(f"<a>{html.escape(m.group(1))}</a>"), t)
    t = re.sub(r"https?://[^\s<>\x00]+", lambda m: keep(f"<a>{html.escape(m.group(0))}</a>"), t)
    lines = []
    for line in inline(t).split("\n"):
        if line.startswith("-# "):
            line = f'<span class="sub">{line[3:]}</span>'
        lines.append(line)
    out = "<br>".join(lines)
    for _ in range(4):
        out = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)
    return out


def colour_of(e: discord.Embed) -> str:
    return f"#{e.colour.value:06x}" if e.colour else "#1e1f22"


def render_embed(e: discord.Embed) -> str:
    parts = []
    if e.author and e.author.name:
        parts.append(f'<div class="e-author">{md(e.author.name)}</div>')
    if e.title:
        parts.append(f'<div class="e-title{" link" if e.url else ""}">{md(e.title)}</div>')
    if e.description:
        parts.append(f'<div class="e-desc">{md(e.description)}</div>')
    if e.fields:
        rows, current = [], []
        for f in e.fields:
            if f.inline and len(current) < 3:
                current.append(f)
                continue
            if current:
                rows.append(current)
            current = [f] if f.inline else []
            if not f.inline:
                rows.append([f])
        if current:
            rows.append(current)
        cells = []
        for row in rows:
            span = 12 // len(row) if all(f.inline for f in row) else 12
            cells += [f'<div class="field" style="grid-column: span {span}"><div class="f-name">{md(f.name)}</div>'
                      f'<div class="f-value">{md(f.value)}</div></div>' for f in row]
        parts.append(f'<div class="e-fields">{"".join(cells)}</div>')
    footer = [md(e.footer.text)] if e.footer and e.footer.text else []
    if e.timestamp:
        t = time.localtime(e.timestamp.timestamp())
        today = time.localtime(NOW)[:3] == t[:3]
        footer.append(time.strftime("Today at %-I:%M %p" if today else "%-m/%-d/%Y %-I:%M %p", t))
    if footer:
        parts.append(f'<div class="e-footer">{" • ".join(footer)}</div>')
    return f'<div class="embed" style="border-color:{colour_of(e)}">{"".join(parts)}</div>'


BUTTON_STYLES = {1: "primary", 2: "secondary", 3: "success", 4: "danger", 5: "secondary"}


def render_components(view) -> str:
    if view is None:
        return ""
    rows = []
    for row in view.to_components():
        items = []
        for c in row["components"]:
            if c["type"] == 2:
                emoji = (c.get("emoji") or {}).get("name") or ""
                label = html.escape(c.get("label") or "")
                arrow = '<span class="ext">↗</span>' if c["style"] == 5 else ""
                cls = f'button {BUTTON_STYLES[c["style"]]}{" disabled" if c.get("disabled") else ""}'
                items.append(f'<span class="{cls}">{emoji}{" " if emoji and label else ""}{label}{arrow}</span>')
            elif c["type"] == 3:
                items.append(f'<span class="select">{html.escape(c.get("placeholder") or "")}<span>⌄</span></span>')
        rows.append(f'<div class="row">{"".join(items)}</div>')
    return f'<div class="components">{"".join(rows)}</div>'


def avatar(person: Person, small: bool = False) -> str:
    cls = "mini" if small else "avatar"
    if person is PANDABOT:
        return f'<img class="{cls}" src="{{avatar}}">'
    bg = {JOCKIE.id: "#5865f2", TUNES.id: "#23a55a"}.get(person.id, person.colour)
    return f'<span class="{cls}" style="background:{bg}">{person.display_name[0].upper()}</span>'


def render_message(m: Msg) -> str:
    ctx = ""
    if m.used and m.used_by:
        ctx = (f'<div class="ctx"><span class="spine"></span>{avatar(m.used_by, True)}'
               f'<span class="ctx-name" style="color:{m.used_by.colour}">{m.used_by.display_name}</span>'
               f' used <span class="cmd">{html.escape(m.used)}</span></div>')
    elif m.reply_to:
        who_, text = m.reply_to
        ctx = (f'<div class="ctx"><span class="spine"></span>{avatar(who_, True)}'
               f'<span class="ctx-name" style="color:{who_.colour}">@{who_.display_name}</span>'
               f' <span class="ctx-text">{md(text)}</span></div>')
    tag = '<span class="app">APP</span>' if m.author.bot else ""
    colour = "#f2f3f5" if m.author.bot else m.author.colour
    body = [f'<div class="head"><span class="name" style="color:{colour}">{m.author.display_name}</span>{tag}'
            f'<span class="time">Today at {time.strftime("%-I:%M %p", time.localtime(NOW))}</span></div>']
    if m.content:
        body.append(f'<div class="content">{md(m.content)}'
                    f'{"<span class=edited> (edited)</span>" if m.edited else ""}</div>')
    body += [render_embed(e) for e in m.embeds]
    for name in m.files:
        body.append(f'<div class="file"><span class="ficon"></span><div><div class="fname">{html.escape(name)}</div>'
                    f'<div class="fsize">2.1 KB</div></div></div>')
    body.append(render_components(m.view))
    if m.reactions:
        body.append('<div class="reactions">' + "".join(
            f'<span class="reaction{" mine" if mine else ""}">{e} <b>{n}</b></span>' for e, n, mine in m.reactions)
            + "</div>")
    if m.ephemeral:
        body.append('<div class="eph"><span class="eye"></span>Only you can see this • <a>Dismiss message</a></div>')
    return (f'<div class="msg{" ephemeral" if m.ephemeral else ""}">{ctx}<div class="line">{avatar(m.author)}'
            f'<div class="body">{"".join(body)}</div></div></div>')


CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #1e1f22; font-family: "gg sans", "Helvetica Neue", Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased; }
.frame { width: 720px; background: #313338; color: #dbdee1; position: relative; padding-bottom: 18px;
  margin-bottom: 40px; overflow: hidden; }
.chan { height: 48px; display: flex; align-items: center; padding: 0 16px; font-weight: 600; color: #f2f3f5;
  font-size: 16px; border-bottom: 1px solid #26272b; margin-bottom: 6px; }
.chan .icon { color: #80848e; margin-right: 8px; font-size: 20px; font-weight: 500; }
.msg { padding: 2px 48px 2px 16px; margin-top: 16px; }
.msg.ephemeral { background: rgba(88,101,242,.07); padding-top: 6px; padding-bottom: 6px; }
.line { display: flex; gap: 16px; }
.avatar { width: 40px; height: 40px; border-radius: 50%; flex: none; display: inline-flex; align-items: center;
  justify-content: center; color: #fff; font-weight: 600; font-size: 17px; }
.mini { width: 16px; height: 16px; border-radius: 50%; display: inline-flex; align-items: center;
  justify-content: center; color: #fff; font-size: 9px; font-weight: 700; margin: 0 4px 0 0; flex: none; }
.ctx { display: flex; align-items: center; font-size: 14px; color: #b5bac1; margin: 0 0 4px 72px;
  position: relative; white-space: nowrap; }
.spine { position: absolute; left: -36px; top: 8px; width: 33px; height: 13px; border-left: 2px solid #4e5058;
  border-top: 2px solid #4e5058; border-top-left-radius: 6px; }
.ctx-name { font-weight: 500; margin-right: 4px; }
.cmd { color: #00a8fc; margin-left: 4px; }
.ctx-text { color: #b5bac1; }
.body { min-width: 0; flex: 1; }
.head { display: flex; align-items: baseline; gap: 6px; line-height: 22px; }
.name { font-weight: 500; font-size: 16px; }
.app { background: #5865f2; color: #fff; font-size: 10px; font-weight: 600; padding: 1px 4px; border-radius: 3px;
  position: relative; top: -2px; }
.time { color: #949ba4; font-size: 12px; margin-left: 2px; }
.content { font-size: 16px; line-height: 1.375; }
.edited { color: #949ba4; font-size: 10px; }
a { color: #00a8fc; text-decoration: none; }
strong { font-weight: 700; color: inherit; }
code { background: #2b2d31; border: 1px solid #1e1f22; border-radius: 4px; padding: 0 3px; font-size: 85%;
  font-family: Menlo, Consolas, monospace; }
.embed code { background: #1e1f22; }
.mention { background: rgba(88,101,242,.3); color: #c9cdfb; border-radius: 3px; padding: 0 2px; font-weight: 500; }
.ts { background: rgba(255,255,255,.06); border-radius: 3px; padding: 0 2px; }
.sub { font-size: 13px; color: #949ba4; }
.cemoji { display: inline-block; width: 18px; height: 18px; border-radius: 50%; vertical-align: -3px;
  background: #1db954; }
.embed { max-width: 520px; background: #2b2d31; border-left: 4px solid; border-radius: 4px;
  padding: 8px 16px 16px 12px; margin-top: 4px; font-size: 14px; line-height: 1.375; }
.e-author { font-weight: 600; color: #f2f3f5; margin-top: 8px; font-size: 14px; }
.e-title { font-weight: 600; color: #f2f3f5; font-size: 16px; margin-top: 8px; }
.e-title.link { color: #00a8fc; }
.e-desc { margin-top: 8px; white-space: normal; }
.e-fields { display: grid; grid-template-columns: repeat(12, 1fr); gap: 8px; margin-top: 8px; }
.f-name { font-weight: 600; color: #f2f3f5; margin-bottom: 2px; }
.e-footer { margin-top: 8px; font-size: 12px; color: #b5bac1; }
.components { margin-top: 6px; }
.row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.button { height: 32px; padding: 0 16px; border-radius: 8px; display: inline-flex; align-items: center;
  font-size: 14px; font-weight: 500; color: #fff; gap: 4px; }
.button.secondary { background: #4e5058; }
.button.primary { background: #5865f2; }
.button.success { background: #248046; }
.button.danger { background: #da373c; }
.button.disabled { opacity: .5; }
.ext { font-size: 13px; margin-left: 6px; opacity: .9; }
.select { width: 400px; height: 40px; background: #1e1f22; border-radius: 4px; display: flex; align-items: center;
  justify-content: space-between; padding: 0 12px; color: #b5bac1; font-size: 15px; }
.reactions { margin-top: 4px; }
.reaction { display: inline-flex; align-items: center; gap: 6px; background: #2b2d31; border: 1px solid #2b2d31;
  border-radius: 8px; padding: 2px 8px; font-size: 15px; }
.reaction b { font-size: 14px; color: #b5bac1; font-weight: 600; }
.reaction.mine { background: rgba(88,101,242,.15); border-color: #5865f2; }
.eph { font-size: 12px; color: #949ba4; margin-top: 6px; display: flex; align-items: center; gap: 4px; }
.eye { width: 14px; height: 9px; border: 2px solid #949ba4; border-radius: 50%; display: inline-block; }
.file { display: flex; gap: 10px; align-items: center; background: #2b2d31; border: 1px solid #1e1f22;
  border-radius: 8px; padding: 12px 14px; width: 400px; margin-top: 6px; }
.ficon { width: 28px; height: 36px; background: #5865f2; border-radius: 3px; flex: none;
  clip-path: polygon(0 0, 70% 0, 100% 25%, 100% 100%, 0 100%); }
.fname { color: #00a8fc; font-size: 16px; }
.fsize { color: #949ba4; font-size: 12px; }
.menu { position: absolute; right: 168px; top: 70px; width: 188px; background: #111214; border-radius: 6px;
  padding: 6px 8px; box-shadow: 0 8px 16px rgba(0,0,0,.4); font-size: 14px; color: #dbdee1; }
.menu .item { padding: 7px 8px; border-radius: 3px; display: flex; align-items: center; gap: 8px;
  justify-content: space-between; }
.menu .item.sel { background: #4752c4; color: #fff; }
.submenu { position: absolute; left: 194px; top: 76px; width: 160px; background: #111214; border-radius: 6px;
  padding: 6px 8px; box-shadow: 0 8px 16px rgba(0,0,0,.4); }
.submenu .item { justify-content: flex-start; }
.submenu img { width: 18px; height: 18px; border-radius: 50%; }
"""


def render_page(scenes: list[Scene], avatar_uri: str) -> str:
    frames = []
    for s in scenes:
        icon, _, label = s.channel.partition(" ") if s.channel[0] in "#🔊@" else ("#", "", s.channel)
        frames.append(f'<section class="frame" id="{s.slug}"><div class="chan"><span class="icon">{icon}</span>'
                      f'{html.escape(label)}</div>{"".join(render_message(m) for m in s.messages)}{s.overlay}'
                      f'</section>')
    page = f'<!doctype html><meta charset="utf-8"><style>{CSS}</style><body>{"".join(frames)}</body>'
    return page.replace("{avatar}", avatar_uri)


def screenshot(scenes: list[Scene]) -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    avatar_uri = "data:image/png;base64," + base64.b64encode(AVATAR.read_bytes()).decode()
    with tempfile.TemporaryDirectory() as tmp:
        page_path = Path(tmp) / "scenes.html"
        page_path.write_text(render_page(scenes, avatar_uri), encoding="utf-8")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": 760, "height": 900}, device_scale_factor=2)
            page.goto(page_path.as_uri())
            page.wait_for_load_state("networkidle")
            for s in scenes:
                el = page.locator(f"#{s.slug}")
                el.screenshot(path=str(OUT / f"{s.slug}.png"))
                box = el.bounding_box()
                s.width, s.height = round(box["width"]), round(box["height"])
            browser.close()


# --- the website page ---------------------------------------------------------------
def write_gallery(scenes: list[Scene]) -> None:
    groups: dict[str, list[Scene]] = {}
    for s in scenes:
        groups.setdefault(s.group, []).append(s)
    sections = []
    for group, items in groups.items():
        figures = "\n".join(
            f'    <figure id="{s.slug}">\n'
            f'      <figcaption><h3>{html.escape(s.command)}</h3><p>{s.blurb}</p></figcaption>\n'
            f'      <img src="img/commands/{s.slug}.png" width="{s.width}" height="{s.height}" loading="lazy"\n'
            f'           alt="PandaBot in Discord: {html.escape(s.command)}">\n'
            f'    </figure>' for s in items)
        sections.append(f'  <h2>{html.escape(group)}</h2>\n  <div class="shots">\n{figures}\n  </div>')
    GALLERY.write_text(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PandaBot commands</title>
  <meta name="description" content="Every PandaBot command, with a screenshot of what it looks like in Discord.">
  <link rel="stylesheet" href="style.css">
</head>
<body>
<main class="wide">
  <p><a href="index.html">← PandaBot</a></p>
  <h1>Commands</h1>
  <p class="lede">What every PandaBot command looks like in Discord. Most people type the <code>p!</code> commands
  (and <code>!like</code> to save); each one also works as a slash command, and a few settings are slash-only.
  <code>p!help</code> lists them all in Discord.</p>
  <!-- Generated by tools/screenshots.py. Edit the descriptions there, then re-run it. -->
{chr(10).join(sections)}

  <footer>
    <a href="index.html">Home</a> · <a href="privacy.html">Privacy policy</a> · <a href="terms.html">Terms of service</a> · <a href="https://discord.gg/vvShzeYx5E">Support server</a> · <a href="https://github.com/uncSoft/pandabot">Source code</a>
  </footer>
</main>
</body>
</html>
""", encoding="utf-8")


def main() -> None:
    scenes = asyncio.run(build_scenes())
    screenshot(scenes)
    write_gallery(scenes)
    print(f"Wrote {len(scenes)} screenshots to {OUT.relative_to(ROOT)} and {GALLERY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
