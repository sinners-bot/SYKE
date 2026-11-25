"""SYKE as a regular: chains, pile-ons, hot takes, jealousy, server events, double-texting, wrong replies,
nicknames, new slang and $freaky."""

import asyncio
import datetime as dt
import time
import types
from dataclasses import replace

import pytest

import syke.bot as botmod
import syke.regular as reg
from syke import ai
from syke.freaky import freaky_rules, says_under_18
from syke.human import Human
from syke.regular import Regular, chain_key
from syke.yap import Turn, trending_words

GID = 42
NS = types.SimpleNamespace


class Channel:
    def __init__(self, cid=10):
        self.id, self.sent = cid, []

    async def send(self, content=None, **kw):
        self.sent.append(content if content is not None else kw.get("stickers"))
        return NS(content=content)

    def history(self, limit=None, before=None):
        async def gen():
            if False:
                yield None
        return gen()

    def typing(self):
        class T:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return T()


def person(uid, name, bot=False):
    return NS(id=uid, display_name=name, name=name.lower(), bot=bot)


def msg(channel, author, content="", mid=None, reference=None, stickers=(), mentions=()):
    m = NS(id=mid or int(time.time_ns() % 10**9), channel=channel, author=author, content=content,
           guild=NS(id=GID, emojis=[], stickers=[]), reference=reference, stickers=list(stickers),
           mentions=list(mentions), attachments=[], reactions=[], replies=[])

    async def add_reaction(e):
        m.reactions.append(e)

    async def reply(text=None, **kw):
        m.replies.append(text)
        return NS(content=text)

    m.add_reaction, m.reply = add_reaction, reply
    return m


@pytest.fixture()
def regular(cog, monkeypatch):
    bot = cog.bot
    bot._connection.user = NS(id=999, name="SYKE", display_name="SYKE", bot=True)
    bot.storage.set_yap(GID, 10, True)
    monkeypatch.setattr(reg.random, "random", lambda: 0.0)          # every "sometimes" happens in these tests
    said = []

    async def write_moment(settings, moment, *a, **k):
        said.append(moment)
        return "line"

    monkeypatch.setattr(ai, "write_moment", write_moment)
    r = Regular(bot)
    r.said = said
    return r


# Chains

async def test_joins_a_chain_once_and_complains_when_it_breaks(regular):
    ch = Channel()
    for i, who in enumerate(("Mo", "Zara", "Cy")):
        await regular.on_message(msg(ch, person(i + 1, who), "W"))
    assert ch.sent == ["W"]
    await regular.on_message(msg(ch, person(4, "Bo"), "W"))
    assert ch.sent == ["W"]                                          # joined already
    regular._cool.clear()
    await regular.on_message(msg(ch, person(5, "Ren"), "nah"))
    assert ch.sent[-1] in [l.format(name="Ren") for l in reg.CHAIN_BREAK_LINES]


async def test_two_people_or_links_are_not_a_chain(regular):
    ch = Channel()
    await regular.on_message(msg(ch, person(1, "Mo"), "W"))
    await regular.on_message(msg(ch, person(1, "Mo"), "W"))
    await regular.on_message(msg(ch, person(2, "Zara"), "W"))
    assert ch.sent == []                                             # only two different people
    assert chain_key(msg(ch, person(1, "Mo"), "https://x.com")) is None
    assert chain_key(msg(ch, person(1, "Mo"), "<@5> lol")) is None
    assert chain_key(msg(ch, person(1, "Mo"), "", stickers=[NS(id=7)])) == "sticker:7"
    from syke.slurs import SLURS
    word = next(iter(SLURS.values())).split("|")[0].strip("()\\b?:")
    if word.isalpha():
        assert chain_key(msg(ch, person(1, "Mo"), word)) is None       # never repeats a slur


async def test_silent_servers_dont_play_along(regular):
    regular.bot.storage.update_config(GID, "yap", level=0)
    ch = Channel()
    for i in range(4):
        await regular.on_message(msg(ch, person(i + 1, f"p{i}"), "W"))
    assert ch.sent == []


# Pile-ons, hot takes, jealousy

