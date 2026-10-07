import asyncio
import random
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import discord
import pytest

from syke.bot import Syke, SykeCommands, _admin_predicate
from syke.config import load_settings
from syke.markov import MarkovChain, build_chain, tokenize
from syke.scanner import Scanner
from syke.storage import Storage

from test_commands import FakeCtx, run

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_tokenize_drops_links_and_defuses_everyone():
    assert tokenize("look https://x.y/z @everyone now") == ["look", "@\u200beveryone", "now"]


def test_chain_prefers_new_sentences():
    texts = ["the cat sat on the mat", "the dog sat on the log", "a cat ate the dog food"]
    chain = MarkovChain(texts, order=1)
    lines = {chain.generate(random.Random(seed)) for seed in range(20)}
    assert lines - {None}
    assert any(line not in texts for line in lines)
    assert MarkovChain([]).generate() is None
    assert build_chain(texts * 60).order == 2 and build_chain(texts).order == 1


def test_corpus_storage_and_optout_purge(tmp_path):
    store = Storage(str(tmp_path / "c.db"))
    assert store.add_corpus(1, [(10, 5, 100, "hi"), (11, 5, 200, "yo")]) == 2
    assert store.add_corpus(1, [(10, 5, 100, "hi")]) == 0
    assert store.corpus(1, 100) == [(100, "hi")]
    assert store.corpus_size(1) == (2, 2)
    store.set_optout(1, 100, True)
    assert store.corpus(1, 100) == []
    assert store.corpus_size(1) == (1, 1)


class FakeChannel(discord.TextChannel):
    def __init__(self, cid: int, messages: list):
        self.id = cid
        self.messages = messages
        self.calls: list = []

    def permissions_for(self, member):
        return discord.Permissions(view_channel=True, read_message_history=True)

    @property
    def mention(self):
        return f"<#{self.id}>"

    @property
    def name(self):
        return f"ch{self.id}"

    async def history(self, limit=100, after=None):
        self.calls.append(after.id if after else None)
        if after is None:
            picked = sorted(self.messages, key=lambda m: m.id, reverse=True)[:limit]
        else:
            picked = sorted((m for m in self.messages if m.id > after.id), key=lambda m: m.id)[:limit]
        for message in picked:
            await asyncio.sleep(0)
            yield message


def fake_message(mid: int, cid: int, author: int, text: str, bot: bool = False):
    return types.SimpleNamespace(
        id=mid, content=text, created_at=BASE + timedelta(minutes=mid), reactions=[],
        channel=types.SimpleNamespace(id=cid),
        author=types.SimpleNamespace(id=author, bot=bot, display_name=f"user{author}"),
    )


def fake_guild(*channels):
    by_id = {ch.id: ch for ch in channels}
    return types.SimpleNamespace(
        id=42, me=object(), name="Lab",
        get_channel_or_thread=by_id.get, get_channel=by_id.get,
    )


def test_scanner_reads_channels_in_parallel_then_only_new_messages():
    a = FakeChannel(1, [fake_message(i, 1, 100, f"a{i}") for i in range(1, 6)])
    b = FakeChannel(2, [fake_message(i, 2, 200, f"b{i}") for i in range(11, 14)] + [fake_message(14, 2, 9, "x", bot=True)])
    guild = fake_guild(a, b)
    scanner = Scanner(scan_limit=4, cache_minutes=0)

    first = run(scanner.scan(guild, [1, 2, 404]))
    assert sorted(m.content for m in first.messages) == ["a2", "a3", "a4", "a5", "b11", "b12", "b13"]
    assert first.skipped_channels == [404]

    a.messages.append(fake_message(6, 1, 100, "a6"))
    second = run(scanner.scan(guild, [1, 2]))
    assert a.calls == [None, 5] and b.calls == [None, 14]
    assert sorted(m.content for m in second.messages if m.channel_id == 1) == ["a3", "a4", "a5", "a6"]

    scanner.invalidate(42)
    run(scanner.scan(guild, [1]))
    assert a.calls[-1] is None


@pytest.fixture()
def lab(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "t.db"), ai_provider="none",
                       min_messages=3, cache_minutes=15)
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    texts = ["i love pizza so much", "pizza is the best food ever", "i love the best memes",
             "memes are so much fun", "ever tried pizza memes", "the best fun is pizza",
             "so much love for memes", "i think pizza is fun", "fun memes are the best",
             "love the food here", "the pizza here is so good", "memes and pizza forever"]
    channel = FakeChannel(1, [fake_message(i + 1, 1, 100, t) for i, t in enumerate(texts)])
    guild = fake_guild(channel)
    bot.storage.track(42, 1)
    cog = SykeCommands(bot)
    return cog, guild, channel


