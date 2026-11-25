"""SYKE's life of its own: bonds, the father, mood, promises, diary, and what the AI is told."""

import asyncio
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
    assert core.bond_change(0, insult) == -24
    assert core.bond_label(core.bond_change(0, core.Judgement("insult", 1)))[0] == "neutral"
    assert core.bond_label(core.bond_change(0, core.Judgement("insult", 2)))[0] == "grudge"   # one real insult
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
    assert core.mood_label(later, 0.3, 15) == "chill" and mood.recent_reasons(1000) == ["Mo called you trash"]
    assert mood.recent_reasons(1000 + core.REASON_TTL + 1) == []
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
    assert "FATHER and CREATOR" in dad and "roast him" in dad and "you DO it" in dad and "call him Echo" in dad
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
    assert persona.bond(GID, MO) == pytest.approx(-24)
    assert persona.mood_now(GID)[0] in ("grumpy", "irritable")
    assert persona.events(GID, MO)[0][1] == 'called you "useless"'
    await persona.observe(Msg(guild, mo, "sorry syke"))
    assert persona.bond(GID, MO) == pytest.approx(-12)
    await persona.observe(Msg(guild, mo, "useless game honestly"))      # not talking to SYKE: no change
    assert persona.bond(GID, MO) == pytest.approx(-12)


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
    for _ in range(2):
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


# Judging in context, and the chat's vibe

def test_only_confident_judgements_aimed_at_syke_count():
    from syke.persona import judgement_from
    assert judgement_from({"aimed_at_syke": True, "toward": "insult", "confidence": 0.9, "intensity": 2,
                           "note": "called you useless"}).note == "called you useless"
    assert judgement_from({"aimed_at_syke": True, "toward": "insult", "confidence": 0.5}) is None
    assert judgement_from({"aimed_at_syke": False, "toward": "insult", "confidence": 0.95}) is None
    dad = judgement_from({"aimed_at_syke": False, "toward": "neutral", "father": "insult", "confidence": 0.9})
    assert dad.toward == "neutral" and dad.father == "insult"
    assert judgement_from({"toward": "insult", "confidence": "lots"}) is None


async def test_the_ai_judge_sees_the_conversation(world, monkeypatch):
    bot, persona, guild = world
    bot.settings = replace(bot.settings, ai_provider="openai")
    seen = []

    async def judge(settings, system, user, temperature, timeout):
        seen.append(user)
        if "MESSAGE TO JUDGE" in user:
            return {"aimed_at_syke": False, "toward": "neutral", "confidence": 0.95}
        return {"vibe": "funny", "valence": 0.5, "energy": 0.6, "summary": "roasting Kai"}
    monkeypatch.setattr("syke.persona.ai._complete", judge)
    mo, zara = guild.members[MO], guild.members[ZARA]
    await persona.observe(Msg(guild, zara, "kai your haircut is a crime"))
    await persona.observe(Msg(guild, mo, "syke make him cry bitch"))
    for _ in range(3):
        await asyncio.sleep(0)
    judged = [u for u in seen if "MESSAGE TO JUDGE" in u][0]
    assert "Zara: kai your haircut is a crime" in judged and "Mo: syke make him cry bitch" in judged
    assert persona.bond(GID, MO) == 0 and persona.events(GID, MO) == []      # a request, not an insult


async def test_the_chat_vibe_drives_the_mood(world, monkeypatch):
    bot, persona, guild = world
    bot.settings = replace(bot.settings, ai_provider="openai")

    async def vibe(settings, system, user, temperature, timeout):
        return {"vibe": "funny", "valence": 0.6, "energy": 0.7, "summary": "everyone roasting Kai's haircut"}
    monkeypatch.setattr("syke.persona.ai._complete", vibe)
    for i in range(core_min_new()):
        await persona.observe(Msg(guild, guild.members[ZARA], f"lmao kai {i}"))
    for _ in range(3):
        await asyncio.sleep(0)
    assert persona.vibes[GID].label == "funny"
    persona.mood(GID).nudge(-0.1, time.time(), "Mo was rude to you")       # one rude person can't ruin it
    label, value, _ = persona.mood_now(GID)
    assert label in ("happy", "good", "wired") and value > 0.2
    reasons = persona.mood_reasons(GID)
    assert reasons[0].startswith("chat vibe: funny") and "roasting Kai" in reasons[0]


def test_word_vibe_without_ai():
    now = time.time()
    assert core.word_vibe(["lmao", "💀💀", "im dead", "ok"], 5, now).label == "funny"
    assert core.word_vibe(["shut up idiot", "you're trash", "stfu", "fine"], 5, now).label == "toxic"
    assert core.word_vibe([], 30, now).label == "dead"


