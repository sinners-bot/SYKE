"""SYKE's inner life, the pure part: how it feels about people, its mood, and the promises it heard.

No Discord or database code here, so every rule is easy to test. `persona.py` wires it to the bot.

* Bonds: a score per member from -100 (nemesis) to +100 (bestie). Insults push it down, kindness up,
  an apology wins half the lost ground back. Old feelings fade (half-life BOND_HALF_LIFE).
* The father: SYKE's owners are its father and creator. That bond is fixed; insulting him makes SYKE
  hold it against you.
* Mood: one number per server from -1 (furious) to +1 (ecstatic) that drifts back to calm, plus an
  energy level from how busy the chat is.
* Promises: "i'll play tonight", "gonna hop on in 10" — the text and when it falls due.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

BOND_HALF_LIFE = 21 * 86400        # feelings fade by half in three weeks
MOOD_HALF_LIFE = 6 * 3600
ENERGY_HALF_LIFE = 2 * 3600
BOND_LIMIT = 100.0

INSULT_HIT = 7.0                    # per intensity point
KIND_HIT = 4.0
FATHER_INSULT_HIT = 10.0
FATHER_PRAISE_HIT = 3.0

# Offline fallback for judging how a message treats SYKE. With AI on, the AI judges instead.
_INSULTS = re.compile(
    r"\b(trash|garbage|useless|stupid|dumb|idiot|retard\w*|shut\s*up|stfu|worst|hate\s+(?:you|u|this)|annoying|"
    r"cringe|mid|bum|clown|loser|ugly|broken|pathetic|l\s+bot|bad\s+bot|kys|die|suck[s]?|fuck\s+(?:you|u|off)|"
    r"dogshit|shit\s+bot|ass\b|dickhead|moron|ratio|nobody\s+asked|nerd)\b", re.I)
_KIND = re.compile(
    r"\b(thanks?|thank\s+(?:you|u)|thx|ty|tysm|love\s+(?:you|u|ya)|ily|good\s+(?:bot|boy|job)|w\s+(?:bot|syke)|"
    r"goat(?:ed)?|best\s+bot|based|legend|king|appreciate|so\s+funny|you(?:'re|\s+are)\s+(?:the\s+best|funny|"
    r"smart|cool|amazing)|proud\s+of\s+(?:you|u)|cutie|my\s+boy)\b", re.I)
_SORRY = re.compile(r"\b(sorry|my\s+bad|apologi[sz]e|forgive\s+me|didn'?t\s+mean\s+it|i\s+take\s+it\s+back)\b",
                    re.I)
_FATHER_WORDS = re.compile(r"\byour\s+(dad|father|daddy|creator|owner|maker|dev|developer)\b", re.I)


@dataclass
class Judgement:
    """How one message treats SYKE (and its father)."""
    toward: str = "neutral"     # insult | kind | apology | neutral
    intensity: int = 1          # 1-3
    father: str = "none"        # insult | kind | none
    note: str = ""              # what they did, past tense, short ("called you useless")


def lexicon_judge(text: str, mentions_father: bool = False) -> Judgement:
    """A rough read without AI: insult, kindness or apology words."""
    text = (text or "").replace("’", "'")
    if mentions_father or _FATHER_WORDS.search(text):
        if _INSULTS.search(text):
            return Judgement("neutral", 2, "insult", f"insulted your father ({snippet(text, 40)})")
        if _KIND.search(text):
            return Judgement("neutral", 1, "kind", "said something nice about your father")
    if _SORRY.search(text):
        return Judgement("apology", 1, "none", "apologised to you")
    insult = _INSULTS.findall(text)
    if insult:
        word = insult[0] if isinstance(insult[0], str) else insult[0][0]
        return Judgement("insult", min(3, len(insult)), "none", f'called you "{word.lower()}"')
    kind = _KIND.findall(text)
    if kind:
        word = kind[0] if isinstance(kind[0], str) else kind[0][0]
        return Judgement("kind", min(3, len(kind)), "none", f'was nice to you ("{word.lower()}")')
    return Judgement()


def snippet(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# Bonds

def faded(score: float, updated: float, now: float) -> float:
    """A bond score after time has softened it."""
    if not score or now <= updated:
        return score
    return score * 0.5 ** ((now - updated) / BOND_HALF_LIFE)


def clamp(value: float, low: float = -BOND_LIMIT, high: float = BOND_LIMIT) -> float:
    return max(low, min(high, value))


def bond_change(score: float, judgement: Judgement) -> float:
    """How much one judged message moves the speaker's bond."""
    delta = 0.0
    if judgement.toward == "insult":
        delta -= INSULT_HIT * judgement.intensity
    elif judgement.toward == "kind":
        # Kindness counts less when SYKE already adores you, and less from someone it can't stand.
        delta += KIND_HIT * judgement.intensity * (0.6 if score > 60 else 0.7 if score < -40 else 1.0)
    elif judgement.toward == "apology":
        delta += max(3.0, -score / 2) if score < 0 else 1.0
    if judgement.father == "insult":
        delta -= FATHER_INSULT_HIT * judgement.intensity
    elif judgement.father == "kind":
        delta += FATHER_PRAISE_HIT
    return delta


