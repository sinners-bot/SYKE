"""Finding (and stitching) real server messages that fit a conversation, for yap and its replies."""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass

from .lexicon import STOPWORDS

TOKEN = re.compile(r"<a?:\w+:\d+>|:\w+:|[a-z0-9']+")
QUESTION_START = re.compile(r"^(who|what|why|how|when|where|which|is|are|do|does|did|can|could|would|should|will|wanna|u)\b")
ANSWER_START = re.compile(r"^(yes|yeah|yea|yep|ye|no|nah|nope|because|bc|cuz|cause|probably|maybe|never|always|obviously|bro|idk|ofc)\b")
MAX_PARTS = 3
MAX_REPLY = 400
FILLER = STOPWORDS | {"who's", "what's", "that's", "there's", "it'll", "anyone", "someone", "everyone",
                      "lol", "lmao", "bro", "guys", "gonna", "wanna", "kinda", "literally", "actually",
                      "though", "thing", "stuff", "still", "even", "much", "okay", "now", "today"}
SUFFIXES = ("ing", "ers", "ies", "ed", "es", "er", "ly", "s")


@dataclass
class Turn:
    author: str
    text: str


def stem(word: str) -> str:
    word = word.strip("'")
    if word.endswith("'s"):
        word = word[:-2]
    for suffix in SUFFIXES:
        if len(word) - len(suffix) >= 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def keywords(text: str) -> set[str]:
    """Stemmed content words (and custom emojis) in `text`."""
    found = set()
    for token in TOKEN.findall(text.lower()):
        if token.startswith((":", "<")):
            found.add(token)
        elif len(token) >= 3 and token not in FILLER and not token.isdigit():
            found.add(stem(token))
    return found


def query_weights(target: str, said: str | None = None, chat: list[Turn] = ()) -> dict[str, float]:
    """How much each keyword matters: the message being answered most, recent chat least."""
    weights: dict[str, float] = {}
    for i, turn in enumerate(chat):
        recency = 0.1 + 0.2 * (i + 1) / len(chat)
        for word in keywords(turn.text):
            weights[word] = max(weights.get(word, 0.0), recency)
    for word in keywords(said or ""):
        weights[word] = max(weights.get(word, 0.0), 0.3)
    for word in keywords(target):
        weights[word] = 1.0
    return weights


def search_terms(weights: dict[str, float], limit: int = 10) -> list[str]:
    words = [w for w in weights if not w.startswith((":", "<"))]
    return sorted(words, key=lambda w: (weights[w], len(w)), reverse=True)[:limit]


def is_question(text: str) -> bool:
    text = text.strip().lower()
    return text.endswith("?") or bool(QUESTION_START.match(text))


def rank(candidates: list[str], weights: dict[str, float], target: str,
         rng: random.Random | None = None) -> list[tuple[float, str]]:
    """Candidates scored by rare shared keywords, best first. Small jitter keeps replies varied."""
    rng = rng or random.Random()
    bags = [keywords(c) for c in candidates]
    df: dict[str, int] = {}
    for bag in bags:
        for word in bag & weights.keys():
            df[word] = df.get(word, 0) + 1
    n = len(candidates)
    question = is_question(target)
    scored = []
    for text, bag in zip(candidates, bags):
        score = sum(weights[w] * (1 + math.log(n / df[w])) for w in bag & weights.keys())
        if question:
            lowered = text.strip().lower()
            if ANSWER_START.match(lowered):
                score += 0.6
            elif is_question(lowered):
                score -= 0.4
        words = len(text.split())
        if words > 40:
            score -= 0.3
        scored.append((score + rng.random() * 0.15, text))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def offline_pick(ranked: list[tuple[float, str]], rng: random.Random | None = None) -> str | None:
    """Without AI: one of the closest matches, or anything if nothing matches."""
    if not ranked:
        return None
    rng = rng or random.Random()
    best = ranked[0][0]
    if best <= 0.2:
        return rng.choice(ranked)[1]
    return rng.choice([text for score, text in ranked if score >= best * 0.8])


def _word_char(c: str) -> bool:
    return c.isalnum() or c in "'’"


def _boundary(text: str, start: int, end: int) -> tuple[int, int]:
    while start > 0 and _word_char(text[start - 1]):
        start -= 1
    while end < len(text) and _word_char(text[end]):
        end += 1
    return start, end


def piece(candidate: str, chunk: str | None) -> str | None:
    """`chunk` as it really appears in `candidate` (whole words only), or None if it doesn't."""
    source = " ".join(candidate.split())
    if not chunk or not chunk.strip():
        return source
    wanted = " ".join(chunk.split()).strip(" .,!?;:-")
    if not wanted:
        return None
    start = source.lower().find(wanted.lower())
    if start < 0:
        return None
    start, end = _boundary(source, start, start + len(wanted))
    return source[start:end].strip()


def stitch(candidates: list[str], parts: list, banned: set[str] = frozenset()) -> str | None:
    """Join the AI's chosen pieces into one reply, refusing anything not copied from the candidates."""
    pieces = []
    for part in parts[:MAX_PARTS]:
        if isinstance(part, int):
            part = {"id": part}
        if not isinstance(part, dict) or not isinstance(part.get("id"), int):
            return None
        index = part["id"]
        if not 0 <= index < len(candidates):
            return None
        text = piece(candidates[index], part.get("text"))
        if text is None:
            return None
        pieces.append(text)
    reply = " ".join(pieces).strip()
    if not reply or len(reply) > MAX_REPLY:
        return None
    if " ".join(reply.lower().split()) in {" ".join(b.lower().split()) for b in banned}:
        return None
    return reply
