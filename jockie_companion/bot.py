"""Companion bot that watches a music bot (Jockie Music by default) and adds
song saving, now-playing announcements in voice chat, play history and stats."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

import discord
from discord import app_commands
from dotenv import load_dotenv

from .db import Like, Play, Store
from .parser import EmbedSnapshot, Kind, MessageSnapshot, Track, parse_message, youtube_link
from .usage import ago, usage
from .stats import (PERIOD_CHOICES, Period, SongPager, SongRow, dj_embed, match_embed, parse_period,
                    rows_from_likes, rows_from_plays, stats_embed, top_embed, who)

log = logging.getLogger("jockie_companion")

HEART = "❤️"
HEART_NAMES = {"❤️", "❤"}
SAVE_PREFIX = "jc:save:"
UNSAVE_PREFIX = "jc:unsave:"
DEFAULT_SUPPORT_URL = "https://ko-fi.com/jtatuncsoft/tip"
DEFAULT_SUPPORT_SERVER = "https://discord.gg/vvShzeYx5E"
SOURCE_URL = "https://github.com/uncSoft/pandabot"  # AGPL: users of the hosted bot can get its source
# View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions
INVITE_PERMISSIONS = 117824
Notify = Callable[[str], Awaitable[object]]  # tells someone something (privately or in a channel)
COMMAND_REQUESTER_WINDOW = 30  # seconds between "m!play x" and the music bot's reply
ANNOUNCE_MODES = ("follow", "channel", "off")
VoiceLike = (discord.VoiceChannel, discord.StageChannel)


def _ids(value: str | None) -> set[int]:
    return {int(x) for x in (value or "").replace(" ", "").split(",") if x}


class Config:
    def __init__(self) -> None:
        self.token = os.environ["DISCORD_TOKEN"]
        self.music_bot_ids = _ids(os.getenv("MUSIC_BOT_IDS"))
        self.music_bot_name_prefix = os.getenv("MUSIC_BOT_NAME_PREFIX", "Jockie Music").lower()
        self.channel_ids = _ids(os.getenv("WATCH_CHANNEL_IDS"))
        self.command_prefixes = tuple(
            p.lower() for p in os.getenv("MUSIC_COMMAND_PREFIXES", "m!").split(",") if p)
        # Only these commands count as "requesting a song" (so m!skip, m!pause etc. don't).
        self.play_commands = {
            c.strip().lower() for c in os.getenv(
                "PLAY_COMMANDS", "p,play,pn,playnext,playnow,pt,playtop,ps,playskip,search").split(",")
            if c.strip()}
        self.db_path = os.getenv("DB_PATH", "data/jockie_companion.db")
        self.react = os.getenv("REACT_TO_NOW_PLAYING", "1") == "1"
        self.dump_path = os.getenv("DEBUG_DUMP_PATH") or None
        # Server admins can override these per server with /announce.
        self.announce_mode = os.getenv("ANNOUNCE_MODE", "follow").lower()
        if self.announce_mode not in ANNOUNCE_MODES:
            raise ValueError(f"ANNOUNCE_MODE must be one of {ANNOUNCE_MODES}")
        self.announce_channel_id = int(os.environ["ANNOUNCE_CHANNEL_ID"]) if os.getenv("ANNOUNCE_CHANNEL_ID") else None
        self.like_keywords = {
            k.strip().lower() for k in os.getenv("LIKE_KEYWORDS", "!like,❤️,❤").split(",") if k.strip()}
        # Default for DMing people a card when they save a song (each person can change it with /dms).
        self.dm_on_save = os.getenv("DM_ON_SAVE", "1") == "1"
        # Prefix for text commands like "p!stats" (slash commands work too).
        self.stats_prefix = os.getenv("STATS_PREFIX", "p!").lower()
        # Tip link shown on the help card and by p!support. Set SUPPORT_URL to "" to hide it.
        self.support_url = os.getenv("SUPPORT_URL", DEFAULT_SUPPORT_URL).strip()
        # Invite to PandaBot's support server (help card, invite card). "" hides it.
        self.support_server = os.getenv("SUPPORT_SERVER_URL", DEFAULT_SUPPORT_SERVER).strip()


def _component_texts(components) -> list[str]:
    """Collect text from Discord's newer component-based messages (TextDisplay etc.)."""
    out: list[str] = []
    for c in components or []:
        content = getattr(c, "content", None)
        if isinstance(content, str) and content:
            out.append(content)
        for attr in ("children", "components"):
            out += _component_texts(getattr(c, attr, None))
        if accessory := getattr(c, "accessory", None):
            out += _component_texts([accessory])
    return out


def raw_record(message: discord.Message) -> dict:
    """A music-bot message as plain JSON, for the capture file and /musicbot check."""
    return {
        "message_id": message.id,
        "author": str(message.author),
        "content": message.content,
        "embeds": [e.to_dict() for e in message.embeds],
        "component_texts": _component_texts(message.components),
    }


def describe_reading(track: Track | None, record: dict) -> str:
    """One line saying how PandaBot read a music-bot message."""
    if track is None:
        text = record.get("content") or next(
            (e.get("description") or e.get("title") or "" for e in record.get("embeds", [])), "") or "(no text)"
        text = " ".join(text.split())
        return f"· Not a song message: {text[:70]}{'…' if len(text) > 70 else ''}"
    label = "▶️ Now playing" if track.kind == Kind.NOW_PLAYING else "➕ Queued"
    artist = f" — {track.artist}" if track.artist else " (no artist found)"
    return f"{label}: **{track.title}**{artist}"


def snapshot(message: discord.Message) -> MessageSnapshot:
    embeds = [
        EmbedSnapshot(
            title=e.title,
            description=e.description,
            url=e.url,
            author_name=e.author.name if e.author else None,
            footer_text=e.footer.text if e.footer else None,
            fields=[(f.name or "", f.value or "") for f in e.fields],
        )
        for e in message.embeds
    ]
    if texts := _component_texts(message.components):
        embeds.append(EmbedSnapshot(description="\n".join(texts)))
    return MessageSnapshot(content=message.content or "", embeds=embeds)


def _fmt(title: str, artist: str | None, url: str | None, width: int = 80) -> str:
    label = f"{title} — {artist}" if artist else title
    if len(label) > width:
        label = label[: width - 1] + "…"
    label = label.replace("[", "(").replace("]", ")")
    return f"[{label}](<{youtube_link(title, artist, url)}>)"


def _play_embed(play: Play, heading: str) -> discord.Embed:
    yt = youtube_link(play.title, play.artist, play.url)
    e = discord.Embed(title=play.title[:256], url=yt, colour=discord.Colour.blurple())
    e.set_author(name=heading)
    if play.artist:
        e.add_field(name="Artist", value=play.artist[:1024])
    links = f"[YouTube]({yt})"
    if play.url and play.url != yt:
        links += f" · [Original]({play.url})"
    e.add_field(name="Links", value=links)
    if play.requester_id:
        e.add_field(name="Requested by", value=f"<@{play.requester_id}>")
    elif play.requester_name:
        e.add_field(name="Requested by", value=play.requester_name[:1024])
    e.set_footer(text="Press Save or type !like to add it to your list")
    e.timestamp = datetime.fromtimestamp(play.played_at, tz=timezone.utc)
    return e


def _save_view(play_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Save song", emoji=HEART, style=discord.ButtonStyle.secondary,
                                    custom_id=f"{SAVE_PREFIX}{play_id}"))
    return view


def _dm_embed(like: Like, guild_name: str, play: Play | None) -> discord.Embed:
    yt = youtube_link(like.title, like.artist, like.url)
    e = discord.Embed(title=like.title[:256], url=yt, colour=discord.Colour.red())
    e.set_author(name=f"{HEART} Saved song")
    if like.artist:
        e.add_field(name="Artist", value=like.artist[:1024])
    links = f"[YouTube]({yt})"
    if like.url and like.url != yt:
        links += f" · [Original]({like.url})"
    e.add_field(name="Links", value=links)
    if play and play.requester_id:
        e.add_field(name="Requested by", value=f"<@{play.requester_id}>")
    elif play and play.requester_name:
        e.add_field(name="Requested by", value=play.requester_name[:1024])
    e.set_footer(text=f"Saved in {guild_name} · /dms off to stop these")
    e.timestamp = datetime.fromtimestamp(like.liked_at, tz=timezone.utc)
    return e


def _unsave_view(like_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Remove from list", style=discord.ButtonStyle.danger,
                                    custom_id=f"{UNSAVE_PREFIX}{like_id}"))
    return view


