"""Stats views: time periods, stat cards, and a paged song list you can save from."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

import discord

from .db import Like, Play, Store
from .parser import youtube_link

HEART = "❤️"
MEDALS = ["🥇", "🥈", "🥉"]


# --- formatting ------------------------------------------------------------
def fmt_song(title: str, artist: str | None, url: str | None, width: int = 80) -> str:
    """'[Title — Artist](youtube link)' trimmed to fit a list line."""
    label = f"{title} — {artist}" if artist else title
    if len(label) > width:
        label = label[: width - 1] + "…"
    label = label.replace("[", "(").replace("]", ")")
    return f"[{label}](<{youtube_link(title, artist, url)}>)"


def fmt_duration(seconds: int) -> str:
    h, m = divmod(round(seconds / 60), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def who(user_id: int | None, name: str | None) -> str:
    return f"<@{user_id}>" if user_id else (name or "someone")


# --- periods -------------------------------------------------------------
@dataclass
class Period:
    since: float
    label: str


_UNITS = [
    (r"h|hr|hrs|hours?", 3600, "hour"),
    (r"d|days?", 86400, "day"),
    (r"w|wks?|weeks?", 7 * 86400, "week"),
    (r"m|mo|mos|months?", 30 * 86400, "month"),
    (r"y|yrs?|years?", 365 * 86400, "year"),
]
PERIOD_CHOICES = ["24h", "7d", "30d", "90d", "1y", "all"]


def parse_period(token: str, now: float | None = None) -> Period | None:
    """'24h', '7d', '2w', '3m' (months), '1y', 'today', 'week', 'month', 'year', 'all'. None if not a period."""
    now = now or time.time()
    t = token.strip().lower()
    if t in ("all", "alltime", "ever", "forever"):
        return Period(0, "all time")
    if t == "today":
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        return Period(midnight.timestamp(), "today")
    named = {"day": "1d", "week": "7d", "month": "30d", "year": "1y"}
    t = named.get(t, t)
    for pattern, seconds, unit in _UNITS:
        if m := re.fullmatch(rf"(\d{{1,4}})\s*(?:{pattern})", t):
            n = int(m.group(1))
            if n == 0:
                return None
            return Period(now - n * seconds, f"last {n} {unit}{'s' if n != 1 else ''}" if n != 1 else f"last {unit}")
    return None


# --- stat cards ----------------------------------------------------------
def stats_embed(store: Store, guild_id: int, user_id: int, name: str, period: Period) -> discord.Embed:
    s = store.user_summary(guild_id, user_id, period.since)
    e = discord.Embed(title=f"🎧 {name} · {period.label}", colour=discord.Colour.blurple())
    if not (s.requests or s.saves or s.heard):
        e.description = "Nothing yet in this period. Request a song with Jockie or ❤️ one to get started."
        return e
    first = f"Requested **{s.requests}** song{'s' * (s.requests != 1)} · **{s.artists}** artist{'s' * (s.artists != 1)}"
    if s.songs_with_length:
        approx = "~" if s.songs_with_length < s.requests else ""
        first += f" · {approx}{fmt_duration(s.known_length_seconds)} of music"
    lines = [first]
    if s.heard:
        lines.append(f"Was in voice for **{s.heard}** song{'s' * (s.heard != 1)}")
    lines.append(f"Saved **{s.saves}** song{'s' * (s.saves != 1)}")
    if s.requests:
        lines.append(f"Hit rate: **{round(100 * s.hit_rate)}%** of their requests got saved by someone else")
    e.description = "\n".join(lines)
    if s.top_artists:
        e.add_field(name="Top artists", inline=False,
                    value=" · ".join(f"{a} ({n})" for a, n in s.top_artists)[:1024])
    if s.most_loved:
        t, a, n = s.most_loved
        e.add_field(name="Most-loved request", inline=False, value=f"{fmt_song(t, a, None)} {HEART} {n}")
    return e


def top_embed(store: Store, guild_id: int, period: Period) -> discord.Embed:
    e = discord.Embed(title=f"📈 Server charts · {period.label}", colour=discord.Colour.gold())
    played = store.top_played(guild_id, period.since, 5)
    if not played:
        e.description = "Nothing played in this period."
        return e
    e.add_field(name="Most played", inline=False, value="\n".join(
        f"`{n}×` {fmt_song(t, a, None, 60)}" for t, a, n in played)[:1024])
    if artists := store.top_artists(guild_id, period.since, 5):
        e.add_field(name="Top artists", inline=True, value="\n".join(f"`{n}×` {a}" for a, n in artists)[:1024])
    if reqs := store.top_requesters(guild_id, period.since, 5):
        e.add_field(name="Top requesters", inline=True,
                    value="\n".join(f"`{n}` {who(uid, nm)}" for uid, nm, n in reqs)[:1024])
    if liked := store.top_liked(guild_id, period.since, 5):
        e.add_field(name="Most saved", inline=False, value="\n".join(
            f"`{n}`{HEART} {fmt_song(t, a, None, 60)}" for t, a, n in liked)[:1024])
    return e


def dj_embed(store: Store, guild_id: int, period: Period) -> discord.Embed:
    e = discord.Embed(title=f"🎚️ DJ leaderboard · {period.label}", colour=discord.Colour.purple())
    rows = store.dj_board(guild_id, period.since)
    if not rows:
        e.description = "No requests with a known requester in this period."
        return e
    lines = []
    for i, r in enumerate(rows):
        rank = MEDALS[i] if i < 3 else f"`{i + 1}.`"
        rate = round(100 * r.loves / r.requests) if r.requests else 0
        lines.append(f"{rank} {who(r.user_id, r.name)}: {HEART} **{r.loves}** from {r.requests} request"
                     f"{'s' * (r.requests != 1)} ({rate}%)")
    e.description = "\n".join(lines)
    e.set_footer(text="Score = how often other people saved the songs they requested")
    return e


def match_embed(store: Store, guild_id: int, a: tuple[int, str], b: tuple[int, str]) -> discord.Embed:
    m = store.taste_match(guild_id, a[0], b[0])
    e = discord.Embed(title=f"💞 {a[1]} × {b[1]}", colour=discord.Colour.pink())
    if m is None:
        e.description = "Not enough listening data for one of you yet. Request, listen or save a few songs first."
        return e
    filled = round(m.score / 10)
    verdict = ("Musical soulmates" if m.score >= 80 else "Great match" if m.score >= 60 else
               "Some overlap" if m.score >= 30 else "Opposites attract")
    e.description = f"`{'█' * filled}{'░' * (10 - filled)}` **{m.score}%** · {verdict}"
    if m.shared_artists:
        e.add_field(name="You both like", value=", ".join(m.shared_artists)[:1024], inline=False)
    if m.shared_songs:
        e.add_field(name="Saved by both of you", inline=False,
                    value="\n".join(fmt_song(t, ar, None, 60) for t, ar in m.shared_songs)[:1024])
    e.set_footer(text="Based on artists each of you requested, heard and saved")
    return e


# --- paged song list -------------------------------------------------------
@dataclass
class SongRow:
    title: str
    artist: str | None
    url: str | None
    at: float
    play: Play | None = None
    note: str = ""


def rows_from_plays(plays: list[Play], show_requester: bool = False) -> list[SongRow]:
    return [SongRow(p.title, p.artist, p.url, p.played_at, p,
                    f" · {who(p.requester_id, p.requester_name)}" if show_requester and
                    (p.requester_id or p.requester_name) else "")
            for p in plays]


def rows_from_likes(likes: list[Like]) -> list[SongRow]:
    return [SongRow(l.title, l.artist, l.url, l.liked_at) for l in likes]


SaveHandler = Callable[[discord.Interaction, SongRow], Awaitable[None]]


class SongPager(discord.ui.View):
    """A song list with ◀ ▶ paging and a menu to save any song on the page."""
    PER_PAGE = 10

    def __init__(self, title: str, rows: list[SongRow], on_save: SaveHandler,
                 empty_text: str = "Nothing here yet.", timeout: float = 1800):
        super().__init__(timeout=timeout)
        self.title, self.rows, self.on_save, self.empty_text = title, rows, on_save, empty_text
        self.page = 0
        self.pages = max(1, math.ceil(len(rows) / self.PER_PAGE))
        self.message: discord.Message | None = None
        self._rebuild()

    def _chunk(self) -> list[tuple[int, SongRow]]:
        start = self.page * self.PER_PAGE
        return list(enumerate(self.rows[start:start + self.PER_PAGE], start))

    def embed(self) -> discord.Embed:
        lines = [f"`{i + 1}.` {fmt_song(r.title, r.artist, r.url, 60)} · <t:{int(r.at)}:R>{r.note}"
                 for i, r in self._chunk()]
        e = discord.Embed(title=self.title, description="\n".join(lines)[:4096] or self.empty_text,
                          colour=discord.Colour.blurple())
        if self.rows:
            e.set_footer(text=f"Page {self.page + 1}/{self.pages} · {len(self.rows)} song"
                              f"{'s' * (len(self.rows) != 1)} · pick one below to save it")
        return e

    def _rebuild(self) -> None:
        self.clear_items()
        if self.pages > 1:
            prev = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary, disabled=self.page == 0)
            nxt = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary,
                                    disabled=self.page >= self.pages - 1)
            prev.callback, nxt.callback = self._prev, self._next
            self.add_item(prev)
            self.add_item(nxt)
        if chunk := self._chunk():
            select = discord.ui.Select(placeholder=f"{HEART} Save a song from this page", options=[
                discord.SelectOption(label=f"{i + 1}. {r.title}"[:100], value=str(i),
                                     description=(r.artist or None) and r.artist[:100])
                for i, r in chunk])
            select.callback = self._make_save(select)
            self.add_item(select)

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page = min(max(self.page + delta, 0), self.pages - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _prev(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, +1)

    def _make_save(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction) -> None:
            await self.on_save(interaction, self.rows[int(select.values[0])])
        return callback

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass
