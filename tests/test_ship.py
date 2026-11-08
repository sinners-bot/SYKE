import types
from datetime import datetime, timedelta, timezone

from syke.ai import public_text, tldr_text, write_ship_blurb, write_tldr
from syke.demo import build_demo_server
from syke.profile import analyze_server
from syke.ship import match_percent, read_ship, ship_name
from syke.tldr import (
    Line, assemble, digest, highlights, offline_summary, prompt_facts, prompt_sample, span_label, tldr_embed,
    topics_of,
)

from test_commands import FakeCtx, run

UTC = timezone.utc
BASE = datetime(2026, 6, 1, tzinfo=UTC)


def _msgs(lines, author, hour=12):
    from syke.models import Msg

    return [
        Msg(author, f"user{author}", text, BASE.replace(hour=hour) + timedelta(minutes=i))
        for i, text in enumerate(lines)
    ]


def test_ship_name_and_match_are_stable():
    left = "Zyro"
    right = "Vex"
    assert ship_name(left, right) == "Zyex"
    server = analyze_server(build_demo_server(), UTC, min_messages=15)
    zyro, vex = server.users[1], server.users[5]
    assert match_percent(zyro, vex) == match_percent(vex, zyro)
    assert 1 <= match_percent(zyro, vex) <= 99
    reading = read_ship(zyro, vex)
    again = read_ship(zyro, vex)
    assert reading.score == again.score
    assert reading.blurb == again.blurb
    assert "Zyro" in reading.blurb and "Vex" in reading.blurb
    assert reading.left_quote != "—" and reading.right_quote != "—"
    # Two people who actually share a vice outscore two dry, mismatched texters.
    freaky = analyze_server(
        _msgs(["down horrendous 😏 come over"] * 12, 10)
        + _msgs(["i'm down horrendous, moan for me"] * 12, 11)
        + _msgs(["ok", "yeah", "later", "sure", "gg", "true"] * 4, 12)
        + _msgs(["Honestly I think the evidence points the other way because of the context."] * 12, 13),
        UTC, min_messages=5,
    )
    assert match_percent(freaky.users[10], freaky.users[11]) > match_percent(freaky.users[12], freaky.users[13])


def test_ship_command_uses_demo_chats(cog):
    ctx = FakeCtx()
    run(cog.ship.callback(cog, ctx, None, None))
    assert ctx.replies[-1].title == "Ship who? 💘"

    ctx.author.bot = False
    run(cog.ship.callback(cog, ctx, ctx.author, None))
    assert ctx.replies[-1].title == "That's just a mirror"

    bot = types.SimpleNamespace(id=9, bot=True, display_name="Robo")
    run(cog.ship.callback(cog, ctx, bot, None))
    assert ctx.replies[-1].title == "Nice try"

    run(cog.demo.callback(cog, ctx, True))
    zyro = types.SimpleNamespace(
        id=1, bot=False, display_name="Zyro", display_avatar=types.SimpleNamespace(url="http://z"),
    )
    vex = types.SimpleNamespace(
        id=5, bot=False, display_name="Vex", display_avatar=types.SimpleNamespace(url="http://v"),
    )
    ctx.author = zyro
    run(cog.ship.callback(cog, ctx, vex, None))
    embed = ctx.replies[-1]
    assert embed.title == "💘 Zyro × Vex"
    assert "%" in embed.description and "Ship name:" in embed.description
    assert embed.fields[0].name == "Zyro" and embed.fields[1].name == "Vex"
    assert "for fun only" in embed.footer.text
    assert len(embed) < 6000

    opted = types.SimpleNamespace(id=5, bot=False, display_name="Vex", display_avatar=types.SimpleNamespace(url="http://v"))
    cog.bot.storage.set_optout(42, 5, True)
    run(cog.ship.callback(cog, ctx, opted, None))
    assert ctx.replies[-1].title == "Off limits"


def _history(items):
    class Hist:
        def __init__(self):
            self.i = 0

        def __aiter__(self):
            self.i = 0
            return self

        async def __anext__(self):
            if self.i >= len(items):
                raise StopAsyncIteration
            item = items[self.i]
            self.i += 1
            return item

    def history(**kwargs):
        return Hist()

    return history


def _message(i, text, author="Ada", author_id=1, minute=0):
    return types.SimpleNamespace(
        id=1000 + i,
        content=text,
        author=types.SimpleNamespace(id=author_id, bot=False, display_name=author),
        created_at=BASE + timedelta(minutes=minute or i),
    )


def test_tldr_summarises_a_long_channel_and_backfills(cog):
    ctx = FakeCtx()
    ctx.channel = types.SimpleNamespace(id=7, name="general", history=_history([]))
    ctx.message = types.SimpleNamespace(id=1)
    run(cog.tldr.callback(cog, ctx))
    assert ctx.replies[-1].title == "Nothing to summarise"

    lines = []
    for i in range(320):
        who = "Ada" if i % 2 == 0 else "Bea"
        lines.append(_message(i, "the raid is cooked tonight and nobody is helping", who, 1 if i % 2 == 0 else 2))
    # Discord history is newest first.
    ctx.channel.history = _history(list(reversed(lines)))
    run(cog.tldr.callback(cog, ctx))
    embed = ctx.replies[-1]
    assert embed.title == "📝 TL;DR · #general"
    assert "320" in embed.description
    assert "raid" in embed.description
    assert "Only **" not in embed.description
    assert embed.fields[0].name == "Vibe"
    assert embed.fields[1].name == "Loudest" and "Ada" in embed.fields[1].value
    assert embed.fields[2].name == "Window" and "<t:" in embed.fields[2].value
    assert embed.footer.text.startswith("320 messages • 2 people")
    assert len(embed) < 6000

    short = [_message(i, "just saying hello in here today", "Ada", 1) for i in range(4)]
    stored = []
    for i in range(310):
        stored.append((
            5000 + i, 7, 3, "older lore about the heist keeps coming up",
            (BASE - timedelta(days=2, minutes=i)).timestamp(), "Cleo", 0, 0,
        ))
    cog.bot.storage.add_corpus(42, stored)
    ctx.channel.history = _history(list(reversed(short)))
    run(cog.tldr.callback(cog, ctx))
    recap = ctx.replies[-1]
    assert "Only **" not in recap.description
    assert "heist" in recap.description or "lore" in recap.description
    assert int(recap.footer.text.split()[0].replace(",", "")) >= 300