def throwback_embed(play: Play) -> discord.Embed:
    e = _play_embed(play, "🔁 Throwback")
    e.description = f"Played <t:{int(play.played_at)}:R>"
    return e


def invite_url(app_id: int) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")


def link_view(*links: tuple[str, str, str | None]) -> discord.ui.View | None:
    """Link buttons from (label, emoji, url); ones without a url are skipped. No callbacks needed."""
    links = tuple(l for l in links if l[2])
    if not links:
        return None
    view = discord.ui.View(timeout=None)
    for label, emoji, url in links:
        view.add_item(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.link, url=url))
    return view


def support_view(url: str) -> discord.ui.View | None:
    return link_view(("Buy me a coffee", "☕", url))


def invite_embed() -> discord.Embed:
    return discord.Embed(
        title="🐼 Add PandaBot to your server", colour=discord.Colour.blurple(),
        description="Now-playing cards in voice chat, one-tap ❤️ saving with DM cards, and music stats "
                    "for Jockie Music (and other music bots via `/musicbot`). Needs Manage Server to add.")


def support_embed(url: str) -> discord.Embed:
    e = discord.Embed(title="☕ Keep PandaBot running", colour=discord.Colour.from_rgb(255, 94, 91))
    e.description = ("PandaBot runs 24/7 on a paid server so it never misses a song. "
                     "If you enjoy it, a coffee helps cover the hosting. Totally optional, and thank you!"
                     + (f"\n\n{url}" if url else ""))
    return e


def text_help_embed(p: str, support_url: str = "") -> discord.Embed:
    e = discord.Embed(title="🎧 PandaBot commands", colour=discord.Colour.blurple())
    e.description = "\n".join([
        f"`{p}stats [@user] [period]`: someone's music profile",
        f"`{p}played [@user] [period]`: everything they requested",
        f"`{p}<name> [period]`: same thing, e.g. `{p}panda 7d`",
        f"`{p}saved [@user]`: their saved songs",
        f"`{p}heard [period]`: songs that played while you were in voice",
        f"`{p}history [period]`: everything played in the server",
        f"`{p}top [period]`: server charts",
        f"`{p}dj [period]`: whose requests get saved the most",
        f"`{p}match @user [@user]`: taste match",
        f"`{p}throwback`: a random song from the past",
        f"`{p}support`: help cover PandaBot's server costs",
        f"`{p}invite`: add PandaBot to another server",
        "`/forgetme`: delete everything PandaBot stores about you",
        f"`{p}backfill [messages]`: (Manage Server) log past plays from old messages, e.g. `{p}backfill 10000`",
        "",
        "**Periods:** `24h` `7d` `2w` `3m` `1y` `today` `week` `month` `all`",
        "**Saving:** tap ❤️, use the menu under any list, type `!like`, or `/save`",
        "**Admins:** `/announce` picks where cards go · `/setupcheck` lists channels I can't see",
        *(["", f"☕ Like PandaBot? [Help with server costs]({support_url})"] if support_url else []),
    ])
    e.set_footer(text="All of these also exist as slash commands · Open source (AGPL): github.com/uncSoft/pandabot")
    return e


def welcome_embed(p: str, problems: list[str], support_server: str = "") -> discord.Embed:
    """Posted once when PandaBot joins a server, so whoever added it knows there's nothing to set up."""
    e = discord.Embed(title="🐼 Thanks for adding PandaBot!", colour=discord.Colour.blurple())
    lines = [
        "**There's nothing to set up.** Play music with Jockie Music as usual (any Jockie instance works) "
        "and I'll pick it up:",
        "• A **Now playing** card in the voice channel's chat, with a **Save song** button",
        f"• Tap {HEART} under Jockie's song posts, or type `!like`, to save what's playing. "
        "Saved songs are DM'd to you",
        f"• `{p}help` lists everything: `{p}stats`, `{p}top`, `{p}saved`, `{p}history`…",
        "",
        "**For admins (all optional)**",
        f"• `{p}backfill 10000` imports songs already played here",
        "• `/announce` picks where the cards go",
        "• `/musicbot add` is only for *other* music bots. Jockie doesn't need it",
    ]
    if problems:
        lines += ["", "⚠️ **I can't see or post in some channels**, so songs requested there are missed. "
                      "Run `/setupcheck` to see which."]
    if support_server:
        lines += ["", f"Questions? [Support server]({support_server})"]
    e.description = "\n".join(lines)
    return e


def welcome_channel(guild) -> discord.abc.Messageable | None:
    """The server's system channel, else a music or general channel, else any channel I can post in."""
    def ok(c) -> bool:
        perms = c.permissions_for(guild.me)
        return perms.view_channel and perms.send_messages and perms.embed_links

    channels = [c for c in guild.text_channels if ok(c)]
    if guild.system_channel and ok(guild.system_channel):
        return guild.system_channel
    for word in ("music", "general", "chat"):
        for c in channels:
            if word in c.name.lower():
                return c
    return channels[0] if channels else None


def usage_embed(bot: "CompanionBot") -> discord.Embed:
    """Owner-only overview of every server (p!usage). Server names come from Discord, not the database."""
    u = usage(bot.store.conn)
    now = time.time()
    joined = {g.id: g for g in bot.guilds}
    e = discord.Embed(title="📊 PandaBot usage", colour=discord.Colour.blurple())
    totals = [
        f"**{len(joined)}** servers · **{u.servers_7d}** played a song this week",
        f"Songs: **{u.plays_7d}** this week · **{u.plays_24h}** today · {u.plays} all time",
        f"Saves: **{u.saves_7d}** this week · {u.saves} all time · {u.savers} people",
        f"{u.requesters} requested · {u.listeners} heard in voice · {u.dms_off} turned DMs off",
    ]
    rows = []
    for gid, g in sorted(joined.items(), key=lambda kv: (
            -(u.servers[kv[0]].plays_7d if kv[0] in u.servers else 0), -(kv[1].member_count or 0))):
        s = u.servers.get(gid)
        bits = [f"{g.member_count or '?'} members"]
        if s and s.plays:
            bits.append(f"{s.plays_7d} plays/wk")
            if s.saves:
                bits.append(f"{s.saves} saves")
            bits.append(f"last {ago(s.last_play, now)}")
        else:
            bits.append("no songs yet")
        rows.append(f"**{discord.utils.escape_markdown(g.name)[:40]}** · " + " · ".join(bits))
    gone = sum(1 for gid in u.servers if gid not in joined)
    body = "\n".join(totals) + "\n\n"
    for i, row in enumerate(rows):
        if len(body) + len(row) > 3900:
            body += f"…and {len(rows) - i} more"
            break
        body += row + "\n"
    if gone:
        body += f"\n{gone} server(s) with history have removed PandaBot."
    e.description = body
    if u.top_week:
        e.add_field(name="Top this week", value="\n".join(
            f"{n}× {_fmt(t, a, None, 60)}" for t, a, n in u.top_week)[:1024], inline=False)
    e.set_footer(text="Only you can see this · tools/usage.py has the raw numbers")
    return e


NO_SONGS_HINT = ("No songs logged in this server yet. If music has been playing, I probably can't see the channel "
                 "the music bot posts in. Admins: run `/setupcheck`.")
BLIND_CHECK_SECONDS = 300  # how long a music bot can sit in voice before "I can't see it" is likely
NOTICE_EVERY = 7 * 86400


def blind_notice_text(bot_name: str) -> str:
    return (f"🐼 **{bot_name}** has been in here for a few minutes, but I haven't seen any of its song messages, "
            f"so nothing is being logged or saved.\n"
            f"Usually that means I can't see the channel where songs are requested. An admin can run "
            f"`/setupcheck`, or allow PandaBot **View Channel** and **Read Message History** in that channel. "
            f"If {bot_name}'s song announcements are turned off, turning them on fixes it too.")


def dm_blocked_text(title: str, guild_name: str) -> str:
    return (f"Saved **{title}** to your list ✅, but Discord won't let me DM you the card. "
            f"To get DM cards: open **{guild_name}** → Privacy Settings → turn on **Direct Messages**. "
            f"Your list is always in `/likes`.")


def _saved_text(added: bool, play_or_title, artist=None, url=None) -> str:
    if isinstance(play_or_title, Play):
        play_or_title, artist, url = play_or_title.title, play_or_title.artist, play_or_title.url
    return f"{'Saved' if added else 'Already in your list'}: {_fmt(play_or_title, artist, url)}"


