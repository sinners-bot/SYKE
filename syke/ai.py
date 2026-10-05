"""LLM roast writer, with a deterministic offline fallback."""

from __future__ import annotations

import json
import logging
import random
import re

import httpx

from .config import Settings
from .profile import Profile
from .traits import HIGHLIGHT_TRAITS, TRAIT_META, TRAITS

log = logging.getLogger("syke.ai")

SAMPLE_SIZE = 250

SYSTEM_PROMPT = """You are SYKE, a Discord bot that writes savage-but-affectionate personality \
reports about server members, in the style of a fake scientific study gone wrong.

Rules:
- Be funny, sarcastic and specific: reference their actual habits, phrases and timing.
- Roast behaviour, never identity. No comments on race, religion, gender, sexuality, \
disability, body, or real mental-health diagnoses. No slurs. Nothing sexual beyond a wink.
- Use the person's name exactly as given. Refer to them in the third person.
- The summary is 3 short paragraphs, max 90 words total. End with a deadpan one-liner \
like "Researchers are concerned."
- Pick highlight messages ONLY by their index from the provided list, or null if none fit.

Reply with JSON only:
{"summary": str,
 "funniest": int|null, "unhinged": int|null, "freakiest": int|null, "toxic": int|null,
 "bonus_achievement": {"emoji": str, "name": str} | null}"""


def _sample_messages(profile: Profile) -> list[str]:
    """Recent messages plus the heuristic highlights, trimmed for the prompt."""
    msgs = sorted(profile.messages, key=lambda m: m.created_at)
    recent = msgs[-SAMPLE_SIZE:]
    highlight_ids = {id(m) for m in profile.highlights.values() if m is not None}
    extra = [m for m in msgs[:-SAMPLE_SIZE] if id(m) in highlight_ids]
    return [m.content[:220] for m in extra + recent if m.content.strip()]


def _user_prompt(profile: Profile, sample: list[str]) -> str:
    st = profile.stats
    payload = {
        "name": profile.name,
        "messages_analyzed": st.message_count,
        "most_active_hours": st.peak_label,
        "late_night_ratio": round(st.late_night_ratio, 2),
        "avg_words_per_message": round(st.avg_words, 1),
        "top_emojis": st.top_emojis,
        "top_words": st.top_words,
        "catchphrases": st.top_phrases,
        "trait_scores_0_to_100": profile.scores,
        "server_rank_top_percent": profile.server_ranks,
        "achievements": [n for _, n in profile.achievements],
    }
    numbered = "\n".join(f"[{i}] {text}" for i, text in enumerate(sample))
    return f"PROFILE DATA:\n{json.dumps(payload, ensure_ascii=False)}\n\nMESSAGES:\n{numbered}"


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model reply")
    return json.loads(match.group(0))


async def _call_openai(settings: Settings, system: str, user: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_model,
                "temperature": 0.9,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _call_anthropic(settings: Settings, system: str, user: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": settings.anthropic_model,
                "max_tokens": 700,
                "temperature": 0.9,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        return "".join(b.get("text", "") for b in resp.json()["content"])


def _apply_ai_result(profile: Profile, data: dict, sample: list[str]) -> None:
    summary = str(data.get("summary", "")).strip()
    if summary:
        profile.summary = summary
        profile.ai_used = True

    by_text = {m.content[:220]: m for m in profile.messages}
    for slot in HIGHLIGHT_TRAITS:
        idx = data.get(slot)
        if isinstance(idx, int) and 0 <= idx < len(sample):
            picked = by_text.get(sample[idx])
            if picked is not None:
                profile.highlights[slot] = picked

    bonus = data.get("bonus_achievement")
    if isinstance(bonus, dict) and bonus.get("name"):
        entry = (str(bonus.get("emoji", "🏅"))[:4], str(bonus["name"])[:32])
        if entry[1] not in {n for _, n in profile.achievements}:
            profile.achievements = [*profile.achievements[:5], entry]


async def write_roast(profile: Profile, settings: Settings) -> Profile:
    """Fill in `profile.summary` (and maybe better highlights). Never raises."""
    profile.summary = fallback_summary(profile)
    if settings.ai_provider == "none":
        return profile

    sample = _sample_messages(profile)
    user_prompt = _user_prompt(profile, sample)
    try:
        if settings.ai_provider == "openai":
            reply = await _call_openai(settings, SYSTEM_PROMPT, user_prompt)
        else:
            reply = await _call_anthropic(settings, SYSTEM_PROMPT, user_prompt)
        _apply_ai_result(profile, _parse_json(reply), sample)
    except Exception:  # the offline roast is always a valid answer
        log.exception("AI roast failed; using offline summary")
    return profile


TRAIT_LINES: dict[str, list[str]] = {
    "funny": [
        "is considerably funnier than average, which is a low bar but still impressive",
        "treats every conversation like an open mic night nobody signed up for",
    ],
    "toxic": [
        "has the patience of a microwave and the diplomacy of a brick",
        "loses all composure the moment someone says something stupid",
    ],
    "cringe": [
        "types like an anime protagonist who discovered the internet yesterday",
        "has never once hesitated before pressing send, and it shows",
    ],
    "freaky": [
        "displays a pattern of messages that cannot be read aloud in public",
        "has been flagged by our scientists for repeated unholy remarks",
    ],
    "serious": [
        "writes paragraphs when a 'lol' would do, like a reply guy with a thesis",
        "is the only one here who actually reads the whole message before replying",
    ],
    "chaotic": [
        "is a highly chaotic member whose keyboard is clearly in danger",
        "operates on pure impulse and caps lock",
    ],
}

CLOSERS = [
    "Researchers are concerned.",
    "Further study has been denied funding.",
    "The lab has requested a restraining order.",
    "Science cannot explain this, and frankly doesn't want to.",
    "Our recommendation: go outside.",
]


def fallback_summary(profile: Profile) -> str:
    rng = random.Random(profile.user_id)
    st = profile.stats
    ranked = sorted(TRAITS, key=lambda t: profile.scores[t], reverse=True)
    top, second = ranked[0], ranked[1]

    first = f"{profile.name} {rng.choice(TRAIT_LINES[top])}."
    if st.late_night_ratio >= 0.25:
        first += " Spends far too much time online after midnight."
    elif profile.server_ranks.get("active", 100) <= 15:
        first += " Has sent more messages than some people have had thoughts."

    second_line = f"Also shows strong {TRAIT_META[second][1].lower()} energy: {rng.choice(TRAIT_LINES[second])}."
    if st.top_phrases:
        second_line += f' Catchphrase detected: "{st.top_phrases[0][0]}".'
    elif st.top_emojis:
        second_line += f" Communicates primarily through {st.top_emojis[0][0]}."

    return f"{first}\n\n{second_line}\n\n{rng.choice(CLOSERS)}"