def ctx_in(guild, author_id=100):
    ctx = FakeCtx()
    ctx.guild = guild
    ctx.author = types.SimpleNamespace(
        id=author_id, bot=False, display_name=f"user{author_id}",
        display_avatar=types.SimpleNamespace(url="http://a"),
        guild_permissions=discord.Permissions(manage_guild=True),
    )
    return ctx


def test_analysis_is_reused_until_the_scan_changes(lab):
    cog, guild, _ = lab
    first, _ = run(cog.bot.analyze(guild))
    assert run(cog.bot.analyze(guild))[0] is first
    cog.bot.scanner.invalidate(42)
    assert run(cog.bot.analyze(guild))[0] is not first


def test_mimic_learns_from_watched_channels(lab):
    cog, guild, _ = lab
    ctx = ctx_in(guild)
    run(cog.mimic.callback(cog, ctx, None))
    embed = ctx.replies[-1]
    assert embed.author.name == "user100 (probably)"
    assert embed.description
    assert "Markov chain of 12 messages" in embed.footer.text

    stranger = ctx_in(guild, author_id=555)
    run(cog.mimic.callback(cog, stranger, None))
    assert stranger.replies[-1].title == "Not enough to go on 🔬"


def test_mimic_respects_optout_and_needs_setup(lab, tmp_path):
    cog, guild, _ = lab
    ctx = ctx_in(guild)
    run(cog.optout.callback(cog, ctx))
    run(cog.mimic.callback(cog, ctx, None))
    assert ctx.replies[-1].title == "Off limits"

    cog.bot.storage.untrack(42, 1)
    stranger = ctx_in(guild, author_id=555)
    run(cog.mimic.callback(cog, stranger, None))
    assert stranger.replies[-1].title == "No channels are being watched"


def test_collect_reads_history_into_corpus(lab):
    cog, guild, channel = lab
    channel.messages += [fake_message(100 + i, 1, 300, f"collected line {i}") for i in range(20)]
    ctx = ctx_in(guild)
    run(cog.collect.callback(cog, ctx, None, 50))
    assert ctx.replies[-1].title == "Collection complete 🧫"
    assert "**32** new" in ctx.replies[-1].description
    assert cog.bot.storage.corpus_size(42) == (32, 2)
    run(cog.collect.callback(cog, ctx, channel, 50))
    assert "**0** new" in ctx.replies[-1].description

    by_name = {c.name: c for c in cog.get_commands()}
    assert _admin_predicate in by_name["collect"].checks
    assert _admin_predicate not in by_name["mimic"].checks


def test_collect_skips_commands(lab):
    cog, guild, channel = lab
    channel.messages += [fake_message(200, 1, 300, "!profile @someone"), fake_message(201, 1, 300, "<@999> help")]
    run(cog.collect.callback(cog, ctx_in(guild), channel, 50))
    assert all(not text.startswith(("!", "<@")) for _, text in cog.bot.storage.corpus(42, 300))


def yap_message(mid, channel_id=1):
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    return types.SimpleNamespace(
        id=mid, content="anyone here", guild=types.SimpleNamespace(id=42),
        author=types.SimpleNamespace(id=777, bot=False),
        channel=types.SimpleNamespace(id=channel_id, send=send),
    ), sent


