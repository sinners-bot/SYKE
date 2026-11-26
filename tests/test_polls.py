"""Polls (form, buttons/list, anonymous or not, blind, multi, prediction, close) and private question boxes."""

import time
import types
from dataclasses import replace

import discord
import pytest

from syke import polls as pl
from syke.bot import Syke
from syke.config import load_settings

NS = types.SimpleNamespace
GID = 42


def member(uid, name, mod=False):
    perms = NS(administrator=False, manage_guild=False, manage_messages=mod)
    return NS(id=uid, display_name=name, name=name.lower(), bot=False, guild_permissions=perms, mention=f"<@{uid}>")


class Channel:
    id = 10

    def __init__(self):
        self.sent, self.edits, self.replies = [], [], []

    async def send(self, content=None, **kw):
        msg = NS(id=900 + len(self.sent), content=content, **kw)
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        channel = self

        class Partial:
            async def edit(self, **kw):
                channel.edits.append((mid, kw))

            async def reply(self, content=None, **kw):
                channel.replies.append((mid, content, kw))
        return Partial()


class Guild:
    id = GID
    name = "Neon Desert"

    def __init__(self):
        self.channel = Channel()
        self.members = {1: member(1, "Boss", mod=True), 2: member(2, "Mo"), 3: member(3, "Zara"),
                        4: member(4, "Kai"), 5: member(5, "Lucia")}

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel(self, cid):
        return self.channel if cid == 10 else None


class Response:
    def __init__(self):
        self.messages, self.modal, self.deferred = [], None, False

    async def send_message(self, content=None, **kw):
        self.messages.append((content, kw))

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **kw):
        self.deferred = True


class Interaction:
    def __init__(self, guild, user):
        self.guild, self.user, self.channel = guild, user, guild.channel
        self.response, self.followups = Response(), []

        async def followup_send(content=None, **kw):
            self.followups.append((content, kw))
        self.followup = NS(send=followup_send)


@pytest.fixture()
def world(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "polls.db"), ai_provider="none", web_enabled=False,
                       owner_ids=frozenset({99}))
    bot = Syke(settings)
    cog = pl.Polls(bot)
    guild = Guild()
    bot.get_guild = lambda gid: guild if gid == GID else None
    return bot, cog, guild


async def make_poll(cog, guild, **kw):
    args = dict(anonymous=False, multi=False, blind=False, style="buttons")
    args.update(kw)
    it = Interaction(guild, guild.members[1])
    await cog.create_poll(it, "Best pizza topping?", "Pepperoni\nMargherita\nPineapple", "1h", **args)
    return cog.poll(1), it


def inner(c):
    return getattr(c, "item", c)


def labels_of(view):
    return [getattr(inner(c), "label", None) for c in view.children]


async def flush(cog):
    for task in list(cog._refresh.values()):
        await task


# Helpers

def test_options_and_lengths_parse_like_people_type():
    assert pl.parse_options("1. Pepperoni\n- Margherita\nB) pineapple\n\npepperoni") == ["Pepperoni", "Margherita",
                                                                                            "pineapple"]
    assert pl.parse_options("a | b | c") == ["a", "b", "c"]
    assert len(pl.parse_options("\n".join(str(i) for i in range(20)))) == 15
    assert pl.parse_length("2h") == (7200.0, None) and pl.parse_length("") == (None, None)
    assert pl.parse_length("soon")[1] and pl.parse_length("30s")[1]


def test_names_are_counted_with_pings_and_partial_names():
    members = {2: ["Mo", "mo"], 3: ["Zara", "zara_x"], 5: ["Lucia", "lucia"]}
    counted = dict(pl.count_names(["Zara", "zar", "<@3>", "mo and lucia", "MO", "my mum"], members))
    assert counted == {"Zara": 3, "Mo": 2, "Lucia": 1, "My Mum": 1}


# Polls

