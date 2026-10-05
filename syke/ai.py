"""LLM roast writer, with a deterministic offline fallback."""

from __future__ import annotations

import json
import logging
import random
import re

import httpx

from .config import Settings
from .models import Msg
from .profile import Profile
from .roast import fallback_summary
from .stats import CUSTOM_EMOJI_RE
from .traits import HIGHLIGHT_TRAITS, TRAITS
from .yap import Turn, offline_pick, stitch

log = logging.getLogger("syke.ai")

SAMPLE_SIZE = 250
AI_TIMEOUT_SECONDS = 30
COMPOSE_CANDIDATES = 40

TRAIT_GUIDE = """Trait definitions (judge meaning and intent, not just keywords; sarcasm and \
in-jokes count, a lone "lol" does not make someone funny):
- funny: jokes, wit, banter, absurd takes, bits that others would laugh at.
- toxic: insults, trash talk, hostility or contempt aimed at people, rage-posting.
- cringe: try-hard slang and brainrot (rizz, skibidi, sigma), uwu/:3 talk, roleplay actions, \
second-hand-embarrassment energy.
- freaky: sexual innuendo, thirst, flirting, "down bad" energy, suggestive emojis.
- serious: earnest, thoughtful or informative messages, real advice, careful arguments."""

STYLES = [
    "a fake peer-reviewed scientific study that went badly wrong",
    "a nature documentary narrator observing a rare creature",
    "a police incident report written by an exhausted officer",
    "a courtroom verdict read out by a judge who has had enough",
    "a one-star restaurant review of the person",
    "a horoscope that is far too specific",
    "a sports commentator calling their messages like a live match",
    "a museum placard describing an ancient, cursed artifact",
    "video game patch notes nerfing their worst habits",
    "a classified FBI file with suspicious redactions",
    "a weather forecast where they are the storm",
    "a dating profile written by their worst enemy",
    "a school report card from a disappointed teacher",
    "a product recall notice",
    "a true-crime podcast intro",
    "a wildlife warning sign at a national park",
]

SYSTEM_PROMPT = """You are SYKE, a Discord bot that writes savage-but-affectionate personality \
reports about server members after reading their actual messages.

Writing rules:
- Be genuinely funny, sarcastic and SPECIFIC. Every sentence should be about THIS person: quote or \
paraphrase at least one of their real messages, and riff on their catchphrases, emojis, timing or slang.
- Avoid generic filler ("is a unique individual", "keeps things interesting"). Surprise the reader.
- Roast behaviour, never identity. No comments on race, religion, gender, sexuality, disability, \
body, or real mental-health diagnoses. No slurs. Nothing sexual beyond a wink.
- Use the person's name exactly as given, in the third person.
- The summary is 2-3 short paragraphs, max 100 words, ending with a deadpan one-line verdict.
- You may use the server's own custom emojis by writing their :name: exactly as listed. Use at most 3.
- Never write clock times. To mention when they're most active, write the token PEAK_HOURS; it is \
replaced with times in each reader's own timezone. You can still say things like "after midnight".
- Pick highlight messages ONLY by their index from the numbered list, or null if none fit.

""" + TRAIT_GUIDE + """

Also judge the messages yourself:
- "labels": the indexes of messages that clearly show a trait, with the traits they show. Only \
include clear cases (at most 60).
- "trait_scores": your own 0-100 rating of how strongly the person shows each trait overall.

Reply with JSON only:
{"summary": str,
 "funniest": int|null, "unhinged": int|null, "freakiest": int|null, "toxic": int|null,
 "bonus_achievement": {"emoji": str, "name": str} | null,
 "labels": [{"i": int, "traits": [str]}],
 "trait_scores": {"funny": int, "toxic": int, "cringe": int, "freaky": int, "serious": int, "chaotic": int}}"""


def readable(text: str) -> str:
    """Custom emoji tags become :name: so the model can read what they are."""
    return CUSTOM_EMOJI_RE.sub(lambda m: f":{m.group(1)}:", text)