BOND_LABELS = [(70, "bestie", "💖"), (40, "favourite", "⭐"), (15, "likes you", "🙂"), (-15, "neutral", "😐"),
               (-40, "grudge", "😒"), (-70, "can't stand you", "😤"), (-101, "nemesis", "🗡️")]


def bond_label(score: float) -> tuple[str, str]:
    for floor, label, icon in BOND_LABELS:
        if score >= floor:
            return label, icon
    return "nemesis", "🗡️"


# Mood

@dataclass
class Mood:
    value: float = 0.0          # -1 furious … +1 ecstatic
    updated: float = 0.0
    energy: float = 0.3         # 0 asleep … 1 wired (from chat activity; not stored)
    energy_at: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def now(self, at: float) -> tuple[float, float]:
        value = self.value * 0.5 ** (max(0.0, at - self.updated) / MOOD_HALF_LIFE)
        energy = 0.3 + (self.energy - 0.3) * 0.5 ** (max(0.0, at - self.energy_at) / ENERGY_HALF_LIFE)
        return value, energy

    def nudge(self, delta: float, at: float, reason: str = "") -> None:
        value, _ = self.now(at)
        self.value, self.updated = max(-1.0, min(1.0, value + delta)), at
        if reason:
            self.reasons = [reason, *[r for r in self.reasons if r != reason]][:6]

    def bump_energy(self, at: float, amount: float = 0.02) -> None:
        _, energy = self.now(at)
        self.energy, self.energy_at = min(1.0, energy + amount), at


def mood_change(judgement: Judgement, father: bool) -> float:
    if judgement.toward == "insult":
        return -0.12 * judgement.intensity * (2.0 if father else 1.0)
    if judgement.toward == "kind":
        return 0.08 * judgement.intensity * (3.0 if father else 1.0)
    if judgement.toward == "apology":
        return 0.05
    if judgement.father == "insult":
        return -0.15
    return 0.0


MOODS = {
    "ecstatic": ("🤩", "over the moon: extra playful, generous, hyping people up"),
    "happy": ("😄", "in a great mood: warm, joking around, easy on people"),
    "good": ("🙂", "in a decent mood: relaxed and friendly"),
    "chill": ("😌", "chill: normal, laid-back"),
    "wired": ("⚡", "wired from all the chat: fast, chaotic, hyper"),
    "bored": ("🥱", "bored, the chat's been dead: dry, low effort, a bit dramatic about the silence"),
    "sleepy": ("😴", "sleepy, it's late: short replies, yawning energy"),
    "grumpy": ("😒", "grumpy, people have been rude: short, sarcastic, a little petty"),
    "irritable": ("😠", "irritable: snappy, quick to clap back"),
    "furious": ("🤬", "furious: cold, savage comebacks, holding grudges hard (still no slurs or real cruelty)"),
}


def mood_label(value: float, energy: float, local_hour: int) -> str:
    if value >= 0.6:
        return "ecstatic"
    if value >= 0.3:
        return "happy"
    if value <= -0.6:
        return "furious"
    if value <= -0.3:
        return "irritable" if energy > 0.5 else "grumpy"
    if value <= -0.12:
        return "grumpy"
    if energy >= 0.75:
        return "wired"
    if value >= 0.12:
        return "good"
    if energy <= 0.12:
        return "sleepy" if 1 <= local_hour < 7 else "bored"
    return "chill"


# Promises