async def test_mod_makes_a_poll_from_the_form_and_people_vote(world):
    bot, cog, guild = world
    p, it = await make_poll(cog, guild)
    assert p.options == ["Pepperoni", "Margherita", "Pineapple"] and p.prediction
    card = guild.channel.sent[0].embed
    assert card.title == "Best pizza topping?" and "SYKE's prediction" in [f.name for f in card.fields]
    view = guild.channel.sent[0].view
    labels = labels_of(view)
    assert labels[:3] == ["A · Pepperoni", "B · Margherita", "C · Pineapple"] and "Voters" in labels
    for uid, option in ((2, 0), (3, 0), (4, 2)):
        await cog.vote_button(Interaction(guild, guild.members[uid]), p.id, option)
    await cog.vote_button(Interaction(guild, guild.members[4]), p.id, 1)        # changed mind
    await flush(cog)
    assert pl.tally(p.options, cog.votes(p.id))[0] == [2, 1, 0]
    redrawn = guild.channel.edits[-1][1]["embed"].description
    assert "2 · 67%" in redrawn and "1 · 33%" in redrawn
    again = Interaction(guild, guild.members[2])
    await cog.vote_button(again, p.id, 0)                                         # press again = take back
    assert "Removed" in again.response.messages[0][0] and pl.tally(p.options, cog.votes(p.id))[0] == [1, 1, 0]


async def test_polls_hold_fifteen_options_in_a_layout_discord_accepts(world):
    bot, cog, guild = world
    it = Interaction(guild, guild.members[1])
    await cog.create_poll(it, "Best number?", "\n".join(f"Option {n}" for n in range(1, 16)), "1h",
                          anonymous=False, multi=False, blind=False, style="buttons")
    p = cog.poll(1)
    assert len(p.options) == 15
    view = cog.poll_view(p)
    rows = {}
    for child in view.children:
        item = getattr(child, "item", child)
        rows.setdefault(item.row, []).append(item.label)
    assert all(len(r) <= 5 for r in rows.values()) and max(rows) <= 4      # Discord: 5 rows of 5
    assert rows[2][-1] == "O · Option 15" and rows[3] == ["Voters"]
    await cog.vote_button(Interaction(guild, guild.members[2]), p.id, 14)
    assert pl.tally(p.options, cog.votes(p.id))[0][14] == 1
    p.style = "list"
    select = [getattr(c, "item", c) for c in cog.poll_view(p).children
              if isinstance(getattr(c, "item", c), discord.ui.Select)][0]
    assert len(select.options) == 15 and select.options[-1].label == "O · Option 15"


async def test_only_mods_can_open_the_forms(world):
    bot, cog, guild = world
    assert not cog.allowed(guild.members[2]) and cog.allowed(guild.members[1])
    assert cog.allowed(member(99, "Echo"))                                       # SYKE's owner


async def test_anonymous_polls_never_show_voters(world):
    bot, cog, guild = world
    p, _ = await make_poll(cog, guild, anonymous=True)
    assert "Voters" not in labels_of(guild.channel.sent[0].view)
    ask = Interaction(guild, guild.members[2])
    await cog.show_voters(ask, p.id)
    assert "anonymous" in ask.response.messages[0][0]


async def test_public_polls_show_who_voted_what(world):
    bot, cog, guild = world
    p, _ = await make_poll(cog, guild)
    await cog.vote_button(Interaction(guild, guild.members[3]), p.id, 2)
    ask = Interaction(guild, guild.members[2])
    await cog.show_voters(ask, p.id)
    fields = {f.name: f.value for f in ask.response.messages[0][1]["embed"].fields}
    assert fields["C · Pineapple (1)"] == "Zara" and fields["A · Pepperoni (0)"] == "nobody"


async def test_list_style_multi_choice_and_blind(world):
    bot, cog, guild = world
    p, _ = await make_poll(cog, guild, multi=True, blind=True, style="list")
    view = guild.channel.sent[0].view
    select = inner(view.children[0])
    assert isinstance(select, discord.ui.Select) and select.max_values == 3
    await cog.vote_list(Interaction(guild, guild.members[2]), p.id, [0, 2])
    await cog.vote_list(Interaction(guild, guild.members[3]), p.id, [2])
    assert pl.tally(p.options, cog.votes(p.id))[0] == [1, 0, 2]
    card = cog.poll_card(cog.poll(p.id), guild)
    assert "%" not in card.description and "Hidden until the poll ends" in str([f.value for f in card.fields])
    peek = Interaction(guild, guild.members[4])
    await cog.show_voters(peek, p.id)
    assert "hidden" in peek.response.messages[0][0]


