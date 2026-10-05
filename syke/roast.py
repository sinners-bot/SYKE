"""Offline roast writer: varied, stat-driven summaries for when no AI is configured (or it fails)."""

from __future__ import annotations

import random

from . import lexicon as lx
from .profile import Profile
from .traits import TRAIT_META, TRAITS

OPENERS: dict[str, list[str]] = {
    "funny": [
        "{name} treats every conversation like an open mic night nobody signed up for",
        "{name} is considerably funnier than average, which is a low bar but still impressive",
        "{name} has turned 'lmao' into a load-bearing part of the server",
        "{name} delivers punchlines with the confidence of someone who's never been booed",
        "{name} is the reason half the server's skull reacts are worn out",
        "{name} runs a comedy club out of the chat, and the cover charge is your dignity",
        "{name} laughs at their own jokes first, loudest, and occasionally correctly",
        "{name} is held together entirely by bits, callbacks and crying-laughing emojis",
    ],
    "toxic": [
        "{name} has the patience of a microwave and the diplomacy of a brick",
        "{name} loses all composure the moment someone says something mildly stupid",
        "{name} types every reply like it's the final round of a rap battle",
        "{name} considers 'skill issue' a complete and valid argument",
        "{name} has been reported to HR by people who don't even work here",
        "{name} treats the reply button like a loaded weapon",
        "{name} could start a fight in an empty channel",
        "{name} speaks fluent ratio and has never once been the bigger person",
    ],
    "cringe": [
        "{name} types like an anime protagonist who discovered the internet yesterday",
        "{name} has never once hesitated before pressing send, and it shows",
        "{name} uses slang like it was bought in bulk from a TikTok clearance sale",
        "{name} has more rizz in their vocabulary than in their life",
        "{name} communicates in a dialect researchers can only describe as 'brainrot, fluent'",
        "{name} says 'slay' with the sincerity of a wedding vow",
        "{name} posts like the second-hand embarrassment is a renewable resource",
        "{name} has single-handedly kept ':3' on life support",
    ],
    "freaky": [
        "{name} displays a pattern of messages that cannot be read aloud in public",
        "{name} has been flagged by our scientists for repeated unholy remarks",
        "{name} turns innocent conversations into something the mods have to squint at",
        "{name} is down bad on a scale our instruments weren't built for",
        "{name} sends messages that make the whole server lean back from the screen",
        "{name} treats every chat like it's after 11pm, even at noon",
        "{name} has a search history our lab legally can't subpoena",
        "{name} put the 'fr' in 'freaky' and refuses to take it out",
    ],
    "serious": [
        "{name} writes paragraphs where a 'lol' would do, like a reply guy with a thesis",
        "{name} is the only one here who reads the whole message before replying",
        "{name} brings citations to a meme fight",
        "{name} says 'well, actually' with their entire chest",
        "{name} treats the server like a debate club that never adjourns",
        "{name} has a take on everything and a source for half of them",
        "{name} explains jokes, politely, until they stop being jokes",
        "{name} is what happens when a LinkedIn post learns to use Discord",
    ],
    "chaotic": [
        "{name} is a highly chaotic member whose keyboard is clearly in danger",
        "{name} operates on pure impulse and caps lock",
        "{name} sends eight messages where one would do, all within the same second",
        "{name} types like they're being chased",
        "{name} has the attention span of a goldfish on an energy drink",
        "{name} treats punctuation as a suggestion and caps lock as a lifestyle",
        "{name} is a jump scare with a keyboard",
        "{name} posts like the building is on fire and they started it",
    ],
}

NPC_OPENERS = [
    "{name} is aggressively, suspiciously normal, which is exactly what a sleeper agent would do",
    "{name} has the personality of a loading screen tip",
    "{name} could be replaced by a bot and the server would need a week to notice",
    "{name} posts like they're trying not to be subpoenaed",
    "{name} is so balanced our instruments assumed they were broken",
    "{name} is the human equivalent of the default profile picture",
]

SECONDARY: dict[str, list[str]] = {
    "funny": ["has a suspiciously good sense of humour", "gets a laugh out of people more often than not",
              "is accidentally hilarious at least once a day"],
    "toxic": ["keeps a sharp tongue on standby", "has a 'shut up' loaded for every occasion",
              "has started at least one argument they're still winning in their head"],
    "cringe": ["has a cringe meter that never quite reaches zero", "says 'bestie' with unsettling frequency",
               "has posted at least one message they'd delete if they had any shame"],
    "freaky": ["has a freaky streak that surfaces at the worst moments", "makes the mods nervous for reasons",
               "has a way of making 'good morning' sound like a threat"],
    "serious": ["has a serious side that ambushes people mid-meme", "will absolutely explain it to you",
                "has opinions with footnotes"],
    "chaotic": ["has chaos energy that leaks into everything", "sends messages in rapid-fire bursts",
                "has a caps lock key that's seen things"],
}

