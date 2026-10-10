"""Per-person copy so IQ, aura and profile cards don't all read the same."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from .traits import TRAITS

if TYPE_CHECKING:
    from .aura import AuraResult
    from .iq import IQResult
    from .profile import Profile

QUOTE_LIMIT = 72


def seeded(*parts: object) -> random.Random:
    """A stable RNG for one person, so two users never share a roll."""
    blob = "|".join(str(p) for p in parts).encode()
    seed = 0
    for byte in blob:
        seed = (seed * 131) ^ byte
    return random.Random(seed)


def clip(text: str, limit: int = QUOTE_LIMIT) -> str:
    text = " ".join(text.split()).replace('"', "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _quote(msg, rng: random.Random, lines: list[str]) -> str:
    if msg is None or not msg.content.strip():
        return ""
    return rng.choice(lines).format(q=clip(msg.content))


def _swings(factors) -> tuple[object | None, object | None]:
    raised = [f for f in factors if f.points > 0]
    dropped = [f for f in factors if f.points < 0]
    raised.sort(key=lambda f: -f.points)
    dropped.sort(key=lambda f: f.points)
    return (raised[0] if raised else None), (dropped[0] if dropped else None)


def iq_blurb(name: str, result: "IQResult") -> str:
    """One or two sentences about THIS person's IQ, not their band."""
    rng = seeded(name, result.iq, result.smartest.content if result.smartest else "",
                 result.dumbest.content if result.dumbest else "")
    up, down = _swings(result.factors)
    quote = _quote(result.smartest if result.iq >= 100 else result.dumbest, rng, [
        'Exhibit A: "{q}"',
        'They really sent "{q}".',
        'The tape shows "{q}".',
        'Best (or worst) exhibit: "{q}".',
    ])
    up_bit = f"{up.name.lower()} ({up.detail})" if up else "sheer vibes"
    down_bit = f"{down.name.lower()} ({down.detail})" if down else "nothing in particular"
    if result.iq >= 130:
        lines = [
            f"{name} lands a {result.iq} because {up_bit} is doing graduate-level work.",
            f"The lab did not expect {up_bit} to push {name} this far.",
            f"{name} is running a {result.iq}. {up_bit.capitalize() if up else 'Something'} paid off.",
        ]
    elif result.iq >= 115:
        lines = [
            f"{name}'s {result.iq} comes from {up_bit}, not from luck.",
            f"Above average, and {up_bit} is why {name} is sitting here.",
            f"{name} reads as {result.iq}: {up_bit} carried, {down_bit} tried to argue.",
        ]
    elif result.iq >= 100:
        lines = [
            f"{name} is a {result.iq} — {up_bit} keeps them upright, {down_bit} keeps them mortal.",
            f"{name} is statistically normal, specifically because {up_bit} cancelled out {down_bit}.",
            f"{name}'s brain defaulted to {result.iq}. {up_bit} vs {down_bit}.",
        ]
    elif result.iq >= 85:
        lines = [
            f"{name} clocks {result.iq} because {down_bit} ate the homework.",
            f"Vibes-based cognition: {down_bit} dragged {name} under 100.",
            f"{name} almost had it, then {down_bit} happened.",
        ]
    else:
        lines = [
            f"{name}'s {result.iq} is what {down_bit} looks like as a lifestyle.",
            f"The instruments winced. {down_bit} was enough to tank {name}.",
            f"{name} scored {result.iq}. {down_bit.capitalize() if down else 'Something'} won.",
        ]
    text = rng.choice(lines)
    return f"{text} {quote}".strip()