_SUBJECT = (r"(?:i'?ll|i\s+will|imma|ima|i'?m\s+gonna|im\s+gonna|i'?m\s+going\s+to|im\s+going\s+to|i\s+gonna|"
            r"i\s+promise(?:\s+(?:to|i'?ll|i\s+will))?|i\s+swear\s+i'?ll)")
# "ill play later" means "i'll", but only where "ill" can't be the adjective ("im ill").
_ILL = re.compile(r"(^|[,.!]\s*|\b(?:ok|okay|yeah|ye|yea|yes|bet|fine|alr|alright|sure|then|and|but|so)\s+)ill\b(?!\s+be\s+sick)",
                  re.I)
_TIME = (r"(?:tonight|tonite|today|later(?:\s+today)?|tomorrow|tmrw|tmr|tmrrw|soon|in\s+a\s+(?:bit|sec|min|minute|few)|"
         r"in\s+\d+\s*(?:s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)|"
         r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|this\s+(?:evening|afternoon|morning|weekend)|next\s+week|"
         r"on\s+(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday)?|"
         r"after\s+(?:school|work|class|dinner|practice|the\s+gym|gym))")
PROMISE = re.compile(rf"(?:^|\b){_SUBJECT}\s+(?!not\b|never\b|see\b|think\b|try\b|maybe\b)([^.!?\n]{{2,140}}?\b{_TIME}\b[^.!?\n]{{0,40}})",
                     re.I)
_NEGATIVE = re.compile(r"\b(won'?t|wont|can'?t|cant|not\s+gonna|never|might|maybe|probably|idk)\b", re.I)
_VOICE_WORDS = re.compile(r"\b(play|playing|hop\s+on|get\s+on|be\s+on|join|vc|voice|call|game|gaming|stream|online|"
                          r"on\s+(?:tonight|later|soon|in))\b", re.I)
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
GRACE = timedelta(minutes=20)


@dataclass
class Promise:
    text: str
    due: datetime
    voiceish: bool


def _end_of(day: datetime) -> datetime:
    return day.replace(hour=23, minute=59, second=0, microsecond=0)


