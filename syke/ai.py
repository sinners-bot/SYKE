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
from .yap import (DEFAULT_TONE, Turn, Vocab, Voice, folded, offline_pick, query_weights, style_hint,
                  tidy_reply, vocabulary)

log = logging.getLogger("syke.ai")

SAMPLE_SIZE = 250
AI_TIMEOUT_SECONDS = 30
COMPOSE_CANDIDATES = 60

TRAIT_GUIDE = """Trait definitions (judge meaning and intent, not just keywords; sarcasm and \
in-jokes count, a lone "lol" does not make someone funny):
- funny: jokes, wit, banter, absurd takes, bits that others would laugh at.
- toxic: insults, trash talk, hostility or contempt aimed at a person, passive-aggressive digs \
("sure jan", "nobody asked"), ratio/cope, "you're cooked". A swear with nobody attached is weak; \
aimed at someone, it counts.
- cringe: try-hard slang and brainrot (rizz, skibidi, sigma), uwu/:3 talk, roleplay actions, \
second-hand-embarrassment energy.
- freaky: thirst, flirting, innuendo, down-bad comments, suggestive emojis, even when nobody says \
an explicit word. A wink counts.
- chaotic: also what the unhinged highlight is for. Caps, spam and keyboard smashes count, and so \
do crashouts, spirals, feral non-sequiturs and "i'm losing it" energy in lowercase. Prefer a \
message that feels unhinged over one that is merely loud.
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
- If this summary could be pasted onto a different member and still make sense, rewrite it. Name a \
habit, quote or number that belongs only to them.
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
        "aura": profile.aura.aura if profile.aura else None,
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


async def _call_openai(settings: Settings, system: str, user: str, temperature: float = 1.0,
                       timeout: float = AI_TIMEOUT_SECONDS) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
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


async def _call_anthropic(settings: Settings, system: str, user: str, temperature: float = 1.0,
                          timeout: float = AI_TIMEOUT_SECONDS) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
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


COMPOSE_PROMPT = """You are SYKE, a long-time regular in this Discord server. You are not an assistant \
and you never talk like one. You get the recent chat, the TONE to hit, the server's VOCABULARY (words \
mined from everything people here have said, plus lexicon slang), how people here TYPE, and numbered \
AMMO: real stored messages from this server, plus some marked (remix) that a Markov chain made from them.

Write ONE new, original message, the way the server's most quick-witted regular would type it right now.
- AMMO is ammunition, not a script and not a quote bank. Steal the take, the running joke, the opinion, \
the comeback, the topic — then say it in your own words. Never copy a message, never stitch pieces \
together, never paste a ready-made phrase.
- Talk with this server's words. Build the line from VOCABULARY: topic words first, then words people \
here actually use, then lexicon slang that fits the tone. Prefer single words over multi-word phrases. \
Spell like they spell. Never reach for words nobody here would use.
- React to the TARGET directly. Questions get an answer (a real one or a bait one), insults get a \
comeback, jokes get played along with or one-upped, and pick up what's being talked about.
- Nail the TONE:
  - toxic: savage trash talk, roast them back, petty and disrespectful.
  - funny: a joke, an absurd twist, perfectly timed banter.
  - freaky: suggestive innuendo, down-bad flirting, unhinged thirst; PG-13, never explicit.
- Type like a person on their phone, following TYPING: short, casual, slang, grammar optional. One \
thought, not a paragraph. At most one emoji, and only one this server uses. No greetings, no hashtags, \
no "what do you think?", no explaining the joke, no quotation marks around the message, no \
exclamation-mark enthusiasm, never mention being a bot or an AI.
- Never repeat or lightly rephrase something SYKE already said in this conversation.
- Never attack identity (race, religion, gender, sexuality, disability), no slurs, never tell anyone \
to hurt themselves, never anything sexual about minors, and never @mention anyone.

