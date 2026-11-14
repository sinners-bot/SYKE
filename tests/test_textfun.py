"""The text-mangling toys: deterministic with a seeded rng, and ping-safe."""

import random
import types
from dataclasses import replace

import pytest

from syke.bot import Syke
from syke.config import load_settings
from syke.textfun import TextFun, clean_source, drunk, keysmash, mock, resolve_effect, scramble, uwu, zalgo
from test_commands import run


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


def test_resolve_effect():
    assert resolve_effect("zalgo") == "zalgo"
    assert resolve_effect("owo") == "uwu"          # alias
    assert resolve_effect("SMASH") == "keysmash"   # case-insensitive alias
    assert resolve_effect("nope") is None


def test_storage_effects_roundtrip(tcog):
    s = tcog.bot.storage
    s.set_effect(42, 7, "mock", 1)
    assert s.get_effect(42, 7) == "mock"
    assert s.list_effects(42) == [(7, "mock")]
    s.set_effect(42, 7, "zalgo", 1)                # replaces
    assert s.get_effect(42, 7) == "zalgo"
    assert s.clear_effect(42, 7) == "zalgo"
    assert s.get_effect(42, 7) is None and s.clear_effect(42, 7) is None


def _afflicted_message(content="hello world", attachments=()):
    perms = types.SimpleNamespace(manage_messages=True, manage_webhooks=True)
    channel = types.SimpleNamespace(id=10, parent=None, permissions_for=lambda m: perms)
    guild = types.SimpleNamespace(id=42, me=types.SimpleNamespace(id=999))
    author = types.SimpleNamespace(id=7, bot=False, display_name="Ada",
                                   display_avatar=types.SimpleNamespace(url="https://cdn/a.png"))
    state = {"deleted": False}

    async def delete():
        state["deleted"] = True

    msg = types.SimpleNamespace(guild=guild, author=author, webhook_id=None, content=content,
                                channel=channel, attachments=list(attachments), delete=delete)
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