def aura_blurb(name: str, result: "AuraResult") -> str:
    """One or two sentences about how THIS person farms or spends aura."""
    rng = seeded(name, result.aura, result.highest.content if result.highest else "",
                 result.lowest.content if result.lowest else "")
    up, down = _swings(result.factors)
    quote = _quote(result.highest if result.aura >= 100 else result.lowest, rng, [
        'Signature move: "{q}"',
        'They typed "{q}" and the room changed.',
        'On record: "{q}"',
        'The clip they cannot live down: "{q}".',
    ])
    up_bit = f"{up.name.lower()} ({up.detail})" if up else "existing"
    down_bit = f"{down.name.lower()} ({down.detail})" if down else "a mild identity crisis"
    if result.aura >= 145:
        lines = [
            f"{name} is farming aura on purpose. {up_bit.capitalize() if up else 'Presence'} is the whole strategy.",
            f"Don't look directly at {name}. {up_bit} put them at {result.aura}.",
            f"{name}'s {result.aura} is just {up_bit} with better lighting.",
        ]
    elif result.aura >= 120:
        lines = [
            f"{name} walks in like they have a theme song. {up_bit} is why.",
            f"{name}'s main-character number is {result.aura}, paid for by {up_bit}.",
            f"{name} is at {result.aura} because {up_bit} outran {down_bit}.",
        ]
    elif result.aura >= 100:
        lines = [
            f"{name} glows at a polite {result.aura}. {up_bit} vs {down_bit}.",
            f"Ambient, not blinding: {up_bit} keeps {name} solvent.",
            f"{name}'s aura is {result.aura} — present, not a light show. Blame {down_bit} for the cap.",
        ]
    elif result.aura >= 75:
        lines = [
            f"{name} is extra in their own plot because {down_bit}.",
            f"{name} is NPC-adjacent: {down_bit} spent what {up_bit} tried to save.",
            f"{name}'s {result.aura} is a group project, and {down_bit} did not pull its weight.",
        ]
    else:
        lines = [
            f"{name} is overdrawn. {down_bit} filed the paperwork.",
            f"{name}'s aura is {result.aura}: {down_bit} was a lifestyle, not a moment.",
            f"{name} typed, and the room got worse. {down_bit}.",
        ]
    text = rng.choice(lines)
    return f"{text} {quote}".strip()


# Distinctive stat identities beat the generic "dominant trait" label.
STAT_PERSONAS: list[tuple] = [
    (lambda p: p.stats.avg_words <= 3.5 and p.aura and p.aura.aura >= 110,
     "The Phantom",
     ["Says less. Hits harder.", "Three words, full soundtrack.", "Left the chat and still won."]),
    (lambda p: p.stats.avg_words <= 4,
     "The Dry Texter",
     ["Writes like punctuation is a luxury tax.", "Every word costs money.",
      "Invented the period, then stopped using it."]),
    (lambda p: p.stats.avg_words >= 20 and p.iq and p.iq.iq >= 120,
     "The Resident Genius",
     ["Brought an essay to a meme fight.", "Cites sources in the group chat.",
      "Has a take, a counter-take, and a PDF."]),
    (lambda p: p.stats.avg_words >= 18,
     "The Essayist",
     ["Thinks the character limit is a dare.", "Averages a short story per thought.",
      "Please allow 5–7 business days to finish reading them."]),
    (lambda p: p.stats.late_night_ratio >= 0.4 and p.scores.get("chaotic", 0) >= 40,
     "The 3am Incident",
     ["Most dangerous after midnight.", "The sun is a personal attack.",
      "Peak hours the rest of us call 'please sleep'."]),
    (lambda p: p.stats.late_night_ratio >= 0.35,
     "The Night Shift",
     ["Clocks in when decent people log off.", "Moonlighting as a problem.",
      "3am is just another meeting."]),
    (lambda p: p.aura and p.aura.aura >= 150,
     "The Aura Farmer",
     ["Collects presence like a seasonal event.", "Could post 'ok' and still win.",
      "The chat orbits them."]),
    (lambda p: p.iq and p.iq.iq >= 140,
     "Galaxy Brain",
     ["Thinks in footnotes.", "Has definitely corrected a teacher.",
      "Their keyboard has tenure."]),
    (lambda p: p.iq and p.iq.iq <= 75,
     "Room Temperature",
     ["The braincell called in sick.", "Thinks in loading screens.",
      "Scientists are studying the typing."]),
    (lambda p: p.aura and p.aura.aura <= 50,
     "Aura Bankruptcy",
     ["Filed for presence Chapter 11.", "Every message is a withdrawal.",
      "The opposite of mysterious."]),
    (lambda p: p.server_ranks.get("active", 100) <= 8 and p.stats.message_count >= 80,
     "The Load-Bearing Wall",
     ["Has sent more messages than some people have had thoughts.",
      "The server would list slightly to the left without them.",
      "Touch grass is no longer a suggestion."]),
]