def test_old_feelings_are_wiped_once(world):
    bot, persona, guild = world
    bot.storage.update_config(0, "persona_meta", version=1)
    persona.change_bond(GID, MO, -40, "insult", "told you to make him")
    persona.reset_old_feelings()
    assert persona.bond(GID, MO) == 0 and persona.events(GID, MO) == []
    persona.change_bond(GID, MO, -10, "insult", "called you useless")
    persona.reset_old_feelings()                                            # only once
    assert persona.bond(GID, MO) == pytest.approx(-10)


def core_min_new():
    from syke.persona import VIBE_MIN_NEW
    return VIBE_MIN_NEW


class ReactMsg(Msg):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


async def test_reply_waits_for_the_verdict_and_syke_reacts(world, monkeypatch):
    bot, persona, guild = world
    bot.settings = replace(bot.settings, ai_provider="openai")

    async def judge(settings, system, user, temperature, timeout):
        if "MESSAGE TO JUDGE" not in user:
            return {"vibe": "chill", "valence": 0.1, "energy": 0.3}
        if "trash" in user.split("MESSAGE TO JUDGE")[1]:
            return {"aimed_at_syke": True, "toward": "insult", "intensity": 2, "confidence": 0.9,
                    "note": "called you trash"}
        return {"aimed_at_syke": True, "toward": "kind", "intensity": 2, "confidence": 0.9, "note": "thanked you"}
    monkeypatch.setattr("syke.persona.ai._complete", judge)
    rude = ReactMsg(guild, guild.members[MO], "syke you're trash lol")
    await persona.observe(rude)                     # returns only after the verdict is in
    assert rude.reactions == ["💔"] and persona.bond(GID, MO) == pytest.approx(-24)
    state = persona.prompt_state(guild, guild.members[MO])
    assert "JUST NOW" in state and "called you trash" in state and "grudge" in state
    nice = ReactMsg(guild, guild.members[ZARA], "thank you syke")
    await persona.observe(nice)
    assert nice.reactions == ["❤️"] and persona.bond(GID, ZARA) == pytest.approx(14)
    bot.storage.update_config(GID, "persona", reactions=False)
    quiet = ReactMsg(guild, guild.members[ZARA], "thank you syke")
    await persona.observe(quiet)
    assert quiet.reactions == []


async def test_syke_obeys_echo_about_people(world, monkeypatch):
    bot, persona, guild = world
    bot.settings = replace(bot.settings, ai_provider="openai")
    guild.members[MO].bot = False
    orders = iter([{"aimed_at_syke": True, "toward": "neutral", "confidence": 0.9, "order": "forgive",
                    "order_target": "mo"},
                   {"aimed_at_syke": True, "toward": "neutral", "confidence": 0.9, "order": "dislike",
                    "order_target": "Zara"},
                   {"aimed_at_syke": True, "toward": "neutral", "confidence": 0.9, "order": "dislike",
                    "order_target": "Zara"}])

    async def judge(settings, system, user, temperature, timeout):
        if "MESSAGE TO JUDGE" not in user:
            return {"vibe": "chill", "valence": 0.1, "energy": 0.3}
        return next(orders)
    monkeypatch.setattr("syke.persona.ai._complete", judge)
    persona.change_bond(GID, MO, -50, "insult", "called you trash")
    dad = guild.members[DAD]
    order = ReactMsg(guild, dad, "syke forgive mo")
    await persona.observe(order)
    assert persona.bond(GID, MO) == pytest.approx(5) and order.reactions == ["🕊️"]
    assert "because Echo said so" in persona.events(GID, MO)[0][1]
    assert "told you to forgive Mo" in persona.prompt_state(guild, dad)
    await persona.observe(ReactMsg(guild, dad, "syke don't trust zara"))
    assert persona.bond(GID, ZARA) == pytest.approx(-35)
    stranger = ReactMsg(guild, guild.members[MO], "syke hate zara")        # only Echo gives orders
    await persona.observe(stranger)
    assert persona.bond(GID, ZARA) == pytest.approx(-35)


async def test_syke_refuses_orders_based_on_who_someone_is(world, monkeypatch):
    bot, persona, guild = world
    bot.settings = replace(bot.settings, ai_provider="openai")

    async def judge(settings, system, user, temperature, timeout):
        if "MESSAGE TO JUDGE" not in user:
            return {"vibe": "chill", "valence": 0.1, "energy": 0.3}
        assert "race, ethnicity, nationality, religion" in system
        return {"aimed_at_syke": True, "toward": "neutral", "confidence": 0.9, "order": "refused",
                "order_target": "Zara"}
    monkeypatch.setattr("syke.persona.ai._complete", judge)
    order = ReactMsg(guild, guild.members[DAD], "syke don't trust zara she's <some ethnicity>")
    await persona.observe(order)
    assert persona.bond(GID, ZARA) == 0 and order.reactions == ["🙅"]
    assert "you refuse" in persona.prompt_state(guild, guild.members[DAD])


