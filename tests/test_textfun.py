"""The text-mangling toys: deterministic with a seeded rng, and ping-safe."""

import random
import types
from dataclasses import replace

import pytest

from syke.bot import Syke
from syke.config import load_settings
from syke.textfun import (
    EFFECTS,
    TextFun,
    clap,
    clean_source,
    drunk,
    high,
    keysmash,
    leet,
    mock,
    resolve_effect,
    reverse,
    scramble,
    stutter,
    uwu,
    zalgo,
)
from test_commands import run

import discord


@pytest.fixture()
def tcog(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "t.db"), ai_provider="none", web_enabled=False)
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, name="SYKE")
    return TextFun(bot)


def test_mock_alternates_caps():
    assert mock("hello world") == "hElLo WoRlD"   # toggle runs continuously across letters
    assert mock("ABC!") == "aBc!"


def test_scramble_keeps_ends_and_is_reversible_length():
    out = scramble("scramble these longwords", random.Random(1))
    for orig, new in zip("scramble these longwords".split(), out.split()):
        assert new[0] == orig[0] and new[-1] == orig[-1]
        assert sorted(new) == sorted(orig)      # same letters, maybe reordered
    assert scramble("hi ok cat", random.Random(1)) == "hi ok cat"   # short words untouched


def test_uwu_softens_rl_and_adds_face():
    out = uwu("really lovely", random.Random(0))
    assert "r" not in out.split(" uwu")[0].replace("really", "")  # r/l became w
    assert out.endswith((" uwu", " owo", " >w<", " :3", " ^w^", " (◕ᴥ◕)"))


def test_drunk_changes_text_but_stays_a_string():
    out = drunk("i am totally fine and sober", random.Random(3))
    assert isinstance(out, str) and out != "i am totally fine and sober"


def test_zalgo_adds_combining_marks():
    base = "cursed"
    out = zalgo(base, rng=random.Random(2))
    assert len(out) > len(base)
    assert all(c in out for c in base)   # original letters still present


def test_keysmash_degrades_and_appends_tail():
    out = keysmash("the quick brown fox jumps", random.Random(5))
    assert " " in out and out != "the quick brown fox jumps"


def test_clean_source_strips_pings():
    assert "@everyone" not in clean_source("hey @everyone look")
    assert "<@123>" not in clean_source("yo <@123> sup")
    assert clean_source("  spaced  ") == "spaced"
    assert len(clean_source("x" * 1000)) <= 400


def test_leet_reverse_clap():
    assert leet("ate toast") == "473 70457"
    assert reverse("abc") == "cba"
    assert clap("one two three") == "one 👏 two 👏 three"


def test_woke_appends_affirming_slogan():
    import random

    from syke.textfun import woke
    out = woke("i love mondays", random.Random(0))
    assert out.startswith("i love mondays — ")   # keeps their text, adds a slogan
    assert out != "i love mondays"


def test_apply_woke_uses_ai_then_falls_back(tcog, monkeypatch):
    from syke import ai
    from syke.textfun import woke, mock

    # With AI available, the woke effect uses the rewrite.
    async def fake_rewrite(settings, text, timeout=8.0):
        return "i love everyone"
    monkeypatch.setattr(ai, "woke_rewrite", fake_rewrite)
    assert run(tcog._apply(woke, "i hate everyone")) == "i love everyone"

    # Non-semantic effects never call AI — mechanical transform only.
    assert run(tcog._apply(mock, "hello")) == "hElLo"

    # Without AI (rewrite returns None), woke falls back to the slogan transform.
    async def no_ai(settings, text, timeout=8.0):
        return None
    monkeypatch.setattr(ai, "woke_rewrite", no_ai)
    out = run(tcog._apply(woke, "whatever"))
    assert out.startswith("whatever — ")


def test_high_and_stutter_change_text():
    import random
    out = high("i am completely normal right now", random.Random(1))
    assert isinstance(out, str) and out != "i am completely normal right now"
    st = stutter("never nervous", random.Random(1))
    assert "-" in st


def test_resolve_effect():
    assert resolve_effect("zalgo") == "zalgo"
    assert resolve_effect("owo") == "uwu"          # alias
    assert resolve_effect("SMASH") == "keysmash"   # case-insensitive alias
    assert resolve_effect("stoned") == "high"      # new drug/high alias
    assert resolve_effect("hacker") == "leet"
    assert resolve_effect("nope") is None
    # Every registered effect resolves to itself and is callable.
    for name, fn in EFFECTS.items():
        assert resolve_effect(name) == name and callable(fn)


