import random
import time
import types
from datetime import datetime, timedelta, timezone

import discord
import pytest

from syke import welcome as welcome_mod
from syke.awards import awards_embed, compute_awards, due_at, parse_day, parse_hour
from syke.community import Community, guess_round
from syke.dashboard import SECTIONS, Dashboard
from syke.models import Msg
from test_commands import run
from test_moderation import Channel, Ctx, Guild, Member, Role


def _msgs(uid, name, texts, when=None, laughs=0, total=0):
    when = when or datetime(2026, 11, 10, 15, tzinfo=timezone.utc)
    return [Msg(uid, name, t, when + timedelta(minutes=i * 7), 1, laughs, total, uid * 1000 + i)
            for i, t in enumerate(texts)]


# Awards

def test_compute_awards_picks_clear_winners():
    yapper = _msgs(1, "Yappy", ["hello there everyone"] * 30)
    funny = _msgs(2, "Clown", ["LMAOOO 💀💀 bro i'm dead 😂😂", "nah that's so funny lmao 😭"] * 6, laughs=3, total=4)
    owl = _msgs(3, "Owl", ["why am i awake"] * 9, when=datetime(2026, 11, 10, 2, tzinfo=timezone.utc))
    result = compute_awards(yapper + funny + owl, timezone.utc, rng=random.Random(1))
    by_key = {a.key: a for a in result.awards}
    assert by_key["yapper"].user_id == 1 and "30 messages" in by_key["yapper"].line
    assert by_key["funny"].user_id == 2 and by_key["funny"].quote
    assert by_key["laughs"].user_id == 2
    assert by_key["night"].user_id == 3
    assert result.top_message.author_id == 2
    assert set(result.winners) >= {1, 2, 3}
    embed = awards_embed(result, "Lab", role_mention="<@&5>")
    assert len(embed.fields) <= 25 and "Weekly Awards" in embed.title
    assert all(len(f.value) <= 1024 for f in embed.fields)


def test_quiet_week_has_no_awards():
    result = compute_awards([], timezone.utc)
    assert result.awards == []
    assert "Nobody" in awards_embed(result, "Lab").description


def test_schedule_parsing():
    assert parse_day("Sunday") == 6 and parse_day("fri") == 4 and parse_day("someday") is None
    assert parse_hour("18") == 18 and parse_hour("18:00") == 18
    assert parse_hour("6pm") == 18 and parse_hour("12am") == 0 and parse_hour("25") is None


def test_due_at_is_the_last_slot_not_the_next():
    tz = timezone.utc
    wednesday = datetime(2026, 11, 11, 12, tzinfo=tz)
    assert due_at(wednesday, 6, 18) == datetime(2026, 11, 8, 18, tzinfo=tz)
    sunday_late = datetime(2026, 11, 15, 19, tzinfo=tz)
    assert due_at(sunday_late, 6, 18) == datetime(2026, 11, 15, 18, tzinfo=tz)
    sunday_early = datetime(2026, 11, 15, 17, tzinfo=tz)
    assert due_at(sunday_early, 6, 18) == datetime(2026, 11, 8, 18, tzinfo=tz)


def test_awards_post_and_move_the_role(cog):
    async def flow():
        guild = Guild()
        role = Role(77, "Winner", position=3)
        guild.roles[77] = role
        guild.member_count = 3
        old = Member(9)
        old.roles.append(role)
        new = Member(1)
        guild.members = {9: old, 1: new}
        community = Community(cog.bot)
        community.bot.storage.track(guild.id, 1)
        now = time.time()
        rows = [(i + 1, 1, 1, "hello there everyone", now - 3600 + i, "Yappy", 0, 0) for i in range(20)]
        community.bot.storage.add_corpus(guild.id, rows)

        async def analyze(_guild):
            return None
        community.bot.analyze = analyze
        config = community.bot.storage.update_config(guild.id, "awards", channel=10, role=77, holders=[9])
        posted, note = await community.post_awards(guild, config)
        assert posted, note
        assert guild.channels[0].sent and "Weekly Awards" in guild.channels[0].sent[-1].title
        assert role in new.roles and role not in old.roles
        assert community.bot.storage.get_config(guild.id, "awards")["holders"] == [1]

    run(flow())


