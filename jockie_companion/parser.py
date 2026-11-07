"""Parse music-bot messages (Jockie Music and similar) into tracks.

Works on plain snapshots rather than discord.py objects so it can be unit
tested without a Discord connection.

Jockie posts labelled embeds, e.g. an "Added Track" message with fields
"Track: GRAILED by 1nonly", "Track Length", "Position in queue", "Requested by".
The parser prefers such labelled fields and falls back to looser patterns
(markdown links, bold text, "Started playing X by Y") for other bots.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import quote_plus


class Kind(str, Enum):
    NOW_PLAYING = "now_playing"
    QUEUED = "queued"


@dataclass
class EmbedSnapshot:
    title: str | None = None
    description: str | None = None
    url: str | None = None
    author_name: str | None = None
    footer_text: str | None = None
    fields: list[tuple[str, str]] = field(default_factory=list)

    def all_text(self) -> str:
        parts = [self.author_name, self.title, self.description, self.footer_text]
        for name, value in self.fields:
            parts += [name, value]
        return "\n".join(p for p in parts if p)


@dataclass
class MessageSnapshot:
    content: str = ""
    embeds: list[EmbedSnapshot] = field(default_factory=list)


@dataclass
class Track:
    kind: Kind
    title: str
    artist: str | None = None
    url: str | None = None
    requester_id: int | None = None
    requester_name: str | None = None
    length_seconds: int | None = None


NOW_PLAYING_RE = re.compile(r"\b(started playing|now playing|playing now|currently playing)\b", re.I)
ADDED_TRACK_RE = re.compile(r"\b(added (track|song)|track added|song added)\b", re.I)
QUEUED_RE = re.compile(r"\b(added|queued|enqueued)\b", re.I)
MULTI_TRACK_RE = re.compile(r"\b\d+\s+(tracks|songs)\b|\badded playlist\b", re.I)
LINK_RE = re.compile(r"\[([^\]]+)\]\(<?(https?://[^)\s>]+)>?\)")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
MENTION_RE = re.compile(r"<@!?(\d+)>")
REQUESTER_RE = re.compile(
    r"(?:requested|added|queued)\s+by\s*:?\s*(?:<@!?(?P<id>\d+)>|\*{0,2}(?P<name>[^*\n()]+?)\*{0,2})\s*(?:$|\n|\))",
    re.I | re.M,
)
BY_RE = re.compile(r"\bby\s+\*{0,2}(?P<artist>[^*\n\[\]()]+?)\*{0,2}\s*(?:$|\n|[(\[`])", re.I | re.M)
DURATION_RE = re.compile(r"[`(\[]+\s*\d{1,2}:\d{2}(?::\d{2})?\s*[`)\]]+")
HEADER_RE = re.compile(
    r"^\W*(started playing|now playing|playing now|currently playing|added (to (the )?queue|track|song)"
    r"|track added|song added|queued)\W*$",
    re.I,
)
TITLE_LABELS = {"track", "song", "title", "now playing", "playing"}
ARTIST_LABELS = {"artist", "author", "uploader", "channel"}
REQUESTER_LABELS = {"requested by", "requester", "added by"}
LENGTH_LABELS = {"track length", "duration", "length", "song length"}
LABELS = TITLE_LABELS | ARTIST_LABELS | REQUESTER_LABELS | LENGTH_LABELS


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"(?m)^\s*(?:#{1,3}|-#)\s+", "", text)  # markdown headings / subtext
    text = CUSTOM_EMOJI_RE.sub("", text)
    text = DURATION_RE.sub("", text)
    text = re.sub(r"[*_`~​‎‏]", "", text)
    return re.sub(r"\s+", " ", text).strip(" -–—:|")


def _label(text: str) -> str:
    return _clean(text).lower()


def normalize(text: str | None) -> str:
    """Lowercase, punctuation-free key for matching the same song across messages."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def parse_duration(text: str | None) -> int | None:
    """'02:21' -> 141, '1:02:03' -> 3723."""
    m = re.search(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b", text or "")
    if not m:
        return None
    h, mins, secs = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return h * 3600 + mins * 60 + secs


def _kind(text: str) -> Kind | None:
    if NOW_PLAYING_RE.search(text):
        return Kind.NOW_PLAYING
    if ADDED_TRACK_RE.search(text) or ("queue" in text.lower() and QUEUED_RE.search(text)):
        return Kind.QUEUED
    return None


def _pseudo_fields(text: str | None) -> list[tuple[str, str]]:
    """Turn "**Track**\\nGRAILED by 1nonly" style lines into (label, value) pairs,
    for bots that use plain text / newer message components instead of embed fields."""
    lines = [l for l in (text or "").splitlines() if _clean(l)]
    return [(a, b) for a, b in zip(lines, lines[1:]) if _label(a) in LABELS and _label(b) not in LABELS]


def _extract_requester(text: str, fields: list[tuple[str, str]]) -> tuple[int | None, str | None]:
    for name, value in fields:
        if _label(name) in REQUESTER_LABELS:
            m = MENTION_RE.search(value)
            return (int(m.group(1)), None) if m else (None, _clean(value) or None)
    m = REQUESTER_RE.search(text)
    if not m:
        return None, None
    if m.group("id"):
        return int(m.group("id")), None
    return None, _clean(m.group("name")) or None


def _strip_requester(text: str) -> str:
    return REQUESTER_RE.sub("\n", text)


def _is_title_candidate(raw: str) -> bool:
    c = _clean(raw)
    return bool(c) and not HEADER_RE.match(c) and _label(raw) not in LABELS \
        and not MENTION_RE.search(raw) and not re.fullmatch(r"[\d:]+", c)


def _title_from(text: str) -> tuple[str | None, str | None, str]:
    """(title, url, text after the title) from a link or bold text."""
    if m := LINK_RE.search(text):
        return m.group(1), m.group(2), text[m.end():]
    for m in BOLD_RE.finditer(text):
        if _is_title_candidate(m.group(1)):
            return m.group(1), None, text[m.end():]
    return None, None, text


def _parse_block(text: str, embed: EmbedSnapshot | None) -> Track | None:
    kind = _kind(text)
    if kind is None or MULTI_TRACK_RE.search(text):
        return None
    description = embed.description if embed else text
    fields = (embed.fields if embed else []) + _pseudo_fields(description)
    requester_id, requester_name = _extract_requester(text, fields)

    def field_value(labels: set[str]) -> str | None:
        return next((v for n, v in fields if _label(n) in labels), None)

    title = url = None
    rest = ""
    if track_value := field_value(TITLE_LABELS):
        # Jockie style: a "Track" field holding "Title by Artist" (possibly as links).
        title, url, rest = _title_from(track_value)
        if not title:
            pieces = re.split(r"(\s+by\s+)", track_value, maxsplit=1, flags=re.I)
            title, rest = pieces[0], "".join(pieces[1:])
    else:
        useful_embed_title = embed.title if embed and embed.title and _is_title_candidate(embed.title) else None
        parts = [useful_embed_title, _strip_requester(description or "")] if embed else [_strip_requester(text)]
        if embed:
            parts += [v for n, v in fields if _label(n) not in REQUESTER_LABELS]
        haystack = "\n".join(p for p in parts if p)

        if m := LINK_RE.search(haystack):
            title, url, rest = m.group(1), m.group(2), haystack[m.end():]
        elif useful_embed_title:
            title, rest = useful_embed_title, haystack[len(useful_embed_title):]
        else:
            title, url, rest = _title_from(haystack)
        if not title:
            # Plain text: "Started playing Song Name by Artist"
            m = re.search(r"(?:started playing|now playing|playing now|currently playing|added|queued)\s*:?\s*(.+)",
                          haystack, re.I)
            if m:
                pieces = re.split(r"(\s+by\s+|\s+to (?:the )?queue)", m.group(1), maxsplit=1, flags=re.I)
                title, rest = pieces[0], "".join(pieces[1:])
    # Jockie puts both inside one link: [**Nobody Speak** **by** **DJ Shadow**](url)
    inline_artist = None
    if title and (parts := re.split(r"\*\*\s*by\s*\*\*", title, maxsplit=1, flags=re.I))[1:]:
        title, inline_artist = parts
    elif title and url and not BY_RE.search(LINK_RE.sub(r"\1", rest)):
        # ...or as plain text: [Tom Sawyer by Rush](url). Split at the last " by ".
        cleaned = _clean(title)
        idx = cleaned.lower().rfind(" by ")
        if idx > 0:
            title, inline_artist = cleaned[:idx], cleaned[idx + 4:]
    title = _clean(title)
    if not title or HEADER_RE.match(title):
        return None

    artist = _clean(field_value(ARTIST_LABELS)) or _clean(inline_artist) or None
    if artist is None and (m := BY_RE.search(LINK_RE.sub(r"\1", rest))):
        artist = _clean(m.group("artist")) or None
    if artist and normalize(artist) == normalize(title):
        artist = None

    return Track(
        kind=kind,
        title=title,
        artist=artist,
        url=url or (embed.url if embed else None),
        requester_id=requester_id,
        requester_name=requester_name,
        length_seconds=parse_duration(field_value(LENGTH_LABELS)),
    )


def parse_message(msg: MessageSnapshot) -> Track | None:
    for embed in msg.embeds:
        if track := _parse_block(embed.all_text(), embed):
            return track
    if msg.content:
        return _parse_block(msg.content, None)
    return None


def youtube_link(title: str, artist: str | None, url: str | None) -> str:
    """The track's own link if it's YouTube, otherwise a YouTube search (no API key needed)."""
    if url and ("youtube.com" in url or "youtu.be" in url):
        return url
    query = f"{title} {artist}" if artist else title
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"
