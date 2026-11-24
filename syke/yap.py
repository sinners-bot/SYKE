"""What yap says: real server messages that fit a conversation, and the server's voice for new lines."""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .lexicon import CHAT_SLANG, FREAKY_WORDS, LAUGH_WORDS, STOPWORDS, TOXIC_WORDS
from .markov import MarkovChain, build_chain
from .models import Msg
from .traits import score_message

TOKEN = re.compile(r"<a?:\w+:\d+>|:\w+:|[a-z0-9']+")
QUESTION_START = re.compile(r"^(who|what|why|how|when|where|which|is|are|do|does|did|can|could|would|should|will|wanna|u)\b")
ANSWER_START = re.compile(r"^(yes|yeah|yea|yep|ye|no|nah|nope|because|bc|cuz|cause|probably|maybe|never|always|obviously|bro|idk|ofc)\b")
CUSTOM_EMOJI_TAG = re.compile(r"<a?:\w+:\d+>")
EMOJI_ID = re.compile(r":(\d+)>$")
URL = re.compile(r"https?://", re.IGNORECASE)
GIF_HOSTS = ("tenor.com/view", "giphy.com/gifs", "media.tenor.co", "media.giphy.com", "c.tenor.com")
VOCAB_STRIP = " .,!?;:()[]{}\"“”*_~`|…"
PINGS = re.compile(r"@(everyone|here)\b|<@[!&]?\d+>")
SPEAKER_TAG = re.compile(r"^\**(syke|me|reply)\**\s*:\s*", re.IGNORECASE)
BOT_TELLS = re.compile(
    r"\b(as an ai|language model|i'?m (just )?a bot|i am (just )?a bot|i'?m syke|i am syke|"
    r"great question|that's a good question|i would say|in conclusion)\b",
    re.IGNORECASE)
COPY_RUN = 5
VOCAB_POOL = 500
VOCAB_WORDS = 80
VOCAB_SLANG = 36
VOCAB_SLANG_MIN = 18
VOCAB_TOPIC = 40
VOCAB_EMOJIS = 10
STYLE_SAMPLES = 60         # real lines kept per server to show the AI how people type
STYLE_SHOWN = 16           # of those, shown in one prompt
# Laugh/cry emoji every chat spams: SYKE may still use them, but they're never offered as "the server's emoji".
FILLER_EMOJI = {"😭", "💀", "😂", "🤣", "😹", "☠️", "☠", "🥲", "😩"}
TONES = ("toxic", "funny", "freaky")
DEFAULT_TONE = "funny"
TONE_FLOOR = 0.45
TONE_BOOST = 0.7
REMIX_PENALTY = 0.25
REMIX_MAX_WORDS = 20
MAX_REPLY = 400
FILLER = STOPWORDS | {"who's", "what's", "that's", "there's", "it'll", "anyone", "someone", "everyone",
                      "lol", "lmao", "bro", "guys", "gonna", "wanna", "kinda", "literally", "actually",
                      "though", "thing", "stuff", "still", "even", "much", "okay", "now", "today"}
SUFFIXES = ("ing", "ers", "ies", "ed", "es", "er", "ly", "s")
TONE_WORDS = {
    "toxic": TOXIC_WORDS,
    "funny": LAUGH_WORDS,
    "freaky": FREAKY_WORDS,
}
# Lexicon entries that help spot toxicity but must never be handed to the AI to say.
NEVER_OFFER = {"retard", "retarded", "cunt", "kys", "kill", "incel", "yourself", "fatass", "ugly",
               "hentai", "ahegao", "nudes", "onlyfans", "breed", "bred", "choke", "degrade",
               "boner", "dtf", "breedable", "rawdog", "fansly"}


@dataclass
class Turn:
    author: str
    text: str
    reply_to: str = ""   # "Name: what they said" when this message answers someone
    author_id: int = 0


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
        recency = 0.15 + 0.35 * (i + 1) / len(chat)
        for word in keywords(turn.text):
            weights[word] = max(weights.get(word, 0.0), recency)
    for word in keywords(said or ""):
        weights[word] = max(weights.get(word, 0.0), 0.5)
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
    emojis: list[str] = field(default_factory=list)   # custom emojis users have used, most-used first
    gifs: list[str] = field(default_factory=list)      # GIF links users have posted, most-used first
    samples: list[str] = field(default_factory=list)   # real everyday lines, to copy how people type