# !guess

def test_guess_round_builds_options_with_the_answer():
    candidates = [(1, 5, 10, "Ada", "this is definitely something ada would say")]
    authors = [(10, "Ada", 50), (11, "Bo", 40), (12, "Cy", 30), (13, "Di", 20), (14, "Ed", 10)]
    row, options = guess_round(candidates, authors, set(), lambda t: False, rng=random.Random(2))
    assert row[2] == 10
    assert len(options) == 4 and (10, "Ada") in options
    assert len({uid for uid, _ in options}) == 4


def test_guess_round_skips_opted_out_and_needs_people():
    candidates = [(1, 5, 10, "Ada", "this is definitely something ada would say")]
    authors = [(10, "Ada", 50), (11, "Bo", 40)]
    assert guess_round(candidates, authors, {10}, lambda t: False) is None
    assert guess_round(candidates, [(10, "Ada", 50)], set(), lambda t: False) is None
    assert guess_round([(1, 5, 10, "Ada", "!profile @someone please")], authors, set(), lambda t: True) is None


def test_guess_scores(cog):
    storage = cog.bot.storage
    assert storage.add_guess_score(42, 1, 1) == 1
    assert storage.add_guess_score(42, 1, 1) == 2
    storage.add_guess_score(42, 2, 0)
    assert storage.guess_leaders(42) == [(1, 2, 2)]


# Welcome

def test_fill_is_plain_replacement():
    assert welcome_mod.fill("hi {name} {name.__class__} {nope}", {"name": "Ada"}) == "hi Ada {name.__class__} {nope}"


def test_render_uses_design_and_placeholders():
    guild = types.SimpleNamespace(name="Lab", member_count=120)
    member = types.SimpleNamespace(mention="<@5>", display_name="Ada", name="ada_2009",
                                   display_avatar=types.SimpleNamespace(url="https://cdn/a.png"))
    config = {"title": "Hi {name}!", "text": "{roast}\nYou are #{count} in {server}.", "colour": 0xFF8800,
              "image": "https://img/x.gif", "footer": "by {server}"}
    content, embed = welcome_mod.render(config, member, guild, "nice name")
    assert content == "<@5>"
    assert embed.title == "Hi Ada!"
    assert embed.description == "nice name\nYou are #120 in Lab."
    assert embed.colour.value == 0xFF8800 and embed.image.url == "https://img/x.gif"
    assert embed.thumbnail.url == "https://cdn/a.png" and embed.footer.text == "by Lab"
    content, _ = welcome_mod.render({"ping": False}, member, guild, "x")
    assert content is None


def test_offline_roast_mentions_the_name():
    for seed in range(20):
        line = welcome_mod.offline_roast("Ada", "ada_2009", has_avatar=False, rng=random.Random(seed))
        assert line and ("Ada" in line or "ada_2009" in line)


def test_parse_colour():
    assert welcome_mod.parse_colour("#ff8800") == 0xFF8800
    assert welcome_mod.parse_colour("FF8800") == 0xFF8800
    assert welcome_mod.parse_colour("orange") is None


