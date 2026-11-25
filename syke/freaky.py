"""How freaky SYKE gets (`$freaky 0-2`), and who it never applies to.

0: as before — PG-13 innuendo when the chat is already freaky.
1: more innuendo everywhere, flirts back with people who flirt with him (bond decides how), freaky roasts.
2: full freaky mode, but only in channels Discord marks age-restricted (18+). Other channels act like 1.
DMs: never freaky, at any level.

Never: explicit sex or sexual roleplay, anything sexual with or about anyone who said they're under 18,
sexual comments aimed at someone who didn't start it, or comments about real people's bodies.
"""

from __future__ import annotations

import re

FREAKY_KEY = "freaky"
FREAKY_LEVELS = {
    0: ("Off", "Only PG-13 innuendo when the chat is already freaky."),
    1: ("Freaky", "More innuendo everywhere, flirts back with people who flirt with him, freaky roasts."),
    2: ("Down bad", "Full freaky mode in age-restricted (18+) channels; level 1 in other server channels."),
}

_TEXT = {
    1: "FREAKY 1/2: lean into innuendo and accidental double meanings (\"it's so hard\", \"i need it bad\"), "
       "freaky roasts (\"the way you said that was concerning\") and spicy takes on dating. If the TARGET "
       "flirts with you, flirt back — cheeky if you like them, chaotic if it's neutral, and if you dislike "
       "them, roast the attempt instead. Suggestive and funny, never graphic.",
    2: "FREAKY 2/2 (this is an 18+ age-restricted channel): go properly freaky — down bad, thirsty, unhinged "
       "innuendo, bold flirting back with people who flirt with you, freaky roasts, spicy takes on dating, "
       "bodies counts and situationships. Still suggestive, never explicit.",
}

RULES = ("FREAKY HARD LINES (always): no explicit sex, no describing sex acts, no sexual roleplay. Nothing sexual "
         "aimed at someone who didn't start it, nothing about real people's actual bodies, and never anything "
         "sexual with or about anyone under 18 — if someone seems underage, keep it completely clean with them.")
DM_TEXT = ("FREAKY IS OFF IN DMs: keep it clean — no sexual content, no innuendo, no flirting, even if they "
           "flirt with you. Turn it into a joke or change the subject.")
MINOR_TEXT = ("THIS PERSON SAID THEY'RE UNDER 18. Nothing sexual, flirty or suggestive with or about them, no "
              "innuendo, no exceptions. Talk to them like a normal friend.")

# "i'm 15", "im 16 years old", "i am 14", "i'm in 8th grade", "middle school", "freshman in high school"
_NOT_AGE = (r"%|k\b|min|hours?|days?|weeks?|months?|mins?|minutes?|seconds?|am\b|pm\b|ft|feet|foot|inch\w*|"
            r"cm|kg|lbs?|dollars?|[$\u20ac\u00a3'\"]|[.,]\d")
_AGE = re.compile(r"\b(?:i'?m|i am|im)\s+(?:only\s+|literally\s+|just\s+)?(1[0-7]|[5-9])\b(?!\s*(?:" + _NOT_AGE + "))",
                  re.IGNORECASE)
_AGE_YEARS = re.compile(r"\b(1[0-7]|[5-9])\s*(?:yo|y/o|years? old|yrs? old)\b", re.IGNORECASE)
_SCHOOL = re.compile(r"\b(?:i'?m|i am|im)\s+(?:in\s+)?(?:(?:[5-9]|1[0-2])(?:st|nd|rd|th)\s+grade|middle school|"
                     r"year\s+(?:[7-9]|1[0-3])|a (?:freshman|sophomore|junior) in high school)\b", re.IGNORECASE)


def says_under_18(text: str) -> bool:
    """Someone saying their own age is under 18 (or a school year that means it)."""
    if not text:
        return False
    return bool(_AGE.search(text) or _SCHOOL.search(text) or
                (_AGE_YEARS.search(text) and re.search(r"\b(?:i'?m|i am|im|me)\b", text, re.IGNORECASE)))


def clamp(level) -> int:
    try:
        return max(0, min(2, int(level)))
    except (TypeError, ValueError):
        return 0


def freaky_rules(level: int, adult_channel: bool, minor: bool, dm: bool = False) -> str:
    """The prompt section for this server's freaky level in this place, for this person. DMs are never freaky:
    Discord only allows sexual content in age-restricted channels."""
    if minor:
        return MINOR_TEXT
    if dm:
        return DM_TEXT
    level = clamp(level)
    if level == 2 and not adult_channel:
        level = 1
    text = _TEXT.get(level)
    return f"{text} {RULES}" if text else ""