async def test_pile_on_after_three_people_reply(regular, monkeypatch):
    ch = Channel()
    target = msg(ch, person(1, "Mo"), "pineapple pizza is the best food ever", mid=500)
    monkeypatch.setattr(reg.random, "choice", lambda seq: seq[0])
    for i, text in enumerate(("ratio", "nah you're tweaking", "worst take of the year")):
        await regular.on_message(msg(ch, person(10 + i, f"p{i}"), text, reference=NS(resolved=target)))
    assert target.reactions == ["💀"] or target.replies == ["line"]


async def test_no_pile_on_when_they_are_hurting(regular):
    ch = Channel()
    target = msg(ch, person(1, "Mo"), "my dog died this morning", mid=501)
    for i, text in enumerate(("lol", "bro", "ratio")):
        await regular.on_message(msg(ch, person(10 + i, f"p{i}"), text, reference=NS(resolved=target)))
    assert target.reactions == [] and target.replies == []


async def test_hot_takes_and_jealousy(regular):
    ch = Channel()
    take = msg(ch, person(1, "Mo"), "unpopular opinion: sushi is mid")
    await regular.on_message(take)
    assert take.replies == ["line"] and "hot take" in regular.said[-1]
    gpt = msg(ch, person(2, "Zara"), "i just asked chatgpt instead")
    await regular.on_message(gpt)
    assert gpt.replies == ["line"] and "chatgpt" in regular.said[-1].lower()
    again = msg(ch, person(3, "Cy"), "claude said the same")
    await regular.on_message(again)
    assert again.replies == []                                       # 30 minute cooldown


async def test_other_bot_commands_make_him_petty(regular):
    ch = Channel()
    m = msg(ch, person(55, "MEE6", bot=True), "Here's your rank card")
    m.interaction_metadata = NS(user=person(2, "Zara"))
    await regular.on_message(m)
    assert ch.sent == ["line"] and "MEE6" in regular.said[-1]


# Server events

async def test_notices_new_emojis_bans_and_pins(regular):
    guild = NS(id=GID, get_channel=lambda cid: Channel(cid))
    sent = []

    async def say(g, where, moment, reply_to=None):
        sent.append(moment)
        return True

    regular.say = say
    emoji = NS(id=77, name="sigma")
    emoji.__str__ = lambda self=None: "<:sigma:77>"
    await regular.on_guild_emojis_update(guild, [], [NS(id=77, name="sigma")])
    assert "new emoji" in sent[-1] and "sigma" in sent[-1]
    regular._cool.clear()
    await regular.on_member_ban(guild, person(5, "Spammer"))
    assert "banned" in sent[-1] and "Spammer" not in sent[-1]
    regular._cool.clear()
    pinned = msg(Channel(), person(1, "Mo"), "i will never lose a 1v1")

    async def pins():
        return [pinned]

    ch = Channel()
    ch.guild, ch.pins = guild, pins
    await regular.on_guild_channel_pins_update(ch, dt.datetime.now(dt.timezone.utc))
    assert "pinned Mo's message" in sent[-1]
    regular._cool.clear()
    await regular.on_guild_channel_pins_update(ch, None)            # an unpin: nothing
    assert len(sent) == 3


# Double-texting and wrong replies

async def test_double_texts_when_his_question_is_ignored(regular):
    ch = Channel()
    mo = person(1, "Mo")
    regular.asked(ch, mo, "wait who won?", now=1000)
    assert await regular.follow_up_ignored(now=1000 + 60) == 0     # too soon
    assert await regular.follow_up_ignored(now=1000 + 7 * 60) == 1
    assert ch.sent[-1] in reg.DOUBLE_LINES
    regular.asked(ch, mo, "and then?", now=2000)
    await regular.on_message(msg(ch, mo, "my bad i was eating"))     # they answered
    assert await regular.follow_up_ignored(now=2000 + 7 * 60) == 0
    regular.asked(ch, mo, "ok cool", now=3000)                       # not a question
    assert (ch.id, mo.id) not in regular._asked