def test_assemble_keeps_order_and_caps_at_four_hundred():
    live = [Line("A", f"new {i}", 1000 - i) for i in range(10)]
    older = [Line("B", f"old {i}", 500 - i) for i in range(350)]
    lines = assemble(live, older)
    assert len(lines) == 360
    assert lines[0].text.startswith("old")
    assert lines[-1].text == "new 0"
    huge = [Line("A", f"n {i}", 5000 - i) for i in range(500)]
    assert len(assemble(huge, older)) == 400


def test_prompt_sample_covers_the_ends():
    lines = [Line("A", f"line {i}", i) for i in range(500)]
    sample = prompt_sample(lines, limit=40)
    assert sample[0].text == "line 0"
    assert any(line.text == "line 499" for line in sample)
    assert len(sample) < 80


def test_offline_ai_helpers_do_not_call_out(cog):
    assert run(write_ship_blurb(cog.bot.settings, "A", "B", 50, "meh", ["hi"], ["yo"])) is None
    assert run(write_tldr(cog.bot.settings, "general", [("A", "hi")], 1)) is None
    assert public_text("hello <@123> and @everyone there", 40) == "hello and there"
    summary, vibe = offline_summary(
        [Line("Ada", "the heist plan is genuinely unhinged tonight", i) for i in range(12)],
        "general",
    )
    assert "Ada" in summary and "heist" in summary and vibe


def _chat(script, start=1_750_000_000.0):
    return [Line(author, text, start + 60 * i) for i, (author, text) in enumerate(script)]


def test_topics_ignore_slang_and_one_person_spamming():
    script = [("Spam", "buy my mixtape right now")] * 40
    for i in range(20):
        script += [
            ("Ada", f"the dungeon boss took {i} tries"),
            ("Bea", "that dungeon is cooked fr lmao"),
            ("Cleo", "dungeon loot was worth it though"),
        ]
    topics = topics_of(_chat(script))
    assert topics[0] == "dungeon"
    assert "cooked" not in topics and "lmao" not in topics
    assert "mixtape" not in topics[:2]


def test_topics_prefer_a_recurring_phrase_over_its_words():
    script = []
    for i in range(10):
        script += [("Ada", f"the ice cream truck is back {i}"), ("Bea", f"chasing the ice cream truck again {i}")]
    assert "cream truck" in topics_of(_chat(script)) or "ice cream" in topics_of(_chat(script))


def test_digest_tracks_drift_span_and_quotes_lines_that_landed():
    script = [("Ada", "who is joining the raid tonight with potions"), ("Bea", "raid boss wiped us again")] * 15
    script += [("Cleo", "pizza thief is still at large in this house"), ("Dan", "pizza tribunal starts right now")] * 15
    script[40] = ("Dan", "i ate the pizza and i regret nothing at all")
    script[41] = ("Ada", "LMAOOO 💀")
    lines = _chat(script)
    facts = digest(lines)
    assert facts.arc == ("raid", "pizza")
    assert facts.span == "59m"
    assert facts.people == 4
    assert any(line.text.startswith("i ate the pizza") for line in facts.highlights)
    assert len({line.author for line in facts.highlights}) == len(facts.highlights)
    summary, _vibe = offline_summary(lines, "general", facts)
    assert "Started on **raid**, ended up on **pizza**" in summary
    assert "Best lines" in summary
    grounding = prompt_facts(facts)
    assert "4 people" in grounding and "raid" in grounding and "59m" in grounding


def test_highlights_skip_short_lines_and_duplicates():
    lines = _chat([("Ada", "ok"), ("Bea", "the same long line here"), ("Cleo", "the same long line here")])
    picked = highlights(lines, [])
    assert [line.text for line in picked] == ["the same long line here"]


def test_span_label_reads_like_a_person_wrote_it():
    assert span_label(20) == "under a minute"
    assert span_label(45 * 60) == "45m"
    assert span_label(3 * 3600) == "3h"
    assert span_label(3 * 3600 + 20 * 60) == "3h 20m"
    assert span_label(2 * 86400 + 4 * 3600) == "2d 4h"


def test_tldr_text_formats_headline_and_bullets():
    text = tldr_text({
        "headline": "**Raid night fell apart**",
        "points": ["- **Ada** begged for a healer <@123>", "", "• **Bea** died twice"],
    })
    assert text == "**Raid night fell apart**\n\n• **Ada** begged for a healer\n• **Bea** died twice"
    assert tldr_text({"summary": "old style summary text"}) == "old style summary text"


def test_tldr_embed_flags_a_thin_window():
    lines = _chat([("Ada", "just the one message about tacos")])
    facts = digest(lines)
    embed = tldr_embed("Tacos.", "Calm.", facts, "food", ai_used=True)
    assert "Only **1** message to go on" in embed.description
    assert embed.footer.text == "1 message • 1 person • AI • $tldr"
    assert [field.name for field in embed.fields] == ["Vibe", "Loudest"]
