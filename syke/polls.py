"""Polls and question boxes, made from a form.

`/poll create` (or `poll` → Create a poll) opens a form: the question, options one per line, how long,
anonymous or not, extras (several choices, results hidden until the end, a dropdown list instead of
buttons) and who to ping (@everyone, @here or a role). The poll card shows live bars, SYKE's prediction, and buttons or a list to vote with. When it
ends SYKE posts the result with a comment in his own mood.

`/poll question` (or `poll` → Ask a question) posts a question people answer privately in a text box
("Who would most likely adopt a dog?"). Only admins and mods can read the answers; for name questions
SYKE counts which members were named most.

Mods (Manage Messages) make them. Buttons keep working after a restart (custom ids carry the poll id).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .owners import is_owner
from . import ai
from .moderation import OK, WARN, embed, parse_duration, problem

log = logging.getLogger("syke.polls")

LETTERS = "ABCDEFGHIJKLMNO"
MAX_OPTIONS = 15
MAX_DURATION = 30 * 86400
REFRESH_DELAY = 1.5
BAR = 12
POLL_COLOUR = discord.Colour.from_str("#7F77DD")
QUESTION_COLOUR = discord.Colour.from_str("#378ADD")

EXTRA_MULTI, EXTRA_BLIND, EXTRA_LIST, EXTRA_ANON = "multi", "blind", "list", "anon"
PING_EVERYONE, PING_HERE = "everyone", "here"

PREDICT_PROMPT = """You are SYKE, a cocky Discord bot with a personality, in the server "{server}". A poll just \
opened. Predict which option wins, in ONE short, casual line (max 20 words, phone typing, lowercase is fine, \
at most one emoji). Be cheeky about the server. No @mentions, no slurs.

Reply with JSON only: {{"pick": <option number from the list>, "line": "..."}}"""

COMMENT_PROMPT = """You are SYKE, a Discord bot with a personality, in the server "{server}". A poll just closed. \
Comment on the result in ONE or TWO short casual lines (max 30 words, phone typing, at most one emoji). You \
predicted {predicted}: {right}. Brag if you were right, get defensive if not. {voters}Your mood: {mood}. \
No @mentions, no slurs, nothing cruel.