async def test_wrong_reply_then_mb(cog, monkeypatch):
    bot = cog.bot
    bot._voices[GID] = (time.monotonic(), botmod.Voice(chain=None, emojis=[], gifs=[]))
    monkeypatch.setattr(botmod, "WRONG_REPLY", 1.0)
    ch = Channel()
    other = msg(ch, person(2, "Zara"), "random stuff", mid=1)

    def history(limit=None, before=None):
        async def gen():
            yield other
        return gen()

    ch.history = history
    target = msg(ch, person(1, "Mo"), "syke whats good", mid=2)
    await bot.deliver_turn(target, {"reply": "nothing much wbu"})
    assert other.replies == ["nothing much wbu"]
    assert target.replies and "wrong reply" in target.replies[0] or "that was for you" in target.replies[0]


# Nicknames, slang and how freaky

def test_nicknames_stick_once_given(cog):
    bot = cog.bot
    mo = person(7, "Mo")
    bot.remember_turn(GID, mo, "i main jett", {"reply": "jett main spotted", "nickname": "jett main"})
    bot.remember_turn(GID, mo, "crocs", {"reply": "crocs king", "nickname": "crocs king"})
    assert bot.storage.memories(GID, 7, "nick") == [("jett main", pytest.approx(time.time(), abs=5))]
    assert "Your nickname for Mo: jett main" in bot.memory_text(GID, mo)


def test_new_slang_is_what_people_just_started_saying():
    now = 100 * 86400.0
    rows = [(f"that is so gyatt fr {i}", i % 5, now - 3600 * i) for i in range(10)]        # new, 5 people
    rows += [("pizza tonight", i % 6, now - 86400 * (5 + i % 20)) for i in range(60)]       # always said
    rows += [("pizza tonight", i % 6, now - 3600 * i) for i in range(8)]
    rows += [("skibidi lol", 1, now - 3600)] * 8                                           # one person only
    assert trending_words(rows, now, botmod.COMMON_WORDS) == ["gyatt"]


def test_freaky_rules_by_place_and_person():
    assert freaky_rules(0, True, False) == ""
    assert "FREAKY 1/2" in freaky_rules(2, False, False)              # level 2 outside 18+ channels acts as 1
    assert "FREAKY 2/2" in freaky_rules(2, True, False)
    assert "UNDER 18" in freaky_rules(2, True, True) and "FREAKY 2/2" not in freaky_rules(2, True, True)
    assert "no explicit sex" in freaky_rules(1, False, False)
    assert says_under_18("im 15") and not says_under_18("im 15 minutes away") and not says_under_18("im 19")


async def test_freaky_command_and_minors(cog):
    bot = cog.bot
    human = Human(bot)
    said = []

    async def reply(content=None, **kw):
        said.append(content or kw["embed"].title)

    perms = NS(manage_guild=False, administrator=False)
    ctx = NS(guild=NS(id=GID), reply=reply, clean_prefix="$", author=NS(id=5, guild_permissions=perms))
    await Human.freaky.callback(human, ctx, "2")
    assert "Only managers" in said[-1] and bot.freaky_level(GID) == 0
    perms.manage_guild = True
    await Human.freaky.callback(human, ctx, "2")
    assert bot.freaky_level(GID) == 2 and "age-restricted" in said[-1]
    adult = NS(is_nsfw=lambda: True)
    assert "FREAKY 2/2" in bot.freaky_text(GID, adult, 7)
    bot.note_age(NS(content="lol im 15 btw", author=NS(id=7)))
    assert bot.storage.is_minor(7) and "UNDER 18" in bot.freaky_text(GID, adult, 7)
    assert "FREAKY 2/2" in bot.freaky_text(GID, adult, 8)            # only that person


async def test_reply_prompt_asks_back_and_carries_freaky(cog, monkeypatch):
    bot = cog.bot
    bot.settings = replace(bot.settings, ai_provider="openai", openai_api_key="k")
    bot._voices[GID] = (time.monotonic(), botmod.Voice(chain=None, emojis=[], gifs=[]))
    bot.storage.update_config(GID, "freaky", level=1)
    bot.storage.add_corpus(GID, [(1, 10, 100, "who up rn"), (2, 10, 101, "this chat is cooked")])
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None, images=None):
        seen.update(system=system, user=user)
        return '{"reply": "you up?", "nickname": "night owl"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    ch = Channel()
    m = msg(ch, person(7, "Mo"), "syke you're kinda cute ngl")
    turn = await bot.build_yap(m, target="you're kinda cute ngl")
    assert turn["nickname"] == "night owl"
    assert "ask something back" in seen["system"] and "FREAKY 1/2" in seen["user"]


