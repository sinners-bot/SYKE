import random
import types

from syke.cards import leaderboard_page
from syke.demo import build_demo_server
from syke.dexify import ELLIPSIS, dexify_text, original_words, tokens_of
from syke.profile import analyze_server
from syke.showcase import tour_page

from test_commands import FakeCtx, run

from datetime import timezone

UTC = timezone.utc


def words_kept(text: str) -> list[str]:
    return [part for chunk in dexify_text(text, random.Random(1)).split(ELLIPSIS) for part in chunk.split() if part]


def test_dexify_inserts_ellipses_after_chunks():
    source = "I think we should go to the store later"
    out = dexify_text(source, random.Random(2))
    assert out.endswith(ELLIPSIS)
    assert out.count(ELLIPSIS) >= 2
    assert words_kept(source) == original_words(source)


def test_dexify_is_seeded_and_keeps_discord_tokens():
    source = "hey <@123> check <#9> and <:KEKW:1> https://ex.com later"
    first = dexify_text(source, random.Random(7))
    second = dexify_text(source, random.Random(7))
    assert first == second
    kept = original_words(first.replace(ELLIPSIS, " "))
    assert "<@123>" in tokens_of(source)
    assert all(token in kept for token in ("<@123>", "<#9>", "<:KEKW:1>", "https://ex.com"))


def test_dexify_short_and_empty_messages():
    assert dexify_text("ok", random.Random(1)) == f"ok{ELLIPSIS}"
    assert dexify_text("   ", random.Random(1)) == ELLIPSIS
    assert dexify_text("Wait what??", random.Random(3)).startswith("Wait")
    assert original_words("Wait what??") == ["Wait", "what"]


def test_dexify_rewrites_the_replied_message_as_plain_text(cog):
    ctx = FakeCtx()
    target = types.SimpleNamespace(
        content="I think we should go later",
        author=types.SimpleNamespace(id=99, bot=False, display_name="Dex"),
    )
    ctx.message = types.SimpleNamespace(
        reference=types.SimpleNamespace(message_id=1, resolved=target, cached_message=target),
    )
    run(cog.dexify.callback(cog, ctx, None, text="this should be ignored"))
    line = ctx.replies[-1]
    assert isinstance(line, str)
    assert ELLIPSIS in line
    assert "think" in line.lower() or "later" in line.lower()
    assert not hasattr(line, "description")


def test_dexify_command_rewrites_supplied_text(cog):
    ctx = FakeCtx()
    run(cog.dexify.callback(cog, ctx, None, text="I think we should go later"))
    line = ctx.replies[-1]
    assert isinstance(line, str)
    assert ELLIPSIS in line


def test_dexify_uses_a_stored_message(cog):
    ctx = FakeCtx()
    cog.bot.storage.add_corpus(42, [
        (i, 1, 1234, "I think we should go to the store later", None, "Tester", 0, 0)
        for i in range(1, 4)
    ])
    run(cog.dexify.callback(cog, ctx, None))
    line = ctx.replies[-1]
    assert isinstance(line, str)
    assert ELLIPSIS in line


def test_dexify_needs_a_target_and_respects_optout(cog):
    ctx = FakeCtx()
    run(cog.dexify.callback(cog, ctx, None))
    assert ctx.replies[-1].title == "No channels are being watched"

    cog.bot.storage.add_corpus(42, [(1, 1, 1234, "yeah we can go later", None, "Tester", 0, 0)])
    run(cog.optout.callback(cog, ctx))
    run(cog.dexify.callback(cog, ctx, None))
    assert ctx.replies[-1].title == "Off limits"

    run(cog.dexify.callback(cog, ctx, None, text="just rewrite this please"))
    assert ELLIPSIS in ctx.replies[-1]


def test_dexify_showcase_mentions_the_command():
    embeds = tour_page("talk", "?")
    assert "`?dexify`" in embeds[0].description
    assert "......" in embeds[0].description
    assert "reply to a message" in embeds[0].description.lower()


def test_leaderboard_card_has_a_podium_and_bars():
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    board = leaderboard_page(server, "funny")
    assert board.title == "😂 Funny — server leaderboard"
    assert board.author.name.startswith("SYKE LEADERBOARD")
    assert "🥇" in board.description and "🥈" in board.description
    assert "`" in board.description and "%" in board.description
    assert "Zyro" in board.description
    assert len(board) < 2000