def test_yap_on_off_and_chiming_in(lab, monkeypatch):
    cog, guild, channel = lab
    bot = cog.bot
    ctx = ctx_in(guild)
    ctx.channel = channel
    run(cog.yap.callback(cog, ctx, True, None))
    assert ctx.replies[-1].title == "Yap on 🗣️"
    assert "hasn't stored any messages" in ctx.replies[-1].description
    assert bot.storage.yap_channels(42) == {1}

    run(bot.analyze(guild))
    bot.storage.add_corpus(42, [(500, 1, 100, "!top funny please")])
    stored = {text for _, text in bot.storage.corpus(42, 100)}

    monkeypatch.setattr("syke.bot.random.random", lambda: 0.99)
    message, sent = yap_message(900)
    run(bot.maybe_yap(message))
    assert sent == []

    monkeypatch.setattr("syke.bot.random.random", lambda: 0.0)
    run(bot.maybe_yap(message))
    assert len(sent) == 1 and sent[0] in stored and not sent[0].startswith("!")
    run(bot.maybe_yap(yap_message(901)[0]))
    assert len(sent) == 1, "cooldown should stop a second yap"

    quiet, quiet_sent = yap_message(902, channel_id=2)
    run(bot.maybe_yap(quiet))
    assert quiet_sent == []

    run(cog.yap.callback(cog, ctx, None, None))
    assert "<#1>" in ctx.replies[-1].description
    run(cog.yap.callback(cog, ctx, False, None))
    assert ctx.replies[-1].title == "Yap off 🤐"
    assert bot.storage.yap_channels(42) == set()

    by_name = {c.name: c for c in cog.get_commands()}
    assert _admin_predicate in by_name["yap"].checks
    assert "markov" not in by_name


def test_ranking_prefers_rare_shared_keywords_and_answers():
    import random

    from syke.yap import Turn, query_weights, rank

    candidates = ["i hate mondays", "pizza is elite", "who wants pizzas tonight", "nah pineapple pizza is a crime"]
    weights = query_weights("is pineapple on pizza good?", chat=[Turn("a", "we're ordering food")])
    ranked = rank(candidates, weights, "is pineapple on pizza good?", random.Random(1))
    assert ranked[0][1] == "nah pineapple pizza is a crime"
    assert ranked[-1][1] == "i hate mondays"
    assert "pizza" in query_weights("pizzas!!")


def test_stitch_only_uses_real_words():
    from syke.yap import stitch

    candidates = ["bro thinks he's him 💀 genuinely", "touch grass immediately", "pizza is elite"]
    assert stitch(candidates, [{"id": 0, "text": "bro thinks he's him"}, {"id": 1}]) == \
        "bro thinks he's him touch grass immediately"
    assert stitch(candidates, [{"id": 0, "text": "thinks he"}]) == "thinks he's"
    assert stitch(candidates, [{"id": 2, "text": "pizza is mid"}]) is None, "invented words"
    assert stitch(candidates, [{"id": 9}]) is None
    assert stitch(candidates, [{"id": 2}], banned={"Pizza  is elite"}) is None, "no parroting"


def test_compose_reply_stitches_with_ai_and_falls_back(monkeypatch):
    from syke import ai
    from syke.yap import Turn

    settings = replace(load_settings(), ai_provider="openai", openai_api_key="k")
    ranked = [(2.0, "pizza is elite"), (1.0, "touch grass"), (0.1, "no way")]
    target = Turn("Mira", "pizza?")

    async def fake_openai(s, system, user, temperature=1.0):
        assert "THEY REPLIED (TARGET): Mira: pizza?" in user and "[1] touch grass" in user
        assert "Zyro: who's hungry" in user
        return '{"parts": [{"id": 0}, {"id": 1, "text": "touch grass"}]}'

    monkeypatch.setattr(ai, "_call_openai", fake_openai)
    chat = [Turn("Zyro", "who's hungry")]
    assert run(ai.compose_reply(settings, ranked, target, chat, said="hello")) == "pizza is elite touch grass"

    async def invents(s, system, user, temperature=1.0):
        return '{"parts": [{"id": 0, "text": "pizza is mid"}]}'

    monkeypatch.setattr(ai, "_call_openai", invents)
    assert run(ai.compose_reply(settings, ranked, target, chat, said="hello")) == "pizza is elite"

    async def broken(s, system, user, temperature=1.0):
        raise RuntimeError("down")

    monkeypatch.setattr(ai, "_call_openai", broken)
    assert run(ai.compose_reply(settings, ranked, target, chat, said="hello")) == "pizza is elite"
    assert run(ai.compose_reply(settings, [], target)) is None


def reply_message(mid, target, text="pizza is the best", author=777):
    replies: list[str] = []

    async def reply(content, **kwargs):
        replies.append(content)

    channel = types.SimpleNamespace(id=1, typing=lambda: _NullTyping())
    message = types.SimpleNamespace(
        id=mid, content=text, guild=types.SimpleNamespace(id=42),
        author=types.SimpleNamespace(id=author, bot=False), channel=channel, reply=reply,
        reference=types.SimpleNamespace(message_id=target.id, resolved=target, cached_message=None),
    )
    return message, replies