Reply with JSON only: {{"line": "..."}}"""

OFFLINE_PREDICT = ["{option} wins. i've seen how you all think",
                   "calling it now: {option}. screenshot this",
                   "{option}. obviously. next question",
                   "my money's on {option}, don't make me look stupid"]
OFFLINE_RIGHT = ["called it. {winner}. i'm never wrong", "{winner}, like i said. you're welcome",
                 "told you {winner}. i know this server too well"]
OFFLINE_WRONG = ["{winner}?? fine. i wasn't even trying", "{winner} won. i'm not mad. i'm just disappointed",
                 "ok {winner}, you got lucky. rematch"]


@dataclass
class Poll:
    id: int
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    question: str
    options: list[str]
    anonymous: bool
    multi: bool
    blind: bool
    style: str
    created: float
    ends_at: float | None
    closed: bool
    prediction: str
    predicted: int


@dataclass
class Question:
    id: int
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    question: str
    kind: str
    created: float
    ends_at: float | None
    closed: bool


# Pure helpers

def parse_options(text: str) -> list[str]:
    """Options from the form: one per line (or split by |), cleaned, de-duplicated, at most 15."""
    raw = text.split("\n") if "\n" in (text or "") else (text or "").split("|")
    out: list[str] = []
    for line in raw:
        line = re.sub(r"^\s*(?:[-•*]|\d+[.)]|[A-Oa-o][.)])\s+", "", line).strip()
        if line and line.lower() not in {o.lower() for o in out}:
            out.append(line[:80])
    return out[:MAX_OPTIONS]


def parse_length(text: str) -> tuple[float | None, str | None]:
    """(seconds or None for no end, error)."""
    text = (text or "").strip().lower().replace(" ", "")
    if text in ("", "none", "never", "no", "0", "-"):
        return None, None
    seconds = parse_duration(text)
    if seconds is None:
        return None, "Write how long like `10m`, `2h`, `1d`, or leave it empty for no end."
    if seconds < 60:
        return None, "A poll needs at least a minute."
    return float(min(seconds, MAX_DURATION)), None


def tally(options: list[str], votes: list[tuple[int, int]]) -> tuple[list[int], int, int]:
    """(votes per option, voters, total votes)."""
    counts = [0] * len(options)
    voters = set()
    for user, option in votes:
        if 0 <= option < len(options):
            counts[option] += 1
            voters.add(user)
    return counts, len(voters), sum(counts)


def bar(share: float, width: int = BAR) -> str:
    filled = round(share * width)
    return "█" * filled + "░" * (width - filled)


def winners(counts: list[int]) -> list[int]:
    top = max(counts) if counts else 0
    return [i for i, n in enumerate(counts) if n == top and top > 0]


def normalise_name(text: str) -> str:
    text = re.sub(r"<@!?\d+>", lambda m: m.group(0), text or "")
    text = re.sub(r"[^\w\s<>@!'-]", "", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def count_names(answers: list[str], members: dict[int, list[str]]) -> list[tuple[str, int]]:
    """Which members were named most. `members`: id -> [display name, username] (lowercase ok).

    Pings count for that member; a name matches a member when it equals or starts their name (3+ letters);
    anything else is counted as written.
    """
    names = {uid: [n.lower() for n in ns if n] for uid, ns in members.items()}
    label = {uid: ns[0] for uid, ns in members.items() if ns}
    counts: Counter = Counter()
    for answer in answers:
        found = set()
        for uid in map(int, re.findall(r"<@!?(\d+)>", answer or "")):
            if uid in label:
                found.add(label[uid])
        if not found:
            for part in re.split(r",|\band\b|&|/|\+|\n", answer or ""):
                key = normalise_name(part).lstrip("@")
                if len(key) < 2:
                    continue
                match = next((label[uid] for uid, ns in names.items() if key in ns), None)
                if match is None and len(key) >= 3:
                    match = next((label[uid] for uid, ns in names.items()
                                  if any(n.startswith(key) or key.startswith(n) and len(n) >= 3 for n in ns)), None)
                found.add(match or key.title())
        for name in found:
            counts[name] += 1
    return counts.most_common()


# Persistent controls

class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"syke:pv:(?P<poll>\d+):(?P<opt>\d+)"):
    def __init__(self, poll: int, option: int, label: str = "Vote", disabled: bool = False) -> None:
        super().__init__(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary,
                                           custom_id=f"syke:pv:{poll}:{option}", disabled=disabled))
        self.poll, self.option = poll, option

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["poll"]), int(match["opt"]), item.label or "Vote")

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Polls")
        if cog is None:
            await interaction.response.send_message("Polls are offline right now.", ephemeral=True)
            return
        await cog.vote_button(interaction, self.poll, self.option)


class VoteSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"syke:pl:(?P<poll>\d+)"):
    def __init__(self, poll: int, options: list[discord.SelectOption] | None = None, multi: bool = False) -> None:
        options = options or [discord.SelectOption(label="Option", value="0")]
        super().__init__(discord.ui.Select(custom_id=f"syke:pl:{poll}", placeholder="Pick your answer",
                                           min_values=0 if multi else 1,
                                           max_values=len(options) if multi else 1, options=options))
        self.poll = poll

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        made = cls(int(match["poll"]), list(item.options) or None, item.max_values > 1)
        return made

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Polls")
        if cog is None:
            await interaction.response.send_message("Polls are offline right now.", ephemeral=True)
            return
        picked = sorted({int(v) for v in self.item.values if v.isdigit()})
        await cog.vote_list(interaction, self.poll, picked)


class VotersButton(discord.ui.DynamicItem[discord.ui.Button], template=r"syke:pw:(?P<poll>\d+)"):
    def __init__(self, poll: int) -> None:
        super().__init__(discord.ui.Button(label="Voters", emoji="👥", style=discord.ButtonStyle.secondary,
                                           custom_id=f"syke:pw:{poll}"))
        self.poll = poll

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["poll"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Polls")
        if cog is not None:
            await cog.show_voters(interaction, self.poll)


class AnswerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"syke:qa:(?P<q>\d+)"):
    def __init__(self, question: int, disabled: bool = False) -> None:
        super().__init__(discord.ui.Button(label="Answer", emoji="✍️", style=discord.ButtonStyle.primary,
                                           custom_id=f"syke:qa:{question}", disabled=disabled))
        self.question = question

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["q"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Polls")
        if cog is not None:
            await cog.open_answer(interaction, self.question)


class AnswersButton(discord.ui.DynamicItem[discord.ui.Button], template=r"syke:qv:(?P<q>\d+)"):
    def __init__(self, question: int) -> None:
        super().__init__(discord.ui.Button(label="View answers (mods)", emoji="🔒",
                                           style=discord.ButtonStyle.secondary, custom_id=f"syke:qv:{question}"))
        self.question = question

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["q"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Polls")
        if cog is not None:
            await cog.show_answers(interaction, self.question)


# Pings

def ping_choice(values) -> str | None:
    """@everyone wins over @here when a form had both ticked."""
    values = set(values or ())
    return PING_EVERYONE if PING_EVERYONE in values else PING_HERE if PING_HERE in values else None


def _can_mention_everyone(channel, who) -> bool:
    if who is None:
        return False
    try:
        return bool(channel.permissions_for(who).mention_everyone)
    except (AttributeError, TypeError):
        perms = getattr(who, "guild_permissions", None)
        return bool(getattr(perms, "administrator", False) or getattr(perms, "mention_everyone", False))


def plan_ping(channel, user, me, role, everyone: str | None) -> tuple[str | None, discord.AllowedMentions, str]:
    """What to put above the card, which pings Discord may send, and a note for the poster if something was dropped.

    @everyone, @here and roles that aren't set to be mentionable need the Mention Everyone permission, from the
    poster (SYKE's owner is always allowed) and from SYKE, or Discord won't ping anyone.
    """
    if role is not None and getattr(role, "is_default", lambda: False)():
        role, everyone = None, everyone or PING_EVERYONE        # the @everyone "role" picked from the role list
    needs = bool(everyone) or (role is not None and not getattr(role, "mentionable", False))
    note = ""
    if needs and not (is_owner(user) or _can_mention_everyone(channel, user)):
        note = ("You need the **Mention Everyone** permission to ping "
                f"{'@' + everyone if everyone else role.mention}, so I posted it without that ping.")
        if everyone:
            everyone = None
        else:
            role = None
    elif needs and not _can_mention_everyone(channel, me):
        note = ("I don't have **Mention Everyone** in this channel, so Discord didn't ping anyone. "
                "Give me that permission, or make the role mentionable.")
    parts = ([f"@{everyone}"] if everyone else []) + ([role.mention] if role is not None else [])
    allowed = discord.AllowedMentions(everyone=bool(everyone), roles=[role] if role is not None else False,
                                      users=False, replied_user=False)
    return (" ".join(parts) or None), allowed, note


# Forms

class PollForm(discord.ui.Modal, title="Create a poll"):
    def __init__(self, cog: "Polls") -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.question = discord.ui.TextInput(placeholder="Best pizza topping?", max_length=200)
        self.options = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=900,
                                            placeholder="Pepperoni\nMargherita\nPineapple")
        self.length = discord.ui.TextInput(placeholder="1h  (10m, 2h, 1d, or empty for no end)", required=False,
                                           max_length=12, default="1h")
        self.extras = discord.ui.Select(min_values=0, max_values=6, required=False, options=[
            discord.SelectOption(label="Anonymous", value=EXTRA_ANON, description="Nobody can see who voted for what"),
            discord.SelectOption(label="Let people pick more than one", value=EXTRA_MULTI),
            discord.SelectOption(label="Hide results until it ends", value=EXTRA_BLIND),
            discord.SelectOption(label="Vote from a list instead of buttons", value=EXTRA_LIST),
            discord.SelectOption(label="Ping @everyone", value=PING_EVERYONE, emoji="📣"),
            discord.SelectOption(label="Ping @here", value=PING_HERE, emoji="📣",
                                 description="Only people who are online"),
        ])
        self.role = discord.ui.RoleSelect(min_values=0, max_values=1, required=False,
                                          placeholder="Nobody")
        self.add_item(discord.ui.Label(text="Question", component=self.question))
        self.add_item(discord.ui.Label(text="Options", description="One per line, 2 to 15",
                                       component=self.options))
        self.add_item(discord.ui.Label(text="How long", component=self.length))
        self.add_item(discord.ui.Label(text="Extras", description="Optional. Votes are public unless Anonymous",
                                       component=self.extras))
        self.add_item(discord.ui.Label(text="Ping a role", description="Optional", component=self.role))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        extras = set(self.extras.values)
        await self.cog.create_poll(interaction, self.question.value, self.options.value, self.length.value,
                                   anonymous=EXTRA_ANON in extras,
                                   multi=EXTRA_MULTI in extras, blind=EXTRA_BLIND in extras,
                                   style="list" if EXTRA_LIST in extras else "buttons",
                                   ping_role=(self.role.values or [None])[0], ping_everyone=ping_choice(extras))


class QuestionForm(discord.ui.Modal, title="Ask a question"):
    def __init__(self, cog: "Polls") -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.question = discord.ui.TextInput(placeholder="Who would most likely adopt a dog?", max_length=200)
        self.length = discord.ui.TextInput(placeholder="1d  (10m, 2h, 1d, or empty for no end)", required=False,
                                           max_length=12, default="1d")
        self.kind = discord.ui.Select(options=[
            discord.SelectOption(label="Names of members", value="names", default=True,
                                 description="SYKE counts who was named most"),
            discord.SelectOption(label="Anything", value="text", description="Free text answers"),
        ])
        self.everyone = discord.ui.Select(min_values=0, max_values=1, required=False, placeholder="Nobody", options=[
            discord.SelectOption(label="@everyone", value=PING_EVERYONE, emoji="📣"),
            discord.SelectOption(label="@here", value=PING_HERE, emoji="📣", description="Only people who are online"),
        ])
        self.role = discord.ui.RoleSelect(min_values=0, max_values=1, required=False, placeholder="Nobody")
        self.add_item(discord.ui.Label(text="Question", description="Answers are private: only admins and mods "
                                       "can read them", component=self.question))
        self.add_item(discord.ui.Label(text="How long", component=self.length))
        self.add_item(discord.ui.Label(text="Answers are", component=self.kind))
        self.add_item(discord.ui.Label(text="Ping everyone", description="Optional", component=self.everyone))
        self.add_item(discord.ui.Label(text="Ping a role", description="Optional", component=self.role))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_question(interaction, self.question.value, self.length.value,
                                       (self.kind.values or ["names"])[0],
                                       ping_role=(self.role.values or [None])[0],
                                       ping_everyone=ping_choice(self.everyone.values))


class AnswerForm(discord.ui.Modal):
    def __init__(self, cog: "Polls", question: Question, previous: str = "") -> None:
        super().__init__(title="Your answer", timeout=900)
        self.cog, self.q = cog, question
        self.answer = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=300,
                                           default=previous or None,
                                           placeholder="Type a name" if question.kind == "names" else "Type your answer")
        self.add_item(discord.ui.TextDisplay(f"**{question.question}**"))
        self.add_item(discord.ui.Label(text="Answer", description="Only admins and mods will see it",
                                       component=self.answer))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_answer(interaction, self.q.id, self.answer.value)


class Launcher(discord.ui.View):
    """The `poll` panel: two buttons that open the forms (only for the mod who asked)."""

    def __init__(self, cog: "Polls", owner: int) -> None:
        super().__init__(timeout=600)
        self.cog, self.owner = cog, owner

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner:
            await interaction.response.send_message("Only the mod who opened this can use it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Create a poll", emoji="📊", style=discord.ButtonStyle.primary)
    async def make_poll(self, interaction: discord.Interaction, _button) -> None:
        await interaction.response.send_modal(PollForm(self.cog))

    @discord.ui.button(label="Ask a question", emoji="✍️", style=discord.ButtonStyle.secondary)
    async def make_question(self, interaction: discord.Interaction, _button) -> None:
        await interaction.response.send_modal(QuestionForm(self.cog))


class AnswersView(discord.ui.View):
    """Mod-only: post the count (no names of who said what) to the channel."""

    def __init__(self, cog: "Polls", question: Question) -> None:
        super().__init__(timeout=600)
        self.cog, self.q = cog, question

    @discord.ui.button(label="Post the results", emoji="📣", style=discord.ButtonStyle.secondary)
    async def post(self, interaction: discord.Interaction, _button) -> None:
        await self.cog.post_question_results(interaction, self.q.id)


# The cog

def can_manage(member) -> bool:
    if is_owner(member):
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild or perms.manage_messages))


class Polls(commands.Cog, name="Polls"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._refresh: dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(VoteButton, VoteSelect, VotersButton, AnswerButton, AnswersButton)
        self.closer.start()

    def cog_unload(self) -> None:
        self.closer.cancel()

    @property
    def storage(self):
        return self.bot.storage

    def allowed(self, member) -> bool:
        return getattr(member, "id", 0) in self.bot.settings.owner_ids or can_manage(member)

    # Storage

    def poll(self, poll_id: int) -> Poll | None:
        rows = self.storage._read(
            "SELECT id, guild_id, channel_id, message_id, author_id, question, options, anonymous, multi, blind, "
            "style, created, ends_at, closed, prediction, predicted FROM polls WHERE id = ?", (poll_id,))
        if not rows:
            return None
        r = list(rows[0])
        r[6] = json.loads(r[6])
        for i in (7, 8, 9, 13):
            r[i] = bool(r[i])
        return Poll(*r)

    def votes(self, poll_id: int) -> list[tuple[int, int]]:
        return self.storage._read("SELECT user_id, option FROM poll_votes WHERE poll_id = ?", (poll_id,))

    def mine(self, poll_id: int, user_id: int) -> set[int]:
        return {r[0] for r in self.storage._read("SELECT option FROM poll_votes WHERE poll_id = ? AND user_id = ?",
                                                 (poll_id, user_id))}

    def question(self, qid: int) -> Question | None:
        rows = self.storage._read("SELECT id, guild_id, channel_id, message_id, author_id, question, kind, created, "
                                  "ends_at, closed FROM questions WHERE id = ?", (qid,))
        if not rows:
            return None
        r = list(rows[0])
        r[9] = bool(r[9])
        return Question(*r)

    def answers(self, qid: int) -> list[tuple[int, str, float]]:
        return self.storage._read("SELECT user_id, answer, at FROM question_answers WHERE question_id = ? ORDER BY at",
                                  (qid,))

    # Cards

    def poll_card(self, p: Poll, guild=None) -> discord.Embed:
        counts, voters, total = tally(p.options, self.votes(p.id))
        hidden = p.blind and not p.closed
        lines = []
        top = set(winners(counts)) if p.closed else set()
        for i, option in enumerate(p.options):
            share = counts[i] / total if total else 0.0
            mark = " 🏆" if i in top else ""
            head = f"**{LETTERS[i]}** · {discord.utils.escape_markdown(option)}{mark}"
            if hidden:
                lines.append(head)
            else:
                lines.append(f"{head}\n`{bar(share)}` {counts[i]} · {round(share * 100)}%")
        author = guild.get_member(p.author_id) if guild else None
        by = f"Poll by {author.display_name}" if author else "Poll"
        if p.closed:
            when = "closed"
        elif p.ends_at:
            when = f"ends <t:{int(p.ends_at)}:R>"
        else:
            when = "open until a mod ends it"
        card = discord.Embed(title=p.question[:256], description="\n".join(lines),
                             colour=OK if p.closed else POLL_COLOUR)
        card.set_author(name=f"📊 {by} · {'closed' if p.closed else 'live'}")
        if hidden:
            card.add_field(name="Results", value="🙈 Hidden until the poll ends.", inline=False)
        if p.prediction:
            card.add_field(name="SYKE's prediction", value=p.prediction[:1024], inline=False)
        notes = [f"{voters} voter{'s' if voters != 1 else ''}", "anonymous" if p.anonymous else "votes are public"]
        if p.multi:
            notes.append("pick as many as you like")
        if not p.closed:
            notes.append("you can change your vote")
        card.add_field(name="​", value=f"-# {' · '.join(notes)} · {when}", inline=False)
        card.set_footer(text=f"Poll #{p.id}")
        return card

    def poll_view(self, p: Poll) -> discord.ui.View | None:
        if p.closed:
            return None
        view = discord.ui.View(timeout=None)
        if p.style == "list":
            options = [discord.SelectOption(label=f"{LETTERS[i]} · {o}"[:100], value=str(i))
                       for i, o in enumerate(p.options)]
            view.add_item(VoteSelect(p.id, options, p.multi))
        else:
            for i, option in enumerate(p.options):
                item = VoteButton(p.id, i, f"{LETTERS[i]} · {option}")
                item.item.row = i // 5
                view.add_item(item)
        if not p.anonymous:
            voters = VotersButton(p.id)
            voters.item.row = (len(p.options) - 1) // 5 + 1 if p.style != "list" else 1   # under the last row of options
            view.add_item(voters)
        return view

    def question_card(self, q: Question, count: int, guild=None) -> discord.Embed:
        author = guild.get_member(q.author_id) if guild else None
        by = f"Question from {author.display_name}" if author else "Question"
        if q.closed:
            when = "closed"
        elif q.ends_at:
            when = f"closes <t:{int(q.ends_at)}:R>"
        else:
            when = "open until a mod closes it"
        how = "Type a member's name." if q.kind == "names" else "Type anything."
        body = (f"Press **Answer** and type yours. {how}\n🔒 Only admins and mods can see the answers."
                if not q.closed else "This question is closed. Thanks for answering.")
        card = discord.Embed(title=q.question[:256], description=body,
                             colour=OK if q.closed else QUESTION_COLOUR)
        card.set_author(name=f"✍️ {by}")
        card.add_field(name="​", value=f"-# {count} answer{'s' if count != 1 else ''} · {when}", inline=False)
        card.set_footer(text=f"Question #{q.id}")
        return card

    def question_view(self, q: Question) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        if not q.closed:
            view.add_item(AnswerButton(q.id))
        view.add_item(AnswersButton(q.id))
        return view

    async def refresh_later(self, guild, kind: str, item_id: int) -> None:
        """Redraw a card once votes settle (one edit per burst, not one per vote)."""
        key = item_id if kind == "poll" else -item_id
        if key in self._refresh:
            return

        async def later():
            try:
                await asyncio.sleep(REFRESH_DELAY)
                await self.redraw(guild, kind, item_id)
            finally:
                self._refresh.pop(key, None)

        self._refresh[key] = asyncio.ensure_future(later())

    async def redraw(self, guild, kind: str, item_id: int) -> None:
        if kind == "poll":
            p = self.poll(item_id)
            if p is None:
                return
            card, view, cid, mid = self.poll_card(p, guild), self.poll_view(p), p.channel_id, p.message_id
        else:
            q = self.question(item_id)
            if q is None:
                return
            card, view = self.question_card(q, len(self.answers(q.id)), guild), self.question_view(q)
            cid, mid = q.channel_id, q.message_id
        channel = guild.get_channel(cid) if guild else None
        if channel is None or not mid:
            return
        with contextlib.suppress(discord.HTTPException, AttributeError):
            await channel.get_partial_message(mid).edit(embed=card, view=view)

    # Making polls

    async def create_poll(self, interaction: discord.Interaction, question: str, options_text: str,
                          length_text: str, *, anonymous: bool, multi: bool, blind: bool, style: str,
                          ping_role=None, ping_everyone: str | None = None) -> None:
        question = " ".join((question or "").split())
        options = parse_options(options_text)
        length, error = parse_length(length_text)
        if error or len(options) < 2 or not question:
            reason = error or ("Give at least 2 different options, one per line." if len(options) < 2
                               else "The poll needs a question.")
            await interaction.response.send_message(embed=problem("Couldn't make the poll", reason), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = time.time()
        pid = self.storage._insert(
            "INSERT INTO polls (guild_id, channel_id, author_id, question, options, anonymous, multi, blind, style, "
            "created, ends_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (interaction.guild.id, interaction.channel.id, interaction.user.id, question, json.dumps(options),
             int(anonymous), int(multi), int(blind), style, now, now + length if length else None))
        pick, line = await self.predict(interaction.guild, question, options)
        self.storage._write("UPDATE polls SET prediction = ?, predicted = ? WHERE id = ?", (line, pick, pid))
        p = self.poll(pid)
        ping, allowed, note = plan_ping(interaction.channel, interaction.user, getattr(interaction.guild, "me", None),
                                        ping_role, ping_everyone)
        try:
            sent = await interaction.channel.send(ping, embed=self.poll_card(p, interaction.guild),
                                                  view=self.poll_view(p), allowed_mentions=allowed)
        except discord.HTTPException:
            self.storage._write("DELETE FROM polls WHERE id = ?", (pid,))
            await interaction.followup.send(embed=problem("Couldn't post it", "I can't send messages here."),
                                            ephemeral=True)
            return
        self.storage._write("UPDATE polls SET message_id = ? WHERE id = ?", (sent.id, pid))
        ends = f" It ends <t:{int(p.ends_at)}:R>." if p.ends_at else " End it with `/poll end`."
        await interaction.followup.send(embed=embed("📊 Poll posted", f"Poll #{pid} is live.{ends}"
                                                    + (f"\n\n⚠️ {note}" if note else ""), WARN if note else OK),
                                        ephemeral=True)

    async def predict(self, guild, question: str, options: list[str]) -> tuple[int, str]:
        if self.bot.settings.ai_provider != "none":
            listed = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
            with contextlib.suppress(Exception):
                data = await ai._complete(self.bot.settings, PREDICT_PROMPT.format(server=guild.name),
                                          f"Poll: {question}\nOptions:\n{listed}", 1.0, 15)
                pick = int(data.get("pick", 0)) - 1
                line = ai.public_text(data.get("line"), 200)
                if 0 <= pick < len(options) and line:
                    return pick, line
        pick = random.randrange(len(options))
        return pick, random.choice(OFFLINE_PREDICT).format(option=options[pick].lower())

    # Voting

    async def _open_poll(self, interaction: discord.Interaction, poll_id: int) -> Poll | None:
        p = self.poll(poll_id)
        if p is None or p.closed or (p.ends_at and p.ends_at <= time.time()):
            await interaction.response.send_message("This poll has ended.", ephemeral=True)
            if p is not None and not p.closed:
                await self.close_poll(p)
            return None
        return p

    async def vote_button(self, interaction: discord.Interaction, poll_id: int, option: int) -> None:
        p = await self._open_poll(interaction, poll_id)
        if p is None or not 0 <= option < len(p.options):
            return
        user = interaction.user.id
        mine = self.mine(p.id, user)
        if option in mine:
            self.storage._write("DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ? AND option = ?",
                                (p.id, user, option))
            text = f"Removed your vote for **{p.options[option]}**."
        else:
            if not p.multi:
                self.storage._write("DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?", (p.id, user))
            self.storage._write("INSERT OR REPLACE INTO poll_votes (poll_id, user_id, option, at) VALUES (?, ?, ?, ?)",
                                (p.id, user, option, time.time()))
            text = (f"You voted **{p.options[option]}**." if not p.multi else
                    f"Added **{p.options[option]}** to your picks.")
            text += " Press it again to take it back."
        await interaction.response.send_message(text, ephemeral=True)
        await self.refresh_later(interaction.guild, "poll", p.id)

    async def vote_list(self, interaction: discord.Interaction, poll_id: int, picked: list[int]) -> None:
        p = await self._open_poll(interaction, poll_id)
        if p is None:
            return
        picked = [i for i in picked if 0 <= i < len(p.options)][:len(p.options) if p.multi else 1]
        self.storage._write("DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?", (p.id, interaction.user.id))
        for i in picked:
            self.storage._write("INSERT OR REPLACE INTO poll_votes (poll_id, user_id, option, at) VALUES (?, ?, ?, ?)",
                                (p.id, interaction.user.id, i, time.time()))
        text = ("You voted **" + "**, **".join(p.options[i] for i in picked) + "**.") if picked else \
            "Your vote is cleared."
        await interaction.response.send_message(text, ephemeral=True)
        await self.refresh_later(interaction.guild, "poll", p.id)

    async def show_voters(self, interaction: discord.Interaction, poll_id: int) -> None:
        p = self.poll(poll_id)
        if p is None:
            await interaction.response.send_message("That poll is gone.", ephemeral=True)
            return
        if p.anonymous:
            await interaction.response.send_message("🔒 This poll is anonymous.", ephemeral=True)
            return
        if p.blind and not p.closed:
            await interaction.response.send_message("🙈 Results (and voters) are hidden until the poll ends.",
                                                    ephemeral=True)
            return
        by_option: dict[int, list[str]] = {i: [] for i in range(len(p.options))}
        for user, option in self.votes(p.id):
            member = interaction.guild.get_member(user)
            if option in by_option:
                by_option[option].append(member.display_name if member else f"<@{user}>")
        card = embed(f"👥 Who voted · {p.question[:200]}", "")
        for i, option in enumerate(p.options):
            names = by_option[i]
            value = ", ".join(discord.utils.escape_markdown(n) for n in names[:40]) or "nobody"
            if len(names) > 40:
                value += f" and {len(names) - 40} more"
            card.add_field(name=f"{LETTERS[i]} · {option[:80]} ({len(names)})", value=value[:1024], inline=False)
        await interaction.response.send_message(embed=card, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # Closing

    @tasks.loop(seconds=30)
    async def closer(self) -> None:
        now = time.time()
        for (pid,) in self.storage._read("SELECT id FROM polls WHERE closed = 0 AND ends_at IS NOT NULL "
                                         "AND ends_at <= ?", (now,)):
            p = self.poll(pid)
            if p is not None:
                with contextlib.suppress(Exception):
                    await self.close_poll(p)
        for (qid,) in self.storage._read("SELECT id FROM questions WHERE closed = 0 AND ends_at IS NOT NULL "
                                         "AND ends_at <= ?", (now,)):
            q = self.question(qid)
            if q is not None:
                with contextlib.suppress(Exception):
                    await self.close_question(q)

    @closer.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def close_poll(self, p: Poll) -> None:
        if self.storage._write("UPDATE polls SET closed = 1 WHERE id = ? AND closed = 0", (p.id,)) == 0:
            return  # someone else closed it first
        p = self.poll(p.id)
        guild = self.bot.get_guild(p.guild_id)
        if guild is None:
            return
        await self.redraw(guild, "poll", p.id)
        counts, voters, total = tally(p.options, self.votes(p.id))
        channel = guild.get_channel(p.channel_id)
        if channel is None:
            return
        top = winners(counts)
        if not top:
            title, line = "Nobody voted", "nobody voted. wow. i'll remember this"
        else:
            names = " and ".join(p.options[i] for i in top)
            share = round(counts[top[0]] / total * 100) if total else 0
            title = f"🏆 {names} {'win' if len(top) > 1 else 'wins'} ({share}%)" if len(top) <= 2 else "🏆 It's a tie"
            line = await self.comment(guild, p, counts, top)
        rest = " · ".join(f"{p.options[i]} {round(n / total * 100) if total else 0}%"
                          for i, n in enumerate(counts) if i not in top)
        body = (f"**{p.question}**\n{rest}\n\n" if rest else f"**{p.question}**\n\n") + f"*{line}*"
        card = discord.Embed(title=title[:256], description=body[:4000], colour=OK)
        card.set_author(name=f"📊 Poll closed · {voters} voter{'s' if voters != 1 else ''}")
        card.set_footer(text=f"Poll #{p.id}")
        author = f"<@{p.author_id}>"
        kwargs = {"embed": card, "content": author, "allowed_mentions": discord.AllowedMentions(users=True)}
        with contextlib.suppress(discord.HTTPException):
            if p.message_id:
                await channel.get_partial_message(p.message_id).reply(mention_author=False, **kwargs)
            else:
                await channel.send(**kwargs)

    async def comment(self, guild, p: Poll, counts: list[int], top: list[int]) -> str:
        winner = " and ".join(p.options[i] for i in top)
        right = p.predicted in top
        if self.bot.settings.ai_provider != "none":
            voters = ""
            if not p.anonymous:
                losers = [i for i in range(len(p.options)) if i not in top and counts[i]]
                names = []
                for user, option in self.votes(p.id):
                    if option in losers:
                        member = guild.get_member(user)
                        if member:
                            names.append(f"{member.display_name} ({p.options[option]})")
                if names:
                    voters = f"People who voted for losing options: {', '.join(names[:8])}. You may tease one. "
            persona = self.bot.get_cog("Persona")
            mood = persona.mood_now(guild.id)[0] if persona else "chill"
            predicted = p.options[p.predicted] if 0 <= p.predicted < len(p.options) else "nothing"
            results = "\n".join(f"{o}: {counts[i]}" for i, o in enumerate(p.options))
            with contextlib.suppress(Exception):
                data = await ai._complete(self.bot.settings, COMMENT_PROMPT.format(
                    server=guild.name, predicted=predicted, right="you were RIGHT" if right else "you were WRONG",
                    voters=voters, mood=mood), f"Poll: {p.question}\nResults:\n{results}\nWinner: {winner}", 1.0, 15)
                line = ai.public_text(data.get("line"), 300)
                if line:
                    return line
        return random.choice(OFFLINE_RIGHT if right else OFFLINE_WRONG).format(winner=winner.lower())

    # Questions

    async def create_question(self, interaction: discord.Interaction, question: str, length_text: str,
                              kind: str, *, ping_role=None, ping_everyone: str | None = None) -> None:
        question = " ".join((question or "").split())
        length, error = parse_length(length_text)
        if error or not question:
            await interaction.response.send_message(
                embed=problem("Couldn't ask it", error or "Write the question."), ephemeral=True)
            return
        now = time.time()
        qid = self.storage._insert(
            "INSERT INTO questions (guild_id, channel_id, author_id, question, kind, created, ends_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (interaction.guild.id, interaction.channel.id, interaction.user.id, question,
             kind if kind in ("names", "text") else "names", now, now + length if length else None))
        q = self.question(qid)
        ping, allowed, note = plan_ping(interaction.channel, interaction.user, getattr(interaction.guild, "me", None),
                                        ping_role, ping_everyone)
        try:
            sent = await interaction.channel.send(ping, embed=self.question_card(q, 0, interaction.guild),
                                                  view=self.question_view(q), allowed_mentions=allowed)
        except discord.HTTPException:
            self.storage._write("DELETE FROM questions WHERE id = ?", (qid,))
            await interaction.response.send_message(embed=problem("Couldn't post it", "I can't send messages here."),
                                                    ephemeral=True)
            return
        self.storage._write("UPDATE questions SET message_id = ? WHERE id = ?", (sent.id, qid))
        await interaction.response.send_message(
            embed=embed("✍️ Question posted", f"Question #{qid} is live. Press **View answers** on it any time."
                        + (f"\n\n⚠️ {note}" if note else ""), WARN if note else OK), ephemeral=True)

    async def open_answer(self, interaction: discord.Interaction, qid: int) -> None:
        q = self.question(qid)
        if q is None or q.closed or (q.ends_at and q.ends_at <= time.time()):
            await interaction.response.send_message("This question is closed.", ephemeral=True)
            if q is not None and not q.closed:
                await self.close_question(q)
            return
        previous = self.storage._read("SELECT answer FROM question_answers WHERE question_id = ? AND user_id = ?",
                                      (qid, interaction.user.id))
        await interaction.response.send_modal(AnswerForm(self, q, previous[0][0] if previous else ""))

    async def save_answer(self, interaction: discord.Interaction, qid: int, answer: str) -> None:
        q = self.question(qid)
        answer = " ".join((answer or "").split())
        if q is None or q.closed:
            await interaction.response.send_message("This question closed before you sent that.", ephemeral=True)
            return
        if not answer:
            await interaction.response.send_message("Your answer was empty.", ephemeral=True)
            return
        existed = self.storage._read("SELECT 1 FROM question_answers WHERE question_id = ? AND user_id = ?",
                                     (qid, interaction.user.id))
        self.storage._write("INSERT OR REPLACE INTO question_answers (question_id, user_id, answer, at) "
                            "VALUES (?, ?, ?, ?)", (qid, interaction.user.id, answer[:300], time.time()))
        await interaction.response.send_message(
            ("✏️ Answer updated." if existed else "✅ Answer sent.") + " Only admins and mods can see it.",
            ephemeral=True)
        await self.refresh_later(interaction.guild, "question", qid)

    def answer_summary(self, guild, q: Question) -> tuple[list[tuple[str, int]], list[str]]:
        rows = self.answers(q.id)
        if q.kind == "names":
            raw = getattr(guild, "members", [])
            members = {m.id: [m.display_name, getattr(m, "name", "")]
                       for m in (raw.values() if isinstance(raw, dict) else raw) if not getattr(m, "bot", False)}
            counted = count_names([a for _, a, _ in rows], members)
        else:
            counted = Counter(normalise_name(a).capitalize() for _, a, _ in rows).most_common()
        said = []
        for user, answer, _ in rows:
            member = guild.get_member(user)
            who = member.display_name if member else f"user {user}"
            said.append(f"**{discord.utils.escape_markdown(who)}**: {discord.utils.escape_markdown(answer)}")
        return counted, said

    async def show_answers(self, interaction: discord.Interaction, qid: int) -> None:
        if not self.allowed(interaction.user):
            await interaction.response.send_message("🔒 Only admins and mods can see the answers.", ephemeral=True)
            return
        q = self.question(qid)
        if q is None:
            await interaction.response.send_message("That question is gone.", ephemeral=True)
            return
        counted, said = self.answer_summary(interaction.guild, q)
        if not said:
            await interaction.response.send_message("No answers yet.", ephemeral=True)
            return
        total = len(said)
        top = "\n".join(f"`{n:>2}×` {discord.utils.escape_markdown(name)} · {round(n / total * 100)}%"
                        for name, n in counted[:12])
        card = embed(f"🔒 Answers · {q.question[:200]}", f"**{total} answer{'s' if total != 1 else ''}**\n\n"
                                                        f"**Most said**\n{top}")
        listing = "\n".join(said)
        files = []
        if len(listing) <= 1000:
            card.add_field(name="Who said what", value=listing, inline=False)
        else:
            card.add_field(name="Who said what", value="\n".join(said[:12])[:1000] + f"\n…full list attached",
                           inline=False)
            text = f"{q.question}\n\n" + "\n".join(s.replace("**", "") for s in said)
            files = [discord.File(__import__("io").BytesIO(text.encode()), filename=f"answers-{q.id}.txt")]
        await interaction.response.send_message(embed=card, files=files, ephemeral=True,
                                                view=AnswersView(self, q),
                                                allowed_mentions=discord.AllowedMentions.none())

    async def post_question_results(self, interaction: discord.Interaction, qid: int) -> None:
        if not self.allowed(interaction.user):
            await interaction.response.send_message("🔒 Mods only.", ephemeral=True)
            return
        q = self.question(qid)
        if q is None:
            await interaction.response.send_message("That question is gone.", ephemeral=True)
            return
        counted, said = self.answer_summary(interaction.guild, q)
        total = len(said)
        if not total:
            await interaction.response.send_message("No answers to post.", ephemeral=True)
            return
        podium = ["🥇", "🥈", "🥉"]
        lines = [f"{podium[i] if i < 3 else '▫️'} **{discord.utils.escape_markdown(name)}** · {n} "
                 f"({round(n / total * 100)}%)" for i, (name, n) in enumerate(counted[:10])]
        card = discord.Embed(title=q.question[:256], description="\n".join(lines), colour=OK)
        card.set_author(name=f"✍️ Results · {total} answer{'s' if total != 1 else ''}")
        card.set_footer(text="Who said what stays private.")
        await interaction.channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message("📣 Posted.", ephemeral=True)

    async def close_question(self, q: Question) -> None:
        if self.storage._write("UPDATE questions SET closed = 1 WHERE id = ? AND closed = 0", (q.id,)) == 0:
            return
        guild = self.bot.get_guild(q.guild_id)
        if guild is not None:
            await self.redraw(guild, "question", q.id)

    # Commands

    @commands.hybrid_group(name="poll", aliases=["polls"], fallback="menu", invoke_without_command=True,
                           description="Make a poll or ask a question (mods)")
    @commands.guild_only()
    async def poll_group(self, ctx: commands.Context) -> None:
        if not self.allowed(ctx.author):
            await ctx.reply(embed=problem("Mods only", "Polls and questions are made by mods (Manage Messages)."),
                            ephemeral=True)
            return
        card = embed("📊 Polls and questions",
                     "**Create a poll**: people vote with buttons or a list, with live results and SYKE's "
                     "prediction.\n**Ask a question**: people type their answer privately; only admins and mods "
                     "can read them.")
        card.set_footer(text="poll list shows what's running · poll end <id> closes one early")
        await ctx.reply(embed=card, view=Launcher(self, ctx.author.id), ephemeral=True)

    async def _form(self, ctx: commands.Context, form) -> None:
        if not self.allowed(ctx.author):
            await ctx.reply(embed=problem("Mods only", "Polls and questions are made by mods (Manage Messages)."),
                            ephemeral=True)
            return
        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(form)
            return
        await ctx.reply(embed=embed("📊 Polls and questions", "Press a button to open the form."),
                        view=Launcher(self, ctx.author.id))

    @poll_group.command(name="create", aliases=["new", "make"], description="Create a poll (opens a form)")
    @commands.guild_only()
    async def poll_create(self, ctx: commands.Context) -> None:
        await self._form(ctx, PollForm(self))

    @poll_group.command(name="question", aliases=["ask", "box"],
                        description="Ask a question people answer privately (opens a form)")
    @commands.guild_only()
    async def poll_question(self, ctx: commands.Context) -> None:
        await self._form(ctx, QuestionForm(self))

    @poll_group.command(name="list", aliases=["running", "open"], description="Polls and questions still running")
    @commands.guild_only()
    async def poll_list(self, ctx: commands.Context) -> None:
        gid = ctx.guild.id
        polls = self.storage._read("SELECT id, question, ends_at, channel_id, message_id FROM polls WHERE guild_id = ? "
                                   "AND closed = 0 ORDER BY id DESC LIMIT 15", (gid,))
        qs = self.storage._read("SELECT id, question, ends_at, channel_id, message_id FROM questions WHERE guild_id = ? "
                                "AND closed = 0 ORDER BY id DESC LIMIT 15", (gid,))

        def line(kind, row):
            i, text, ends, cid, mid = row
            link = f"https://discord.com/channels/{gid}/{cid}/{mid}" if mid else ""
            when = f"ends <t:{int(ends)}:R>" if ends else "no end"
            title = discord.utils.escape_markdown(text[:60])
            return f"`{kind}{i}` [{title}]({link}) · {when}" if link else f"`{kind}{i}` {title} · {when}"

        body = "\n".join([line("#", r) for r in polls] + [line("Q", r) for r in qs]) or "Nothing running."
        card = embed("📊 Running now", body)
        card.set_footer(text="poll end <id> closes a poll · poll end Q<id> closes a question")
        await ctx.reply(embed=card, ephemeral=True)

    @poll_group.command(name="end", aliases=["close", "stop"], description="End a poll or question early")
    @app_commands.describe(which="The poll number (or Q and the question number), from poll list")
    @commands.guild_only()
    async def poll_end(self, ctx: commands.Context, which: str) -> None:
        text = which.strip().lstrip("#").upper()
        is_q = text.startswith("Q")
        number = text[1:] if is_q else text
        if not number.isdigit():
            await ctx.reply(embed=problem("Which one?", "Use the number from `poll list`, like `12` or `Q4`."),
                            ephemeral=True)
            return
        item = self.question(int(number)) if is_q else self.poll(int(number))
        if item is None or item.guild_id != ctx.guild.id:
            await ctx.reply(embed=problem("Not found", "No poll or question with that number here."), ephemeral=True)
            return
        if not (self.allowed(ctx.author) or ctx.author.id == item.author_id):
            await ctx.reply(embed=problem("Mods only", "Only a mod or whoever made it can end it."), ephemeral=True)
            return
        if item.closed:
            await ctx.reply(embed=embed("Already closed", "That one has already ended.", WARN), ephemeral=True)
            return
        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        if is_q:
            await self.close_question(item)
        else:
            await self.close_poll(item)
        await ctx.reply(embed=embed("Ended ✅", f"{'Question' if is_q else 'Poll'} #{number} is closed.", OK),
                        ephemeral=True)
