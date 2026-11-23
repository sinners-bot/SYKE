"""SYKE's life of its own: bonds, the father, mood, promises, diary, and what the AI is told."""

import time
import types
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
import pytest

from syke import persona_core as core
from syke.bot import Syke
from syke.config import load_settings
from syke.persona import Persona

NS = types.SimpleNamespace
GID, DAD, MO, ZARA = 42, 1342786189576634398, 7, 8
VIENNA = ZoneInfo("Europe/Vienna")


def person(uid, name):
    return NS(id=uid, display_name=name, name=name, bot=False, guild_permissions=NS(manage_guild=False))


class Guild:
    id = GID
    name = "Neon Desert"

    def __init__(self):
        self.members = {DAD: person(DAD, "Ivan"), MO: person(MO, "Mo"), ZARA: person(ZARA, "Zara")}

    def get_member(self, uid):
        return self.members.get(uid)

    def get_channel(self, cid):
        return None


class Channel:
    id = 10


class Msg:
    _next = 100

    def __init__(self, guild, author, text, mentions=()):
        Msg._next += 1
        self.id, self.guild, self.author, self.content = Msg._next, guild, author, text
        self.channel, self.mentions, self.reference = Channel(), list(mentions), None
        self.replies = []

    async def reply(self, text=None, **kw):
        self.replies.append(text)
        return NS(content=text)


class Ctx:
    def __init__(self, guild, author):
        self.guild, self.author, self.prefix, self.clean_prefix, self.replies = guild, author, "$", "$", []

    async def reply(self, content=None, **kw):
        self.replies.append(kw.get("embed") or content)

    def typing(self):
        class T:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False
        return T()


@pytest.fixture()
def world(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "p.db"), ai_provider="none", web_enabled=False,
                       owner_ids=frozenset({DAD}))
    bot = Syke(settings)
    bot._connection.user = NS(id=999, mention="<@999>", name="SYKE")
    bot.storage.set_timezone(GID, "Europe/Vienna") if hasattr(bot.storage, "set_timezone") else None
    persona = Persona(bot)
    guild = Guild()
    return bot, persona, guild


# Pure rules

def test_bonds_move_fade_and_label():
    insult = core.Judgement("insult", 2, "none", "called you trash")
    assert core.bond_change(0, insult) == -14
    assert core.bond_change(-40, core.Judgement("apology")) == 20          # sorry wins half back
    assert core.bond_change(0, core.Judgement("neutral", 1, "insult")) == -10  # dissing dad costs you
    assert core.faded(-40, 0, core.BOND_HALF_LIFE) == pytest.approx(-20)
    assert core.bond_label(75)[0] == "bestie" and core.bond_label(-20)[0] == "grudge"
    assert core.bond_label(0)[0] == "neutral" and core.bond_label(-99)[0] == "nemesis"


def test_mood_fades_back_to_calm():
    mood = core.Mood()
    mood.nudge(-0.8, 1000, "Mo called you trash")
    value, _ = mood.now(1000)
    assert core.mood_label(value, 0.3, 15) == "furious"
    later, _ = mood.now(1000 + 3 * core.MOOD_HALF_LIFE)
    assert core.mood_label(later, 0.3, 15) == "chill" and mood.reasons == ["Mo called you trash"]
    assert core.mood_label(0, 0.05, 3) == "sleepy" and core.mood_label(0, 0.05, 15) == "bored"
    assert core.mood_label(0, 0.9, 15) == "wired"


@pytest.mark.parametrize("text, due", [
    ("ill play tonight", "23:59"), ("I will hop on in 10 mins", "15:30"), ("imma be on later", "20:00"),
    ("ok ill join vc at 8", "20:20"), ("i swear ill pay you back on friday", "Fri"), ("i’ll be there in a bit", "16:00"),
])
def test_finds_promises_with_deadlines(text, due):
    now = datetime(2026, 11, 23, 15, 0, tzinfo=VIENNA)        # a Monday
    found = core.find_promise(text, now)
    assert found is not None and due in found.due.strftime("%a %H:%M")


@pytest.mark.parametrize("text", ["gonna sleep", "its gonna rain tonight", "i wont play tonight",
                                  "will you play tonight?", "im ill today", "i might play tonight", "i'll see later"])
def test_ignores_things_that_arent_promises(text):
    assert core.find_promise(text, datetime(2026, 11, 23, 15, 0, tzinfo=VIENNA)) is None


def test_state_block_knows_the_father():
    dad = core.state_block(mood="happy", mood_reasons=[], target_name="Echo", target_is_father=True, bond=None,
                           events=[], father_names=["Echo"], father_in_chat=True, promises=[], now=0)
    assert "FATHER and CREATOR" in dad and "roast him, insult him" in dad and "call him Echo" in dad
    other = core.state_block(mood="grumpy", mood_reasons=["Mo called you trash"], target_name="Mo",
                             target_is_father=False, bond=-30, events=[("insult", "called you trash", 0)],
                             father_names=["Echo"], father_in_chat=False,
                             promises=[("broken", "i'll play tonight", 0)], now=3600)
    assert "Echo is your father and creator" in other and "Only you get to roast" in other and "grudge" in other
    assert "called you trash (1h ago)" in other and "they didn't do it" in other


