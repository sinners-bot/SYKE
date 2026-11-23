"""Finding the right upload of a song: the real track, not a cover, a sped-up edit or a one-hour loop.

`pick_uploads` scores search results (YouTube Music and YouTube) against the artist and title we want and
returns the good ones best first. The rest become backups the player switches to when YouTube refuses one.
"""

from __future__ import annotations

import re
import unicodedata

# Words that mean "not the original song" (allowed when the wanted title says so itself).
BAD_WORDS = ("cover", "karaoke", "instrumental", "remix", "sped up", "speed up", "slowed", "reverb", "nightcore",
             "8d", "live", "reaction", "1 hour", "one hour", "10 hours", "loop", "extended", "mashup", "tutorial",
             "piano version", "acoustic version", "type beat", "bass boosted", "clean version", "fan made", "edit",
             "parody", "lyrics translation", "перевод")
GOOD_WORDS = ("official audio", "official music video", "official video", "audio", "official", "topic")


def fold(text: str) -> str:
    """Lowercase, no accents, no brackets' punctuation; keeps every alphabet (Cyrillic, Arabic, …)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("&", " and ").replace("’", "'")
    return " ".join(re.sub(r"[^\w\s']", " ", text).split())


def words(text: str) -> set[str]:
    out = set()
    for w in fold(text).split():
        w = w.strip("'")
        if len(w) > 1 or (w and not w.isascii()):
            out.add(w)
    return out


def score_upload(wanted_title: str, wanted_artist: str, title: str, author: str, duration: float | None,
                 source: str = "") -> float:
    """How likely an upload is the song we want (higher is better, below 0 means no)."""
    want_t, want_a = words(wanted_title), words(wanted_artist)
    have = words(f"{title} {author}")
    have_title = fold(title)
    if not want_t:
        return 0.0
    title_hit = len(want_t & have) / len(want_t)
    artist_hit = len(want_a & have) / len(want_a) if want_a else 0.5
    if title_hit < 0.6:
        return -1.0
    score = 2.0 * title_hit + 1.2 * artist_hit
    wanted_full = fold(f"{wanted_title} {wanted_artist}")
    for bad in BAD_WORDS:
        if bad in have_title and bad not in wanted_full:
            score -= 1.5
    if any(good in fold(f"{title} {author}") for good in GOOD_WORDS):
        score += 0.3
    if "youtube music" in (source or "").lower() or fold(author).endswith(" topic"):
        score += 0.4
    if duration:
        if duration < 60 or duration > 900:
            score -= 2.0
        elif duration > 480:
            score -= 0.4
    return score


def split_query(query: str, title: str = "") -> tuple[str, str]:
    """(title, artist) from a radio query "Artist - Title" or a label "Title — Artist"."""
    if " - " in (query or ""):
        artist, song = query.split(" - ", 1)
        return song.strip(), artist.strip()
    if " — " in (title or ""):
        song, artist = title.split(" — ", 1)
        return song.strip(), artist.strip()
    return (query or title).strip(), ""


def pick_uploads(wanted_title: str, wanted_artist: str, results: list[dict], keep: int = 4) -> list[dict]:
    """The search results that really are this song, best first (each has title, author, duration, source)."""
    scored = []
    seen = set()
    for r in results:
        key = r.get("identifier") or r.get("url") or r.get("title")
        if key in seen:
            continue
        seen.add(key)
        s = score_upload(wanted_title, wanted_artist, r.get("title", ""), r.get("author", ""), r.get("duration"),
                         r.get("source", ""))
        if s > 2.0:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:keep]]


# For station searches without a known song: drop compilations, hour-long mixes and other non-songs.
NOT_A_SONG = re.compile(r"\b(mix|compilation|playlist|full album|1 hour|one hour|\d+ hours?|non ?stop|megamix|"
                        r"best of|top \d+|hits \d{4}|podcast|interview|reaction|tutorial|live stream)\b", re.I)


def plausible_song(title: str, duration: float | None) -> bool:
    if NOT_A_SONG.search(title or ""):
        return False
    return duration is None or 90 <= duration <= 480