TEXT_PERMS = ("view_channel", "read_message_history", "send_messages", "add_reactions")
VOICE_PERMS = ("view_channel", "send_messages", "embed_links", "add_reactions")
PERM_LABELS = {"view_channel": "View Channel", "read_message_history": "Read Message History",
               "send_messages": "Send Messages", "add_reactions": "Add Reactions", "embed_links": "Embed Links"}
MUSIC_WORDS = ("music", "jockie", "song", "dj", "bot", "command", "cmd", "request", "playlist", "radio", "panda")
FIX_HOW = ("To fix one: right-click the channel → Edit Channel → Permissions → add PandaBot (or its role) "
           "→ turn on the permissions named above. Then run `/setupcheck` again.")


def _names(channels, sign: str = "#", limit: int = 12) -> str:
    """Channel names, music-looking ones first (those are the ones that matter most)."""
    names = sorted((c.name for c in channels), key=lambda n: not any(w in n.lower() for w in MUSIC_WORDS))
    more = f" and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(sign + n for n in names[:limit]) + more


def _labels(perms) -> str:
    names = [f"**{PERM_LABELS[n]}**" for n in perms]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def setup_problems(guild, me) -> list[str]:
    """Channels the bot can't see or post in, in plain words. Missing permissions fail silently otherwise."""
    problems = []
    readable, unreadable = [], []
    for c in guild.text_channels:
        p = c.permissions_for(me)
        (readable if p.view_channel and p.read_message_history else unreadable).append((c, p))
    if unreadable:
        problems.append(
            f"I can't see {len(unreadable)} text channel(s): {_names(c for c, _ in unreadable)}. Songs requested "
            f"or announced there are missed. If people use the music bot in any of them, allow PandaBot "
            f"{_labels(('view_channel', 'read_message_history'))} there.")
    mute = [c for c, p in readable if not p.send_messages]
    if mute:
        problems.append(
            f"I can read but not reply in {_names(mute)}. That's fine for read-only channels like rules; "
            f"anywhere people chat, allow {_labels(('send_messages',))} so `p!` commands and `!like` get an answer.")
    no_heart = [c for c, p in readable if p.send_messages and not p.add_reactions]
    if no_heart:
        problems.append(f"I can't add the ❤️ under songs in {_names(no_heart)}. Allow {_labels(('add_reactions',))}.")
    voice_missing: dict[str, list] = {}
    for c in (*guild.voice_channels, *guild.stage_channels):
        missing = tuple(n for n in VOICE_PERMS if not getattr(c.permissions_for(me), n))
        if missing:
            voice_missing.setdefault(missing, []).append(c)
    for missing, chans in voice_missing.items():
        problems.append(
            f"I can't post song cards in the chat of {_names(chans, '🔊')}. Allow {_labels(missing)} there, "
            f"or send cards to one channel with `/announce`.")
    return problems


class RequestTracker:
    """Remembers recent "m!p ..." commands so the next song can be credited to whoever asked:
    same channel first, then anywhere in the server (request in #general, announced in #music)."""

    def __init__(self) -> None:
        self.by_channel: dict[int, tuple[int, str, str, float]] = {}
        self.by_guild: dict[int, tuple[int, str, str, float]] = {}

    def record(self, message: discord.Message, at: float) -> None:
        entry = (message.author.id, message.author.display_name, message.author.name, at)
        self.by_channel[message.channel.id] = entry
        self.by_guild[message.guild.id] = entry

    def match(self, channel_id: int, guild_id: int, printed_name: str | None, now: float
              ) -> tuple[int, str] | None:
        for entry in (self.by_channel.get(channel_id), self.by_guild.get(guild_id)):
            if not entry:
                continue
            user_id, display, username, at = entry
            name_ok = not printed_name or printed_name.lower() in {display.lower(), username.lower()}
            if 0 <= now - at <= COMMAND_REQUESTER_WINDOW and name_ok:
                # One request → one song, so a later song from the queue isn't credited to the same command.
                for table in (self.by_channel, self.by_guild):
                    for key in [k for k, v in table.items() if v is entry]:
                        del table[key]
                return user_id, display
        return None