STAT_JABS = [
    (lambda p: p.stats.late_night_ratio >= 0.3,
     ["Spends a worrying amount of time online after midnight.", "Is most active when decent people are asleep.",
      "Treats 3am like prime time."]),
    (lambda p: p.server_ranks.get("active", 100) <= 10,
     ["Has sent more messages than some people have had thoughts.", "Is basically a load-bearing wall of this server.",
      "Should, statistically speaking, touch grass."]),
    (lambda p: p.stats.avg_words <= 4,
     ["Writes like every word costs money.", "Communicates exclusively in fortune-cookie-sized fragments."]),
    (lambda p: p.stats.avg_words >= 20,
     ["Averages {words} words a message, so bring snacks.", "Thinks the message length limit is a dare."]),
]

EVIDENCE = [
    'Exhibit A: "{quote}"',
    'Key evidence: "{quote}" No further questions.',
    'They really typed "{quote}" and hit send.',
    'The lab has framed this one: "{quote}"',
    'Witnesses still bring up "{quote}".',
]

CATCHPHRASE = [
    'Catchphrase detected: "{phrase}".',
    'Has said "{phrase}" so often it should be trademarked.',
    'Linguists have traced "{phrase}" back to them.',
]

EMOJI_HABIT = [
    "Communicates primarily through {emoji}.",
    "Would be speechless without {emoji}.",
    "Holds the server record for {emoji} abuse.",
]

PEAK = [
    "Peak activity: {hours}, when the damage is done.",
    "Clocks in between {hours}, like it's a shift.",
]

CLOSERS = [
    "Researchers are concerned.",
    "Further study has been denied funding.",
    "The lab has requested a restraining order.",
    "Science cannot explain this, and frankly doesn't want to.",
    "Our recommendation: go outside.",
    "This report has been forwarded to their mother.",
    "Prognosis: unfixable, but entertaining.",
    "The ethics board has asked us to stop watching.",
    "Two interns quit while compiling this.",
    "We ran the numbers twice. They're worse the second time.",
    "Case closed. Case reopened. Case thrown into the sea.",
    "Peer review was unanimous: yikes.",
    "Conclusion: a menace, but our menace.",
    "We'd say 'get help', but help has filed a complaint.",
    "Please do not feed after midnight.",
    "Results have been sealed for the public's safety.",
]

HIGHLIGHT_FOR = {"funny": "funniest", "chaotic": "unhinged", "freaky": "freakiest", "toxic": "toxic"}


def _quote(text: str, limit: int = 90) -> str:
    text = " ".join(text.split()).replace('"', "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fitting_emoji(trait: str, server_emojis: dict[str, str], rng: random.Random) -> str | None:
    """A server emoji whose name suits the trait, e.g. :KEKW: for funny."""
    needles = lx.EMOJI_NAME_HINTS.get(trait, ())
    matches = [tag for name, tag in server_emojis.items() if any(n in name.lower() for n in needles)]
    return rng.choice(matches) if matches else None


def fallback_summary(
    profile: Profile, rng: random.Random | None = None, server_emojis: dict[str, str] | None = None
) -> str:
    """Three short paragraphs built from the member's real numbers and messages, different every run."""
    rng = rng or random.Random()
    st = profile.stats
    name = profile.name
    ranked = sorted(TRAITS, key=lambda t: profile.scores[t], reverse=True)
    top, second = ranked[0], ranked[1]

    if profile.scores[top] < 20:
        first = rng.choice(NPC_OPENERS).format(name=name) + "."
    else:
        first = rng.choice(OPENERS[top]).format(name=name) + "."
    jabs = [rng.choice(lines) for check, lines in STAT_JABS if check(profile)]
    if jabs:
        first += " " + rng.choice(jabs).format(words=round(st.avg_words))

    middle: list[str] = []
    if profile.scores[second] >= 15:
        middle.append(f"Also {rng.choice(SECONDARY[second])} ({TRAIT_META[second][1].lower()}: "
                      f"{profile.scores[second]}%).")
    evidence = [m for t in (top, second) if (slot := HIGHLIGHT_FOR.get(t)) and (m := profile.highlights.get(slot))]
    evidence += [m for m in profile.highlights.values() if m is not None and m not in evidence]
    options = []
    if evidence:
        options.append(rng.choice(EVIDENCE).format(quote=_quote(evidence[0].content)))
    if st.top_phrases:
        options.append(rng.choice(CATCHPHRASE).format(phrase=st.top_phrases[0][0]))
    if st.top_emojis:
        options.append(rng.choice(EMOJI_HABIT).format(emoji=st.top_emojis[0][0]))
    options.append(rng.choice(PEAK).format(hours=st.peak_label))
    rng.shuffle(options)
    middle.extend(options[:2])

    closer = rng.choice(CLOSERS)
    emoji = fitting_emoji(top, server_emojis or {}, rng)
    if emoji:
        closer += f" {emoji}"
    return f"{first}\n\n{' '.join(middle)}\n\n{closer}"