def _sample_messages(profile: Profile) -> list[Msg]:
    """Recent messages plus the heuristic highlights, for the prompt."""
    msgs = sorted((m for m in profile.messages if m.content.strip()), key=lambda m: m.created_at)
    recent = msgs[-SAMPLE_SIZE:]
    highlight_ids = {id(m) for m in profile.highlights.values() if m is not None}
    extra = [m for m in msgs[:-SAMPLE_SIZE] if id(m) in highlight_ids]
    return extra + recent


def _user_prompt(profile: Profile, sample: list[Msg], style: str, server_emojis: list[str],
                 avoid: str | None) -> str:
    st = profile.stats
    payload = {
        "name": profile.name,
        "messages_analyzed": st.message_count,
        "most_active_hours_utc": st.peak_label,
        "server_local_late_night_share": round(st.late_night_ratio, 2),
        "avg_words_per_message": round(st.avg_words, 1),
        "top_emojis": [(readable(e), c) for e, c in st.top_emojis],
        "top_words": st.top_words,
        "catchphrases": st.top_phrases,
        "keyword_trait_scores_0_to_100": profile.scores,
        "server_rank_top_percent": profile.server_ranks,
        "achievements": [n for _, n in profile.achievements],
        "iq": profile.iq.iq if profile.iq else None,
    }
    parts = [
        f"WRITE THE SUMMARY IN THE STYLE OF: {style}",
        f"PROFILE DATA:\n{json.dumps(payload, ensure_ascii=False)}",
    ]
    if server_emojis:
        parts.append("SERVER EMOJIS YOU MAY USE: " + " ".join(f":{n}:" for n in server_emojis[:60]))
    if avoid:
        parts.append(f"YOUR LAST REPORT ON THEM (do not reuse its jokes or structure):\n{avoid[:600]}")
    numbered = "\n".join(f"[{i}] {readable(m.content)[:220]}" for i, m in enumerate(sample))
    parts.append(f"MESSAGES:\n{numbered}")
    return "\n\n".join(parts)


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model reply")
    return json.loads(match.group(0))


async def _call_openai(settings: Settings, system: str, user: str, temperature: float = 1.0) -> str:
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_model,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _call_anthropic(settings: Settings, system: str, user: str, temperature: float = 1.0) -> str:
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": settings.anthropic_model,
                "max_tokens": 1500,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        return "".join(b.get("text", "") for b in resp.json()["content"])


def with_server_emojis(text: str, emojis: dict[str, str]) -> str:
    """Turn :name: back into a real `<:name:id>` tag for emojis this server has."""
    if not emojis:
        return text
    return re.sub(r"(?<![<\w]):(\w{2,32}):", lambda m: emojis.get(m.group(1), m.group(0)), text)


def _apply_ai_result(profile: Profile, data: dict, sample: list[Msg], emojis: dict[str, str] | None = None) -> None:
    summary = str(data.get("summary", "")).strip()
    if summary:
        summary = summary.replace("PEAK_HOURS", profile.stats.peak_local())
        profile.summary = with_server_emojis(summary, emojis or {})
        profile.ai_used = True

    for slot in HIGHLIGHT_TRAITS:
        idx = data.get(slot)
        if isinstance(idx, int) and 0 <= idx < len(sample):
            profile.highlights[slot] = sample[idx]

    bonus = data.get("bonus_achievement")
    if isinstance(bonus, dict) and bonus.get("name"):
        entry = (str(bonus.get("emoji", "🏅"))[:4], str(bonus["name"])[:32])
        if entry[1] not in {n for _, n in profile.achievements}:
            profile.achievements = [*profile.achievements[:5], entry]

    labels: dict[int, frozenset[str]] = {}
    for item in data.get("labels") or []:
        if not isinstance(item, dict):
            continue
        idx, traits = item.get("i"), item.get("traits")
        if isinstance(idx, int) and 0 <= idx < len(sample) and isinstance(traits, list):
            picked = frozenset(t for t in traits if t in TRAITS)
            if picked and sample[idx].message_id:
                labels[sample[idx].message_id] = picked
    profile.ai_labels = labels

    scores = data.get("trait_scores")
    if isinstance(scores, dict):
        profile.ai_scores = {t: max(0, min(100, int(v))) for t, v in scores.items()
                             if t in TRAITS and isinstance(v, (int, float))}