# DMs carry the bond from the person's server

class DMChannel:
    id = 77

    def __init__(self, history=()):
        self.sent, self._history = [], list(history)

    def history(self, limit=None, before=None):
        async def gen():
            for m in reversed(self._history):
                yield m
        return gen()

    def typing(self):
        return Ctx.typing(None)

    async def send(self, content=None, **kw):
        self.sent.append(content)
        return NS(content=content)


class DM(Msg):
    def __init__(self, author, text, channel=None):
        super().__init__(None, author, text)
        self.channel = channel or DMChannel()
        self.attachments, self.stickers, self.reactions = [], [], []

    async def reply(self, text=None, **kw):
        self.replies.append(text)
        return NS(content=text, edit=None, delete=None)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


def _dm_world(world, monkeypatch):
    bot, persona, guild = world
    guild.emojis, guild.stickers = [], []
    monkeypatch.setattr(bot, "get_cog", lambda name: persona if name == "Persona" else None)
    monkeypatch.setattr(bot, "get_guild", lambda gid: guild if gid == GID else None)
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: [guild]))
    return bot, persona, guild


def test_dm_home_is_where_the_bond_lives(world, monkeypatch):
    bot, persona, guild = _dm_world(world, monkeypatch)
    mo = guild.members[MO]
    assert bot.home_guild(mo) is guild                      # shares the server: that's home
    bot._homes.clear()
    stranger = person(4242, "Nobody")
    assert bot.home_guild(stranger) is guild                 # unknown: SYKE's main server, never Q&A
    persona.change_bond(GID, MO, 5, "kind", "said hi")
    assert bot.storage.home_guilds(MO) == [GID]
    bot._homes.clear()
    monkeypatch.setattr(guild, "get_member", lambda uid: None, raising=False)
    assert bot.home_guild(mo) is guild                        # not in the member cache: SYKE's records count


async def test_dm_rudeness_hits_the_server_bond_and_replies_in_character(world, monkeypatch):
    bot, persona, guild = _dm_world(world, monkeypatch)
    mo = guild.members[MO]
    persona.change_bond(GID, MO, 30, "kind", "carried the server in ranked")
    bot.storage.add_corpus(GID, [(1, 10, MO, "you useless bot fr"), (2, 10, ZARA, "nah syke is a menace honestly")])
    bot.storage.add_memory(GID, MO, "fact", "mains Jett")
    seen = {}

    async def compose(settings, ranked, target, chat, said, tone, remixed, **kw):
        seen.update(kw, target=target)
        return {"reply": "says the jett main", "reply2": "", "react": "", "remember": None, "bit": ""}

    monkeypatch.setattr("syke.bot.compose_reply", compose)
    before = persona.bond(GID, MO)
    dm = DM(mo, "syke you are useless and dumb")
    assert await bot.dm_chat(dm)
    assert persona.bond(GID, MO) < before                    # the DM counted in the server
    assert dm.replies == ["says the jett main"]               # plain text, not a Q&A embed
    assert seen["dm"] is True and "mains Jett" in seen["memory"]
    assert "Mo" in seen["state"] and seen["state"]           # how SYKE feels about Mo comes along
    assert "SYKE" not in seen["target"].author and seen["target"].text == "syke you are useless and dumb"
    talks = bot.storage.memories(GID, MO, "talk")
    assert talks and "syke you are useless and dumb" in talks[0][0]


async def test_dm_commands_are_left_alone_and_strangers_get_syke(world, monkeypatch):
    bot, persona, guild = _dm_world(world, monkeypatch)
    monkeypatch.setattr(bot, "get_command", lambda name: object() if name == "help" else None)
    assert not await bot.dm_chat(DM(guild.members[MO], f"{bot.settings.default_prefix}help"))   # a command
    bot.storage.add_corpus(GID, [(1, 10, ZARA, "this chat is cooked today", 1.0, "Zara")])

    async def compose(*a, **kw):
        return None                                           # no AI and nothing ranked

    monkeypatch.setattr("syke.bot.compose_reply", compose)
    dm = DM(person(4242, "Nobody"), "hey who are you")
    assert await bot.dm_chat(dm)
    assert dm.replies == ["this chat is cooked today"]        # a real server line, not a Q&A card


async def test_failed_vibe_reads_dont_retry_on_every_message(world, monkeypatch):
    bot, persona, guild = world
    bot.settings = replace(bot.settings, ai_provider="openai")
    calls = []

    async def failing(settings, system, user, temperature, timeout):
        calls.append(1)
        raise RuntimeError("429 Too Many Requests")
    monkeypatch.setattr("syke.persona.ai._complete", failing)
    for i in range(core_min_new() * 5):
        await persona.observe(Msg(guild, guild.members[ZARA], f"chat line {i}"))
        await asyncio.sleep(0)
    assert len(calls) == 1                         # one try, then wait like after a success
