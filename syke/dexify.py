"""Rewrite a message the way Dex types: pauses. Lots of them."""

from __future__ import annotations

import random
import re

ELLIPSIS = "......"
TRAIL = re.compile(r"[.!?,;:~…]+$")
# Keep Discord tokens (mentions, channels, custom emoji, URLs) glued together.
TOKEN = re.compile(
    r"<a?:\w+:\d+>|<@[!&]?\d+>|<#\d+>|https?://\S+|[^\s]+",
    re.IGNORECASE,
)


def tokens_of(text: str) -> list[str]:
    return TOKEN.findall(" ".join(text.split()))


def dexify_text(text: str, rng: random.Random | None = None) -> str:
    """Insert `......` after random 1–4 word chunks of `text`."""
    rng = rng or random.Random()
    words = [TRAIL.sub("", token) or token for token in tokens_of(text)]
    words = [w for w in words if w]
    if not words:
        return ELLIPSIS

    chunks: list[str] = []
    i = 0
    while i < len(words):
        remaining = len(words) - i
        n = rng.randint(1, min(4, remaining))
        if remaining - n == 1 and remaining > 1:
            n = remaining
        chunks.append(" ".join(words[i:i + n]))
        i += n
    return f"{ELLIPSIS} ".join(chunks) + ELLIPSIS


def original_words(text: str) -> list[str]:
    """The words `dexify_text` would rewrite, without trailing punctuation."""
    return [TRAIL.sub("", token) or token for token in tokens_of(text) if TRAIL.sub("", token) or token]