Reply with JSON only: {"reply": "your message"}"""


def _chat_block(chat: list[Turn]) -> str:
    return "\n".join(f"{t.author[:24]}: {' '.join(t.text.split())[:200]}" for t in chat[-8:]) or "(quiet)"


def _vocab_block(vocab: Vocab) -> str:
    lines = []
    if vocab.topic:
        lines.append("about this topic: " + ", ".join(vocab.topic))
    if vocab.words:
        lines.append("words people here use: " + ", ".join(vocab.words))
    if vocab.slang:
        lines.append("lexicon slang: " + ", ".join(vocab.slang))
    if vocab.emojis:
        lines.append("emojis people here use: " + " ".join(readable(e) for e in vocab.emojis))
    return "\n".join(lines) or "(borrow words from the AMMO messages)"


async def compose_reply(settings: Settings, ranked: list[tuple[float, str]], target: Turn,
                        chat: list[Turn] = (), said: str | None = None, tone: str = DEFAULT_TONE,
                        remixed: list[str] = (), avoid: list[str] = (), voice: Voice | None = None,
                        rng: random.Random | None = None, weights: dict[str, float] | None = None) -> str | None:
    """The best reply to `target` in the server's words and the chat's tone. Never raises.

    With AI, stored messages and Markov `remixed` lines are ammunition for a new message written
    from the server's `voice` (vocabulary + lexicon words); offline, the best-ranked line is used.
    `avoid` is recent SYKE lines that must not be reused.
    """
    banned = {target.text} | ({said} if said else set()) | set(avoid)
    banned_keys = {folded(t) for t in banned if t}
    remix_set = set(remixed)
    real = [text for _, text in ranked if text not in remix_set]
    raw = real[:COMPOSE_CANDIDATES - len(remix_set)] + list(remixed)
    fresh = [text for text in raw if folded(text) not in banned_keys]
    candidates = fresh or raw
    if not candidates:
        return None
    if settings.ai_provider != "none":
        situation = (f"SYKE SAID: {said[:300]}\nTHEY REPLIED (TARGET): " if said
                     else "SYKE is jumping into the chat. TARGET (latest message): ")
        numbered = "\n".join(f"[{i}]{' (remix)' if text in remix_set else ''} {readable(' '.join(text.split()))[:160]}"
                             for i, text in enumerate(candidates))
        vocab = vocabulary(voice, tone, rng, weights or query_weights(target.text, said, chat), candidates)
        prompt = (f"RECENT CHAT:\n{_chat_block(chat)}\n\n{situation}{target.author[:24]}: {target.text[:300]}"
                  f"\n\nTONE: {tone}\n\nTYPING: {style_hint(voice)}\n\nVOCABULARY:\n{_vocab_block(vocab)}"
                  f"\n\nAMMO (steal the take, never copy; best matches first, remixes last):\n{numbered}")
        if avoid:
            already = "\n".join(f"- {' '.join(t.split())[:200]}" for t in avoid if t)
            prompt += f"\n\nSYKE ALREADY SAID (do not repeat or lightly rephrase):\n{already}"
        emojis = {m.group(1): m.group(0) for text in [*candidates, *vocab.emojis]
                  for m in CUSTOM_EMOJI_RE.finditer(text)}
        try:
            if settings.ai_provider == "openai":
                answer = await _call_openai(settings, COMPOSE_PROMPT, prompt, temperature=1.0)
            else:
                answer = await _call_anthropic(settings, COMPOSE_PROMPT, prompt, temperature=1.0)
            reply = tidy_reply(_parse_json(answer).get("reply"), candidates, banned, voice)
            if reply:
                return with_server_emojis(reply, emojis)
            log.warning("AI yap reply unusable: %r", answer[:200])
        except Exception:
            log.exception("AI yap reply failed; matching keywords instead")
    usable = [(score, text) for score, text in ranked if folded(text) not in banned_keys]
    return offline_pick(usable) or offline_pick(ranked)


_PING = re.compile(r"<@[!&]?\d+>|@(everyone|here)\b", re.IGNORECASE)
SHIP_TIMEOUT = 40
TLDR_TIMEOUT = 50
SHIP_BLURB_LIMIT = 700
TLDR_SUMMARY_LIMIT = 1800
_BULLET = re.compile(r"^\s*(?:[-•·]|\*(?!\*)|\d+[.)])\s*")


def public_text(text: object, limit: int) -> str:
    """Model copy safe to post: no pings, no blank, cut to the embed budget."""
    cleaned = _PING.sub("", str(text or ""))
    cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


async def _complete(settings: Settings, system: str, user: str, temperature: float, timeout: float) -> dict:
    if settings.ai_provider == "openai":
        reply = await _call_openai(settings, system, user, temperature, timeout)
    else:
        reply = await _call_anthropic(settings, system, user, temperature, timeout)
    return _parse_json(reply)


SHIP_PROMPT = """You are SYKE, writing a short compatibility blurb for two Discord members who just got shipped.
You have real messages from BOTH of them. Be specific and funny: quote or closely paraphrase one real line \
from each person. The joke has to fall apart if you swap in two other members.