def emoji_id(tag: str) -> int | None:
    """The numeric id out of a `<:name:id>` / `<a:name:id>` tag."""
    m = EMOJI_ID.search(tag)
    return int(m.group(1)) if m else None


def _gif_url(token: str) -> str | None:
    low = token.lower()
    if not low.startswith(("http://", "https://")):
        return None
    clean = token.rstrip(").,!?*>\"'")
    low = clean.lower()
    if low.endswith(".gif") or any(h in low for h in GIF_HOSTS):
        return clean
    return None


_GIF_SLUG = re.compile(r"/(?:view|gifs|clip)/([a-z0-9-]+)", re.IGNORECASE)
_GIF_STOP = {"gif", "gifs", "the", "a", "an", "of", "to", "and", "with", "mp4"}


def gif_name(url: str, limit: int = 4) -> str:
    """What a GIF link calls itself, in order: tenor .../view/crying-cat-sad-gif-123 -> 'crying cat sad'."""
    m = _GIF_SLUG.search(url or "")
    slug = m.group(1) if m else (url.rsplit("/", 1)[-1] if url else "")
    slug = re.sub(r"\.(gif|mp4|webp|png|jpe?g)$", "", slug, flags=re.IGNORECASE)
    words = [w for w in re.split(r"[-_]", slug.lower())
             if len(w) >= 3 and not any(ch.isdigit() for ch in w) and w not in _GIF_STOP]   # no ids like abc123
    return " ".join(list(dict.fromkeys(words))[:limit])


def gif_keywords(url: str) -> set[str]:
    """Descriptive words from a GIF link's slug, e.g. tenor .../view/happy-dance-gif-123 -> {happy, dance}."""
    m = _GIF_SLUG.search(url or "")
    slug = m.group(1) if m else (url.rsplit("/", 1)[-1] if url else "")
    slug = re.sub(r"\.(gif|mp4|webp|png|jpe?g)$", "", slug, flags=re.IGNORECASE)
    words = set()
    for w in re.split(r"[-_]", slug.lower()):
        w = w.strip()
        if len(w) >= 3 and not w.isdigit() and w not in _GIF_STOP:
            words.add(w)
    return words


def pick_gif(gifs: list[str], context: str, minimum: int = 1) -> str | None:
    """Choose the GIF whose slug words best overlap the conversation; None if fewer than `minimum` match."""
    terms = {w for w in re.findall(r"[a-z]{3,}", (context or "").lower())} - STOPWORDS
    if not terms:
        return None
    best, best_score = None, 0
    for g in gifs:
        score = len(gif_keywords(g) & terms)
        if score > best_score:
            best, best_score = g, score
    return best if best_score >= minimum else None


def mine_expressions(texts: Iterable[str]) -> tuple[list[str], list[str]]:
    """Custom emojis and GIF links that appear in `texts`, each ordered by how often they're used."""
    emojis: Counter = Counter()
    gifs: Counter = Counter()
    for text in texts:
        for raw in (text or "").split():
            if CUSTOM_EMOJI_TAG.fullmatch(raw):
                emojis[raw] += 1
            else:
                g = _gif_url(raw)
                if g:
                    gifs[g] += 1
    return [e for e, _ in emojis.most_common()], [g for g, _ in gifs.most_common()]


