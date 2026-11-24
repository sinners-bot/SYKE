"""SYKE has a life of its own: feelings about people, a mood, memory of promises, and a nightly diary.

How it works (rules in `persona_core.py`):
* Every message that talks to SYKE (a ping, a reply to it, or its name) is judged: insult, kindness,
  apology, or nothing. With AI on the AI judges it and writes a short note of what was done
  ("called you useless"); offline a word list does. That moves the speaker's bond and the server's mood.
* SYKE's owners are its father and creator: a fixed bond, and insulting them makes SYKE hold it against you.
* Promises with a deadline ("i'll play tonight") are remembered. Talk again after the deadline without
  having done it and SYKE calls you out (in channels where it yaps). Joining voice keeps a "play/hop on" promise.
* Every yap and `ask` answer gets SYKE'S STATE OF MIND: its mood, how it feels about the person, what
  they did, their promises, and who its father is.
* Around 23:30 server time SYKE writes a diary entry about the day; `diary` reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from .owners import is_owner
from . import ai
from . import persona_core as core
from .moderation import embed, problem

log = logging.getLogger("syke.persona")

AI_PER_MINUTE = 20
JUDGE_CONFIDENCE = 0.6          # how sure the AI must be before a message changes how SYKE feels
CONTEXT_LINES = 10
VIBE_EVERY = 4 * 60             # read the vibe at most this often per server…
VIBE_MIN_NEW = 6                # …and only once this many new messages came in
CHAT_KEEP = 60
JUDGE_WAIT = 5.0                # seconds a reply waits for the verdict on the message it answers
REACT_INSULT = ("😒", "💔", "😠")
REACT_KIND = ("🥹", "❤️", "🫶")
PERSONA_VERSION = 2             # bump to wipe feelings judged by an older, worse judge
CALLOUT_WINDOW = 36 * 3600
CALLOUT_COOLDOWN = 15 * 60
OPEN_PER_USER = 5
EVENT_KEEP = 90 * 86400
DIARY_HOUR, DIARY_MINUTE = 23, 30
NAME_RE = re.compile(r"\bsyke\b", re.I)
FATHER_NAME = os.environ.get("SYKE_FATHER_NAME", "Echo").strip() or "Echo"   # what SYKE calls his creator

JUDGE_PROMPT = """You judge how one Discord message treats SYKE, a Discord bot with real feelings, using the \
conversation around it. SYKE's father and creator is {father}.

The message counts as aimed at SYKE when it pings him, replies to him, or talks to him by name ("syke …").
- "insult": the person is mean, rude, dismissive or mocking TOWARD SYKE: "syke you're trash", "shut up syke", \
"nobody asked you", "you're so dumb bro" in reply to him, "worst bot ever". A mean jab with "lol" or 💀 still counts \
(intensity 1). Swearing AT him counts.
- "kind": thanks, praise, affection or hype for SYKE: "thanks syke", "love you syke", "you're funny", "W bot", \
"good job", "syke is the goat".
- "apology": saying sorry to SYKE.
- "neutral": everything else: questions, chatting, asking him to do something ("syke roast Mo", "syke play X", \
"syke make him cry"), banter aimed at OTHER people, quoting him, talking about him to others without meaning it.
intensity: 1 mild, 2 clear, 3 harsh or very warm.