def test_storage_effects_roundtrip(tcog):
    s = tcog.bot.storage
    s.set_effect(42, 7, "mock", 1)
    assert s.get_effect(42, 7) == "mock"
    assert s.list_effects(42) == [(7, "mock")]
    s.set_effect(42, 7, "zalgo", 1)                # replaces
    assert s.get_effect(42, 7) == "zalgo"
    assert s.clear_effect(42, 7) == "zalgo"
    assert s.get_effect(42, 7) is None and s.clear_effect(42, 7) is None


def _afflicted_message(content="hello world", attachments=(), bulk_fails=False):
    perms = types.SimpleNamespace(manage_messages=True, manage_webhooks=True)
    state = {"deleted": False, "bulk": None, "single": False, "events": []}

    async def delete_messages(messages, *, reason=None):
        if bulk_fails:
            raise discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"), "nope")
        state["deleted"] = True
        state["bulk"] = list(messages)
        state["events"].append("delete")

    async def delete():
        state["deleted"] = state["single"] = True
        state["events"].append("delete")

    channel = types.SimpleNamespace(id=10, parent=None, permissions_for=lambda m: perms,
                                    delete_messages=delete_messages)
    guild = types.SimpleNamespace(id=42, me=types.SimpleNamespace(id=999))
    author = types.SimpleNamespace(id=7, bot=False, display_name="Ada",
                                   display_avatar=types.SimpleNamespace(url="https://cdn/a.png"))
    msg = types.SimpleNamespace(id=1_300_000_000_000_000_000, guild=guild, author=author, webhook_id=None,
                                content=content, channel=channel, attachments=list(attachments), delete=delete)
    return msg, state


def test_afflicted_message_rewritten_via_webhook(tcog, monkeypatch):
    tcog.bot.storage.set_effect(42, 7, "mock", 1)
    sent = {}

    async def hook_send(content=None, **kw):
        sent["content"] = content
        sent["kw"] = kw

    async def fake_webhook(channel):
        return types.SimpleNamespace(send=hook_send)

    monkeypatch.setattr(tcog, "_webhook_for", fake_webhook)
    msg, state = _afflicted_message("hello world")
    run(tcog.on_message(msg))
    assert state["deleted"] is True
    assert sent["content"] == "hElLo WoRlD"             # mock effect applied
    assert sent["kw"]["username"] == "Ada"             # reposted as the user
    assert sent["kw"]["avatar_url"] == "https://cdn/a.png"


def test_listener_skips_commands_and_unafflicted(tcog, monkeypatch):
    calls = {"hook": 0}

    async def fake_webhook(channel):
        calls["hook"] += 1
        return None

    monkeypatch.setattr(tcog, "_webhook_for", fake_webhook)
    # Unafflicted user: no rewrite.
    msg, state = _afflicted_message("just chatting")
    run(tcog.on_message(msg))
    assert state["deleted"] is False and calls["hook"] == 0
    # Afflicted, but it's a command invocation: left alone.
    tcog.bot.storage.set_effect(42, 7, "mock", 1)
    cmd, cstate = _afflicted_message("!help")
    run(tcog.on_message(cmd))
    assert cstate["deleted"] is False and calls["hook"] == 0


def _hook(tcog, monkeypatch, state=None):
    sent = []

    async def hook_send(content=None, **kw):
        if state is not None:
            state["events"].append("repost")
        sent.append((content, kw))

    async def fake_webhook(channel):
        return types.SimpleNamespace(send=hook_send)

    monkeypatch.setattr(tcog, "_webhook_for", fake_webhook)
    return sent


def test_afflicted_message_vanishes_through_bulk_delete(tcog, monkeypatch):
    """Snipe bots log single deletes; a bulk delete with a placeholder ID slips past them."""
    tcog.bot.storage.set_effect(42, 7, "mock", 1)
    msg, state = _afflicted_message("hello world")
    sent = _hook(tcog, monkeypatch, state)
    run(tcog.on_message(msg))
    original, ghost = state["bulk"]
    assert original is msg and not state["single"]
    assert ghost.id != msg.id and isinstance(ghost, discord.Object)   # placeholder that doesn't exist
    assert state["events"] == ["delete", "repost"]                     # gone before the rewrite goes up
    assert sent[0][0] == "hElLo WoRlD"


def test_vanish_falls_back_to_a_normal_delete(tcog, monkeypatch):
    tcog.bot.storage.set_effect(42, 7, "mock", 1)
    msg, state = _afflicted_message("hello", bulk_fails=True)
    _hook(tcog, monkeypatch, state)
    run(tcog.on_message(msg))
    assert state["single"] is True