@dataclass
class Vocab:
    """Words offered to the AI for one reply, taken from stored messages and the lexicon."""
    words: list[str]
    slang: list[str]
    emojis: list[str]
    topic: list[str] = field(default_factory=list)


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
    emojis, gifs = mine_expressions(texts)
    return Voice(
        chain=build_chain(texts),
        words=server_words(texts),
        lowercase=sum(t == t.lower() for t in lettered) / len(lettered),
        periods=sum(t.rstrip().endswith(".") and not t.rstrip().endswith("..") for t in texts) / len(texts),
        length=lengths[len(lengths) // 2],
        emojis=emojis,
        gifs=gifs,
        samples=style_samples(texts),
    )


def style_samples(texts: list[str], count: int = STYLE_SAMPLES, rng: random.Random | None = None) -> list[str]:
    """Ordinary chat lines (no links, pings or GIFs, 2-25 words), spread over the corpus."""
    rng = rng or random.Random(len(texts))
    ok = []
    seen = set()
    for text in texts:
        line = " ".join(text.split())
        n = len(line.split())
        if not 2 <= n <= 25 or URL.search(line) or PINGS.search(line) or line.startswith((">", "```")):
            continue
        key = folded(line)
        if key in seen:
            continue
        seen.add(key)
        ok.append(line)
    return rng.sample(ok, min(count, len(ok)))


def _is_emoji(token: str) -> bool:
    return bool(CUSTOM_EMOJI_TAG.fullmatch(token)) or not any(c.isalnum() for c in token)


def topic_words(counts: Counter, weights: dict[str, float], limit: int = VOCAB_TOPIC) -> list[str]:
    """Stored-message words that overlap the conversation, strongest first."""
    if not weights:
        return []
    scored: list[tuple[float, str]] = []
    for word, n in counts.items():
        if not word or word in FILLER or word in NEVER_OFFER or _is_emoji(word):
            continue
        key = word if word.startswith(("<", ":")) else stem(word)
        weight = weights.get(key, 0.0) or weights.get(word, 0.0)
        if weight:
            scored.append((weight * (1 + math.log(1 + n)), word))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [word for _, word in scored[:limit]]


def vocabulary(voice: Voice | None, tone: str, rng: random.Random | None = None,
               weights: dict[str, float] | None = None, extra: Iterable[str] = ()) -> Vocab:
    """The server's own words, plus lexicon slang for `tone` (ones the server uses first)."""
    rng = rng or random.Random()
    counts = Counter(voice.words) if voice else Counter()
    if extra:
        counts += server_words(extra)
    common = [w for w, n in counts.most_common(VOCAB_POOL)
              if n >= 2 and not _is_emoji(w) and len(w) >= 3 and w not in FILLER and w not in NEVER_OFFER]
    words = common[:VOCAB_WORDS]

    tone_words = {w for w in TONE_WORDS.get(tone, TONE_WORDS[DEFAULT_TONE]) if w not in NEVER_OFFER}
    lexicon = tone_words | CHAT_SLANG
    used = sorted((w for w in lexicon if counts[w]), key=lambda w: -counts[w])[:VOCAB_SLANG]
    unused = sorted(w for w in tone_words if not counts[w] and w.isalpha())
    slang = used + rng.sample(unused, min(max(VOCAB_SLANG_MIN - len(used), 4), len(unused)))

    emojis = [w for w, n in counts.most_common()
              if _is_emoji(w) and n >= 2 and w not in FILLER_EMOJI][:VOCAB_EMOJIS]
    return Vocab(words, slang, emojis, topic_words(counts, weights or {}))


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
        if len(theirs) >= run and any(words[i:i + len(theirs)] == theirs
                                    for i in range(len(words) - len(theirs) + 1)):
            return True
        if long and any(theirs[i:i + run] in long for i in range(len(theirs) - run + 1)):
            return True
    return False


def reply_cap(voice: Voice | None) -> int:
    """Most words a reply may have: a bit more than this server's typical message."""
    if voice is None or not voice.length:
        return 24
    return max(16, min(30, voice.length * 3 + 8))


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=\S)")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+|\s+(?:—|-|–)\s+")


def fit_reply(text: str, cap: int, voice: Voice | None = None) -> str | None:
    """Trim a good reply to chat size instead of throwing it away.

    At most two sentences (one when this server never uses full stops), cut at a sentence or clause
    end so it stays within `cap` words. None if there's no clean place to cut.
    """
    sentences = [x.strip() for x in _SENTENCE_END.split(text) if x.strip()]
    sentences = sentences[:2]
    out = []
    for sentence in sentences:
        if len(" ".join([*out, sentence]).split()) <= cap:
            out.append(sentence)
        else:
            break
    if out:
        joined = " ".join(out)
        if joined.endswith(".") and not joined.endswith("..") and (voice is None or voice.periods < 0.2):
            joined = joined[:-1].rstrip()
        return joined
    first = sentences[0] if sentences else text
    pieces = _CLAUSE_END.split(first)
    built = ""
    for piece in pieces:
        trial = f"{built} {piece}".strip() if built else piece
        if len(trial.split()) > cap:
            break
        built = trial
    built = built.rstrip(",;:—–- ")
    return built if built and len(built.split()) >= 3 else None


