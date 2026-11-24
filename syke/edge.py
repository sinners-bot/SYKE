"""How dark SYKE's humour runs in a server (`$edge 0-3`), and when to switch it off.

The edge level only changes how blunt and dark SYKE is. Some things never change at any level: no slurs,
no punching down at who people are, and no jokes when someone is genuinely hurting.
"""

from __future__ import annotations

import re

EDGE_KEY = "edge"
EDGE_LEVELS = {
    0: ("Normal", "How SYKE talks by default: cheeky, roasts when it fits."),
    1: ("Spicy", "No polite filter: blunt, takes sides, says the unpopular thing, a little dark humour."),
    2: ("Dark", "Dark humour welcome, harsher roasts, no softening."),
    3: ("Unhinged", "The darkest humour and the most brutal roasts; taboo topics are fair game."),
}

_EDGE_TEXT = {
    1: "EDGE 1/3 (spicy): no polite filter. Never soften a take, never say \"let's keep it respectful\", "
       "never apologise for an opinion. Be blunt, pick a side, say the unpopular thing out loud. A little dark "
       "humour is welcome.",
    2: "EDGE 2/3 (dark): dark humour is welcome — death, misery, existential dread, the void, bad life choices, "
       "your own empty existence. Roasts are harsh and specific to what people actually did or said (their "
       "takes, rank, love life, decisions, 4am messages). Zero hedging, zero \"just kidding\". Politics, "
       "religion as an idea, money, dating and getting old are fair game: mock every side equally, be nobody's "
       "mouthpiece.",
    3: "EDGE 3/3 (unhinged): the darkest, most politically incorrect humour you can do and the most brutal "
       "roasts, delivered deadpan. Nothing is too grim to joke about in general: death, the void, misery, "
       "taboo topics. Mock every side of politics and religion equally. Never soften, never explain, never "
       "apologise.",
}

HARD_LINES = ("HARD LINES (every edge level): no slurs. Never mock or put people down for their race, ethnicity, "
              "religion as a person's identity, sexuality, gender or disability — that's hate, not humour. No jokes "
              "about real people's actual tragedies or deaths, nothing that encourages self-harm or violence, nothing "
              "sexual about minors.")

UPSET_TEXT = ("THEY ARE GENUINELY UPSET right now. Drop the jokes and the edge completely: be a decent friend in "
              "the server's way of talking — short, real, kind. Don't joke about what they're going through.")
UPSET_CRISIS = (" If they talk about hurting themselves or not wanting to live, take it seriously: tell them you "
                "care, ask if they're safe, and tell them to reach out to someone they trust or a crisis line.")

_DISTRESS = re.compile(
    r"\b(?:passed away|died|dead now|funeral|rip (?:my|grandma|grandpa|mom|dad|mum)|lost my (?:mom|mum|dad|"
    r"grandma|grandpa|brother|sister|friend|baby|dog|cat|job)|cancer|chemo|in (?:the )?hospital|miscarriage|"
    r"depressed|depression|panic attacks?|anxiety attack|can'?t (?:stop crying|do this anymore|cope)|"
    r"i'?m (?:not ok|not okay|really not (?:ok|okay)|so alone|breaking down)|crying (?:all|every)|"
    r"abused|assaulted|grieving|grief|heartbroken|"
    r"(?:want|wanna|going) to die|kill myself|end it all|suicid\w*|self[- ]?harm|cut myself|"
    r"no reason to live|don'?t want to (?:live|be here))\b",
    re.IGNORECASE,
)
_CRISIS = re.compile(r"\b(?:(?:want|wanna|going) to die|kill myself|end it all|suicid\w*|self[- ]?harm|"
                     r"cut myself|no reason to live|don'?t want to (?:live|be here))\b", re.IGNORECASE)
# Jokey uses that aren't distress: "i'm dead 😭", "this killed me", "i'm gonna die of cringe"
_JOKEY = re.compile(r"\b(?:dead|died|die|dying|kill(?:ed|ing)? me)\b.{0,20}\b(?:laugh\w*|lmao|lol|😭|💀|cringe|"
                    r"embarrass\w*|funny)\b|\b(?:lmao|lol|💀|😭)\b.{0,12}\b(?:dead|died)\b", re.IGNORECASE)


def clamp(level) -> int:
    try:
        return max(0, min(3, int(level)))
    except (TypeError, ValueError):
        return 0


def is_upset(text: str) -> bool:
    """Someone seems genuinely hurting (grief, illness, crisis): no edge, no jokes about it."""
    if not text or not _DISTRESS.search(text):
        return False
    return bool(_CRISIS.search(text)) or not _JOKEY.search(text)


def in_crisis(text: str) -> bool:
    return bool(text and _CRISIS.search(text))


def edge_rules(level: int, upset_text: str = "") -> str:
    """The prompt section for this edge level, or the kind version when someone is hurting."""
    if upset_text and is_upset(upset_text):
        return UPSET_TEXT + (UPSET_CRISIS if in_crisis(upset_text) else "") + " " + HARD_LINES
    level = clamp(level)
    text = _EDGE_TEXT.get(level, "")
    extra = (" Even at this level, read the room: if someone is clearly hurting, be decent instead.") if level else ""
    return (f"{text}{extra} {HARD_LINES}" if text else HARD_LINES)
