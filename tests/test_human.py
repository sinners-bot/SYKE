"""SYKE as a regular: edge level, texting habits, memory of people, noticing people and starting chats."""

import asyncio
import time
import types
from dataclasses import replace

import pytest

import syke.bot as botmod
from syke import ai, edge
from syke.human import Human, fact_worthy
from syke.yap import Turn, Voice, make_typo

GID = 42


def _voice(bot):
    bot._voices[GID] = (time.monotonic(), Voice(chain=None, emojis=[], gifs=[]))


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _channel(cid=10, history=()):
    sent = []

    async def send(content=None, **kwargs):
        msg = _sent(content)
        sent.append(msg)
        return msg

    def hist(limit=None, before=None):
        async def gen():
            for m in history:
                yield m
        return gen()

    return types.SimpleNamespace(id=cid, send=send, sent=sent, typing=lambda: _Typing(), history=hist)


def _sent(content):
    msg = types.SimpleNamespace(content=content, id=500, edited=None, deleted=False)

    async def edit(content=None, **kwargs):
        msg.edited = content

    async def delete():
        msg.deleted = True

    msg.edit, msg.delete = edit, delete
    return msg


def _message(channel, content="hi", author_id=7, name="Ada", mid=99, guild=None):
    replies, reactions = [], []

    async def reply(content=None, **kwargs):
        msg = _sent(content)
        replies.append(msg)
        return msg

    async def add_reaction(emoji):
        reactions.append(emoji)

    guild = guild or types.SimpleNamespace(id=GID, emojis=[types.SimpleNamespace(id=111)], stickers=[],
                                           get_channel=lambda cid: channel)
    return types.SimpleNamespace(id=mid, guild=guild, channel=channel, content=content, mentions=[],
                                 author=types.SimpleNamespace(id=author_id, display_name=name, name=name.lower(),
                                                              bot=False),
                                 reference=None, attachments=[], stickers=[],
                                 reply=reply, replies=replies, add_reaction=add_reaction, reactions=reactions)


def _ai_on(bot):
    bot.settings = replace(bot.settings, ai_provider="openai", openai_api_key="k")


# Edge

def test_upset_people_get_kindness_not_jokes():
    assert edge.is_upset("my grandma passed away last night")
    assert edge.is_upset("i lost my job today") and edge.is_upset("i want to die")
    assert not edge.is_upset("im dead 😭😭") and not edge.is_upset("this killed me lmao")
    assert not edge.is_upset("i failed my exam again")
    rules = edge.edge_rules(3, "my dog died this morning")
    assert "GENUINELY UPSET" in rules and "EDGE 3" not in rules
    assert "crisis line" in edge.edge_rules(3, "i want to kill myself")
    assert "EDGE 3/3" in edge.edge_rules(3) and "no slurs" in edge.edge_rules(3)
    assert edge.edge_rules(0).startswith("HARD LINES")


async def test_edge_command_only_managers_change_it(cog):
    bot = cog.bot
    human = Human(bot)
    said = []

    async def reply(content=None, **kwargs):
        said.append(content or kwargs["embed"].title)

    perms = types.SimpleNamespace(manage_guild=False, administrator=False)
    ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=GID), reply=reply, clean_prefix="$",
                                author=types.SimpleNamespace(guild_permissions=perms))
    await Human.edge.callback(human, ctx, "3")
    assert "Only managers" in said[-1] and bot.edge_level(GID) == 0
    perms.manage_guild = True
    await Human.edge.callback(human, ctx, "3")
    assert bot.edge_level(GID) == 3 and "Unhinged" in said[-1]
    await Human.edge.callback(human, ctx, "9")
    assert "0 to 3" in said[-1]
    await Human.edge.callback(human, ctx, None)
    assert "3 · Unhinged" in said[-1]