def test_full_freaky_in_dms_only_after_discords_18_check(cog):
    bot = cog.bot
    bot.storage.update_config(GID, "freaky", level=2)
    assert "FREAKY 1/2" in bot.freaky_text(GID, None, 7)              # a DM, no 18+ check yet: level 1
    bot.note_age(NS(content="hey", author=NS(id=7), guild=NS(id=GID), channel=NS(id=3, is_nsfw=lambda: True)))
    dm = bot.freaky_text(GID, None, 7)
    assert "private DM" in dm and "if they're flirting" in dm and "never explicit" in dm
    bot.note_age(NS(content="im 16 tho", author=NS(id=7), guild=None, channel=NS(id=4)))
    assert "UNDER 18" in bot.freaky_text(GID, None, 7)                # said they're 16: nothing, ever
    bot.storage.update_config(GID, "freaky", level=1)
    bot.note_age(NS(content="hi", author=NS(id=8), guild=NS(id=GID), channel=NS(id=3, is_nsfw=lambda: True)))
    assert "FREAKY 1/2" in bot.freaky_text(GID, None, 8)              # server isn't on level 2


# Roles

class Role:
    def __init__(self, name, position, managed=False, default=False, booster=False):
        self.name, self.position, self.managed = name, position, managed
        self._default, self._booster = default, booster

    def is_default(self):
        return self._default

    def is_premium_subscriber(self):
        return self._booster


async def test_reply_prompt_knows_peoples_roles(cog, monkeypatch):
    bot = cog.bot
    everyone = Role("@everyone", 0, default=True)
    mo = NS(id=7, display_name="Mo", name="mo", bot=False,
            roles=[everyone, Role("Female", 5), Role("Gamer", 3), Role("──── Games ────", 4),
                   Role("MEE6", 9, managed=True), Role("Server Booster", 8, managed=True, booster=True),
                   Role("SYKE · contaminated", 1)])
    zara = NS(id=8, display_name="Zara", name="zara", bot=False, roles=[everyone, Role("Male", 2), Role("Mod", 6)])
    guild = NS(id=GID, get_member=lambda uid: {7: mo, 8: zara}.get(uid))
    mo.guild = guild
    assert bot.role_names(mo) == ["Server Booster", "Female", "Gamer"]
    text = await bot.roles_text(guild, mo, [Turn("Zara", "lol", "", 8)])
    assert text == "Mo: Server Booster, Female, Gamer\nZara: Mod, Male"

    bot.settings = replace(bot.settings, ai_provider="openai", openai_api_key="k")
    bot._voices[GID] = (time.monotonic(), botmod.Voice(chain=None, emojis=[], gifs=[]))
    bot.storage.add_corpus(GID, [(1, 10, 100, "who up rn"), (2, 10, 101, "this chat is cooked")])
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None, images=None):
        seen.update(system=system, user=user)
        return '{"reply": "sis what"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    m = msg(Channel(), mo, "syke what do you think")
    m.guild = guild
    await bot.build_yap(m, target="what do you think")
    assert "ROLES:\nMo: Server Booster, Female, Gamer" in seen["user"]
    assert "she/her" in seen["system"] and "Never mock anyone for a gender" in seen["system"]


async def test_dm_roles_come_from_the_home_server(cog, monkeypatch):
    bot = cog.bot
    fetched = []

    async def fetch_member(uid):
        fetched.append(uid)
        return NS(id=uid, roles=[Role("@everyone", 0, default=True), Role("Female", 3)])

    guild = NS(id=GID, get_member=lambda uid: None, fetch_member=fetch_member)
    dm_author = NS(id=7, display_name="Mo", name="mo", bot=False)
    assert await bot.roles_text(guild, dm_author) == "Mo: Female"
    assert await bot.roles_text(guild, dm_author) == "Mo: Female" and fetched == [7]   # asked Discord once
