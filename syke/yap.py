"""What yap says: real server messages that fit a conversation, and the server's voice for new lines."""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .lexicon import (CHAT_SLANG, FREAKY_PHRASES, FREAKY_WORDS, LAUGH_PHRASES, LAUGH_WORDS, STOPWORDS,
                      TOXIC_PHRASES, TOXIC_WORDS)
from .markov import MarkovChain, build_chain
from .models import Msg
from .traits import score_message

TOKEN = re.compile(r"<a?:\w+:\d+>|:\w+:|[a-z0-9']+")
QUESTION_START = re.compile(r"^(who|what|why|how|when|where|which|is|are|do|does|did|can|could|would|should|will|wanna|u)\b")
ANSWER_START = re.compile(r"^(yes|yeah|yea|yep|ye|no|nah|nope|because|bc|cuz|cause|probably|maybe|never|always|obviously|bro|idk|ofc)\b")
CUSTOM_EMOJI_TAG = re.compile(r"<a?:\w+:\d+>")
URL = re.compile(r"https?://", re.IGNORECASE)
VOCAB_STRIP = " .,!?;:()[]{}\"“”*_~`|…"
PINGS = re.compile(r"@(everyone|here)\b|<@[!&]?\d+>")
SPEAKER_TAG = re.compile(r"^\**(syke|me|reply)\**\s*:\s*", re.IGNORECASE)
BOT_TELLS = re.compile(r"\b(as an ai|language model|i'?m (just )?a bot|i am (just )?a bot|i'?m syke|i am syke)\b",
                       re.IGNORECASE)
COPY_RUN = 5
VOCAB_POOL = 400
VOCAB_WORDS = 60
VOCAB_SLANG = 30
VOCAB_SLANG_MIN = 18
VOCAB_PHRASES = 8
VOCAB_EMOJIS = 10
TONES = ("toxic", "funny", "freaky")
DEFAULT_TONE = "funny"
TONE_FLOOR = 0.45
TONE_BOOST = 0.7
REMIX_PENALTY = 0.25
REMIX_MAX_WORDS = 20
# "you're so bad", "ur trash": aimed insults the word lists alone can miss.
AIMED_INSULT = re.compile(
    r"\b(you'?re|youre|you are|ur|u r|you|u)\b(\s+\w+){0,3}?\s+(bad|trash|ass|mid|washed|garbage|dogwater|"
    r"worst|ugly|dumb|stupid|clown|annoying|weird|loser|broke|cringe|fat|slow|useless|irrelevant|ratio)\b",
    re.IGNORECASE)
MAX_REPLY = 400
FILLER = STOPWORDS | {"who's", "what's", "that's", "there's", "it'll", "anyone", "someone", "everyone",
                      "lol", "lmao", "bro", "guys", "gonna", "wanna", "kinda", "literally", "actually",
                      "though", "thing", "stuff", "still", "even", "much", "okay", "now", "today"}
SUFFIXES = ("ing", "ers", "ies", "ed", "es", "er", "ly", "s")
TONE_LEXICON = {
    "toxic": (TOXIC_WORDS, TOXIC_PHRASES),
    "funny": (LAUGH_WORDS, LAUGH_PHRASES),
    "freaky": (FREAKY_WORDS, FREAKY_PHRASES),
}
# Lexicon entries that help spot toxicity but must never be handed to the AI to say.
NEVER_OFFER = {"retard", "retarded", "cunt", "kys", "kill", "incel", "yourself", "fatass", "ugly",
               "hentai", "ahegao", "nudes", "onlyfans", "breed", "bred", "choke", "degrade"}
NEVER_OFFER_PHRASES = {"go back to", "on my face", "spit in", "rail me", "send pics", "send feet", "nobody likes",
                       "no one likes"}


@dataclass
class Turn:
    author: str
    text: str


def folded(text: str) -> str:
    """Lowercased, whitespace-collapsed form used to tell two lines apart."""
    return " ".join(text.lower().split())


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
         rng: random.Random | None = None, tone: str | None = None,
         labelled: set[str] = frozenset(), remixed: set[str] = frozenset()) -> list[tuple[float, str]]:
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
        if tone:
            labels = frozenset({tone}) if text in labelled else frozenset()
            score += min(tone_scores(text, labels)[tone], 2.5) * TONE_BOOST
        if text in remixed:
            score -= REMIX_PENALTY
        scored.append((score + rng.random() * 0.4, text))
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