class CompanionBot(discord.Client):
    def __init__(self, config: Config, store: Store) -> None:
        intents = discord.Intents.default()  # includes voice_states, used to find the music bot's voice channel
        intents.message_content = True  # privileged: enable in the Developer Portal
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.store = store
        self.tree = app_commands.CommandTree(self)
        self._tasks: set[asyncio.Task] = set()
        self.requests = RequestTracker()
        self._backfilling: set[int] = set()
        self._commands_registered = False
        self._owner_ids: set[int] | None = None
        self._music_seen: dict[int, float] = {}  # guild → when a music bot last posted anything I could read
        self._blind_checks: dict[int, asyncio.Task] = {}
        self._dm_blocked_noticed: dict[int, float] = {}
        self._music_bot_cache: dict[int, tuple[set[int], tuple[str, ...]]] = {}
        # (guild, bot) → last few messages and how they were read, for /musicbot check
        self._recent_music: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=8))
        register_commands(self)

    async def sync_commands(self) -> None:
        """Register the slash commands globally, so they work in every server PandaBot joins (and
        Discord's App Directory sees them), then remove the per-server copies older versions made,
        which would otherwise show every command twice."""
        try:
            await self.tree.sync()
        except discord.HTTPException as exc:
            log.warning("Couldn't register slash commands: %s", exc)
            return
        for guild in self.guilds:
            try:
                if await self.tree.fetch_commands(guild=guild):
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                    log.info("Removed old per-server commands from %s", guild.id)
            except discord.HTTPException as exc:
                log.warning("Couldn't check old commands in server %s: %s", guild.id, exc)

    async def on_app_command_completion(self, interaction: discord.Interaction, command) -> None:
        log.info("/%s by %s", command.qualified_name, interaction.user)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Someone added PandaBot to a new server. Commands are global, so there's nothing to register;
        post a short welcome so whoever added it knows that, and how saving works."""
        log.info("Joined server %s (%s)", guild.name, guild.id)
        self._log_setup_check(guild)
        channel = welcome_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=welcome_embed(self.config.stats_prefix, setup_problems(guild, guild.me),
                                                   self.config.support_server))
        except discord.HTTPException as exc:
            log.warning("Couldn't post the welcome in %s: %s", guild.id, exc)

    async def is_owner(self, user: discord.abc.User) -> bool:
        """The app's owner, or anyone on the team that owns it (from the Developer Portal)."""
        if self._owner_ids is None:
            app = await self.application_info()
            self._owner_ids = {m.id for m in app.team.members} if app.team else {app.owner.id}
        return user.id in self._owner_ids

    async def send_usage(self, message: discord.Message) -> None:
        """p!usage: the report goes to the owner's DMs, never into a server channel."""
        try:
            await message.author.send(embed=usage_embed(self))
        except discord.HTTPException as exc:
            log.warning("Couldn't DM the usage report: %s", exc)
            return
        if message.guild:
            await message.add_reaction("📬")

    def _log_setup_check(self, guild: discord.Guild) -> None:
        problems = setup_problems(guild, guild.me)
        if problems:
            log.warning("Setup check for %s:\n  - %s", guild.name, "\n  - ".join(problems))
        else:
            log.info("Setup check for %s: all channels look good", guild.name)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d server(s)", self.user, self.user.id, len(self.guilds))
        if not self.guilds:
            log.warning("I'm not in any server yet. Invite me with the OAuth2 URL from the README.")
        if not self._commands_registered:  # on_ready also fires after reconnects; once per start is enough
            self._commands_registered = True
            await self.sync_commands()
        for guild in self.guilds:
            self._log_setup_check(guild)

    # --- helpers ---------------------------------------------------------
    def server_music_bots(self, guild_id: int | None) -> tuple[set[int], tuple[str, ...]]:
        """(bot ids, text prefixes) a server added with /musicbot, cached until it changes."""
        if guild_id is None:
            return set(), ()
        if guild_id not in self._music_bot_cache:
            rows = self.store.music_bots(guild_id)
            self._music_bot_cache[guild_id] = (
                {bot_id for bot_id, _, _ in rows},
                tuple(p.lower() for _, _, p in rows if p))
        return self._music_bot_cache[guild_id]

    def is_music_bot(self, user: discord.abc.User, guild_id: int | None = None) -> bool:
        """Jockie (by name, or MUSIC_BOT_IDS) everywhere, plus any bots this server added."""
        if not user.bot or user.id == self.user.id:
            return False
        if user.id in self.server_music_bots(guild_id)[0]:
            return True
        if self.config.music_bot_ids:
            return user.id in self.config.music_bot_ids
        return user.name.lower().startswith(self.config.music_bot_name_prefix)

    def watching(self, channel_id: int) -> bool:
        return not self.config.channel_ids or channel_id in self.config.channel_ids

    def is_play_command(self, text: str, guild_id: int | None = None) -> bool:
        """'m!p <song>' / '!play <song>' typed in any channel, with Jockie's or this server's bots' prefixes."""
        prefixes = {*self.config.command_prefixes, *self.server_music_bots(guild_id)[1]}
        for prefix in sorted(prefixes, key=len, reverse=True):  # longest first: '!!' before '!'
            if text.startswith(prefix):
                words = text[len(prefix):].split(maxsplit=1)
                return bool(words) and words[0] in self.config.play_commands
        return False

    @staticmethod
    def voice_channel_of(guild: discord.Guild, user_id: int) -> discord.abc.GuildChannel | None:
        for ch in (*guild.voice_channels, *guild.stage_channels):
            if user_id in ch.voice_states:
                return ch
        return None

    def announce_settings(self, guild_id: int) -> tuple[str, int | None]:
        return self.store.get_announce_settings(guild_id) or (
            self.config.announce_mode, self.config.announce_channel_id)

    def current_play(self, guild: discord.Guild, member: discord.abc.User,
                     channel: discord.abc.GuildChannel | None) -> Play | None:
        """The song most relevant to this person: the one in the voice chat they're typing in,
        else the voice channel they're sitting in, else the latest in the server."""
        vc_id = channel.id if isinstance(channel, VoiceLike) else None
        if vc_id is None and isinstance(member, discord.Member) and member.voice and member.voice.channel:
            vc_id = member.voice.channel.id
        return self.store.latest_play(guild.id, vc_id)

    # --- watching the music bot -----------------------------------------
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        """A music bot joined voice: if none of its messages show up within a few minutes, I'm probably
        blind to the channel it posts in. Say so once, where the listeners are."""
        if (not after.channel or before.channel == after.channel or self.config.channel_ids
                or not self.is_music_bot(member, member.guild.id) or member.guild.id in self._blind_checks):
            return
        gid = member.guild.id
        task = asyncio.create_task(self.check_blind(member, after.channel, time.time()))
        self._blind_checks[gid] = task
        task.add_done_callback(lambda _: self._blind_checks.pop(gid, None))

    async def check_blind(self, music_bot, voice, joined_at: float, delay: float = BLIND_CHECK_SECONDS) -> bool:
        await asyncio.sleep(delay)
        guild = voice.guild
        if music_bot.id not in voice.voice_states or not any(not m.bot for m in voice.members):
            return False  # it left, or nobody is listening
        if self._music_seen.get(guild.id, 0) >= joined_at - 60:
            if not self.store.recent(guild.id, joined_at - 60, limit=1):
                log.warning("%s posted in %s but no song was read: check the parser (/musicbot check)",
                            music_bot, guild.id)
            return False
        log.warning("%s has been in voice in %s for %ds with no messages I can see", music_bot, guild.id, delay)
        now = time.time()
        last = self.store.notice_sent_at(guild.id, "blind")
        if last and now - last < NOTICE_EVERY:
            return False
        me = guild.me
        p = voice.permissions_for(me)
        target = voice if (p.view_channel and p.send_messages) else welcome_channel(guild)
        if target is None:
            return False
        try:
            await target.send(content=blind_notice_text(music_bot.display_name))
        except discord.HTTPException as exc:
            log.warning("Couldn't post the can't-see notice in %s: %s", guild.id, exc)
            return False
        self.store.record_notice(guild.id, "blind", now)
        return True

    def with_hint(self, guild_id: int, text: str) -> str:
        """Empty results get a pointer to /setupcheck when the server has never logged a song."""
        return f"{text}\n\n{NO_SONGS_HINT}" if not self.store.count_plays(guild_id) else text

    def hinted(self, guild_id: int, embed: discord.Embed) -> discord.Embed:
        if not self.store.count_plays(guild_id):
            embed.description = f"{embed.description or ''}\n\n{NO_SONGS_HINT}".strip()
        return embed

    async def on_message(self, message: discord.Message) -> None:
        if not message.author.bot and message.content.strip().lower() == f"{self.config.stats_prefix}usage":
            if await self.is_owner(message.author):
                return await self.send_usage(message)
        if not message.guild:
            return
        if not message.author.bot:
            text = message.content.strip().lower()
            if text.startswith(self.config.stats_prefix):
                await self.handle_text_command(message)
            elif text in self.config.like_keywords and self.keyword_allowed(message.guild.id, message.channel, text):
                await self.keyword_like(message)
            elif self.is_play_command(text, message.guild.id):
                self.requests.record(message, time.time())
            return
        if self.watching(message.channel.id) and self.is_music_bot(message.author, message.guild.id):
            self._music_seen[message.guild.id] = time.time()
            await self.handle_music_message(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # Some music bots post a placeholder and edit the song in afterwards.
        if after.guild and self.watching(after.channel.id) and self.is_music_bot(after.author, after.guild.id):
            self._music_seen[after.guild.id] = time.time()
            await self.handle_music_message(after)

    def _requester(self, message: discord.Message, track: Track, tracker: RequestTracker,
                   now: float) -> tuple[int | None, str | None]:
        """Prefer sources that give a real user ID; fall back to the name the music bot printed."""
        if track.requester_id:
            return track.requester_id, track.requester_name
        meta = getattr(message, "interaction_metadata", None)  # music bot answering a slash command
        if meta and getattr(meta, "user", None):
            return meta.user.id, meta.user.display_name
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message) and not ref.author.bot:
            return ref.author.id, ref.author.display_name
        if found := tracker.match(message.channel.id, message.guild.id, track.requester_name, now):
            return found
        return None, track.requester_name

    def _dump(self, message: discord.Message) -> None:
        if not self.config.dump_path:
            return
        record = raw_record(message)
        with open(self.config.dump_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def handle_music_message(self, message: discord.Message, live: bool = True,
                                   tracker: RequestTracker | None = None) -> None:
        """Record what the music bot posted. live=False is for backfilling old messages:
        no reactions, announcements or voice lookups, and times come from the message."""
        if live:
            self._dump(message)
        track = parse_message(snapshot(message))
        if live:
            self._recent_music[(message.guild.id, message.author.id)].append((time.time(), raw_record(message), track))
        if not track:
            return
        now = time.time() if live else message.created_at.timestamp()
        requester_id, requester_name = self._requester(message, track, tracker or self.requests, now)

        if track.kind == Kind.QUEUED:
            self.store.add_pending(message.guild.id, track.title, requester_id, requester_name,
                                   track.length_seconds)
            # ❤️ on "Added Track" saves that song too, even before it plays
            self.store.add_track_message(message.id, message.guild.id, message.channel.id, track)
            if live and self.config.react:
                try:
                    await message.add_reaction(HEART)
                except discord.HTTPException as exc:
                    log.warning("Couldn't add reaction (missing Add Reactions permission?): %s", exc)
            return

        length = track.length_seconds
        if (requester_id is None and requester_name is None) or length is None:
            if pending := self.store.pop_pending(message.guild.id, track.title):
                if requester_id is None and requester_name is None:
                    requester_id, requester_name = pending.requester_id, pending.requester_name
                length = length or pending.length_seconds
        voice_channel = self.voice_channel_of(message.guild, message.author.id) if live else None
        play_id, created = self.store.record_play(
            message.guild.id, message.channel.id, message.id, track, requester_id, requester_name,
            voice_channel_id=voice_channel.id if voice_channel else None,
            at=None if live else now, length_seconds=length)
        if not created or not live:
            return
        if voice_channel:  # remember who was listening, for "songs I heard" and taste matching
            self.store.add_listeners(play_id, [m.id for m in getattr(voice_channel, "members", []) if not m.bot])
        log.info("Now playing in %s / %s: %s — %s", message.guild.name,
                 voice_channel.name if voice_channel else "?", track.title, track.artist)
        if self.config.react:
            try:
                await message.add_reaction(HEART)
            except discord.HTTPException as exc:
                log.warning("Couldn't add reaction (missing Add Reactions permission?): %s", exc)
        await self.announce(message, play_id, voice_channel)

    async def backfill(self, guild: discord.Guild, per_channel: int = 2000) -> tuple[int, int]:
        """Read back through old messages and log the music bot's past plays. Returns (channels, new plays)."""
        tracker = RequestTracker()
        before = self.store.count_plays(guild.id)
        channels = [c for c in (*guild.text_channels, *guild.voice_channels)
                    if c.permissions_for(guild.me).view_channel and c.permissions_for(guild.me).read_message_history]
        for channel in channels:
            try:
                messages = [m async for m in channel.history(limit=per_channel)]
            except discord.HTTPException:
                continue
            for m in reversed(messages):  # oldest first, so requests line up with the songs they queued
                if not m.author.bot:
                    if self.is_play_command(m.content.strip().lower(), guild.id):
                        tracker.record(m, m.created_at.timestamp())
                elif self.is_music_bot(m.author, guild.id):
                    await self.handle_music_message(m, live=False, tracker=tracker)
        return len(channels), self.store.count_plays(guild.id) - before

    async def announce(self, source: discord.Message, play_id: int,
                       voice_channel: discord.abc.GuildChannel | None) -> None:
        """Post "Now playing" in the music bot's voice-channel chat, or a fixed channel."""
        mode, channel_id = self.announce_settings(source.guild.id)
        fixed = source.guild.get_channel(channel_id) if channel_id else None
        if mode == "follow":
            target = voice_channel or fixed
        elif mode == "channel":
            target = fixed
        else:
            return
        if target is None or target.id == source.channel.id:
            return  # nowhere to post, or the music bot already posted right there
        play = self.store.play_by_id(play_id)
        try:
            sent = await target.send(embed=_play_embed(play, "Now playing"), view=_save_view(play_id))
            # No ❤️ row on our own card: it already has a Save button. A ❤️ someone adds still saves.
            self.store.add_announcement(sent.id, play_id)
        except discord.HTTPException as exc:
            log.warning("Couldn't announce in #%s (check Send Messages there): %s",
                        getattr(target, "name", target.id), exc)

    # --- saving & unsaving (every path goes through these) ---------------
    def dm_enabled(self, user_id: int) -> bool:
        pref = self.store.get_dm_pref(user_id)
        return self.config.dm_on_save if pref is None else pref

    async def save(self, user_id: int, guild_id: int, title: str, artist: str | None, url: str | None,
                   play: Play | None = None, on_dm_blocked: Notify | None = None) -> bool:
        """Save a song to someone's list and DM them a card for it. False if it was already saved."""
        like_id = self.store.add_like(guild_id, user_id, title, artist, url, play.id if play else None)
        log.info("Save by %s: %s — %s (%s)", user_id, title, artist, "new" if like_id else "already saved")
        if like_id is None:
            return False
        if self.dm_enabled(user_id):
            # In the background: buttons and slash commands must answer Discord within 3 seconds,
            # and sending a DM can take longer than that.
            self._spawn(self._send_save_dm(user_id, like_id, play, on_dm_blocked))
        return True

    async def save_play(self, user_id: int, play: Play, on_dm_blocked: Notify | None = None) -> bool:
        return await self.save(user_id, play.guild_id, play.title, play.artist, play.url, play, on_dm_blocked)

    @staticmethod
    def tell_privately(interaction: discord.Interaction) -> Notify:
        """For buttons and slash commands: a follow-up only that person can see."""
        return lambda text: interaction.followup.send(text, ephemeral=True)

    def tell_in_channel(self, channel_id: int, user_id: int) -> Notify:
        """For hearts (no private reply possible): a short @mention in the channel that deletes itself,
        at most once an hour per person so it doesn't turn into spam."""
        async def notify(text: str) -> None:
            if time.time() - self._dm_blocked_noticed.get(user_id, 0) < 3600:
                return
            self._dm_blocked_noticed[user_id] = time.time()
            await self.get_partial_messageable(channel_id).send(
                f"<@{user_id}> {text}", delete_after=60,
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]))
        return notify

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)  # keep a reference so the task isn't garbage-collected mid-flight
        task.add_done_callback(self._tasks.discard)

    async def wait_for_background(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def _send_save_dm(self, user_id: int, like_id: int, play: Play | None,
                            on_dm_blocked: Notify | None = None) -> None:
        like = self.store.get_like(like_id)
        if not like:
            return  # unsaved again before we got to it
        guild = self.get_guild(like.guild_id)
        guild_name = guild.name if guild else "the server"
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            sent = await user.send(embed=_dm_embed(like, guild_name, play), view=_unsave_view(like_id))
        except discord.Forbidden as exc:
            # Discord refuses (their privacy settings for this server, or they haven't finished its
            # rules/onboarding screen). The song is still saved, so tell them rather than fail silently.
            log.info("Couldn't DM user %s: %s", user_id, exc)
            if on_dm_blocked:
                try:
                    await on_dm_blocked(dm_blocked_text(like.title, guild_name))
                except discord.HTTPException as notice_exc:
                    log.info("Couldn't tell user %s either: %s", user_id, notice_exc)
            return
        except discord.HTTPException as exc:
            log.info("Couldn't DM user %s: %s", user_id, exc)
            return
        if self.store.get_like(like_id):
            self.store.set_like_dm(like_id, sent.channel.id, sent.id)
        else:  # unsaved while the DM was on its way: take the card back
            try:
                await sent.delete()
            except discord.HTTPException:
                pass

    async def forget_dm(self, like: Like | None) -> None:
        """Delete the DM card for a song that was just unsaved, so the DMs stay in sync with the list."""
        if not (like and like.dm_channel_id and like.dm_message_id):
            return
        try:
            await self.get_partial_messageable(like.dm_channel_id).get_partial_message(like.dm_message_id).delete()
        except discord.HTTPException:
            pass

    # --- saving: keywords, reactions, buttons ---------------------------
    def keyword_allowed(self, guild_id: int, channel: discord.abc.GuildChannel, keyword: str) -> bool:
        """'!like' is an explicit command and works anywhere. A bare '❤️' message only counts in
        music-related channels, so a heart in general chat doesn't silently save songs."""
        if keyword.startswith("!") or isinstance(channel, VoiceLike):
            return True
        _, announce_channel = self.announce_settings(guild_id)
        if channel.id == announce_channel:
            return True
        if self.config.channel_ids and channel.id in self.config.channel_ids:
            return True
        return self.store.is_music_channel(channel.id)

    async def keyword_like(self, message: discord.Message) -> None:
        play = self.current_play(message.guild, message.author, message.channel)
        if not play:
            await message.reply(self.with_hint(message.guild.id, "Nothing has played yet."), mention_author=False,
                                delete_after=60)
            return
        added = await self.save_play(message.author.id, play,
                                     self.tell_in_channel(message.channel.id, message.author.id))
        await message.reply(_saved_text(added, play), mention_author=False, delete_after=30,
                            suppress_embeds=True)

    # --- p! text commands -------------------------------------------------
    async def save_row(self, interaction: discord.Interaction, row: SongRow) -> None:
        """'Save a song from this page' menu on song lists."""
        added = await self.save(interaction.user.id, interaction.guild_id, row.title, row.artist, row.url, row.play,
                                self.tell_privately(interaction))
        await interaction.response.send_message(_saved_text(added, row.title, row.artist, row.url), ephemeral=True)

    def song_list(self, guild_id: int, title: str, rows: list[SongRow], empty: str) -> SongPager:
        return SongPager(title, rows, self.save_row, self.with_hint(guild_id, empty))

    async def handle_text_command(self, message: discord.Message) -> None:
        """p!stats, p!played @panda 7d, p!panda, p!top month ... (see p!help)."""
        p = self.config.stats_prefix
        words = message.content.strip()[len(p):].split()
        cmd = words[0].lower() if words else "help"
        log.info("%s%s by %s in #%s: %s", p, cmd, message.author, getattr(message.channel, "name", "?"),
                 " ".join(words[1:])[:80])
        rest = words[1:]
        mentioned = [m for m in message.mentions if not m.bot]
        period: Period | None = None
        name_words = []
        for w in rest:
            if w.startswith("<@"):
                continue
            if period is None and (pp := parse_period(w)):
                period = pp
            else:
                name_words.append(w)
        guild = message.guild

        def person(default_self: bool = True) -> tuple[int, str] | None | str:
            """Mention → typed name (from who has requested songs) → the author. 'unknown' if a name didn't match."""
            if mentioned:
                return mentioned[0].id, mentioned[0].display_name
            if name_words:
                return self.store.find_requester(guild.id, " ".join(name_words)) or "unknown"
            return (message.author.id, message.author.display_name) if default_self else None

        async def reply(embed: discord.Embed, view: discord.ui.View | None = None) -> None:
            sent = await message.reply(embed=embed, view=view, mention_author=False)
            if isinstance(view, SongPager):
                view.message = sent

        async def unknown_person() -> None:
            await message.reply(f"I don't know anyone called **{' '.join(name_words)}** who has requested songs. "
                                f"Try an @mention.", mention_author=False)

        def per(default: str) -> Period:
            return period or parse_period(default)

        if cmd in ("help", "commands", "h", "?"):
            await reply(text_help_embed(p, self.config.support_url),
                        link_view(("Add to your server", "➕", invite_url(self.user.id)),
                                  ("Support server", "💬", self.config.support_server),
                                  ("Buy me a coffee", "☕", self.config.support_url)))
        elif cmd in ("invite", "add"):
            await reply(invite_embed(), link_view(("Add to your server", "➕", invite_url(self.user.id)),
                                                  ("Support server", "💬", self.config.support_server)))
        elif cmd in ("support", "coffee", "tip", "donate", "kofi"):
            await reply(support_embed(self.config.support_url), support_view(self.config.support_url))
        elif cmd in ("stats", "me", "profile"):
            who_ = person()
            if who_ == "unknown":
                return await unknown_person()
            await reply(self.hinted(guild.id, stats_embed(self.store, guild.id, who_[0], who_[1], per("30d"))))
        elif cmd in ("played", "requests", "requested"):
            who_ = person()
            if who_ == "unknown":
                return await unknown_person()
            pr = per("7d")
            pager = self.song_list(guild.id, f"🎵 Requested by {who_[1]} · {pr.label}",
                                   rows_from_plays(self.store.requests_by(guild.id, who_[0], pr.since)),
                                   "No requests in this period.")
            await reply(pager.embed(), pager)
        elif cmd in ("saved", "likes", "liked"):
            who_ = person()
            if who_ == "unknown":
                return await unknown_person()
            pager = self.song_list(guild.id, f"{HEART} Saved by {who_[1]}",
                                   rows_from_likes(self.store.likes(guild.id, who_[0])), "No saved songs yet.")
            await reply(pager.embed(), pager)
        elif cmd in ("heard", "listened"):
            who_ = person()
            if who_ == "unknown":
                return await unknown_person()
            pr = per("24h")
            pager = self.song_list(guild.id, f"🎧 Heard by {who_[1]} · {pr.label}",
                                   rows_from_plays(self.store.heard_by(guild.id, who_[0], pr.since), True),
                                   "Nothing yet. I only know what you heard since I started watching voice.")
            await reply(pager.embed(), pager)
        elif cmd in ("history", "recent"):
            pr = per("24h")
            pager = self.song_list(guild.id, f"🕘 Played in {guild.name} · {pr.label}",
                                   rows_from_plays(self.store.recent(guild.id, pr.since), True),
                                   "Nothing played in this period.")
            await reply(pager.embed(), pager)
        elif cmd in ("top", "charts", "chart"):
            await reply(self.hinted(guild.id, top_embed(self.store, guild.id, per("7d"))))
        elif cmd in ("dj", "djs", "leaderboard", "lb"):
            await reply(self.hinted(guild.id, dj_embed(self.store, guild.id, per("30d"))))
        elif cmd in ("match", "compat", "taste"):
            people = [(m.id, m.display_name) for m in mentioned]
            if not people and name_words:
                found = self.store.find_requester(guild.id, " ".join(name_words))
                if not found:
                    return await unknown_person()
                people = [found]
            if not people:
                await message.reply(f"Who with? `{p}match @someone` or `{p}match @a @b`", mention_author=False)
                return
            if len(people) == 1:
                people.insert(0, (message.author.id, message.author.display_name))
            await reply(match_embed(self.store, guild.id, people[0], people[1]))
        elif cmd in ("throwback", "random", "tb"):
            play = self.store.random_play(guild.id)
            if not play:
                await message.reply(self.with_hint(guild.id, "No history yet."), mention_author=False)
                return
            await reply(throwback_embed(play), _save_view(play.id))
        elif cmd == "backfill":
            if not message.author.guild_permissions.manage_guild:
                await message.reply("Only people with Manage Server can run a backfill.", mention_author=False)
                return
            depth = next((int(w) for w in rest if w.isdigit()), 2000)
            await self.run_backfill(guild, lambda text: message.reply(text, mention_author=False), depth)
        else:
            # "p!panda 7d": a person's name used as the command → their requests
            name_words.insert(0, words[0])
            found = self.store.find_requester(guild.id, " ".join(name_words))
            if not found:
                await message.reply(f"Unknown command. Try `{p}help`.", mention_author=False)
                return
            pr = per("7d")
            pager = self.song_list(guild.id, f"🎵 Requested by {found[1]} · {pr.label}",
                                   rows_from_plays(self.store.requests_by(guild.id, found[0], pr.since)),
                                   "No requests in this period.")
            await reply(pager.embed(), pager)

    async def run_backfill(self, guild: discord.Guild, send, per_channel: int = 2000) -> None:
        per_channel = max(100, min(per_channel, 50000))
        if guild.id in self._backfilling:
            await send("A backfill is already running.")
            return
        self._backfilling.add(guild.id)
        status = await send(f"📚 Reading back through the last {per_channel:,} messages in each channel for "
                            f"past songs… this can take a few minutes.")

        async def work() -> None:
            try:
                channels, added = await self.backfill(guild, per_channel)
                text = f"📚 Backfill done: checked {channels} channels, added **{added}** past plays."
                log.info("Backfill in %s: %d channels, %d new plays", guild.name, channels, added)
            except Exception as exc:  # report rather than fail silently in the background
                log.exception("Backfill failed")
                text = f"Backfill stopped with an error: {exc}"
            finally:
                self._backfilling.discard(guild.id)
            try:
                await status.edit(content=text)
            except (discord.HTTPException, AttributeError):
                await send(text)
        self._spawn(work())

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.emoji.name not in HEART_NAMES or payload.user_id == self.user.id:
            return
        if payload.member and payload.member.bot:
            return
        notify = self.tell_in_channel(payload.channel_id, payload.user_id)
        if play := self.store.play_by_message(payload.message_id):
            await self.save_play(payload.user_id, play, notify)
        elif queued := self.store.track_by_message(payload.message_id):  # an "Added Track" message
            guild_id, title, artist, url = queued
            await self.save(payload.user_id, guild_id, title, artist, url, on_dm_blocked=notify)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.emoji.name not in HEART_NAMES or payload.user_id == self.user.id:
            return
        if play := self.store.play_by_message(payload.message_id):
            await self.forget_dm(self.store.remove_like_for_play(payload.user_id, play))
        elif queued := self.store.track_by_message(payload.message_id):
            guild_id, title, artist, _ = queued
            await self.forget_dm(self.store.remove_like_for_track(guild_id, payload.user_id, title, artist))

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id.startswith(SAVE_PREFIX):
            play = self.store.play_by_id(int(custom_id.removeprefix(SAVE_PREFIX)))
            if not play:
                await interaction.response.send_message("That song isn't in the history any more.", ephemeral=True)
                return
            added = await self.save_play(interaction.user.id, play, self.tell_privately(interaction))
            await interaction.response.send_message(_saved_text(added, play), ephemeral=True)
        elif custom_id.startswith(UNSAVE_PREFIX):
            # "Remove from list" button on a DM card
            like_id = int(custom_id.removeprefix(UNSAVE_PREFIX))
            like = self.store.get_like(like_id)
            if like and like.user_id != interaction.user.id:
                await interaction.response.send_message("That isn't your saved song.", ephemeral=True)
                return
            if like:
                self.store.remove_like(like_id)
            await interaction.response.defer()
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass


def register_commands(bot: CompanionBot) -> None:
    tree = bot.tree

    @tree.command(name="nowplaying", description="Show the song that's playing, with a save button")
    @app_commands.guild_only()
    async def nowplaying(interaction: discord.Interaction) -> None:
        play = bot.current_play(interaction.guild, interaction.user, interaction.channel)
        if not play:
            await interaction.response.send_message(
                bot.with_hint(interaction.guild_id, "I haven't seen anything play yet."), ephemeral=True)
            return
        await interaction.response.send_message(embed=_play_embed(play, "Last started playing"),
                                                view=_save_view(play.id))

    @tree.command(name="save", description="Save the song that's playing to your list")
    @app_commands.guild_only()
    async def save(interaction: discord.Interaction) -> None:
        play = bot.current_play(interaction.guild, interaction.user, interaction.channel)
        if not play:
            await interaction.response.send_message(
                bot.with_hint(interaction.guild_id, "I haven't seen anything play yet."), ephemeral=True)
            return
        added = await bot.save_play(interaction.user.id, play, bot.tell_privately(interaction))
        await interaction.response.send_message(_saved_text(added, play), ephemeral=True)

    @tree.command(name="support", description="Help cover PandaBot's server costs")
    async def support(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=support_embed(bot.config.support_url),
                                                view=support_view(bot.config.support_url) or discord.utils.MISSING)

    @tree.command(name="invite", description="Add PandaBot to another server")
    async def invite(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=invite_embed(), view=link_view(("Add to your server", "➕", invite_url(bot.user.id)),
                                                 ("Support server", "💬", bot.config.support_server)))

    @tree.command(name="forgetme", description="Delete everything PandaBot stores about you, in every server")
    async def forgetme(interaction: discord.Interaction) -> None:
        view = discord.ui.View(timeout=120)
        confirm = discord.ui.Button(label="Yes, delete my data", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def do_confirm(i: discord.Interaction) -> None:
            likes, counts = bot.store.forget_user(i.user.id)
            for like in likes:
                bot._spawn(bot.forget_dm(like))
            log.info("Forgot user %s: %s", i.user.id, counts)
            await i.response.edit_message(view=None, content=(
                f"Done. Deleted {counts['saves']} saved song(s) and their DM cards, and "
                f"{counts['heard']} listening record(s), and took your name off "
                f"{counts['requests']} song request(s). Server play history keeps the songs themselves."))

        async def do_cancel(i: discord.Interaction) -> None:
            await i.response.edit_message(view=None, content="Cancelled. Nothing was deleted.")

        confirm.callback, cancel.callback = do_confirm, do_cancel
        view.add_item(confirm)
        view.add_item(cancel)
        await interaction.response.send_message(
            "This deletes, in **every** server: your saved songs (and their DM cards), the record of songs "
            "you heard in voice, your DM setting, and your name on songs you requested. It can't be undone.",
            view=view, ephemeral=True)

    @tree.command(name="dms", description="Get (or stop getting) a DM card for each song you save")
    async def dms(interaction: discord.Interaction, enabled: bool) -> None:
        bot.store.set_dm_pref(interaction.user.id, enabled)
        msg = ("I'll DM you a card for each song you save. Your DMs with me become your running list."
               if enabled else "OK, no more DMs. Your list is still in /likes and /export.")
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="setupcheck", description="List channels I can't see or post in")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setupcheck(interaction: discord.Interaction) -> None:
        problems = setup_problems(interaction.guild, interaction.guild.me)
        jockies = [ch for ch in (*interaction.guild.voice_channels, *interaction.guild.stage_channels)
                   if any(m.bot and bot.is_music_bot(m, interaction.guild_id) for m in ch.members)]
        lines = (["**All channels look good.**"] if not problems else
                 ["**Things to fix:**", *("• " + p for p in problems), "", FIX_HOW])
        lines.append("")
        lines.append("Music bot in voice now: " + (", ".join(f"🔊{c.name}" for c in jockies) or
                                                   "none I can see (start a song, then run this again)"))
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    musicbot = app_commands.Group(name="musicbot", description="Which music bots PandaBot follows in this server",
                                  default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def forget_cache(guild_id: int) -> None:
        bot._music_bot_cache.pop(guild_id, None)

    @musicbot.command(name="add", description="Follow another music bot in this server (Jockie is always on)")
    @app_commands.describe(music_bot="The music bot to follow",
                           prefix="Its text-command prefix, e.g. ! or ;; (leave empty if it only uses /play)")
    async def musicbot_add(interaction: discord.Interaction, music_bot: discord.Member,
                           prefix: app_commands.Range[str, 1, 10] | None = None) -> None:
        if not music_bot.bot or music_bot.id == bot.user.id:
            await interaction.response.send_message("Pick a music **bot** (not a person, and not me).", ephemeral=True)
            return
        if bot.is_music_bot(music_bot):  # Jockie (every instance) is followed in every server already
            await interaction.response.send_message(
                f"**{music_bot.display_name}** is already followed automatically, in every server, so there's "
                f"nothing to add. Play a song with it and I'll post a card in the voice channel's chat.\n"
                f"Nothing showing up? Run `/setupcheck` to find channels I can't see.", ephemeral=True)
            return
        prefix = (prefix or "").strip() or None
        if prefix and " " in prefix:
            await interaction.response.send_message("The prefix can't contain spaces.", ephemeral=True)
            return
        bot.store.add_music_bot(interaction.guild_id, music_bot.id, music_bot.display_name, prefix)
        forget_cache(interaction.guild_id)
        requests = "`/play` requests are credited automatically" + (
            f", and so are `{prefix}play` / `{prefix}p`" if prefix else "")
        await interaction.response.send_message(
            f"Following **{music_bot.display_name}** in this server. {requests}.\n"
            f"Play a couple of songs with it, then run `/musicbot check` to see how I read its messages.",
            ephemeral=True)

    @musicbot.command(name="remove", description="Stop following a music bot you added")
    async def musicbot_remove(interaction: discord.Interaction, music_bot: discord.Member) -> None:
        removed = bot.store.remove_music_bot(interaction.guild_id, music_bot.id)
        forget_cache(interaction.guild_id)
        note = (" Jockie is followed by default, so it stays on."
                if not removed and music_bot.name.lower().startswith(bot.config.music_bot_name_prefix) else "")
        await interaction.response.send_message(
            (f"Stopped following **{music_bot.display_name}**." if removed
             else f"I wasn't following **{music_bot.display_name}** as an added bot.") + note, ephemeral=True)

    @musicbot.command(name="list", description="Music bots PandaBot follows in this server")
    async def musicbot_list(interaction: discord.Interaction) -> None:
        default = (", ".join(f"<@{i}>" for i in bot.config.music_bot_ids) if bot.config.music_bot_ids
                   else f"any bot named \"{bot.config.music_bot_name_prefix.title()}…\"")
        lines = [f"**Always:** {default} · prefix " + ", ".join(f"`{p}`" for p in bot.config.command_prefixes)]
        for bot_id, name, prefix in bot.store.music_bots(interaction.guild_id):
            lines.append(f"<@{bot_id}> · " + (f"prefix `{prefix}`" if prefix else "slash commands only"))
        lines.append("\nAdd one with `/musicbot add`. Check one with `/musicbot check`.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @musicbot.command(name="check", description="See how PandaBot read a music bot's recent messages")
    @app_commands.describe(music_bot="Leave empty to check every music bot that posted recently")
    async def musicbot_check(interaction: discord.Interaction, music_bot: discord.Member | None = None) -> None:
        recent = {bot_id: entries for (gid, bot_id), entries in bot._recent_music.items()
                  if gid == interaction.guild_id and entries and (music_bot is None or bot_id == music_bot.id)}
        if not recent:
            who_ = f"**{music_bot.display_name}**" if music_bot else "a music bot"
            await interaction.response.send_message(
                f"I haven't seen {who_} post since I last restarted. Play a song with it, then try again."
                + ("" if music_bot is None or bot.is_music_bot(music_bot, interaction.guild_id)
                   else f" (I'm not following it yet: `/musicbot add` first.)"), ephemeral=True)
            return
        e = discord.Embed(title="🔎 How I read recent music-bot messages", colour=discord.Colour.blurple())
        raw = []
        for bot_id, entries in recent.items():
            lines = [f"<t:{int(at)}:t> {describe_reading(track, record)}" for at, record, track in list(entries)[-5:]]
            e.add_field(name=entries[-1][1]["author"][:256], value="\n".join(lines)[:1024], inline=False)
            raw += [record for _, record, _ in entries]
        e.set_footer(text="Something misread? Send the attached file to whoever maintains PandaBot.")
        file = discord.File(io.BytesIO(json.dumps(raw, ensure_ascii=False, indent=1).encode()),
                            filename="music_bot_messages.json")
        await interaction.response.send_message(embed=e, file=file, ephemeral=True)

    tree.add_command(musicbot)

    @tree.command(name="announce", description="Choose where new songs get announced")
    @app_commands.describe(
        mode="follow = the voice chat the music bot is in; channel = always one channel; off = don't announce",
        channel="The channel to use for 'channel' mode (also the fallback for 'follow')")
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in ANNOUNCE_MODES])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def announce(interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None,
                       channel: discord.TextChannel | discord.VoiceChannel | discord.StageChannel | None = None
                       ) -> None:
        current_mode, current_channel = bot.announce_settings(interaction.guild_id)
        if mode is None and channel is None:
            where = f" (fallback/target: <#{current_channel}>)" if current_channel else ""
            await interaction.response.send_message(f"Announcements: **{current_mode}**{where}", ephemeral=True)
            return
        new_mode = mode.value if mode else current_mode
        new_channel = channel.id if channel else current_channel
        if new_mode == "channel" and not new_channel:
            await interaction.response.send_message("Pick a `channel` for channel mode.", ephemeral=True)
            return
        bot.store.set_announce_settings(interaction.guild_id, new_mode, new_channel)
        msg = {
            "follow": "I'll announce each song in the voice chat the music bot is playing in"
                      + (f", or <#{new_channel}> if I can't tell." if new_channel else "."),
            "channel": f"I'll announce each song in <#{new_channel}>.",
            "off": "Announcements are off. ❤️ on the music bot's messages and /save still work.",
        }[new_mode]
        await interaction.response.send_message(msg, ephemeral=True)

    period_choices = [app_commands.Choice(name=c, value=c) for c in PERIOD_CHOICES]

    def pick(choice: app_commands.Choice[str] | None, default: str) -> Period:
        return parse_period(choice.value if choice else default)

    async def send_list(interaction: discord.Interaction, pager: SongPager, ephemeral: bool = False) -> None:
        await interaction.response.send_message(embed=pager.embed(), view=pager, ephemeral=ephemeral)
        pager.message = await interaction.original_response()

    @tree.command(name="likes", description="Saved songs (yours or someone else's), with paging")
    @app_commands.describe(member="Whose list to show", public="Post it in the channel instead of just to you")
    @app_commands.guild_only()
    async def likes(interaction: discord.Interaction, member: discord.Member | None = None,
                    public: bool = False) -> None:
        member = member or interaction.user
        pager = bot.song_list(interaction.guild_id, f"{HEART} Saved by {member.display_name}",
                              rows_from_likes(bot.store.likes(interaction.guild_id, member.id)),
                              "No saved songs yet.")
        await send_list(interaction, pager, ephemeral=not public)

    @tree.command(name="stats", description="Someone's music profile: requests, top artists, saves")
    @app_commands.choices(period=period_choices)
    @app_commands.guild_only()
    async def stats(interaction: discord.Interaction, member: discord.Member | None = None,
                    period: app_commands.Choice[str] | None = None) -> None:
        member = member or interaction.user
        await interaction.response.send_message(
            embed=bot.hinted(interaction.guild_id, stats_embed(bot.store, interaction.guild_id, member.id,
                                                               member.display_name, pick(period, "30d"))))

    @tree.command(name="played", description="Everything someone requested in a time period")
    @app_commands.choices(period=period_choices)
    @app_commands.guild_only()
    async def played(interaction: discord.Interaction, member: discord.Member | None = None,
                     period: app_commands.Choice[str] | None = None) -> None:
        member, pr = member or interaction.user, pick(period, "7d")
        await send_list(interaction, bot.song_list(interaction.guild_id, 
            f"🎵 Requested by {member.display_name} · {pr.label}",
            rows_from_plays(bot.store.requests_by(interaction.guild_id, member.id, pr.since)),
            "No requests in this period."))

    @tree.command(name="heard", description="Songs that played while you were in voice")
    @app_commands.choices(period=period_choices)
    @app_commands.guild_only()
    async def heard(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None) -> None:
        pr = pick(period, "24h")
        await send_list(interaction, bot.song_list(interaction.guild_id, 
            f"🎧 Heard by {interaction.user.display_name} · {pr.label}",
            rows_from_plays(bot.store.heard_by(interaction.guild_id, interaction.user.id, pr.since), True),
            "Nothing yet. I only know what you heard since I started watching voice."), ephemeral=True)

    @tree.command(name="history", description="Everything played in the server")
    @app_commands.choices(period=period_choices)
    @app_commands.guild_only()
    async def history(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None) -> None:
        pr = pick(period, "24h")
        await send_list(interaction, bot.song_list(interaction.guild_id, 
            f"🕘 Played in {interaction.guild.name} · {pr.label}",
            rows_from_plays(bot.store.recent(interaction.guild_id, pr.since), True),
            "Nothing played in this period."), ephemeral=True)

    @tree.command(name="top", description="Server charts: songs, artists, requesters, most saved")
    @app_commands.choices(period=period_choices)
    @app_commands.guild_only()
    async def top(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None) -> None:
        await interaction.response.send_message(embed=bot.hinted(interaction.guild_id, top_embed(bot.store, interaction.guild_id, pick(period, "7d"))))

    @tree.command(name="dj", description="Whose requests other people save the most")
    @app_commands.choices(period=period_choices)
    @app_commands.guild_only()
    async def dj(interaction: discord.Interaction, period: app_commands.Choice[str] | None = None) -> None:
        await interaction.response.send_message(embed=bot.hinted(interaction.guild_id, dj_embed(bot.store, interaction.guild_id, pick(period, "30d"))))

    @tree.command(name="match", description="Taste match between you and someone (or two people)")
    @app_commands.guild_only()
    async def match(interaction: discord.Interaction, member: discord.Member,
                    other: discord.Member | None = None) -> None:
        if other:  # compare two other people
            a, b = (member.id, member.display_name), (other.id, other.display_name)
        else:  # compare yourself with someone
            a, b = (interaction.user.id, interaction.user.display_name), (member.id, member.display_name)
        await interaction.response.send_message(embed=match_embed(bot.store, interaction.guild_id, a, b))

    @tree.command(name="throwback", description="A random song from the server's history")
    @app_commands.guild_only()
    async def throwback(interaction: discord.Interaction) -> None:
        play = bot.store.random_play(interaction.guild_id)
        if not play:
            await interaction.response.send_message(bot.with_hint(interaction.guild_id, "No history yet."), ephemeral=True)
            return
        await interaction.response.send_message(embed=throwback_embed(play), view=_save_view(play.id))

    @tree.command(name="backfill", description="Read back through old messages to log the music bot's past plays")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(messages="How many recent messages to read in each channel (default 2000)")
    async def backfill(interaction: discord.Interaction,
                       messages: app_commands.Range[int, 100, 50000] = 2000) -> None:
        await interaction.response.send_message("Starting…", ephemeral=True)
        await bot.run_backfill(interaction.guild, lambda text: interaction.channel.send(text), messages)

    @tree.command(name="unlike", description="Remove a song from your list by its number in /likes")
    @app_commands.guild_only()
    async def unlike(interaction: discord.Interaction, number: app_commands.Range[int, 1]) -> None:
        like = bot.store.remove_like_at(interaction.guild_id, interaction.user.id, number)
        await bot.forget_dm(like)
        msg = f"Removed {_fmt(like.title, like.artist, like.url)}" if like else "No song with that number."
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="export", description="Download your saved songs as a CSV file")
    @app_commands.guild_only()
    async def export(interaction: discord.Interaction) -> None:
        rows = bot.store.likes(interaction.guild_id, interaction.user.id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["title", "artist", "source_url", "youtube", "saved_at"])
        for l in rows:
            w.writerow([l.title, l.artist or "", l.url or "", youtube_link(l.title, l.artist, l.url),
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(l.liked_at))])
        file = discord.File(io.BytesIO(buf.getvalue().encode()), filename="saved_songs.csv")
        await interaction.response.send_message(f"{len(rows)} saved song(s).", file=file, ephemeral=True)

    @tree.context_menu(name="Save song")
    @app_commands.guild_only()
    async def save_from_message(interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click any music-bot message (even an old one) → Apps → Save song."""
        if play := bot.store.play_by_message(message.id):
            added = await bot.save_play(interaction.user.id, play, bot.tell_privately(interaction))
            await interaction.response.send_message(_saved_text(added, play), ephemeral=True)
            return
        track = parse_message(snapshot(message))
        if not track:
            await interaction.response.send_message("I couldn't find a song in that message.", ephemeral=True)
            return
        added = await bot.save(interaction.user.id, interaction.guild_id, track.title, track.artist, track.url,
                               on_dm_blocked=bot.tell_privately(interaction))
        await interaction.response.send_message(_saved_text(added, track.title, track.artist, track.url),
                                                ephemeral=True)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_TOKEN is empty. Paste your bot token into .env (see README step 1).")
    if token.startswith("http") or token.count(".") != 2:
        raise SystemExit("DISCORD_TOKEN doesn't look like a bot token (it should be three chunks separated by "
                         "dots, not a link). Developer Portal → Bot → Reset Token → Copy, then paste into .env.")
    config = Config()
    bot = CompanionBot(config, Store(config.db_path))
    try:
        bot.run(config.token, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the token. Reset it in the Developer Portal → Bot and paste the new one.")
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("Turn on 'Message Content Intent' in the Developer Portal → Bot → "
                         "Privileged Gateway Intents, click Save, then start me again.")