PAIR_PERSONAS: dict[tuple[str, str], tuple[str, list[str]]] = {
    ("funny", "toxic"): ("The Roast Comic", ["Puns, then a ratio.", "Laughter with a body count.",
                                            "The joke was you."]),
    ("funny", "chaotic"): ("The Unhinged Headliner", ["Bits at 200km/h.", "Comedy, then a keyboard smash.",
                                                     "The set has no setlist."]),
    ("funny", "cringe"): ("The Try-Hard Comic", ["Wants the laugh react more than the joke.",
                                                "Brainrot, but committed.", "Funny on a technicality."]),
    ("funny", "freaky"): ("The Dirty Comic", ["The punchline needs a warning label.",
                                             "They said it for the bit. Probably.", "After-dark open mic."]),
    ("funny", "serious"): ("The Deadpan", ["Explains the joke, then the joke is funnier.",
                                          "Dry ice in human form.", "A thesis, then a 💀."]),
    ("toxic", "funny"): ("The Mean Clown", ["Roasts first, laughs second.", "Charm and a restraining order.",
                                           "Sorry — not sorry — 💀."]),
    ("toxic", "chaotic"): ("Professional Menace", ["Patience: not found.", "Types with the capslock on purpose.",
                                                  "HR has a folder."]),
    ("toxic", "cringe"): ("The Loud L", ["Second-hand embarrassment with a ratio.",
                                        "Cringe, but aggressive about it.", "Lost the argument and the vibe."]),
    ("toxic", "freaky"): ("The Problem", ["The mods keep a separate folder.",
                                         "Unholy and unkind.", "Do not combine with after midnight."]),
    ("toxic", "serious"): ("The Debate Lord", ["Citations, then a 'skill issue'.",
                                              "Won the argument, lost the room.", "Well, actually — shut up."]),
    ("cringe", "funny"): ("Certified Cringelord", ["The bit is that there is no bit.",
                                                  "Fluent in a language nobody asked for.",
                                                  "They hit send. We all felt it."]),
    ("cringe", "chaotic"): ("The Brainrot Storm", ["Skibidi in all caps.", "A jump scare in slang.",
                                                  "The algorithm made them, then fled."]),
    ("cringe", "freaky"): ("The Public Menace", ["Cannot be read aloud at work.",
                                                "':3' and a crime.", "The group chat needed eye bleach."]),
    ("cringe", "toxic"): ("The Ratio Victim", ["Tried a catchphrase. It caught them.",
                                              "Cringe with a body count — their own.",
                                              "Posted it. We remember."]),
    ("cringe", "serious"): ("The LinkedIn Gamer", ["A thinkpiece in rizz.",
                                                  "Sincere, which makes it worse.",
                                                  "Explains the slang incorrectly."]),
    ("freaky", "funny"): ("The After Dark", ["The joke has a rating.", "Horny and hilarious, unfortunately.",
                                            "They said it. The 💀s agreed."]),
    ("freaky", "chaotic"): ("The Degenerate Wildcard", ["Impulse, then a suggestive emoji.",
                                                       "No thoughts, just 😏.",
                                                       "Every chat is after midnight."]),
    ("freaky", "toxic"): ("HR's Problem", ["Unholy remarks, holy rage.",
                                          "The folder has a folder.", "Do not @ them after 10."]),
    ("freaky", "serious"): ("The Unhinged Academic", ["A thesis, then a thirst trap.",
                                                     "Sources, then 😏.", "Office hours are cancelled."]),
    ("freaky", "cringe"): ("Down Bad, Professionally", ["The search history is a documentary.",
                                                       "Posted it with their whole chest.",
                                                       "We cannot unsee the ':3'."]),
    ("serious", "funny"): ("The Dry Scholar", ["A footnote, then a bit.", "Funny because it's sourced.",
                                              "Killed the joke by explaining it. On purpose."]),
    ("serious", "toxic"): ("The Reply Guy", ["Has a PDF ready.", "Corrected you, then you.",
                                            "Won on points. Lost on aura."]),
    ("serious", "chaotic"): ("The Unstable Analyst", ["A thread, in caps.", "Data, then a keyboard smash.",
                                                     "The graph is on fire."]),
    ("serious", "cringe"): ("The Sincere One", ["Means every word, including the slang.",
                                               "A thinkpiece nobody requested.",
                                               "Earnest. Painfully."]),
    ("serious", "freaky"): ("The After-Hours Lecturer", ["Office hours got weird.",
                                                        "A citation, then a 😏.",
                                                        "The reading list is banned."]),
    ("chaotic", "funny"): ("The Wildcard", ["Eight messages, one thought, three 💀s.",
                                           "Types like the building is on fire.",
                                           "A jump scare with a punchline."]),
    ("chaotic", "toxic"): ("The Walking Incident", ["Caps lock is a lifestyle.",
                                                   "Started it. Finished it. Started it again.",
                                                   "The channel still has the scorch marks."]),
    ("chaotic", "cringe"): ("The Keyboard Smash", ["ASDFGHJKL, but make it slang.",
                                                  "Impulse purchases of words.",
                                                  "No draft. No shame. No period."]),
    ("chaotic", "freaky"): ("After Midnight", ["The hour is a warning label.",
                                              "Chaotic and down bad. A combo meal.",
                                              "Please do not feed."]),
    ("chaotic", "serious"): ("The Unstable Essay", ["A manifesto, in bursts.",
                                                   "Thinks in paragraphs, types in explosions.",
                                                   "The take arrived in 11 messages."]),
}