class _NullTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_replies_to_yaps_get_a_matching_comeback(lab, monkeypatch):
    cog, guild, _ = lab
    bot = cog.bot
    run(bot.analyze(guild))
    bot.storage.set_yap(42, 1, True)

    yap = types.SimpleNamespace(id=5000, content="memes are so much fun", embeds=[],
                                author=types.SimpleNamespace(id=999))
    monkeypatch.setattr("syke.bot.discord.Message", types.SimpleNamespace)  # resolved replies are Messages
    assert run(bot.replied_yap(reply_message(1, yap)[0])) == "memes are so much fun"

    embed_reply = types.SimpleNamespace(id=5001, content="", embeds=["card"], author=yap.author)
    assert run(bot.replied_yap(reply_message(2, embed_reply)[0])) is None
    someone_else = types.SimpleNamespace(id=5002, content="hey", embeds=[], author=types.SimpleNamespace(id=1))
    assert run(bot.replied_yap(reply_message(3, someone_else)[0])) is None

    message, replies = reply_message(4, yap, text="what about pizza though")
    run(bot.answer_yap_reply(message, yap.content))
    assert len(replies) == 1
    assert "pizza" in replies[0] and replies[0] != "memes are so much fun"
    first = replies[0]

    run(bot.answer_yap_reply(message, yap.content))
    assert len(replies) == 1, "per-user cooldown"

    bot._last_comeback.clear()
    again, more = reply_message(6, yap, text="what about pizza though")
    run(bot.answer_yap_reply(again, yap.content))
    assert len(more) == 1
    assert more[0] != first and more[0] != yap.content, "don't reuse the last comeback"

    bot.storage.set_yap(42, 1, False)
    assert run(bot.replied_yap(reply_message(5, yap)[0])) is None


def test_yap_replies_keep_changing(lab):
    cog, guild, _ = lab
    bot = cog.bot
    run(bot.analyze(guild))
    bot.storage.set_yap(42, 1, True)
    yap = types.SimpleNamespace(id=5000, content="memes are so much fun", embeds=[],
                                author=types.SimpleNamespace(id=999))

    seen = []
    for i in range(4):
        bot._last_comeback.clear()
        message, replies = reply_message(20 + i, yap, text="what about pizza though")
        run(bot.answer_yap_reply(message, yap.content))
        assert replies, f"expected a reply on turn {i}"
        seen.append(replies[0])
    assert len(set(seen)) == len(seen), seen


def test_scanme_adds_older_messages_to_profile(lab):
    cog, guild, channel = lab
    bot = cog.bot
    bot.scanner.scan_limit = 5
    server, _ = run(bot.analyze(guild))
    assert server.users[100].stats.message_count == 5

    ctx = ctx_in(guild)
    run(cog.scanme.callback(cog, ctx))
    embed = ctx.replies[-1]
    assert embed.title == "Scan complete ⛏️"
    assert "found **12** of yours" in embed.description and "**7** SYKE hadn't seen" in embed.description
    assert "draws on **12** messages" in embed.description

    server, _ = run(bot.analyze(guild))
    assert server.users[100].stats.message_count == 12
    assert server.users[100].name == "user100"

    bot.storage.set_optout(42, 100, True)
    server, _ = run(bot.analyze(guild))
    assert 100 not in server.users


def test_profile_saves_ai_labels_and_rescores(lab, monkeypatch):
    cog, guild, _ = lab
    bot = cog.bot
    server, _ = run(bot.analyze(guild))
    seen = {}

    async def fake_roast(profile, settings, emojis=None, avoid=None):
        seen["avoid"] = avoid
        profile.summary, profile.ai_used = "roasted", True
        profile.ai_labels = {m.message_id: frozenset({"freaky"}) for m in profile.messages}
        return profile

    monkeypatch.setattr("syke.bot.write_roast", fake_roast)
    ctx = ctx_in(guild)
    ctx.guild.emojis = []
    before = server.users[100].scores["freaky"]
    run(cog.profile.callback(cog, ctx, None))
    assert "\n> roasted\n" in ctx.replies[-1].description
    assert len(bot.storage.labels(42)) == 12
    assert "😏 Freaky" in ctx.replies[-1].description
    server, _ = run(bot.analyze(guild))
    assert server.users[100].scores["freaky"] > before

    run(cog.profile.callback(cog, ctx, None))
    assert seen["avoid"] == "roasted"