async def test_reply_prompt_carries_edge_and_memory(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    _voice(bot)
    bot.storage.update_config(GID, "edge", level=2)
    bot.storage.add_memory(GID, 7, "fact", "has an exam on Friday", "how'd the exam go", time.time() + 86400)
    bot.storage.add_memory(GID, 7, "bit", "calls everyone 'chief'")
    bot.storage.add_memory(GID, 7, "talk", "Ada: ur music is trash → you: says the jett main", now=time.time() - 86400)
    bot.storage.add_corpus(GID, [(1, 10, 100, "exams are pain honestly"), (2, 10, 101, "friday is the worst day")])
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None):
        seen["system"], seen["user"] = system, user
        return ('{"read": "x", "reply": "you gonna fail anyway chief", "reply2": "jk good luck", '
                '"react": "💀", "remember": {"fact": "studies at night", "followup": "", "days": 2}, "bit": ""}')

    monkeypatch.setattr(ai, "_call_openai", fake)
    msg = _message(_channel(), "syke my exam is friday im cooked")
    turn = await bot.build_yap(msg, target="my exam is friday im cooked")
    assert turn["reply"] == "you gonna fail anyway chief" and turn["reply2"] == "jk good luck" and turn["react"] == "💀"
    assert "EDGE 2/3" in seen["user"]
    assert "About Ada: has an exam on Friday" in seen["user"] and "Running bits with Ada: calls everyone 'chief'" in seen["user"]
    assert "Your earlier talks with Ada: (yesterday) Ada: ur music is trash" in seen["user"]
    assert '"reply2"' in seen["system"] and '"remember"' in seen["system"]


async def test_upset_message_switches_edge_off(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    _voice(bot)
    bot.storage.update_config(GID, "edge", level=3)
    bot.storage.add_corpus(GID, [(1, 10, 100, "dogs are the best honestly")])
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None):
        seen["user"] = user
        return '{"reply": "damn im really sorry bro, he was a good boy"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    await bot.build_yap(_message(_channel(), "my dog died this morning"), target="my dog died this morning")
    assert "GENUINELY UPSET" in seen["user"] and "EDGE 3" not in seen["user"]


# Texting habits

def test_typo_is_one_thumb_slip():
    for seed in range(30):
        import random
        out = make_typo("that is actually crazy bro", random.Random(seed))
        if out:
            wrong, right = out
            assert right in "that is actually crazy bro".split() and wrong != "that is actually crazy bro"
            assert len(wrong.split()) == 5
    assert make_typo("ok lol") is None


async def test_turn_with_reaction_and_second_message(cog, monkeypatch):
    bot = cog.bot
    _voice(bot)
    monkeypatch.setattr(botmod, "REACT_WITH_REPLY", 1.0)
    ch = _channel()
    msg = _message(ch)
    await bot.deliver_turn(msg, {"reply": "nah", "reply2": "that's actually crazy", "react": "👀"})
    assert msg.reactions == ["👀"]
    assert [r.content for r in msg.replies] == ["nah"] and [m.content for m in ch.sent] == ["that's actually crazy"]


async def test_react_only_and_bad_reactions(cog):
    bot = cog.bot
    _voice(bot)
    msg = _message(_channel())
    await bot.deliver_turn(msg, {"reply": "", "react": "😂"})
    assert msg.reactions == ["😂"] and not msg.replies
    msg = _message(_channel())
    await bot.deliver_turn(msg, {"reply": "", "react": "lol 😂 💀"})        # not one emoji: nothing
    await bot.deliver_turn(msg, {"reply": "", "react": "<:gone:12345>"})   # not this server's emoji
    assert not msg.reactions and not msg.replies
    assert bot.usable_reaction(msg.guild, "<:pepe:111>") == "<:pepe:111>"


async def test_typo_then_fix(cog, monkeypatch):
    bot = cog.bot
    _voice(bot)
    monkeypatch.setattr(botmod, "TYPO_CHANCE", 1.0)
    ch = _channel()
    msg = _message(ch)
    await bot.deliver_turn(msg, {"reply": "that is actually crazy bro"})
    fix = ch.sent[0].content
    assert fix.startswith("*") and fix[1:] in "that is actually crazy bro".split()
    assert msg.replies[0].content != "that is actually crazy bro"


