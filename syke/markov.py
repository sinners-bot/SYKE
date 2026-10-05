"""Word-level Markov chains that babble in the style of a member (or the whole server)."""

from __future__ import annotations

import random
import re
from collections import defaultdict
from collections.abc import Iterable

URL_RE = re.compile(r"https?://\S+")
EVERYONE_RE = re.compile(r"@(everyone|here)")
START = "\x02"
END = "\x03"
MAX_WORDS = 40


def tokenize(text: str) -> list[str]:
    text = EVERYONE_RE.sub("@\u200b\\1", URL_RE.sub("", text))
    return text.split()


class MarkovChain:
    def __init__(self, texts: Iterable[str], order: int = 2) -> None:
        self.order = order
        self.transitions: dict[tuple[str, ...], list[str]] = defaultdict(list)
        self.originals: set[str] = set()
        self.sentences = 0
        for text in texts:
            words = tokenize(text)
            if not words:
                continue
            self.sentences += 1
            self.originals.add(" ".join(words).lower())
            padded = [START] * order + words + [END]
            for i in range(len(padded) - order):
                self.transitions[tuple(padded[i:i + order])].append(padded[i + order])

    def _walk(self, rng: random.Random) -> list[str]:
        state = (START,) * self.order
        out: list[str] = []
        while len(out) < MAX_WORDS:
            options = self.transitions.get(state)
            if not options:
                break
            word = rng.choice(options)
            if word == END:
                break
            out.append(word)
            state = (*state[1:], word)
        return out

    def generate(self, rng: random.Random | None = None, attempts: int = 30) -> str | None:
        """A new line, preferring ones that aren't a verbatim copy of a real message."""
        rng = rng or random.Random()
        fallback: str | None = None
        for _ in range(attempts):
            words = self._walk(rng)
            if not words:
                continue
            line = " ".join(words)
            if line.lower() not in self.originals and len(words) >= 3:
                return line
            if fallback is None or len(line) > len(fallback):
                fallback = line
        return fallback


def build_chain(texts: list[str]) -> MarkovChain:
    """Order 2 reads more naturally, but small corpora need order 1 to produce anything new."""
    return MarkovChain(texts, order=2 if len(texts) >= 150 else 1)