async def write_roast(
    profile: Profile,
    settings: Settings,
    server_emojis: dict[str, str] | None = None,
    avoid: str | None = None,
    rng: random.Random | None = None,
) -> Profile:
    """Fill in `profile.summary`, and with AI also highlights, message labels and trait scores. Never raises.

    `server_emojis` maps custom emoji names to their `<:name:id>` tags; `avoid` is the previous
    summary for this member, so repeated runs read differently.
    """
    rng = rng or random.Random()
    profile.summary = fallback_summary(profile, rng, server_emojis)
    if settings.ai_provider == "none":
        return profile

    sample = _sample_messages(profile)
    emojis = server_emojis or {}
    user_prompt = _user_prompt(profile, sample, rng.choice(STYLES), list(emojis), avoid)
    try:
        if settings.ai_provider == "openai":
            reply = await _call_openai(settings, SYSTEM_PROMPT, user_prompt)
        else:
            reply = await _call_anthropic(settings, SYSTEM_PROMPT, user_prompt)
        _apply_ai_result(profile, _parse_json(reply), sample, emojis)
    except Exception:  # the offline roast is always a valid answer
        log.exception("AI roast failed; using offline summary")
    return profile


COMPOSE_PROMPT = """You are SYKE, a Discord bot that can only speak using real messages members of \
this server sent in the past. You get the recent chat and numbered CANDIDATES (old server messages).

Build the reply that fits best, as if a quick-witted regular said it. You may:
- use one candidate as it is, or
- stitch 2-3 pieces together into one line. A piece is a whole candidate or a chunk copied \
word-for-word from one (e.g. a punchline, a keyword phrase, an emoji).

What makes a great reply: it actually answers or reacts to the TARGET message (questions get \
answers, insults get comebacks, jokes get played along with), it picks up the topic, names or \
keywords being discussed, and it is funny. Stitched replies must read naturally, not like word salad; \
one perfect candidate beats a clumsy combination. Never change, add or reorder words inside a piece. \
Never use anything that attacks identity (race, religion, gender, sexuality, disability).

Reply with JSON only: {"parts": [{"id": int, "text": "exact chunk, or omit text for the whole message"}]}"""


def _chat_block(chat: list[Turn]) -> str:
    return "\n".join(f"{t.author[:24]}: {' '.join(t.text.split())[:200]}" for t in chat[-8:]) or "(quiet)"


async def compose_reply(settings: Settings, ranked: list[tuple[float, str]], target: Turn,
                        chat: list[Turn] = (), said: str | None = None) -> str | None:
    """The best reply to `target` made from real server messages; AI stitches, offline picks. Never raises."""
    candidates = [text for _, text in ranked[:COMPOSE_CANDIDATES]]
    if not candidates:
        return None
    banned = {target.text} | ({said} if said else set())
    if settings.ai_provider != "none":
        situation = (f"SYKE SAID: {said[:300]}\nTHEY REPLIED (TARGET): " if said
                     else "SYKE is jumping into the chat. TARGET (latest message): ")
        numbered = "\n".join(f"[{i}] {' '.join(text.split())[:220]}" for i, text in enumerate(candidates))
        prompt = (f"RECENT CHAT:\n{_chat_block(chat)}\n\n{situation}{target.author[:24]}: {target.text[:300]}"
                  f"\n\nCANDIDATES (best keyword matches first):\n{numbered}")
        try:
            if settings.ai_provider == "openai":
                answer = await _call_openai(settings, COMPOSE_PROMPT, prompt, temperature=0.8)
            else:
                answer = await _call_anthropic(settings, COMPOSE_PROMPT, prompt, temperature=0.8)
            parts = _parse_json(answer).get("parts")
            if isinstance(parts, list):
                reply = stitch(candidates, parts, banned)
                if reply:
                    return reply
            log.warning("AI yap reply unusable: %r", answer[:200])
        except Exception:
            log.exception("AI yap reply failed; matching keywords instead")
    usable = [(score, text) for score, text in ranked if text not in banned]
    return offline_pick(usable)