async def test_closing_posts_results_with_a_comment_and_stops_votes(world):
    bot, cog, guild = world
    p, _ = await make_poll(cog, guild)
    for uid, option in ((2, 0), (3, 0), (4, 2)):
        await cog.vote_button(Interaction(guild, guild.members[uid]), p.id, option)
    bot.storage._write("UPDATE polls SET ends_at = ? WHERE id = ?", (time.time() - 1, p.id))
    await cog.closer.coro(cog)
    mid, content, kw = guild.channel.replies[-1]
    assert mid == guild.channel.sent[0].id and content == "<@1>"
    assert "Pepperoni wins (67%)" in kw["embed"].title and kw["embed"].description.count("*") >= 2
    assert cog.poll(p.id).closed and guild.channel.edits[-1][1]["view"] is None
    late = Interaction(guild, guild.members[5])
    await cog.vote_button(late, p.id, 1)
    assert "ended" in late.response.messages[0][0]
    await cog.closer.coro(cog)
    assert len(guild.channel.replies) == 1                                        # closed only once


async def test_bad_forms_explain_what_to_fix(world):
    bot, cog, guild = world
    it = Interaction(guild, guild.members[1])
    await cog.create_poll(it, "Q?", "only one", "1h", anonymous=False, multi=False, blind=False, style="buttons")
    assert "at least 2" in it.response.messages[0][1]["embed"].description and not guild.channel.sent


# Question box

async def test_question_answers_are_private_and_counted_for_mods(world):
    bot, cog, guild = world
    it = Interaction(guild, guild.members[1])
    await cog.create_question(it, "Who would most likely adopt a dog?", "1d", "names")
    q = cog.question(1)
    card = guild.channel.sent[0].embed
    assert card.title == "Who would most likely adopt a dog?" and "Only admins and mods" in card.description
    assert labels_of(guild.channel.sent[0].view) == ["Answer", "View answers (mods)"]
    for uid, answer in ((2, "Zara"), (3, "lucia"), (4, "<@3>"), (5, "zara obviously")):
        press = Interaction(guild, guild.members[uid])
        await cog.open_answer(press, q.id)
        assert isinstance(press.response.modal, pl.AnswerForm)
        await cog.save_answer(Interaction(guild, guild.members[uid]), q.id, answer)
    edit = Interaction(guild, guild.members[2])
    await cog.open_answer(edit, q.id)
    assert edit.response.modal.answer.default == "Zara"                          # their answer, to change
    snoop = Interaction(guild, guild.members[2])
    await cog.show_answers(snoop, q.id)
    assert "Only admins and mods" in snoop.response.messages[0][0]
    mod = Interaction(guild, guild.members[1])
    await cog.show_answers(mod, q.id)
    content, kw = mod.response.messages[0]
    assert kw["ephemeral"] and "4 answers" in kw["embed"].description
    assert "3×` Zara" in kw["embed"].description and "**Mo**: Zara" in kw["embed"].fields[0].value
    await cog.post_question_results(Interaction(guild, guild.members[1]), q.id)
    posted = guild.channel.sent[-1].embed
    assert "🥇 **Zara** · 3" in posted.description and "Mo" not in posted.description   # who said what stays private


async def test_closed_questions_stop_taking_answers(world):
    bot, cog, guild = world
    await cog.create_question(Interaction(guild, guild.members[1]), "Best duo?", "", "text")
    q = cog.question(1)
    assert q.ends_at is None
    await cog.close_question(q)
    late = Interaction(guild, guild.members[2])
    await cog.open_answer(late, q.id)
    assert "closed" in late.response.messages[0][0] and late.response.modal is None
    assert labels_of(guild.channel.edits[-1][1]["view"]) == ["View answers (mods)"]


def test_forms_fit_discord():
    cog = NS()
    assert len(pl.PollForm(cog).children) == 5 and len(pl.QuestionForm(cog).children) == 4