def test_verifying_sends_the_welcome(cog):
    from syke.verify import grant

    async def flow():
        guild = Guild()
        member = Member(5)
        member.name = "ada_2009"
        member.avatar = None
        member.guild_avatar = None
        member.guild = guild
        member.display_avatar = types.SimpleNamespace(url="https://cdn/a.png")
        guild.member_count = 10
        verified = Role(60, "Verified", position=2)
        verified.managed = False
        verified.is_default = lambda: False
        guild.roles[60] = verified
        community = Community(cog.bot)
        cog.bot.storage.update_verification(guild.id, role_id=60)
        cog.bot.storage.update_config(guild.id, "welcome", channel=11, title="Welcome {name}")
        events = []
        cog.bot.dispatch = lambda name, *args: events.append((name, args))
        result = await grant(cog.bot, guild, member, None, prefix="!", admin=False)
        assert "in" in result.title.lower()
        assert events == [("syke_verified", (member,))]
        await community.on_syke_verified(member)
        welcome_channel = guild.channels[1]
        assert welcome_channel.sent and welcome_channel.sent[-1].title == "Welcome user5"
        # Already verified: no second welcome.
        events.clear()
        await grant(cog.bot, guild, member, None, prefix="!", admin=False)
        assert events == []

    run(flow())


def test_welcome_commands_save_the_design(cog):
    community = Community(cog.bot)
    guild = Guild()
    ctx = Ctx(guild, Member(2, position=20, manage_guild=True))
    run(community.welcome_message.callback(community, ctx, text="Hey {mention}\\n{roast}"))
    assert cog.bot.storage.get_config(guild.id, "welcome")["text"] == "Hey {mention}\n{roast}"
    run(community.welcome_colour.callback(community, ctx, "#00ff00"))
    assert cog.bot.storage.get_config(guild.id, "welcome")["colour"] == 0x00FF00
    run(community.welcome_message.callback(community, ctx, text="reset"))
    assert "text" not in cog.bot.storage.get_config(guild.id, "welcome")


# Dashboard

class DashGuild(Guild):
    def get_member(self, uid):
        return None


def test_dashboard_renders_every_section(cog):
    async def flow():
        guild = DashGuild()
        cog.bot.get_cog = lambda name: Community(cog.bot) if name == "Community" else None
        for key, *_ in SECTIONS:
            view = Dashboard(cog.bot, guild, owner_id=2, section=key)
            page = view.page()
            assert page.title and len(page.description or "") <= 4096
            assert len(view.children) >= 1
            rows = {item.row for item in view.children}
            assert max(rows) <= 4
            view.stop()

    run(flow())


def test_welcome_points_to_roles_and_colour_channels():
    guild = Guild()
    guild.member_count = 50
    member = types.SimpleNamespace(mention="<@5>", display_name="Ada", name="ada",
                                   display_avatar=types.SimpleNamespace(url="https://cdn/a.png"))
    _, card = welcome_mod.render({}, member, guild, "roast")
    assert card.description.endswith("enjoy the chaos.")
    _, card = welcome_mod.render({"roles_channel": 10, "colors_channel": 11}, member, guild, "roast")
    assert card.description.endswith(
        "enjoy the chaos. Grab your roles in <#10> and pick a name colour in <#11>.")
    _, card = welcome_mod.render({"colors_channel": 11}, member, guild, "roast")
    assert card.description.endswith("enjoy the chaos. Pick a name colour in <#11>.")
    _, card = welcome_mod.render({"text": "Hi {name}!", "roles_channel": 10}, member, guild, "roast")
    assert card.description == "Hi Ada!\n\nGrab your roles in <#10>."
    _, card = welcome_mod.render({"text": "Roles: {roles}", "roles_channel": 10}, member, guild, "roast")
    assert card.description == "Roles: <#10>"


def test_welcome_roles_commands(cog):
    community = Community(cog.bot)
    guild = Guild()
    ctx = Ctx(guild, Member(2, position=20, manage_guild=True))
    run(community.welcome_roles.callback(community, ctx, guild.channels[0], ""))
    run(community.welcome_colors.callback(community, ctx, guild.channels[1], ""))
    assert "Grab your roles in <#10> and pick a name colour in <#11>." in ctx.replies[-1].description
    run(community.welcome_roles.callback(community, ctx, None, "off"))
    assert "roles_channel" not in cog.bot.storage.get_config(guild.id, "welcome")