@dataclass
class Voice:
    """How a server talks: a Markov chain of its messages, the words it uses and how it types."""
    chain: MarkovChain | None
    words: Counter = field(default_factory=Counter)
    lowercase: float = 0.0
    periods: float = 0.0
    length: int = 0


@dataclass
class Vocab:
    """Words offered to the AI for one reply, all taken from the server or the lexicon."""
    words: list[str]
    slang: list[str]
    phrases: list[str]
    emojis: list[str]


def _vocab_token(raw: str) -> str | None:
    if CUSTOM_EMOJI_TAG.fullmatch(raw):
        return raw
    if raw.startswith(("<", "@", "#")) or URL.match(raw):
        return None
    token = raw.strip(VOCAB_STRIP).lower()
    if not token or len(token) > 20 or token.isdigit():
        return None
    if any(c.isalpha() for c in token) and not all(c.isalnum() or c in "'’-" for c in token):
        return None
    return token


def server_words(texts: Iterable[str]) -> Counter:
    """How many messages use each word or emoji (lowercased; custom emoji tags kept as they are)."""
    counts: Counter = Counter()
    for text in texts:
        found = {_vocab_token(raw) for raw in text.split()}
        found.discard(None)
        counts.update(found)
    return counts


def build_voice(texts: list[str]) -> Voice:
    texts = [t for t in texts if t.strip()]
    if not texts:
        return Voice(None)
    lengths = sorted(len(t.split()) for t in texts)
    lettered = [t for t in texts if any(c.isalpha() for c in t)] or texts
    return Voice(
        chain=build_chain(texts),
        words=server_words(texts),
        lowercase=sum(t == t.lower() for t in lettered) / len(lettered),
        periods=sum(t.rstrip().endswith(".") and not t.rstrip().endswith("..") for t in texts) / len(texts),
        length=lengths[len(lengths) // 2],
    )


def _is_emoji(token: str) -> bool:
    return bool(CUSTOM_EMOJI_TAG.fullmatch(token)) or not any(c.isalnum() for c in token)


def vocabulary(voice: Voice | None, tone: str, rng: random.Random | None = None) -> Vocab:
    """The server's own words, plus lexicon slang for `tone` (ones the server uses first)."""
    rng = rng or random.Random()
    counts = voice.words if voice else Counter()
    common = [w for w, n in counts.most_common(VOCAB_POOL)
              if n >= 2 and not _is_emoji(w) and len(w) >= 3 and w not in FILLER and w not in NEVER_OFFER]
    words = rng.sample(common, min(VOCAB_WORDS, len(common)))

    tone_words, tone_phrases = TONE_LEXICON.get(tone, TONE_LEXICON[DEFAULT_TONE])
    tone_words = {w for w in tone_words if w not in NEVER_OFFER}
    tone_phrases = [p for p in tone_phrases if not NEVER_OFFER & set(p.split()) and p not in NEVER_OFFER_PHRASES]
    lexicon = tone_words | CHAT_SLANG
    used = sorted((w for w in lexicon if counts[w]), key=lambda w: -counts[w])[:VOCAB_SLANG]
    unused = sorted(w for w in tone_words if not counts[w] and w.isalpha())
    slang = used + rng.sample(unused, min(max(VOCAB_SLANG_MIN - len(used), 4), len(unused)))

    phrases = [p for p in tone_phrases if all(counts[w] for w in p.split())]
    if len(phrases) < VOCAB_PHRASES:
        rest = [p for p in tone_phrases if p not in phrases]
        phrases += rng.sample(rest, min(VOCAB_PHRASES - len(phrases), len(rest)))
    phrases = rng.sample(phrases, min(VOCAB_PHRASES, len(phrases)))

    emojis = [w for w, n in counts.most_common() if _is_emoji(w) and n >= 2][:VOCAB_EMOJIS]
    return Vocab(words, slang, phrases, emojis)


def style_hint(voice: Voice | None) -> str:
    """How the server types, in words the AI can copy."""
    if voice is None or voice.chain is None:
        return "short, casual, mostly lowercase, barely any punctuation"
    length = max(voice.length, 3)
    case = ("almost always all lowercase" if voice.lowercase >= 0.75 else
            "mostly lowercase" if voice.lowercase >= 0.5 else "normal capitalisation")
    period = "rarely ends with a period" if voice.periods < 0.2 else "often ends with a period"
    return f"usually around {length} words, {case}, {period}"


def _words_of(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def copies(reply: str, sources: Iterable[str], run: int = COPY_RUN) -> bool:
    """True if `reply` is one of `sources`, contains one, or lifts `run` words in a row from one."""
    key = folded(reply).strip(" .!?")
    words = tuple(_words_of(reply))
    if not words:
        return False
    long = {words[i:i + run] for i in range(len(words) - run + 1)}
    for source in sources:
        if folded(source).strip(" .!?") == key:
            return True
        theirs = tuple(_words_of(source))
        if len(theirs) >= 3 and any(words[i:i + len(theirs)] == theirs
                                    for i in range(len(words) - len(theirs) + 1)):
            return True
        if long and any(theirs[i:i + run] in long for i in range(len(theirs) - run + 1)):
            return True
    return False


def tidy_reply(text, sources: Iterable[str] = (), banned: Iterable[str] = (),
               voice: Voice | None = None) -> str | None:
    """The AI's message cleaned up to look typed by a regular, or None if it's unusable or copied."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    text = SPEAKER_TAG.sub("", text).strip()
    if len(text) >= 2 and text[0] in "\"“'" and text[-1] in "\"”'":
        text = text[1:-1].strip()
    if not text or len(text) > MAX_REPLY or len(text.split()) < 2 or PINGS.search(text) or BOT_TELLS.search(text):
        return None
    if voice is None or voice.periods < 0.2:
        if text.endswith(".") and not text.endswith(".."):
            text = text[:-1].rstrip()
    if not text or len(text.split()) < 2:
        return None
    first = text.split()[0]
    if (voice is None or voice.lowercase >= 0.5) and first[0].isupper() and not (len(first) > 1 and first.isupper()):
        text = text[0].lower() + text[1:]
    if copies(text, [*banned, *sources]):
        return None
    return text


def tone_scores(text: str, labels: frozenset[str] = frozenset()) -> dict[str, float]:
    msg = Msg(0, "", text, datetime(2000, 1, 1, 12, tzinfo=timezone.utc))
    values = score_message(msg, timezone.utc, ai_labels=labels)
    if AIMED_INSULT.search(text):
        values["toxic"] += 0.8
    return {t: values[t] for t in TONES}


def detect_tone(target: str, chat: list[Turn] = ()) -> str:
    """The vibe to answer with: toxic, funny or freaky, from the target message and recent chat."""
    totals = dict.fromkeys(TONES, 0.0)
    for weight, text in [*((0.35, t.text) for t in chat[-5:]), (1.0, target)]:
        for tone, value in tone_scores(text).items():
            totals[tone] += weight * value
    best = max(TONES, key=lambda t: totals[t])
    return best if totals[best] >= TONE_FLOOR else DEFAULT_TONE


def remixes(chain: MarkovChain | None, weights: dict[str, float], tone: str, count: int = 6,
            rng: random.Random | None = None, attempts: int = 120) -> list[str]:
    """New Markov lines in the server's voice that hit the topic or the tone, best first."""
    if chain is None or not chain.sentences:
        return []
    rng = rng or random.Random()
    scored: dict[str, float] = {}
    for _ in range(attempts):
        line = chain.generate(rng, attempts=3)
        if (not line or not 3 <= len(line.split()) <= REMIX_MAX_WORDS or line in scored
                or " ".join(line.split()).lower() in chain.originals):
            continue
        topical = sum(weights.get(w, 0.0) for w in keywords(line))
        vibe = tone_scores(line)[tone]
        if topical or vibe >= 0.5:
            scored[line] = topical + min(vibe, 2.0) * 0.5
    return sorted(scored, key=scored.get, reverse=True)[:count]