async def test_rare_edit_and_nvm(cog, monkeypatch):
    bot = cog.bot
    _voice(bot)
    monkeypatch.setattr(botmod, "EDIT_INSTEAD", 1.0)
    ch = _channel()
    msg = _message(ch)
    await bot.deliver_turn(msg, {"reply": "nah", "reply2": "wait actually yeah"})
    assert msg.replies[0].edited == "nah\nwait actually yeah" and not ch.sent
    monkeypatch.setattr(botmod, "NVM_CHANCE", 1.0)
    ch = _channel()
    msg = _message(ch)
    await bot.deliver_turn(msg, {"reply": "pineapple pizza is elite"})
    assert msg.replies[0].deleted and [m.content for m in ch.sent] == ["nvm"]


async def test_random_yap_can_just_react(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    _voice(bot)
    bot.settings = replace(bot.settings, yap_chance=1.0)
    monkeypatch.setattr(botmod, "YAP_REACT_ONLY", 1.0)
    monkeypatch.setattr(botmod, "YAP_REACT_LATER", 0.0)
    bot.storage.set_yap(GID, 10, True)
    bot.storage.add_corpus(GID, [(1, 10, 100, "pizza is elite honestly"), (2, 10, 101, "who wants pizza fr")])

    async def fake(*a, **k):
        return '{"why": "x", "pick": 0, "react": "🍕"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    ch = _channel()
    msg = _message(ch, "pizza tonight?")
    await bot.maybe_yap(msg)
    assert msg.reactions == ["🍕"] and not ch.sent


# Memory

async def test_reply_remembers_the_talk_the_fact_and_the_bit(cog):
    bot = cog.bot
    author = types.SimpleNamespace(id=7, display_name="Ada", name="ada")
    bot.remember_turn(GID, author, "i got a new dog called milo", {
        "reply": "milo better be smarter than you", "reply2": "",
        "remember": {"fact": "got a new dog called Milo", "followup": "how's Milo", "days": 3},
        "bit": "milo is the real brains of the family"})
    assert bot.storage.memories(GID, 7, "fact")[0][0] == "got a new dog called Milo"
    assert bot.storage.memories(GID, 7, "bit")[0][0] == "milo is the real brains of the family"
    assert "Ada: i got a new dog called milo → you: milo better be smarter" in bot.storage.memories(GID, 7, "talk")[0][0]
    assert bot.storage.due_followup(GID, 7) is None                      # not yet
    later = time.time() + 4 * 86400
    memory_id, fact, followup = bot.storage.due_followup(GID, 7, later)
    assert followup == "how's Milo"
    bot.storage.mark_asked(memory_id)
    assert bot.storage.due_followup(GID, 7, later) is None
    bot.storage.set_optout(GID, 7, True)                                  # opting out forgets everything
    assert not bot.storage.memories(GID, 7, "fact")


def test_fact_hint_is_cheap_and_picky():
    assert fact_worthy("my exam is on friday im so cooked")
    assert fact_worthy("i got a new dog yesterday")
    assert not fact_worthy("lol")
    assert not fact_worthy("that guy is so funny honestly")
    assert not fact_worthy("my clip https://youtu.be/x from the match")


async def test_facts_from_chat_become_followups(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    human = Human(bot)
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None):
        seen["user"] = user
        return '{"facts": [{"who": 1, "fact": "has a job interview on Monday", "followup": "how did the interview go", "days": 2}, {"who": 9, "fact": "bad index"}]}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    human._facts[GID] = [(5, "Bo", "my brother has an exam lol"), (7, "Ada", "i have a job interview on monday im scared")]
    assert await human.flush_facts(GID) == 1
    assert "[1] Ada: i have a job interview on monday" in seen["user"] and "TODAY:" in seen["user"]
    assert bot.storage.memories(GID, 7, "fact")[0][0] == "has a job interview on Monday"


async def test_due_followup_is_asked_when_they_talk(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    _voice(bot)
    human = Human(bot)
    monkeypatch.setattr("syke.human.FOLLOWUP_CHANCE", 1.0)
    bot.storage.set_yap(GID, 10, True)
    bot.storage.add_memory(GID, 7, "fact", "has a job interview on Monday", "how did the interview go",
                           time.time() - 60)
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None):
        seen["user"] = user
        return '{"message": "yo how did the interview go"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    msg = _message(_channel(), "back from work finally")
    human._seen[(GID, 7)] = time.time() - 3600
    await human.on_message(msg)
    assert [r.content for r in msg.replies] == ["yo how did the interview go"]
    assert "they mentioned: has a job interview on Monday" in seen["user"]
    assert bot.storage.due_followup(GID, 7) is None                       # asked once
    msg2 = _message(_channel(), "anyway", mid=100)
    human._last_moment.clear()
    await human.on_message(msg2)
    assert not msg2.replies


async def test_welcome_back_and_birthday_once(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    _voice(bot)
    human = Human(bot)
    bot.storage.set_yap(GID, 10, True)
    moments = []

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None):
        moments.append(user.split("\n")[0])
        return '{"message": "look who crawled back"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    bot.storage.add_corpus(GID, [(1, 10, 7, "see you guys", time.time() - 30 * 86400)])
    msg = _message(_channel(), "yo whats up", mid=50)
    await human.on_message(msg)
    assert msg.replies and "first time in 30 days" in moments[-1]

    from datetime import datetime
    today = datetime.now(bot.tz_for(GID)).date()
    bot.storage.set_config(GID, "birthdays", {"people": {"8": [today.month, today.day]}})
    human._last_moment.clear()
    bday = _message(_channel(), "hey guys", author_id=8, name="Cy", mid=51)
    human._seen[(GID, 8)] = time.time() - 600
    await human.on_message(bday)
    assert bday.replies and "Cy's birthday today" in moments[-1]
    human._last_moment.clear()
    again = _message(_channel(), "thanks", author_id=8, name="Cy", mid=52)
    await human.on_message(again)
    assert not again.replies                                             # wished once


async def test_moments_skip_people_talking_to_syke_and_optouts(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    human = Human(bot)
    bot.storage.set_yap(GID, 10, True)
    bot.storage.add_corpus(GID, [(1, 10, 7, "bye", time.time() - 40 * 86400)])
    msg = _message(_channel(), "<@999> yo", mid=60)
    msg.mentions = [types.SimpleNamespace(id=999)]
    await human.on_message(msg)
    assert not msg.replies
    bot.storage.set_optout(GID, 9, True)
    out = _message(_channel(), "i have an exam friday honestly", author_id=9, mid=61)
    await human.on_message(out)
    assert not out.replies and not human._facts.get(GID)


async def test_conversation_starter_when_quiet(cog, monkeypatch):
    bot = cog.bot
    _ai_on(bot)
    _voice(bot)
    human = Human(bot)
    bot.storage.set_yap(GID, 10, True)
    monkeypatch.setattr("syke.human.OPENER_HOURS", range(0, 24))
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None):
        seen["user"] = user
        return '{"message": "who tryna hop on rn"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    ch = _channel()
    guild = types.SimpleNamespace(id=GID, get_channel=lambda cid: ch if cid == 10 else None)
    now = time.time()
    assert not await human.maybe_open(guild, now, chance=1.0)            # no activity known: dead channel
    human._activity[10].extend([now - 3 * 3600 + i for i in range(20)])
    assert not await human.maybe_open(guild, now - 3 * 3600 + 600, chance=1.0)   # quiet only 10 min
    assert await human.maybe_open(guild, now, chance=1.0)
    assert [m.content for m in ch.sent] == ["who tryna hop on rn"] and "quiet for" in seen["user"]
    assert not await human.maybe_open(guild, now + 3600, chance=1.0)      # not again so soon
    data = bot.storage.get_config(GID, "human_moments")
    assert data["opener_count"] == 1


# How talkative SYKE is

def test_talk_levels_parse_numbers_and_names():
    from syke.yap import talk_level
    assert talk_level("0") == 0 and talk_level("7") == 7 and talk_level("chill") == 3
    assert talk_level("MAX") == 7 and talk_level("off") == 0 and talk_level("default") == 4
    assert talk_level("9") is None and talk_level("loud") is None and talk_level(None) is None


async def test_talkative_command_sets_the_pace(cog):
    bot = cog.bot
    said = []

    async def reply(content=None, **kwargs):
        said.append(kwargs["embed"].title + " " + (kwargs["embed"].description or ""))

    perms = types.SimpleNamespace(manage_guild=False, administrator=False)
    ctx = types.SimpleNamespace(guild=types.SimpleNamespace(id=GID), reply=reply, prefix="$", clean_prefix="$",
                                author=types.SimpleNamespace(id=5, guild_permissions=perms), interaction=None)
    assert bot.yap_pace(GID) == (bot.settings.yap_chance, bot.settings.yap_cooldown, 2)   # Railway defaults
    await cog.talkative.callback(cog, ctx, "7")
    assert "Managers only" in said[-1] and bot.talk_level_of(GID) is None
    perms.manage_guild = True
    await cog.talkative.callback(cog, ctx, "quiet")
    assert bot.talk_level_of(GID) == 2 and bot.yap_pace(GID) == (0.07, 300, 1) and "2 · Quiet" in said[-1]
    await cog.talkative.callback(cog, ctx, "11")
    assert "Pick a level" in said[-1]
    await cog.talkative.callback(cog, ctx, None)
    assert "➜ **2 · Quiet**" in said[-1] and "7 · Nonstop" in said[-1]
    await cog.talkative.callback(cog, ctx, "0")
    assert bot.yap_pace(GID) == (0.0, 0, 0)


async def test_silent_never_yaps_and_nonstop_does(cog, monkeypatch):
    bot = cog.bot
    bot.storage.set_yap(GID, 10, True)
    bot.storage.add_corpus(GID, [(1, 10, 100, "pizza is elite honestly"), (2, 10, 101, "who wants pizza fr")])
    _voice(bot)
    calls = []

    async def fake_yap(message, reactions=None):
        calls.append(message.id)
        return None

    monkeypatch.setattr(bot, "contextual_yap", fake_yap)
    bot.storage.update_config(GID, "yap", level=0)
    for i in range(30):
        bot._last_yap.clear()
        await bot.maybe_yap(_message(_channel(), "pizza?", mid=i))
    assert not calls
    bot.storage.update_config(GID, "yap", level=7)
    monkeypatch.setattr("syke.bot.random.random", lambda: 0.5)          # under Nonstop's 80%
    await bot.maybe_yap(_message(_channel(), "pizza?", mid=100))
    assert calls == [100]
    await bot.maybe_yap(_message(_channel(), "pizza?", mid=101))       # 12s gap between yaps
    assert calls == [100]


async def test_starters_follow_the_talk_level(cog, monkeypatch):
    bot = cog.bot
    human = Human(bot)
    bot.storage.set_yap(GID, 10, True)
    monkeypatch.setattr("syke.human.OPENER_HOURS", range(0, 24))
    ch = _channel()
    guild = types.SimpleNamespace(id=GID, get_channel=lambda cid: ch)
    now = time.time()
    human._activity[10].extend([now - 2 * 3600 + i for i in range(20)])
    bot.storage.update_config(GID, "yap", level=0)
    assert human.opener_channel(guild, now) is None
    bot.storage.update_config(GID, "yap", level=7)
    assert human.opener_channel(guild, now) is ch