# The cog

async def test_insults_and_apologies_change_the_bond_and_mood(world):
    bot, persona, guild = world
    mo = guild.members[MO]
    await persona.observe(Msg(guild, mo, "syke you are useless and dumb"))
    assert persona.bond(GID, MO) == pytest.approx(-14)
    assert persona.mood_now(GID)[0] in ("grumpy", "irritable")
    assert persona.events(GID, MO)[0][1] == 'called you "useless"'
    await persona.observe(Msg(guild, mo, "sorry syke"))
    assert persona.bond(GID, MO) == pytest.approx(-7)
    await persona.observe(Msg(guild, mo, "useless game honestly"))      # not talking to SYKE: no change
    assert persona.bond(GID, MO) == pytest.approx(-7)


async def test_the_father_is_loved_and_defended(world):
    bot, persona, guild = world
    dad, zara = guild.members[DAD], guild.members[ZARA]
    await persona.observe(Msg(guild, dad, "syke you're trash lol"))
    assert persona.bond(GID, DAD) == 100                                 # dad can't lose the bond
    await persona.observe(Msg(guild, zara, "ivan is a clown"))
    assert persona.bond(GID, ZARA) < 0 and "father" in persona.events(GID, ZARA)[0][1]
    state = persona.prompt_state(guild, dad)
    assert "FATHER and CREATOR" in state and "talking to Echo" in state and "Ivan" not in state
    assert "Echo is your father" in persona.prompt_state(guild, zara)
    await persona.observe(Msg(guild, zara, "echo is trash at this game"))   # his new name counts too
    assert len([e for e in persona.events(GID, ZARA, 10) if "father" in e[1]]) == 2
    assert bot.speaker_name(dad) == "Echo" and bot.speaker_name(zara) == "Zara"


async def test_broken_promise_gets_called_out_in_yap_channels(world, monkeypatch):
    bot, persona, guild = world
    mo = guild.members[MO]
    bot.storage.set_yap_channel(GID, 10, True) if hasattr(bot.storage, "set_yap_channel") else \
        monkeypatch.setattr(bot.storage, "yap_channels", lambda gid: {10})
    await persona.observe(Msg(guild, mo, "ill play in 5 mins"))
    rows = persona.promises(GID, MO)
    assert rows[0][1] == "open"
    bot.storage._write("UPDATE persona_promises SET due = ? WHERE id = ?", (time.time() - 60, rows[0][0]))
    later = Msg(guild, mo, "anyway what's up")
    await persona.observe(later)
    assert later.replies and "play in 5 mins" in later.replies[0]
    assert persona.promises(GID, MO)[0][1] == "broken" and persona.bond(GID, MO) == pytest.approx(-3)


async def test_joining_voice_keeps_a_play_promise(world):
    bot, persona, guild = world
    mo = guild.members[MO]
    mo.guild = guild
    await persona.observe(Msg(guild, mo, "i'll hop on vc in 20 mins"))
    await persona.on_voice_state_update(mo, NS(channel=None), NS(channel=NS(name="Lounge")))
    assert persona.promises(GID, MO)[0][1] == "kept" and persona.bond(GID, MO) == pytest.approx(4)


async def test_commands_show_feelings_and_diary(world):
    bot, persona, guild = world
    mo, zara = guild.members[MO], guild.members[ZARA]
    await persona.observe(Msg(guild, mo, "syke you're trash"))
    for _ in range(3):
        await persona.observe(Msg(guild, zara, "thanks syke, love you"))
    ctx = Ctx(guild, mo)
    await persona.bond_cmd.callback(persona, ctx, None)
    assert "Neutral" in ctx.replies[-1].description or "Grudge" in ctx.replies[-1].description
    assert "called you" in ctx.replies[-1].description
    await persona.bond_cmd.callback(persona, ctx, guild.members[DAD])
    assert "Echo, my father and creator" in ctx.replies[-1].description and "roast him" in ctx.replies[-1].description
    await persona.mood_cmd.callback(persona, ctx)
    assert "Made by my dad, Echo" in ctx.replies[-1].description
    await persona.favourites_cmd.callback(persona, ctx)
    fields = {f.name: f.value for f in ctx.replies[-1].fields}
    assert "Echo" in fields["👨‍👧 Father & creator"] and "Zara" in fields["💖 Favourites"]
    today = persona.local_now(GID).date()
    text = await persona.write_diary(guild, today, force=True)
    assert text.startswith("Dear diary") and "Zara" in text and "Mo" in text
    await persona.diary.callback(persona, ctx, when="today")
    assert "diary" in ctx.replies[-1].title.lower()


async def test_forgive_needs_dad_or_a_manager(world):
    bot, persona, guild = world
    mo, zara = guild.members[MO], guild.members[ZARA]
    await persona.observe(Msg(guild, mo, "syke shut up you're trash"))
    await persona.forgive_cmd.callback(persona, Ctx(guild, zara), mo)
    assert persona.bond(GID, MO) < 0
    await persona.forgive_cmd.callback(persona, Ctx(guild, guild.members[DAD]), mo)
    assert persona.bond(GID, MO) == pytest.approx(0)