def strip_links(text: str) -> str:
    """A stored line without links (GIF links would make the 'text' reply a GIF)."""
    return " ".join(w for w in (text or "").split() if not w.lower().startswith(("http://", "https://", "<http")))


def tidy_reply(text, sources: Iterable[str] = (), banned: Iterable[str] = (),
               voice: Voice | None = None) -> str | None:
    """The AI's message cleaned up to look typed by a regular, or None if it's unusable or copied."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    text = SPEAKER_TAG.sub("", text).strip()
    if len(text) >= 2 and text[0] in "\"“'" and text[-1] in "\"”'":
        text = text[1:-1].strip()
    text = strip_links(text)
    if not text or len(text) > MAX_REPLY or len(text.split()) < 2 or PINGS.search(text) or BOT_TELLS.search(text):
        return None
    if voice is None or voice.periods < 0.2:
        if text.endswith(".") and not text.endswith(".."):
            text = text[:-1].rstrip()
    if not text or len(text.split()) < 2:
        return None
    text = fit_reply(text, reply_cap(voice), voice)
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


def make_typo(text: str, rng: random.Random | None = None) -> tuple[str, str] | None:
    """`text` with one word mistyped the way thumbs do (swap, drop or double a letter), plus the right word."""
    rng = rng or random
    words = text.split(" ")
    spots = [i for i, w in enumerate(words) if re.fullmatch(r"[A-Za-z]{5,}", w)]
    if not spots:
        return None
    i = rng.choice(spots)
    word = words[i]
    j = rng.randrange(1, len(word) - 2)
    kind = rng.choice(("swap", "drop", "double"))
    if kind == "swap" and word[j] != word[j + 1]:
        wrong = word[:j] + word[j + 1] + word[j] + word[j + 2:]
    elif kind == "drop":
        wrong = word[:j] + word[j + 1:]
    else:
        wrong = word[:j] + word[j] + word[j:]
    if wrong == word:
        return None
    words[i] = wrong
    return " ".join(words), word


# How talkative SYKE is in a server: (name, chance per message, seconds between yaps, conversation starters a day)
TALK_KEY = "yap"
TALK_LEVELS = {
    0: ("Silent", 0.0, 0, 0),
    1: ("Rare", 0.03, 600, 1),
    2: ("Quiet", 0.07, 300, 1),
    3: ("Chill", 0.12, 150, 2),
    4: ("Normal", 0.20, 75, 2),
    5: ("Chatty", 0.35, 45, 3),
    6: ("Yapper", 0.55, 25, 4),
    7: ("Nonstop", 0.80, 12, 5),
}
TALK_WORDS = {name.lower(): level for level, (name, *_rest) in TALK_LEVELS.items()} | {
    "off": 0, "none": 0, "low": 2, "default": 4, "medium": 4, "high": 6, "max": 7}


def talk_level(value) -> int | None:
    """A talkativeness level from "0"-"7" or a name ("chill", "max"); None if it isn't one."""
    text = str(value or "").strip().lower()
    if text.isdigit() and int(text) in TALK_LEVELS:
        return int(text)
    return TALK_WORDS.get(text)


NAME_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def name_words(name: str) -> set[str]:
    """The parts of a display name people would call someone by: 'Ging™' -> {'ging'}."""
    return {w.casefold() for w in NAME_WORD.findall(name or "")}


def talks_to_someone_else(text: str, known: set[str], allowed: set[str] = frozenset()) -> bool:
    """A real line that pings someone or names a member other than `allowed` (the person SYKE answers).
    Repeating it as a reply would call them by someone else's name."""
    if "<@" in (text or ""):
        return True
    words = {w.casefold() for w in NAME_WORD.findall(text or "")}
    return bool((words & known) - allowed)
