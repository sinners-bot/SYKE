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

    run(cog.markov.callback(cog, ctx))
    assert ctx.replies[-1].author.name == "Lab, collectively"

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
    run(cog.markov.callback(cog, ctx))
    assert ctx.replies[-1].title == "No channels are being watched"


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