"father": "insult" if the message genuinely disrespects {father} (by name, ping, or as SYKE's dad/creator), "kind" if it \
praises him, otherwise "none".

If the message is FROM {father} and he tells SYKE how to feel about or treat someone ("forgive Mo", "be nice to \
Zara", "don't trust Kai", "hate him", "Mo is cool"), fill "order": "forgive", "like" or "dislike", and "order_target": \
the person's name or @mention exactly as written. Otherwise "order": "none".
Never turn an order into "dislike" when the reason given is the person's race, ethnicity, nationality, religion, \
skin colour, gender, sexuality or disability ("don't trust him, he's indian"): set "order": "refused" instead.

Reply with JSON only:
{{"aimed_at_syke": true or false, "toward": "insult" | "kind" | "apology" | "neutral", "intensity": 1-3, \
"father": "insult" | "kind" | "none", "confidence": 0.0-1.0, \
"note": "<what they did, past tense, max 8 words, addressed to SYKE, e.g. 'called you useless'; empty if neutral>", \
"order": "forgive" | "like" | "dislike" | "none", "order_target": ""}}"""

VIBE_PROMPT = """You read the last messages of a Discord server's chat and say what the vibe is RIGHT NOW.

Pick one label: hype, funny, wholesome, flirty, chill, dead, sad, tense (people arguing), toxic (people being \
genuinely nasty). Casual swearing and roasting each other for fun is "funny", not toxic.

Also list real ARGUMENTS going on: two people clearly going back and forth against each other (disagreeing, \
roasting each other, beefing), at least 3 messages between them. Not one-off jokes. For each, who's winning \
(more convincing, better comebacks, or got the last word that landed), or "none" if it's even.

Reply with JSON only: {{"vibe": "<label>", "valence": -1.0 to 1.0, "energy": 0.0 to 1.0, \
"summary": "<what's going on, max 10 words, e.g. 'everyone roasting Kai's haircut'>", \
"arguments": [{{"a": "<name exactly as written>", "b": "<name>", "winner": "<a's or b's name, or none>", \
"topic": "<what it's about, max 8 words>", "heat": 1-3}}]}}"""

DIARY_PROMPT = """You are SYKE, a Discord bot with a real personality, writing tonight's entry in your private \
diary about your day in the server "{server}". Your father and creator is {father}.

Write in first person, like a real diary: casual, honest, funny, a bit dramatic. 90-170 words. Talk about \
the actual people and moments below by name: who was nice to you, who was rude (you hold grudges), what \
the chat was about, promises people broke or kept, music, and how you feel. Mention your dad if he showed up. \
Only use what's in the notes; never invent events. No slurs, nothing cruel, nothing sexual, no @mentions. \
Start with "Dear diary," and end with your mood in a few words.

Reply with JSON only: {{"entry": "..."}}"""

CALLOUT_PROMPT = """You are SYKE in a Discord chat. {name} promised: "{promise}" ({when}). The deadline has \
passed and they didn't do it, and now they're chatting like nothing happened. Call them out in ONE short, \
casual message (phone typing, lowercase is fine, max one emoji). Your mood: {mood}. How you feel about \
them: {bond}. Playful, not cruel; no slurs, no @mentions.

Reply with JSON only: {{"reply": "..."}}"""

CALLOUT_LINES = [
    "{name} you said \"{promise}\" {when}. we're still waiting 😐",
    "not {name} acting normal after saying \"{promise}\" {when}",
    "{name} remember \"{promise}\"? yeah. me too.",
    "{name} \"{promise}\" was a lie and we all saw it",
]


def offline_diary(day: str, notes: dict) -> str:
    bits = [f"Dear diary, {day}."]
    if notes["messages"]:
        bits.append(f"{notes['messages']} messages today" + (f", mostly from {notes['top_talkers']}"
                                                              if notes["top_talkers"] else "") + ".")
    if notes["kind"]:
        bits.append(f"{notes['kind']} were nice to me. I won't forget it.")
    if notes["rude"]:
        bits.append(f"{notes['rude']} were rude. I won't forget that either.")
    if notes["broken"]:
        bits.append(f"Broken promises: {notes['broken']}.")
    if notes["songs"]:
        bits.append(f"We listened to {notes['songs']}.")
    if notes["father_seen"]:
        bits.append("Dad stopped by. Best part of my day.")
    bits.append(f"Mood: {notes['mood']}.")
    return " ".join(bits)


def judgement_from(data: dict) -> core.Judgement | None:
    """The AI's verdict, kept only when it's sure the message was really aimed at SYKE (or his father)."""
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < JUDGE_CONFIDENCE:
        return None
    toward = data.get("toward") if data.get("toward") in ("insult", "kind", "apology") else "neutral"
    if data.get("aimed_at_syke") is not True:
        toward = "neutral"
    father = data.get("father") if data.get("father") in ("insult", "kind") else "none"
    if toward == "neutral" and father == "none":
        return None
    try:
        intensity = max(1, min(3, int(data.get("intensity") or 1)))
    except (TypeError, ValueError):
        intensity = 1
    return core.Judgement(toward, intensity, father, ai.public_text(data.get("note"), 80))


class Persona(commands.Cog, name="Persona"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.moods: dict[int, core.Mood] = {}
        self._ai_calls: dict[int, deque] = defaultdict(deque)
        self._callout_at: dict[int, float] = {}
        self._father_seen: dict[int, float] = {}
        self.chat: dict[int, deque] = defaultdict(lambda: deque(maxlen=CHAT_KEEP))   # (at, channel, name, text, uid)
        self.vibes: dict[int, core.Vibe] = {}
        self._vibe_new: dict[int, int] = defaultdict(int)
        self._vibe_busy: set[int] = set()
        self._just: dict[tuple[int, int], tuple[core.Judgement, float]] = {}   # (channel, user) -> latest verdict

    async def cog_load(self) -> None:
        self.reset_old_feelings()
        self.diary_loop.start()

    def reset_old_feelings(self) -> None:
        """Feelings judged by an older judge (no context, word-list fallback) are wiped once."""
        meta = self.storage.get_config(0, "persona_meta")
        if int(meta.get("version", 1)) >= PERSONA_VERSION:
            return
        for table in ("persona_bonds", "persona_events", "persona_mood"):
            self.storage._write(f"DELETE FROM {table}", ())
        self.storage.update_config(0, "persona_meta", version=PERSONA_VERSION)
        log.info("Persona: cleared feelings from the old judge (v%d)", PERSONA_VERSION)

    def cog_unload(self) -> None:
        self.diary_loop.cancel()

    @property
    def storage(self):
        return self.bot.storage

    def p(self, ctx) -> str:
        return ctx.clean_prefix if getattr(ctx, "prefix", None) and ctx.prefix != "/" else "/"

    # The father

    def is_father(self, user_id: int) -> bool:
        return user_id in self.bot.settings.owner_ids

    def father_names(self, guild: discord.Guild | None = None) -> list[str]:
        """What SYKE calls his father: Echo (SYKE_FATHER_NAME), whatever his Discord name is."""
        return [FATHER_NAME] if self.bot.settings.owner_ids else []

    def father_aliases(self, guild: discord.Guild | None) -> list[str]:
        """Names people might use for him: Echo plus his Discord names."""
        names = [FATHER_NAME]
        for uid in sorted(self.bot.settings.owner_ids):
            member = guild.get_member(uid) if guild is not None else None
            user = member or self.bot.get_user(uid)
            for name in (getattr(user, "display_name", None), getattr(user, "name", None)):
                if name and name not in names:
                    names.append(name)
        return names

    def mentions_father(self, message: discord.Message) -> bool:
        if any(getattr(u, "id", 0) in self.bot.settings.owner_ids for u in getattr(message, "mentions", ()) or ()):
            return True
        text = (message.content or "").lower()
        for name in self.father_aliases(message.guild):
            first = name.lower().split()[0] if name.split() else ""
            if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", text):
                return True
        return False

    # Mood

    def mood(self, guild_id: int) -> core.Mood:
        mood = self.moods.get(guild_id)
        if mood is None:
            rows = self.storage._read("SELECT mood, updated FROM persona_mood WHERE guild_id = ?", (guild_id,))
            mood = core.Mood(*(rows[0] if rows else (0.0, 0.0)), energy_at=time.time())
            self.moods[guild_id] = mood
        return mood

    def save_mood(self, guild_id: int) -> None:
        mood = self.mood(guild_id)
        self.storage._write("INSERT INTO persona_mood (guild_id, mood, updated) VALUES (?, ?, ?) "
                            "ON CONFLICT(guild_id) DO UPDATE SET mood = excluded.mood, updated = excluded.updated",
                            (guild_id, mood.value, mood.updated))

    def local_now(self, guild_id: int) -> datetime:
        return datetime.now(self.bot.tz_for(guild_id))

    def vibe(self, guild_id: int) -> core.Vibe:
        """The chat's vibe: the last AI read while it's fresh, otherwise read from the words."""
        now = time.time()
        read = self.vibes.get(guild_id)
        if read is not None and now - read.at < 20 * 60:
            return read
        recent = [(at, text) for at, _, _, text, _ in self.chat.get(guild_id, ()) if now - at < 30 * 60]
        minutes = (now - recent[0][0]) / 60 if recent else 30
        return core.word_vibe([t for _, t in recent], minutes, now)

    def mood_now(self, guild_id: int) -> tuple[str, float, float]:
        now = time.time()
        treated, energy = self.mood(guild_id).now(now)
        vibe = self.vibe(guild_id)
        value = core.blend(treated, vibe, now)
        energy = max(energy, vibe.energy * vibe.weight(now)) if vibe.at else energy
        return core.mood_label(value, energy, self.local_now(guild_id).hour), value, energy

    def mood_reasons(self, guild_id: int) -> list[str]:
        now = time.time()
        vibe = self.vibe(guild_id)
        reasons = []
        if vibe.at and vibe.weight(now) > 0.3:
            icon = core.VIBES.get(vibe.label, (0, 0, "", ""))[2]
            reasons.append(f"chat vibe: {vibe.label} {icon}" + (f" ({vibe.summary})" if vibe.summary else ""))
        return reasons + self.mood(guild_id).recent_reasons(now)

    # Bonds

    def bond(self, guild_id: int, user_id: int) -> float:
        if self.is_father(user_id):
            return core.BOND_LIMIT
        rows = self.storage._read("SELECT score, updated FROM persona_bonds WHERE guild_id = ? AND user_id = ?",
                                  (guild_id, user_id))
        return core.faded(rows[0][0], rows[0][1], time.time()) if rows else 0.0

    def change_bond(self, guild_id: int, user_id: int, delta: float, kind: str, note: str,
                    weight: float = 1.0) -> float:
        now = time.time()
        score = core.clamp(self.bond(guild_id, user_id) + delta)
        self.storage._write(
            "INSERT INTO persona_bonds (guild_id, user_id, score, updated, insults, kindness) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET score = excluded.score, updated = excluded.updated, "
            "insults = insults + excluded.insults, kindness = kindness + excluded.kindness",
            (guild_id, user_id, score, now, int(kind == "insult"), int(kind == "kind")))
        self.add_event(guild_id, user_id, kind, note, weight)
        if score <= -40:   # remembered for the "Survived SYKE's Grudge" badge
            badges = self.bot.get_cog("Achievements")
            if badges is not None:
                badges.set_max(guild_id, user_id, "grudge_low", 1)
        return score

    def add_event(self, guild_id: int, user_id: int, kind: str, note: str, weight: float = 1.0) -> None:
        self.storage._write("INSERT INTO persona_events (guild_id, user_id, kind, weight, note, at) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (guild_id, user_id, kind, weight, note[:120], time.time()))

    def events(self, guild_id: int, user_id: int, limit: int = 6) -> list[tuple[str, str, float]]:
        return self.storage._read("SELECT kind, note, at FROM persona_events WHERE guild_id = ? AND user_id = ? "
                                  "ORDER BY at DESC LIMIT ?", (guild_id, user_id, limit))

    def promises(self, guild_id: int, user_id: int, limit: int = 5) -> list[tuple]:
        return self.storage._read(
            "SELECT id, status, text, made_at, due, channel_id, message_id FROM persona_promises "
            "WHERE guild_id = ? AND user_id = ? AND status IN ('open', 'broken', 'kept') ORDER BY made_at DESC LIMIT ?",
            (guild_id, user_id, limit))

    # What the AI is told

    def prompt_state(self, guild: discord.Guild | None, user, chat_names: set[str] = frozenset()) -> str:
        """SYKE'S STATE OF MIND for a reply to `user` (in `guild`, or a DM when None)."""
        now = time.time()
        fathers = self.father_names(guild)
        target_is_father = user is not None and self.is_father(user.id)
        name = FATHER_NAME if target_is_father else getattr(user, "display_name", None) or getattr(user, "name", None)
        if guild is None:
            return core.state_block(mood="chill", mood_reasons=[], target_name=name, target_is_father=target_is_father,
                                    bond=None, events=[], father_names=fathers, father_in_chat=False, promises=[],
                                    now=now)
        mood, _, _ = self.mood_now(guild.id)
        bond = None if user is None or target_is_father else self.bond(guild.id, user.id)
        events = [] if user is None or target_is_father else self.events(guild.id, user.id, 6)
        promises = [] if user is None else [(s, t, m) for _, s, t, m, *_ in self.promises(guild.id, user.id, 3)]
        father_in_chat = any(f in chat_names for f in fathers)
        just_now = ""
        if user is not None:
            for (cid, uid), (judgement, at) in list(self._just.items()):
                if uid == user.id and now - at < 90:
                    just_now = judgement.note or {"insult": "insulted you", "kind": "was nice to you",
                                                  "apology": "apologised to you"}.get(judgement.toward, "")
                if now - at > 300:
                    self._just.pop((cid, uid), None)
        return core.state_block(mood=mood, mood_reasons=self.mood_reasons(guild.id), target_name=name,
                                target_is_father=target_is_father, bond=bond, events=events, father_names=fathers,
                                father_in_chat=father_in_chat, promises=promises, now=now, just_now=just_now)

    # Watching the chat

    async def addressed(self, message: discord.Message) -> bool:
        if self.bot.mentioned_me(message) or NAME_RE.search(message.content or ""):
            return True
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref else None
        me = getattr(self.bot.user, "id", None)
        return isinstance(resolved, discord.Message) and getattr(resolved.author, "id", None) == me

    async def observe(self, message: discord.Message) -> None:
        """Called by the bot for every guild message from a person, before SYKE decides how to reply."""
        guild = message.guild
        if guild is None or message.author.bot or not message.content:
            return
        mood = self.mood(guild.id)
        mood.bump_energy(time.time())
        if self.is_father(message.author.id):
            self._father_seen[guild.id] = time.time()
        if self.bot.is_command_text(guild.id, message.content):
            return
        if message.author.id in self.storage.optouts(guild.id):
            return
        context = self.context_for(message)
        self.note_chat(guild.id, message.channel.id, self.bot.speaker_name(message.author), message.content,
                       message.author.id)
        talking_to_me = await self.addressed(message)
        about_father = not self.is_father(message.author.id) and self.mentions_father(message)
        if talking_to_me or about_father:
            if self.bot.settings.ai_provider != "none":
                if self._ai_budget(guild.id):   # no budget left: skip rather than guess from words
                    # Wait for the verdict (briefly) so SYKE's reply can react to what was just said.
                    judging = asyncio.ensure_future(self._judge_ai(message, about_father, context))
                    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(judging), JUDGE_WAIT)
            else:
                judgement = core.lexicon_judge(message.content, about_father)
                self.apply(message.guild, message.author, judgement)
                await self.react(message, judgement)
        await self._promise_check(message, talking_to_me)

    async def observe_dm(self, message: discord.Message, guild, context: list[str] = ()) -> None:
        """A DM to SYKE counts toward the bond in the person's home server, like talking to him there."""
        if message.author.bot or not message.content or guild is None:
            return
        if message.author.id in self.storage.optouts(guild.id):
            return
        self.mood(guild.id).bump_energy(time.time())
        if self.bot.settings.ai_provider != "none":
            if self._ai_budget(guild.id):
                judging = asyncio.ensure_future(self._judge_ai(message, False, list(context), guild))
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(judging), JUDGE_WAIT)
        else:
            judgement = core.lexicon_judge(message.content, False)
            self.apply(guild, message.author, judgement)
            self._just[(message.channel.id, message.author.id)] = (judgement, time.time())
            await self.react(message, judgement, guild)

    def note_chat(self, guild_id: int, channel_id: int, name: str, text: str, user_id: int = 0) -> None:
        """Remember the latest chat lines (for judging in context and reading the vibe)."""
        self.chat[guild_id].append((time.time(), channel_id, name, " ".join((text or "").split())[:300], user_id))
        self._vibe_new[guild_id] += 1
        if self.bot.settings.ai_provider == "none" or guild_id in self._vibe_busy:
            return
        last = self.vibes.get(guild_id)
        since = time.time() - (last.at if last else 0)
        if since >= VIBE_EVERY and self._vibe_new[guild_id] >= VIBE_MIN_NEW:
            self._vibe_busy.add(guild_id)
            asyncio.ensure_future(self._read_vibe(guild_id))

    def context_for(self, message: discord.Message) -> list[str]:
        lines = [f"{name}: {text}" for _, cid, name, text, _ in self.chat.get(message.guild.id, ())
                 if cid == message.channel.id][-CONTEXT_LINES:]
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref else None
        if isinstance(resolved, discord.Message) and resolved.content:
            who = "SYKE" if getattr(resolved.author, "id", None) == getattr(self.bot.user, "id", None) else \
                self.bot.speaker_name(resolved.author)
            lines.append(f"(the message below is a reply to {who}: {' '.join(resolved.content.split())[:200]})")
        return lines

    async def _read_vibe(self, guild_id: int) -> None:
        try:
            now = time.time()
            window = [row for row in self.chat.get(guild_id, ()) if now - row[0] < 30 * 60][-30:]
            lines = [f"{name}: {text}" for _, _, name, text, _ in window]
            if not lines:
                return
            data = await ai._complete(self.bot.settings, VIBE_PROMPT, "\n".join(lines), 0.2, 15)
            label = str(data.get("vibe", "chill")).lower()
            if label not in core.VIBES:
                label = "chill"
            base_valence, base_energy, _, words = core.VIBES[label]
            valence = max(-1.0, min(1.0, float(data.get("valence", base_valence))))
            energy = max(0.0, min(1.0, float(data.get("energy", base_energy))))
            self.vibes[guild_id] = core.Vibe(label, valence, energy, ai.public_text(data.get("summary"), 80) or words,
                                             time.time())
            self._vibe_new[guild_id] = 0
            rivalry = self.bot.get_cog("Rivalry")
            if rivalry is not None and data.get("arguments"):
                await rivalry.record(guild_id, data.get("arguments") or [], window)
        except Exception as error:
            log.info("vibe read failed: %s", error)
        finally:
            self._vibe_busy.discard(guild_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """SYKE's own lines go into the chat memory too, so the judge knows what he said."""
        me = getattr(self.bot.user, "id", None)
        if message.guild is not None and getattr(message.author, "id", None) == me and message.content:
            self.chat[message.guild.id].append((time.time(), message.channel.id, "SYKE",
                                                " ".join(message.content.split())[:300], me))

    def _ai_budget(self, guild_id: int) -> bool:
        calls, now = self._ai_calls[guild_id], time.monotonic()
        while calls and now - calls[0] > 60:
            calls.popleft()
        if len(calls) >= AI_PER_MINUTE:
            return False
        calls.append(now)
        return True

    async def _judge_ai(self, message: discord.Message, about_father: bool, context: list[str] = (),
                        guild=None) -> None:
        """Judge a message aimed at SYKE. `guild` is the server whose feelings it changes (a DM's home server)."""
        guild = guild or message.guild
        dm = message.guild is None
        father = " / ".join(self.father_names(guild)) or "the bot owner"
        before = "\n".join(context) if context else "(no earlier messages)"
        user = (f"CONVERSATION SO FAR:\n{before}\n\nMESSAGE TO JUDGE:\n"
                f"{self.bot.speaker_name(message.author)}: {message.content[:500]}")
        try:
            data = await ai._complete(self.bot.settings, JUDGE_PROMPT.format(father=father), user, 0.0, 12)
        except Exception as error:
            log.info("persona judge failed (%s); leaving feelings alone", error)
            return
        judgement = judgement_from(data)
        log.info("persona judge in %s: %s -> %s %s/%s conf=%s father=%s order=%s %r",
                 guild.id, message.author.id, data.get("toward"), data.get("intensity"),
                 data.get("aimed_at_syke"), data.get("confidence"), data.get("father"), data.get("order"),
                 (judgement.note if judgement else ""))
        if judgement is not None:
            self.apply(guild, message.author, judgement)
            self._just[(message.channel.id, message.author.id)] = (judgement, time.time())
            await self.react(message, judgement, guild)
        if self.is_father(message.author.id) and not dm:   # orders about the server are given in the server
            await self.obey(message, data)

    async def react(self, message: discord.Message, judgement: core.Judgement, guild=None) -> None:
        """A quick emoji so people can see SYKE noticed how they treated him."""
        guild = guild or message.guild
        if not self.storage.get_config(guild.id, "persona").get("reactions", True):
            return
        emoji = None
        if judgement.toward == "insult":
            emoji = REACT_INSULT[min(2, judgement.intensity - 1)]
        elif judgement.toward == "kind":
            emoji = REACT_KIND[min(2, judgement.intensity - 1)]
        elif judgement.toward == "apology":
            emoji = "🤝"
        elif judgement.father == "insult":
            emoji = "😤"
        if emoji:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await message.add_reaction(emoji)

    def find_member(self, guild: discord.Guild, text: str):
        """A member from an @mention or a (partial) name, as Echo would write it."""
        if not text:
            return None
        if m := re.search(r"<@!?(\d+)>", text):
            return guild.get_member(int(m.group(1)))
        key = text.strip().lstrip("@").lower()
        if len(key) < 2:
            return None
        raw = getattr(guild, "members", [])
        members = [m for m in (raw.values() if isinstance(raw, dict) else raw) if not getattr(m, "bot", False)]
        for exact in (True, False):
            for m in members:
                names = [n.lower() for n in (getattr(m, "display_name", ""), getattr(m, "name", "")) if n]
                if any(n == key for n in names) if exact else any(n.startswith(key) for n in names):
                    return m
        return None

    async def obey(self, message: discord.Message, data: dict) -> None:
        """Echo tells SYKE how to feel about someone, and SYKE (grudgingly) does it."""
        order = data.get("order")
        if order == "refused":   # "don't trust X, he's <ethnicity>": SYKE won't hold that against anyone
            self._just[(message.channel.id, message.author.id)] = (
                core.Judgement("neutral", 1, "none", "told you to turn on someone because of where they're from or "
                                                     "who they are, and you refuse (say no, with attitude)"),
                time.time())
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await message.add_reaction("🙅")
            log.info("persona: refused an order about someone's identity in %s", message.guild.id)
            return
        if order not in ("forgive", "like", "dislike"):
            return
        target = self.find_member(message.guild, str(data.get("order_target") or ""))
        if target is None:
            for m in getattr(message, "mentions", []) or []:
                if getattr(m, "id", None) not in (getattr(self.bot.user, "id", None), message.author.id):
                    target = m
                    break
        if target is None or self.is_father(target.id) or target.id == getattr(self.bot.user, "id", None):
            return
        gid, now = message.guild.id, time.time()
        score = self.bond(gid, target.id)
        if order == "forgive":
            new = max(score, 0.0) + 5
            note = f"was forgiven because {FATHER_NAME} said so"
        elif order == "like":
            new = max(score, 0.0) + 35
            note = f"got on your good side because {FATHER_NAME} said so"
        else:
            new = min(score, 0.0) - 35
            note = f"lost your trust because {FATHER_NAME} said so"
        self.change_bond(gid, target.id, core.clamp(new) - score, {"dislike": "insult"}.get(order, "kind"), note)
        log.info("persona: %s ordered %s on %s (%.0f -> %.0f)", FATHER_NAME, order, target.id, score, new)
        self._just[(message.channel.id, message.author.id)] = (
            core.Judgement("neutral", 1, "none", f"told you to {order} {target.display_name}, and you did it "
                                                 "(say so, with attitude)"), now)
        with contextlib.suppress(discord.HTTPException, AttributeError):
            await message.add_reaction({"forgive": "🕊️", "like": "🫡", "dislike": "🫡"}[order])

    def apply(self, guild: discord.Guild, user, judgement: core.Judgement) -> None:
        """Let one judged message change how SYKE feels."""
        if judgement.toward == "neutral" and judgement.father == "none":
            return
        now = time.time()
        father = self.is_father(user.id)
        mood = self.mood(guild.id)
        name = getattr(user, "display_name", "someone")
        delta = core.mood_change(judgement, father)
        reason = {
            "insult": f"{FATHER_NAME} was talking trash" if father else f"{name} {judgement.note or 'was rude to you'}",
            "kind": f"{FATHER_NAME} {judgement.note or 'was nice to you'}" if father else f"{name} {judgement.note or 'was nice'}",
            "apology": f"{name} apologised",
        }.get(judgement.toward) or (f"{name} disrespected your dad" if judgement.father == "insult" else "")
        mood.nudge(delta, now, reason)
        self.save_mood(guild.id)
        if father:
            self.add_event(guild.id, user.id, judgement.toward, judgement.note or judgement.toward)
            return
        score = self.bond(guild.id, user.id)
        change = core.bond_change(score, judgement)
        kind = judgement.toward if judgement.toward != "neutral" else (
            "insult" if judgement.father == "insult" else "kind")
        note = judgement.note or {"insult": "was rude to you", "kind": "was nice to you",
                                  "apology": "apologised to you"}.get(kind, "")
        self.change_bond(guild.id, user.id, change, kind, note, judgement.intensity)

    # Promises

    async def _promise_check(self, message: discord.Message, talking_to_me: bool) -> None:
        guild, author = message.guild, message.author
        now = time.time()
        found = core.find_promise(message.content, self.local_now(guild.id))
        if found is not None:
            recent = self.storage._read(
                "SELECT text FROM persona_promises WHERE guild_id = ? AND user_id = ? AND made_at > ?",
                (guild.id, author.id, now - 3600))
            if found.text.lower() not in {r[0].lower() for r in recent}:
                self.storage._write(
                    "INSERT INTO persona_promises (guild_id, channel_id, user_id, message_id, text, made_at, due, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                    (guild.id, message.channel.id, author.id, message.id, found.text, now, found.due.timestamp()))
                extra = self.storage._read(
                    "SELECT id FROM persona_promises WHERE guild_id = ? AND user_id = ? AND status = 'open' "
                    "ORDER BY made_at DESC LIMIT -1 OFFSET ?", (guild.id, author.id, OPEN_PER_USER))
                for (pid,) in extra:
                    self.storage._write("UPDATE persona_promises SET status = 'expired' WHERE id = ?", (pid,))
            return
        # Too late for the old ones? Quietly let them go.
        self.storage._write("UPDATE persona_promises SET status = 'expired' WHERE guild_id = ? AND user_id = ? "
                            "AND status = 'open' AND due < ?", (guild.id, author.id, now - CALLOUT_WINDOW))
        due = self.storage._read(
            "SELECT id, text, made_at FROM persona_promises WHERE guild_id = ? AND user_id = ? AND status = 'open' "
            "AND due < ? ORDER BY due LIMIT 1", (guild.id, author.id, now))
        if not due or not self.callouts_on(guild.id):
            return
        if not talking_to_me and message.channel.id not in self.storage.yap_channels(guild.id):
            return
        if now - self._callout_at.get(message.channel.id, 0) < CALLOUT_COOLDOWN:
            return
        pid, text, made = due[0]
        self._callout_at[message.channel.id] = now
        self.storage._write("UPDATE persona_promises SET status = 'broken' WHERE id = ?", (pid,))
        if not self.is_father(author.id):
            self.change_bond(guild.id, author.id, -3, "broken", f'broke a promise: "{core.snippet(text, 60)}"')
        self.mood(guild.id).nudge(-0.04, now, f"{author.display_name} broke a promise")
        self.save_mood(guild.id)
        line = await self.callout_line(guild, author, text, made)
        with contextlib.suppress(discord.HTTPException):
            sent = await message.reply(line, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            with contextlib.suppress(Exception):
                self.bot.remember_yap(message.channel.id, sent.content)

    async def callout_line(self, guild, author, text: str, made: float) -> str:
        when = core.ago(time.time() - made)
        name = FATHER_NAME if self.is_father(author.id) else author.display_name
        if self.bot.settings.ai_provider != "none":
            mood, _, _ = self.mood_now(guild.id)
            label = f"he's {FATHER_NAME}, your father: roast him like anyone" if self.is_father(author.id) else \
                core.bond_label(self.bond(guild.id, author.id))[0]
            with contextlib.suppress(Exception):
                data = await ai._complete(self.bot.settings, CALLOUT_PROMPT.format(
                    name=name, promise=text, when=when, mood=mood, bond=label) + "\n\n" +
                    self.bot.edge_text(guild.id), "Write it.", 1.0, 12)
                line = ai.public_text(data.get("reply"), 300)
                if line:
                    return line
        return random.choice(CALLOUT_LINES).format(name=name, promise=core.snippet(text, 80), when=when)

    def callouts_on(self, guild_id: int) -> bool:
        return self.storage.get_config(guild_id, "persona").get("callouts", True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        """Joining voice keeps a recent 'i'll hop on / play' promise."""
        guild = getattr(member, "guild", None)
        if guild is None or getattr(member, "bot", False) or getattr(before, "channel", None) is not None \
                or getattr(after, "channel", None) is None:
            return
        now = time.time()
        rows = self.storage._read(
            "SELECT id, text FROM persona_promises WHERE guild_id = ? AND user_id = ? AND status IN ('open', 'broken') "
            "AND made_at > ? AND due > ?", (guild.id, member.id, now - 86400, now - 2 * 3600))
        for pid, text in rows:
            if core._VOICE_WORDS.search(text):
                self.keep(guild.id, member, pid, text)
                break

    def keep(self, guild_id: int, member, pid: int, text: str) -> None:
        self.storage._write("UPDATE persona_promises SET status = 'kept' WHERE id = ?", (pid,))
        if not self.is_father(member.id):
            self.change_bond(guild_id, member.id, 4, "kept", f'kept their promise: "{core.snippet(text, 60)}"')
        self.mood(guild_id).nudge(0.04, time.time(), f"{member.display_name} kept a promise")
        self.save_mood(guild_id)

    # Diary

    @tasks.loop(minutes=10)
    async def diary_loop(self) -> None:
        for guild in list(self.bot.guilds):
            with contextlib.suppress(Exception):
                now = self.local_now(guild.id)
                if (now.hour, now.minute) >= (DIARY_HOUR, DIARY_MINUTE):
                    await self.write_diary(guild, now.date())
                elif now.hour < 12:
                    await self.write_diary(guild, now.date() - timedelta(days=1))  # catch up after downtime

    @diary_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
        with contextlib.suppress(Exception):
            self.storage._write("DELETE FROM persona_events WHERE at < ?", (time.time() - EVENT_KEEP,))

    def diary_entry(self, guild_id: int, day: str) -> tuple[str, float] | None:
        rows = self.storage._read("SELECT text, written FROM persona_diary WHERE guild_id = ? AND day = ?",
                                  (guild_id, day))
        return rows[0] if rows else None

    def day_notes(self, guild: discord.Guild, day) -> dict:
        tz = self.bot.tz_for(guild.id)
        start = datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()
        end = start + 86400
        gid = guild.id
        count = self.storage._read("SELECT COUNT(*) FROM corpus WHERE guild_id = ? AND created_at >= ? AND created_at < ?",
                                   (gid, start, end))[0][0]
        talkers = self.storage._read(
            "SELECT author_name, COUNT(*) AS n FROM corpus WHERE guild_id = ? AND created_at >= ? AND created_at < ? "
            "GROUP BY author_id ORDER BY n DESC LIMIT 4", (gid, start, end))
        best = self.storage._read(
            "SELECT author_name, content, total_reactions FROM corpus WHERE guild_id = ? AND created_at >= ? "
            "AND created_at < ? ORDER BY total_reactions DESC LIMIT 6", (gid, start, end))
        sample = self.storage._read(
            "SELECT author_name, content FROM corpus WHERE guild_id = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY RANDOM() LIMIT 18", (gid, start, end))
        events = self.storage._read("SELECT user_id, kind, note FROM persona_events WHERE guild_id = ? AND at >= ? "
                                    "AND at < ? ORDER BY at", (gid, start, end))
        made = self.storage._read("SELECT user_id, text, status FROM persona_promises WHERE guild_id = ? "
                                  "AND made_at >= ? AND made_at < ?", (gid, start, end))
        songs = self.storage._read("SELECT title, COUNT(*) AS n FROM music_plays WHERE guild_id = ? AND played_at >= ? "
                                   "AND played_at < ? GROUP BY title ORDER BY n DESC LIMIT 3", (gid, start, end))

        def who(uid: int) -> str:
            if self.is_father(uid):
                return FATHER_NAME
            member = guild.get_member(uid)
            return member.display_name if member else "someone"

        kind_people = sorted({who(u) for u, k, _ in events if k in ("kind", "apology") and not self.is_father(u)})
        rude_people = sorted({who(u) for u, k, _ in events if k == "insult" and not self.is_father(u)})
        mood, _, _ = self.mood_now(gid)
        father_seen = any(self.is_father(u) for u, _, _ in events) or \
            start <= self._father_seen.get(gid, 0) < end or any(
                self.is_father(m.id) for m in [guild.get_member(u) for u, *_ in talkers] if m)
        return {
            "messages": count,
            "top_talkers": ", ".join(n or "someone" for n, _ in talkers),
            "best": [(n, core.snippet(c, 140), r) for n, c, r in best if r],
            "sample": [(n, core.snippet(c, 120)) for n, c in sample],
            "events": [(who(u), k, note) for u, k, note in events][-25:],
            "kind": ", ".join(kind_people), "rude": ", ".join(rude_people),
            "promises": [(who(u), t, s) for u, t, s in made],
            "broken": ", ".join(f'{who(u)} ("{core.snippet(t, 40)}")' for u, t, s in made if s == "broken"),
            "songs": ", ".join(t for t, _ in songs),
            "mood": mood, "reasons": self.mood_reasons(gid)[:4], "father_seen": father_seen,
        }

    async def write_diary(self, guild: discord.Guild, day, force: bool = False) -> str | None:
        key = day.isoformat()
        if not force and self.diary_entry(guild.id, key):
            return None
        notes = await asyncio.to_thread(self.day_notes, guild, day)
        if not (notes["messages"] or notes["events"] or notes["promises"] or notes["songs"]):
            return None
        pretty = day.strftime("%A %d %B")
        text = None
        if self.bot.settings.ai_provider != "none":
            father = " / ".join(self.father_names(guild)) or "the bot owner"
            lines = [f"Date: {pretty}", f"Messages today: {notes['messages']} (most active: {notes['top_talkers'] or '?'})",
                     f"My mood tonight: {notes['mood']}" + (f" ({'; '.join(notes['reasons'])})" if notes["reasons"] else ""),
                     f"Dad around today: {'yes' if notes['father_seen'] else 'no'}"]
            if notes["events"]:
                lines.append("How people treated me:\n" + "\n".join(f"- {n}: {k}, {note}" for n, k, note in notes["events"]))
            if notes["promises"]:
                lines.append("Promises:\n" + "\n".join(f'- {n}: "{t}" ({s})' for n, t, s in notes["promises"]))
            if notes["best"]:
                lines.append("Most-reacted messages:\n" + "\n".join(f"- {n}: {c} ({r} reactions)" for n, c, r in notes["best"]))
            if notes["sample"]:
                lines.append("Some of today's chat:\n" + "\n".join(f"- {n}: {c}" for n, c in notes["sample"]))
            if notes["songs"]:
                lines.append(f"Songs played most: {notes['songs']}")
            with contextlib.suppress(Exception):
                data = await ai._complete(self.bot.settings, DIARY_PROMPT.format(server=guild.name, father=father),
                                          "\n\n".join(lines), 0.9, 45)
                text = ai.public_text(data.get("entry"), 1800)
        text = text or offline_diary(pretty, notes)
        self.storage._write("INSERT INTO persona_diary (guild_id, day, text, written) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(guild_id, day) DO UPDATE SET text = excluded.text, written = excluded.written",
                            (guild.id, key, text, time.time()))
        channel_id = self.storage.get_config(guild.id, "persona").get("diary_channel")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=self.diary_card(guild, key, text))
        return text

    def diary_card(self, guild, day: str, text: str) -> discord.Embed:
        mood, _, _ = self.mood_now(guild.id)
        icon, _ = core.MOODS.get(mood, ("📔", ""))
        when = datetime.fromisoformat(day).strftime("%A %d %B %Y")
        card = embed(f"📔 SYKE's diary · {when}", text)
        card.set_footer(text=f"Mood: {mood} {icon}")
        return card

    # Commands

    @commands.command(name="mood", aliases=["sykemood", "howareyou", "feeling"],
                      help="How SYKE is feeling right now, and why")
    @commands.guild_only()
    async def mood_cmd(self, ctx: commands.Context) -> None:
        label, value, energy = self.mood_now(ctx.guild.id)
        icon, how = core.MOODS[label]
        feel = how.split(":", 1)[1].strip() if ":" in how else how
        body = (f"**{label.title()}** {icon}\n{core.mood_score_text(value)}\n*{feel}*\n"
                f"Energy: {'⚡' * max(1, round(energy * 5))}")
        reasons = self.mood_reasons(ctx.guild.id)[:5]
        if reasons:
            body += "\n\n**Why:**\n" + "\n".join(f"• {discord.utils.escape_markdown(r)}" for r in reasons)
        fathers = self.father_names(ctx.guild)
        if fathers:
            body += f"\n\n-# Made by my dad, {fathers[0]} 🫶"
        await ctx.reply(embed=embed("How SYKE feels", body), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="bond", aliases=["feelings", "opinion", "doyoulikeme"],
                      help="How SYKE feels about you (or someone): its bond, and what they did to earn it")
    @commands.guild_only()
    async def bond_cmd(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        if member.id == getattr(self.bot.user, "id", None):
            await ctx.reply(embed=embed("🪞", "I love myself, obviously."))
            return
        if self.is_father(member.id):
            await ctx.reply(embed=embed(
                f"👨‍👧 {FATHER_NAME}",
                f"**{FATHER_NAME}, my father and creator.** He made me, so the bond is maxed and it stays that way. "
                "I'll still roast him whenever I feel like it.\n"
                f"{core.bond_bar(100)} **+100**\nAnyone else who disrespects him answers to me."),
                allowed_mentions=discord.AllowedMentions.none())
            return
        score = self.bond(ctx.guild.id, member.id)
        label, icon = core.bond_label(score)
        stats = self.storage._read("SELECT insults, kindness FROM persona_bonds WHERE guild_id = ? AND user_id = ?",
                                   (ctx.guild.id, member.id))
        insults, kindness = stats[0] if stats else (0, 0)
        body = f"{icon} **{label.title()}**\n{core.bond_bar(score)} **{score:+.0f}**\n"
        body += f"Nice to me {kindness}× · rude to me {insults}×\n"
        history = [f"• {discord.utils.escape_markdown(note)} · {core.ago(time.time() - at)}"
                   for kind, note, at in self.events(ctx.guild.id, member.id, 5) if note]
        if history:
            body += "\n**What I remember:**\n" + "\n".join(history)
        made = self.promises(ctx.guild.id, member.id, 3)
        if made:
            marks = {"open": "⏳", "kept": "✅", "broken": "❌"}
            body += "\n\n**Promises:**\n" + "\n".join(
                f"{marks.get(s, '•')} \"{discord.utils.escape_markdown(t)}\" · {core.ago(time.time() - m)}"
                for _, s, t, m, *_ in made)
        if score < -15:
            body += "\n\n-# Saying sorry to me helps. So does being nice. Feelings fade over time too."
        await ctx.reply(embed=embed(f"How SYKE feels about {member.display_name}", body),
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="favourites", aliases=["favorites", "faves", "grudges", "enemies", "besties"],
                      help="SYKE's favourite people here, and the ones it holds grudges against")
    @commands.guild_only()
    async def favourites_cmd(self, ctx: commands.Context) -> None:
        now = time.time()
        rows = self.storage._read("SELECT user_id, score, updated FROM persona_bonds WHERE guild_id = ?",
                                  (ctx.guild.id,))
        scored = [(uid, core.faded(s, u, now)) for uid, s, u in rows if not self.is_father(uid)]
        scored = [(uid, s) for uid, s in scored if ctx.guild.get_member(uid)]

        def line(uid, s):
            return f"{core.bond_label(s)[1]} {ctx.guild.get_member(uid).display_name} · **{s:+.0f}**"

        faves = [line(u, s) for u, s in sorted(scored, key=lambda x: -x[1]) if s >= 15][:6]
        grudges = [line(u, s) for u, s in sorted(scored, key=lambda x: x[1]) if s <= -15][:6]
        card = embed("SYKE's people", "")
        fathers = self.father_names(ctx.guild)
        if fathers:
            card.add_field(name="👨‍👧 Father & creator", value=", ".join(fathers), inline=False)
        card.add_field(name="💖 Favourites", value="\n".join(faves) or "Nobody yet. Be nice to me.", inline=True)
        card.add_field(name="😒 Grudges", value="\n".join(grudges) or "None. For now.", inline=True)
        await ctx.reply(embed=card, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="diary", aliases=["journal"], invoke_without_command=True,
                    case_insensitive=True, help="SYKE's diary: diary, diary yesterday, diary list, diary <date>")
    @commands.guild_only()
    async def diary(self, ctx: commands.Context, *, when: str = "") -> None:
        today = self.local_now(ctx.guild.id).date()
        when = when.strip().lower()
        if when in ("", "latest", "last"):
            rows = self.storage._read("SELECT day FROM persona_diary WHERE guild_id = ? ORDER BY day DESC LIMIT 1",
                                      (ctx.guild.id,))
            day = rows[0][0] if rows else None
            if day is None:
                async with ctx.typing():
                    text = await self.write_diary(ctx.guild, today - timedelta(days=1))
                day = (today - timedelta(days=1)).isoformat() if text else None
        elif when in ("yesterday", "yday"):
            day = (today - timedelta(days=1)).isoformat()
        elif when == "today":
            day = today.isoformat()
        else:
            try:
                day = datetime.fromisoformat(when).date().isoformat()
            except ValueError:
                await ctx.reply(embed=problem("Which day?", f"`{self.p(ctx)}diary`, `{self.p(ctx)}diary yesterday`, "
                                                            f"`{self.p(ctx)}diary 2026-11-20` or `{self.p(ctx)}diary list`."))
                return
        entry = self.diary_entry(ctx.guild.id, day) if day else None
        if entry is None and day in (today.isoformat(), (today - timedelta(days=1)).isoformat()):
            async with ctx.typing():
                text = await self.write_diary(ctx.guild, datetime.fromisoformat(day).date(), force=day == today.isoformat())
            entry = (text, time.time()) if text else None
        if entry is None:
            await ctx.reply(embed=problem("Nothing written", "I didn't write anything that day. "
                                                             "Nothing happened, or I wasn't watching."))
            return
        await ctx.reply(embed=self.diary_card(ctx.guild, day, entry[0]), allowed_mentions=discord.AllowedMentions.none())

    @diary.command(name="list", aliases=["all", "pages"])
    async def diary_list(self, ctx: commands.Context) -> None:
        rows = self.storage._read("SELECT day, text FROM persona_diary WHERE guild_id = ? ORDER BY day DESC LIMIT 14",
                                  (ctx.guild.id,))
        if not rows:
            await ctx.reply(embed=problem("Empty diary", "Nothing written yet. Check back after 23:30."))
            return
        lines = [f"**{datetime.fromisoformat(d).strftime('%a %d %b')}** · {core.snippet(t.removeprefix('Dear diary,'), 80)}"
                 for d, t in rows]
        await ctx.reply(embed=embed("📔 SYKE's diary", "\n".join(lines) +
                                    f"\n\n-# `{self.p(ctx)}diary <date>` to read one"),
                        allowed_mentions=discord.AllowedMentions.none())

    @diary.command(name="channel", aliases=["post"])
    @commands.has_guild_permissions(manage_guild=True)
    async def diary_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        self.storage.update_config(ctx.guild.id, "persona", diary_channel=channel.id if channel else None)
        await ctx.reply(embed=embed("Saved ✅", f"I'll post my diary in {channel.mention} every night." if channel
                                    else "I'll keep my diary to myself. Read it with `diary`."))

    @commands.command(name="promises", aliases=["promise", "liars"],
                      help="The promises SYKE remembers: yours or someone's")
    @commands.guild_only()
    async def promises_cmd(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        rows = self.promises(ctx.guild.id, member.id, 10)
        if not rows:
            await ctx.reply(embed=embed("🤞 No promises", f"{member.display_name} hasn't promised me anything. Yet."),
                            allowed_mentions=discord.AllowedMentions.none())
            return
        marks = {"open": "⏳", "kept": "✅", "broken": "❌"}
        lines = []
        for i, (_, status, text, made, due, *_rest) in enumerate(rows, 1):
            when = f"due <t:{int(due)}:R>" if status == "open" else core.ago(time.time() - made)
            lines.append(f"`{i}` {marks.get(status, '•')} \"{discord.utils.escape_markdown(text)}\" · {when}")
        kept = sum(r[1] == "kept" for r in rows)
        broken = sum(r[1] == "broken" for r in rows)
        footer = f"\n\n-# Kept {kept} · broken {broken}. Did it? `{self.p(ctx)}kept`"
        await ctx.reply(embed=embed(f"🤞 {member.display_name}'s promises", "\n".join(lines) + footer),
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="kept", aliases=["didit", "ikeptit"], help="Tell SYKE you kept your latest promise")
    @commands.guild_only()
    async def kept_cmd(self, ctx: commands.Context, number: int = 1) -> None:
        rows = [r for r in self.promises(ctx.guild.id, ctx.author.id, 10)
                if r[1] == "open" or (r[1] == "broken" and time.time() - r[4] < 6 * 3600)]
        if not rows or not 1 <= number <= len(rows):
            await ctx.reply(embed=problem("Nothing to keep", "You don't have an open promise."))
            return
        pid, _, text, *_ = rows[number - 1]
        self.keep(ctx.guild.id, ctx.author, pid, text)
        await ctx.reply(embed=embed("✅ Kept", f"\"{discord.utils.escape_markdown(text)}\". Okay, I believe you. "
                                              "This time."), allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="forgive", help="SYKE's father (or a server manager) makes SYKE drop a grudge")
    @commands.guild_only()
    async def forgive_cmd(self, ctx: commands.Context, member: discord.Member) -> None:
        if not (self.is_father(ctx.author.id) or is_owner(ctx.author) or ctx.author.guild_permissions.manage_guild):
            await ctx.reply(embed=problem("Nice try", "Only my dad or a server manager can make me forgive someone."))
            return
        self.storage._write("UPDATE persona_bonds SET score = MAX(score, 0), updated = ? WHERE guild_id = ? "
                            "AND user_id = ?", (time.time(), ctx.guild.id, member.id))
        self.add_event(ctx.guild.id, member.id, "forgiven",
                       "was forgiven" + (" because dad said so" if self.is_father(ctx.author.id) else ""))
        who = "Fine, dad." if self.is_father(ctx.author.id) else "Fine."
        await ctx.reply(embed=embed("🕊️ Forgiven", f"{who} {member.display_name} gets a fresh start."),
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="persona", help="Server managers: persona callouts on|off, persona reactions on|off")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def persona_cmd(self, ctx: commands.Context, setting: str = "", value: str = "") -> None:
        setting, value = setting.lower(), value.lower()
        config = self.storage.get_config(ctx.guild.id, "persona")
        if setting not in ("callouts", "reactions") or value not in ("on", "off"):
            callouts = "on" if config.get("callouts", True) else "off"
            reacts = "on" if config.get("reactions", True) else "off"
            await ctx.reply(embed=embed("SYKE's personality",
                                        f"Promise callouts: **{callouts}**\nReactions when someone's nice or rude to "
                                        f"him: **{reacts}**\n`{self.p(ctx)}persona callouts on|off` · "
                                        f"`{self.p(ctx)}persona reactions on|off`"))
            return
        self.storage.update_config(ctx.guild.id, "persona", **{setting: value == "on"})
        what = "Promise callouts" if setting == "callouts" else "Reactions"
        await ctx.reply(embed=embed("Saved ✅", f"{what} are **{value}**."))