def due_from(phrase: str, now: datetime) -> datetime:
    """When a time phrase falls due, in `now`'s timezone."""
    p = phrase.lower()
    if m := re.search(r"in\s+(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)\b", p):
        n, unit = int(m.group(1)), m.group(2)
        seconds = n if unit.startswith("s") else n * 3600 if unit.startswith("h") else n * 60
        return now + timedelta(seconds=min(seconds, 7 * 86400)) + GRACE
    if re.search(r"in\s+a\s+(bit|sec|min|minute|few)|soon", p):
        return now + timedelta(hours=1)
    if m := re.search(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", p):
        hour, minute, half = int(m.group(1)) % 24, int(m.group(2) or 0), m.group(3)
        if half == "pm" and hour < 12:
            hour += 12
        elif half == "am" and hour == 12:
            hour = 0
        when = now.replace(hour=hour, minute=min(minute, 59), second=0, microsecond=0)
        if not half and hour < 12 and when < now and when + timedelta(hours=12) > now:
            when += timedelta(hours=12)        # "at 8" said at 3pm means 8pm
        if when < now:
            when += timedelta(days=1)
        if "tomorrow" in p or "tmr" in p:
            when = when + timedelta(days=1) if when.date() == now.date() else when
        return when + GRACE
    if re.search(r"tomorrow|tmrw|tmr|tmrrw", p):
        return _end_of(now + timedelta(days=1))
    if "next week" in p:
        return _end_of(now + timedelta(days=7))
    if "weekend" in p:
        return _end_of(now + timedelta(days=(6 - now.weekday()) % 7))
    if m := re.search(r"on\s+(mon|tue|wed|thu|fri|sat|sun)", p):
        ahead = (_DAYS[m.group(1)] - now.weekday()) % 7 or 7
        return _end_of(now + timedelta(days=ahead))
    for word, hour in (("morning", 12), ("afternoon", 17), ("evening", 21)):
        if f"this {word}" in p:
            when = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            return when if when > now else now + timedelta(hours=2)
    if "after" in p:
        when = now.replace(hour=20, minute=0, second=0, microsecond=0)
        return when if when > now else now + timedelta(hours=3)
    if "later" in p:
        return now + timedelta(hours=5)
    if now.hour < 5:                            # "tonight" at 2am means the coming night
        return now.replace(hour=23, minute=59, second=0, microsecond=0)
    return _end_of(now)


def find_promise(text: str, now: datetime) -> Promise | None:
    """A promise with a deadline in `text`, if there is one ("i'll play tonight")."""
    text = " ".join((text or "").replace("’", "'").replace("‘", "'").split())
    if not text or len(text) > 400 or text.rstrip().endswith("?"):
        return None
    text = _ILL.sub(lambda m: f"{m.group(1)}i'll", text)
    m = PROMISE.search(text)
    if m is None:
        return None
    start = m.start()
    clause = text[start:m.end()].strip(" ,")
    if _NEGATIVE.search(text[max(0, start - 20):m.end()]):
        return None
    time_match = re.search(_TIME, clause, re.I)
    if time_match is None:
        return None
    return Promise(snippet(clause, 140), due_from(time_match.group(0), now), bool(_VOICE_WORDS.search(clause)))


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


# What the AI is told

def state_block(*, mood: str, mood_reasons: list[str], target_name: str | None, target_is_father: bool,
                bond: float | None, events: list[tuple[str, str, float]], father_names: list[str],
                father_in_chat: bool, promises: list[tuple[str, str, float]], now: float) -> str:
    """SYKE'S STATE OF MIND for a reply: mood, feelings about the person, the father, their promises.

    events: (kind, note, at) newest first. promises: (status, text, made_at).
    """
    lines = []
    icon, how = MOODS.get(mood, MOODS["chill"])
    why = f" (because: {'; '.join(mood_reasons[:3])})" if mood_reasons else ""
    lines.append(f"- Your mood: {mood} {icon} — {how}{why}.")
    father = " / ".join(father_names) or "the bot owner"
    if target_is_father:
        lines.append(f"- You're talking to {target_name or father}, your FATHER and CREATOR. He made you and you "
                     "love him: respect, loyalty, affection. Call him dad sometimes. You can tease him gently, "
                     "but never roast him for real, never disrespect him, and always take his side. If he's "
                     "harsh with you, you're hurt and a little dramatic about it, not angry.")
    else:
        lines.append(f"- {father} is your father and creator. Never disrespect him; if anyone talks bad about "
                     f"him, defend him and hold it against them." + (" He's in this chat." if father_in_chat else ""))
        if target_name and bond is not None:
            label, _ = bond_label(bond)
            feel = {
                "bestie": "they're one of your favourite people here: warm, loyal, hype them up, take their side",
                "favourite": "you like them a lot: friendlier and more forgiving than usual",
                "likes you": "you like them: a bit warmer than usual",
                "neutral": "no strong feelings",
                "grudge": "you hold a grudge: colder, petty, less patient; you may bring up what they did",
                "can't stand you": "you can't stand them: cold, savage, you bring up what they did",
                "nemesis": "they're your nemesis: open hostility, savage comebacks, you never let them forget "
                           "what they did (still no slurs, identity attacks or real cruelty)",
            }[label]
            lines.append(f"- {target_name}: {label} (bond {bond:+.0f}/100) — {feel}.")
            memories = [f"{note} ({ago(now - at)})" for kind, note, at in events if note][:4]
            if memories:
                lines.append(f"- What {target_name} did to you lately: " + "; ".join(memories) + ".")
    if promises:
        said = []
        for status, text, made in promises[:3]:
            state = {"open": "still pending", "broken": "they didn't do it", "kept": "they did it"}.get(status, status)
            said.append(f'"{text}" ({ago(now - made)}, {state})')
        lines.append(f"- Promises {target_name or 'they'} made: " + "; ".join(said) +
                     ". Bring a broken one up if it fits.")
    return "SYKE'S STATE OF MIND (let it colour how you talk; never recite it like a status report):\n" + \
        "\n".join(lines)


def mood_score_text(value: float) -> str:
    filled = round((value + 1) * 5)
    return "▰" * filled + "▱" * (10 - filled)


def bond_bar(score: float) -> str:
    filled = round((score + 100) / 20)
    return "▰" * filled + "▱" * (10 - filled)


def decay_factor(seconds: float, half_life: float) -> float:
    return math.pow(0.5, max(0.0, seconds) / half_life)