def test_detect_tone_reads_the_vibe():
    from syke.yap import Turn, detect_tone

    assert detect_tone("you're actually so bad at this game") == "toxic"
    assert detect_tone("shut up idiot") == "toxic"
    assert detect_tone("she's so fine ngl 😏") == "freaky"
    assert detect_tone("LMAOOO 😭😭") == "funny"
    assert detect_tone("what are we doing tonight") == "funny", "banter by default"
    assert detect_tone("ok", [Turn("a", "stfu you're so dumb"), Turn("b", "ur trash")]) == "toxic"


def test_stitch_allows_a_little_glue():
    from syke.yap import stitch

    candidates = ["ur aim is trash", "skill issue tbh"]
    assert stitch(candidates, [{"id": 0}, {"id": 1, "glue": "and also"}]) == "ur aim is trash and also skill issue tbh"
    assert stitch(candidates, [{"id": 0, "glue": "this is way too many words"}]) is None
    assert stitch(candidates, [{"id": 0, "glue": "@everyone"}]) is None


def test_remixes_are_new_and_fit_topic_or_tone():
    import random

    from syke.markov import build_chain
    from syke.yap import query_weights, remixes

    texts = ["stfu you're so dumb", "you're actually trash at valorant", "valorant is so mid",
             "i love valorant so much", "ur so dumb at valorant lmao", "nobody asked you're trash"] * 3
    chain = build_chain(texts)
    lines = remixes(chain, query_weights("valorant tonight?"), "toxic", rng=random.Random(4))
    assert lines
    originals = {t.lower() for t in texts}
    assert all(line.lower() not in originals for line in lines)
    assert remixes(None, {}, "funny") == []


def test_labelled_messages_by_tone(tmp_path):
    from syke.storage import Storage

    storage = Storage(str(tmp_path / "l.db"))
    storage.add_corpus(42, [(1, 1, 5, "come here 😏"), (2, 1, 5, "ur trash"), (3, 1, 5, "lol")])
    storage.save_labels(42, {1: frozenset({"freaky"}), 2: frozenset({"toxic", "funny"})})
    assert storage.labelled_messages(42, "freaky", 10) == ["come here 😏"]
    assert storage.labelled_messages(42, "toxic", 10) == ["ur trash"]
    assert storage.labelled_messages(42, "serious", 10) == []


def test_compose_prompt_has_tone_and_remixes(monkeypatch):
    from syke import ai
    from syke.yap import Turn

    settings = replace(load_settings(), ai_provider="openai", openai_api_key="k")
    seen = {}

    async def fake_openai(s, system, user, temperature=1.0):
        seen["user"], seen["system"] = user, system
        return '{"parts": [{"id": 0}, {"id": 1, "glue": "plus"}]}'

    monkeypatch.setattr(ai, "_call_openai", fake_openai)
    ranked = [(2.0, "ur aim is trash"), (1.0, "skill issue tbh")]
    reply = run(ai.compose_reply(settings, ranked, Turn("Dex", "1v1 me"), [], "lol", "toxic", ["skill issue tbh"]))
    assert reply == "ur aim is trash plus skill issue tbh"
    assert "TONE: toxic" in seen["user"] and "[1] (remix) skill issue tbh" in seen["user"]
    assert "savage trash talk" in seen["system"]
    assert "Never repeat a line SYKE already said" in seen["system"]


def test_compose_reply_skips_recent_lines(monkeypatch):
    from syke import ai
    from syke.yap import Turn

    settings = replace(load_settings(), ai_provider="none")
    ranked = [(2.0, "pizza is elite"), (1.0, "touch grass"), (0.1, "no way")]
    target = Turn("Mira", "pizza?")
    assert run(ai.compose_reply(settings, ranked, target, avoid=["pizza is elite"])) == "touch grass"

    settings = replace(load_settings(), ai_provider="openai", openai_api_key="k")
    seen = {}

    async def fake_openai(s, system, user, temperature=1.0):
        seen["user"] = user
        return '{"parts": [{"id": 0}]}'

    monkeypatch.setattr(ai, "_call_openai", fake_openai)
    reply = run(ai.compose_reply(settings, ranked, target, said="hello", avoid=["pizza is elite"]))
    assert reply == "touch grass"
    assert "SYKE ALREADY SAID" in seen["user"] and "pizza is elite" in seen["user"]
    assert "[0] pizza is elite" not in seen["user"]