SOLO_PERSONAS: dict[str, tuple[str, list[str]]] = {
    "funny": ("The Class Clown", ["Lives for the laugh react.", "Every chat is an open mic.",
                                  "Comedy first, consequences never."]),
    "toxic": ("The Menace", ["Patience: not found.", "Types with clenched fists.",
                             "Has a ratio for every occasion."]),
    "cringe": ("The Cringelord", ["Has never once hesitated before pressing send.",
                                 "Brainrot, fluent.", "Second-hand embarrassment on tap."]),
    "freaky": ("The Degenerate", ["Messages cannot be read aloud in public.",
                                 "Down bad, professionally.", "The mods keep a separate folder."]),
    "serious": ("The Philosopher", ["Writes essays where a 'lol' would do.",
                                   "Brings sources to a meme fight.", "Well, actually."]),
    "chaotic": ("The Wildcard", ["Operates on pure impulse and caps lock.",
                                "Eight messages, one thought.", "A jump scare with a keyboard."]),
}

NPC = ("The NPC", ["Aggressively, suspiciously normal.", "Default settings, never changed.",
                   "Background character energy.", "Could be a bot. We checked twice."])


def persona(profile: "Profile") -> tuple[str, str]:
    """A title + tagline unique to this member's mix of stats and traits."""
    rng = seeded(profile.user_id, profile.name, tuple(profile.scores[t] for t in TRAITS),
                 round(profile.stats.avg_words, 1), profile.iq.iq if profile.iq else 0,
                 profile.aura.aura if profile.aura else 0)
    for check, title, lines in STAT_PERSONAS:
        try:
            if check(profile):
                return title, rng.choice(lines)
        except Exception:
            continue
    ranked = sorted(TRAITS, key=lambda t: profile.scores[t], reverse=True)
    top, second = ranked[0], ranked[1]
    if profile.scores[top] >= 25 and profile.scores[second] >= 20:
        pair = PAIR_PERSONAS.get((top, second))
        if pair:
            return pair[0], rng.choice(pair[1])
    if profile.scores[top] >= 25:
        title, lines = SOLO_PERSONAS[top]
        return title, rng.choice(lines)
    return NPC[0], rng.choice(NPC[1])