def test_afflicted_reposts_are_text_only(tcog, monkeypatch):
    tcog.bot.storage.set_effect(42, 7, "mock", 1)
    file = types.SimpleNamespace(url="https://cdn/secret.png")
    msg, state = _afflicted_message("look https://example.com", attachments=[file])
    sent = _hook(tcog, monkeypatch, state)
    run(tcog.on_message(msg))
    content, kw = sent[0]
    assert "secret.png" not in content                 # attachments aren't carried over
    assert kw["suppress_embeds"] is True              # and links don't unfurl
    assert kw["allowed_mentions"].everyone is False


def test_afflicted_file_only_messages_just_disappear(tcog, monkeypatch):
    tcog.bot.storage.set_effect(42, 7, "mock", 1)
    msg, state = _afflicted_message("", attachments=[types.SimpleNamespace(url="https://cdn/x.png")])
    sent = _hook(tcog, monkeypatch, state)
    run(tcog.on_message(msg))
    assert state["deleted"] is True and sent == []


def test_text_only_role_denies_everything_but_typing():
    from syke.textfun import AFFLICT_DENY
    for perm in ("attach_files", "embed_links", "use_application_commands", "use_external_apps",
                 "add_reactions", "use_external_stickers", "send_polls", "send_voice_messages"):
        assert perm in AFFLICT_DENY
    assert "send_messages" not in AFFLICT_DENY and "read_message_history" not in AFFLICT_DENY


class _FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.overwrites = {}

    def overwrites_for(self, role):
        return self.overwrites.get(role.id, discord.PermissionOverwrite())

    async def set_permissions(self, role, *, overwrite, reason=None):
        self.overwrites[role.id] = overwrite


def _guild_with_roles(manage_roles=True):
    roles = {}
    channels = [_FakeChannel(1), _FakeChannel(2)]
    guild = types.SimpleNamespace(id=42, channels=channels, get_role=lambda rid: roles.get(rid),
                                  me=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(
                                      manage_roles=manage_roles)))

    async def create_role(*, name, permissions, mentionable, reason):
        role = types.SimpleNamespace(id=555, name=name)
        roles[555] = role
        return role

    guild.create_role = create_role
    return guild, channels


def _member(guild):
    member = types.SimpleNamespace(id=7, bot=False, guild=guild, roles=[])

    async def add_roles(*roles, reason=None):
        member.roles.extend(roles)

    async def remove_roles(*roles, reason=None):
        for r in roles:
            member.roles.remove(r)

    member.add_roles, member.remove_roles = add_roles, remove_roles
    return member


def test_lock_to_text_creates_the_role_and_denies_it_everywhere(tcog):
    from syke.textfun import AFFLICT_DENY
    guild, channels = _guild_with_roles()
    spawned = []
    tcog.bot.spawn = lambda coro: spawned.append(coro)
    member = _member(guild)
    assert run(tcog._lock_to_text(member)) is True
    assert [r.name for r in member.roles] == ["SYKE · afflicted"]
    for coro in spawned:
        run(coro)
    for ch in channels:
        ow = ch.overwrites[555]
        assert all(getattr(ow, p) is False for p in AFFLICT_DENY)
        assert ow.send_messages is None                  # they can still type
    # new channels get the overwrite too
    fresh = _FakeChannel(3)
    fresh.guild = guild
    run(tcog.on_guild_channel_create(fresh))
    assert fresh.overwrites[555].attach_files is False


def test_lock_to_text_needs_manage_roles(tcog):
    guild, _ = _guild_with_roles(manage_roles=False)
    assert run(tcog._lock_to_text(_member(guild))) is False


def test_rejoining_doesnt_shake_off_the_lock(tcog):
    guild, _ = _guild_with_roles()
    tcog.bot.spawn = lambda coro: coro.close()
    tcog.bot.storage.set_effect(42, 7, "uwu", 1)
    member = _member(guild)
    run(tcog.on_member_join(member))
    assert [r.name for r in member.roles] == ["SYKE · afflicted"]


def test_cure_lifts_the_text_only_lock(tcog):
    guild, _ = _guild_with_roles()
    tcog.bot.spawn = lambda coro: coro.close()
    tcog.bot.storage.set_effect(42, 7, "uwu", 1)
    member = _member(guild)
    member.mention = "<@7>"
    run(tcog._lock_to_text(member))
    replies = []

    async def reply(content=None, **kw):
        replies.append(kw.get("embed"))

    ctx = types.SimpleNamespace(guild=guild, reply=reply)
    run(tcog.cure_cmd.callback(tcog, ctx, member))
    assert member.roles == [] and tcog.bot.storage.get_effect(42, 7) is None
    assert "Cured" in replies[-1].title