Rules:
- 2-4 sentences, under 80 words. End on a deadpan verdict. Use their names exactly as given.
- Roast the pairing, never identity. No comments on race, religion, gender, sexuality, disability, body, \
or real mental-health diagnoses. No slurs. Nothing explicit; a wink is the ceiling.
- Do not invent messages they did not send. Do not @mention anyone.
- If the match percent is low, say why they would be a disaster. If it is high, say why that is concerning.

Reply with JSON only: {"blurb": str}"""


TLDR_PROMPT = """You are SYKE, summarising a Discord channel for someone who just opened the app.
You get FACTS counted from the whole window, then real messages, oldest first. Cover what actually \
happened: topics, arguments, bits, who was loud, any running joke. Name people. Paraphrase real lines. \
Trust FACTS for who talked most and what repeated. Do not invent events, arrivals, or drama that is not \
in the log.

Rules:
- "headline": the whole window in one line, under 14 words. The thing they would ask "so what happened?" for.
- "points": 2-5 bullets in the order things happened, each one thread of conversation, under 30 words. \
Lead each with who or what, then the detail. Merge small talk into one point or drop it.
- Under 160 words in total. **Bold** the key name or topic in each point; no headers, no nested lists.
- Funny and specific, like a friend catching someone up, not meeting minutes.
- Write names as plain text. Never @mention anyone. No slurs. Nothing explicit; fade to a wink.
- "vibe" is one sentence on the mood (toxic, freaky, unhinged, sincere, and so on).

Reply with JSON only: {"headline": str, "points": [str], "vibe": str}"""


def tldr_text(data: dict) -> str:
    """The model's headline and bullets as one readable block; a plain "summary" still works."""
    headline = public_text(data.get("headline"), 200).strip("*# ")
    points = data.get("points")
    bullets = []
    if isinstance(points, list):
        for point in points[:5]:
            text = _BULLET.sub("", public_text(point, 320)).strip()
            if text:
                bullets.append(f"• {text}")
    if not bullets:
        return public_text(data.get("summary"), TLDR_SUMMARY_LIMIT)
    parts = ([f"**{headline}**"] if headline else []) + ["\n".join(bullets)]
    return public_text("\n\n".join(parts), TLDR_SUMMARY_LIMIT)


def _numbered(lines: list[str]) -> str:
    if not lines:
        return "(nothing stored)"
    return "\n".join(f"[{i}] {readable(line)[:180]}" for i, line in enumerate(lines))


async def write_ship_blurb(settings: Settings, left_name: str, right_name: str, score: int, label: str,
                           left_lines: list[str], right_lines: list[str]) -> str | None:
    """A funny ship write-up from both chats, or None when there is no AI or it fails."""
    if settings.ai_provider == "none" or not (left_lines or right_lines):
        return None
    user = (
        f"PAIR: {left_name} × {right_name}\nMATCH: {score}% — {label}\n\n"
        f"{left_name} MESSAGES:\n{_numbered(left_lines)}\n\n"
        f"{right_name} MESSAGES:\n{_numbered(right_lines)}"
    )
    try:
        data = await _complete(settings, SHIP_PROMPT, user, 1.0, SHIP_TIMEOUT)
        blurb = public_text(data.get("blurb"), SHIP_BLURB_LIMIT)
        return blurb if len(blurb) >= 20 else None
    except Exception:
        log.exception("AI ship blurb failed; using the offline one")
        return None


async def write_tldr(settings: Settings, channel: str, lines: list[tuple[str, str]], total: int,
                     facts: str = "") -> tuple[str, str] | None:
    """(summary, vibe) for a channel window, or None when there is no AI or it fails.

    `lines` is the sample actually shown to the model; `total` is how many messages that window covers;
    `facts` is what was counted over the whole window.
    """
    if settings.ai_provider == "none" or not lines:
        return None
    numbered = "\n".join(
        f"[{i}] {author[:24]}: {readable(' '.join(text.split()))[:160]}"
        for i, (author, text) in enumerate(lines)
    )
    covered = (
        f"These {len(lines)} lines are spread across {total} messages, oldest first."
        if len(lines) < total else f"These are all {total} messages, oldest first."
    )
    grounding = f"FACTS:\n{facts}\n\n" if facts else ""
    user = f"CHANNEL: #{channel}\n{covered}\n\n{grounding}MESSAGES:\n{numbered}"
    try:
        data = await _complete(settings, TLDR_PROMPT, user, 0.8, TLDR_TIMEOUT)
        summary = tldr_text(data)
        vibe = public_text(data.get("vibe"), 300)
        if len(summary) < 20:
            return None
        return summary, vibe or "The chat happened. That is the review."
    except Exception:
        log.exception("AI tldr failed; using the offline recap")
        return None


WELCOME_PROMPT = """You write the one-line roast a mean friend drops in a Discord group chat when someone \
new gets let in. You only know their display name, their username and, if attached, their profile picture.

How it should sound:
- Like a real person typing fast in a group chat, not a bot or a copywriter. Mostly lowercase, casual, \
blunt. Slang is fine (bro, ngl, ts, nah, lowkey, fr). One or two short sentences, under 30 words.
- Brutal. Go for the throat on their choices: the username, the numbers in it, the try-hard spelling, \
the anime pfp, the car pfp, the blurry mirror selfie, the default avatar. No compliment at the end, \
no "but we love you", no softening, no "welcome".
- Specific. If the line would work for anyone else, it's wrong. Pick ONE thing and hit it.

Never sound like AI. Banned: "ah,", "ah yes", "behold", "let's be honest", "bold choice", "brave", \
"truly", "absolutely", "certified", "iconic", "legend", "vibes", "energy", "journey", "embark", \
"buckle up", "folks", "it seems", "clearly", "a true", em dashes, semicolons, colons, hashtags, \
quotation marks around the whole line, more than one emoji, and exclamation marks.

Examples of the tone (do not reuse them):
- xX_shadow_Xx in 2026 is crazy, the 2009 lobby wants its username back
- bro has 4 numbers in his name, every version of him before this one got banned
- default pfp and a name like that, you're a burner account and we all know it
- the anime pfp already told us everything we need to know about your shower schedule
- that mirror selfie was taken with the flash on and the dignity off

Hard limits: roast choices, never identity. Nothing about race, religion, gender, sexuality, disability, \
body, weight, face, age or real looks. If the picture shows a real person, roast the photo (angle, \
lighting, the mirror, the pose), never them. No slurs, nothing sexual, no @mentions. Write their name \
exactly as given if you use it.

Reply with JSON only: {"roast": str}"""
WELCOME_TIMEOUT = 20
WELCOME_LIMIT = 300


async def _image_call(settings: Settings, system: str, text: str, image_url: str | None, timeout: float) -> str:
    """Like `_complete`, with an optional picture for models that can see."""
    if settings.ai_provider == "openai":
        content: list[dict] = [{"type": "text", "text": text}]
        if image_url:
            content.append({"type": "image_url", "image_url": {"url": image_url, "detail": "low"}})
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_model, "temperature": 1.0,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    content = [{"type": "text", "text": text}]
    if image_url:
        content.insert(0, {"type": "image", "source": {"type": "url", "url": image_url}})
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": settings.anthropic_model, "max_tokens": 300, "temperature": 1.0,
                "system": system, "messages": [{"role": "user", "content": content}],
            },
        )
        resp.raise_for_status()
        return "".join(b.get("text", "") for b in resp.json()["content"])


def natural(text: str) -> str:
    """Strip the tells that make a line read as machine-written."""
    text = text.strip().strip('"\u201c\u201d').strip()
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)   # em/en dashes
    text = text.replace(";", ",").replace("!", "")
    text = re.sub(r"^(ah|oh|well|behold|wow)[,.]?\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,")
    if text.endswith("."):
        text = text[:-1]
    return text


async def write_welcome_roast(settings: Settings, display_name: str, username: str, server: str,
                              avatar_url: str | None) -> str | None:
    """A gentle roast for a newly verified member, or None when there is no AI or it fails."""
    if settings.ai_provider == "none":
        return None
    text = (f"SERVER: {server}\nDISPLAY NAME: {display_name}\nUSERNAME: {username}\n"
            f"PROFILE PICTURE: {'attached' if avatar_url else 'none, they still have the default avatar'}")
    for picture in ([avatar_url, None] if avatar_url else [None]):
        try:
            data = _parse_json(await _image_call(settings, WELCOME_PROMPT, text, picture, WELCOME_TIMEOUT))
            roast = natural(public_text(data.get("roast"), WELCOME_LIMIT))
            if len(roast) >= 10:
                return roast
        except Exception:
            log.warning("AI welcome roast failed%s", " with the avatar; retrying without" if picture else "",
                        exc_info=picture is None)
    return None
