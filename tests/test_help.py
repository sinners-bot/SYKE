"""The help menu must document every real command, and only real commands."""

import types
from dataclasses import replace

import pytest

from syke.automod import AutoMod
from syke.bot import Syke, SykeCommands
from syke.community import Community
from syke.config import load_settings
from syke.dashboard import DashboardCog
from syke.help import (
    CATALOG,
    CATEGORY_OF,
    category_embed,
    command_embed,
    overview_embed,
    resolve_command,
)
from syke.moderation import Moderation
from syke.security import Security
from test_commands import run


@pytest.fixture()
def bot(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "h.db"), ai_provider="none", web_enabled=False)
    b = Syke(settings)
    b._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    names = []
    for cog in (SykeCommands(b), Moderation(b), AutoMod(b), Security(b), Community(b), DashboardCog(b)):
        run(b.add_cog(cog))
        names.append(cog.qualified_name)
    yield b
    for name in names:  # unloads cogs -> cancels their background loops, no leaked tasks
        run(b.remove_cog(name))


def _top_level_names(bot):
    return {c.qualified_name for c in bot.commands if not c.hidden}


def test_every_command_is_in_a_category(bot):
    documented = set(CATEGORY_OF)
    live = _top_level_names(bot)
    missing = live - documented
    assert not missing, f"commands not in any help category: {sorted(missing)}"


def test_catalog_has_no_stale_entries(bot):
    live = _top_level_names(bot)
    stale = set(CATEGORY_OF) - live
    assert not stale, f"help lists commands that don't exist: {sorted(stale)}"


def test_each_command_lives_in_exactly_one_category():
    seen = {}
    for cat in CATALOG:
        for name in cat.commands:
            assert name not in seen, f"{name} is in both {seen[name]} and {cat.key}"
            seen[name] = cat.key


def test_embeds_build_for_every_category(bot):
    ov = overview_embed(bot, "!", "v1")
    assert ov.description and all(c.title in ov.description for c in CATALOG)
    for cat in CATALOG:
        e = category_embed(bot, cat, "!")
        assert e.description and cat.title in e.title
        # Commands render as compact one-liners (name — description), not two lines.
        assert "╰" not in e.description


def test_command_detail_includes_subcommands(bot):
    automod = resolve_command(bot, "automod")
    e = command_embed(bot, automod, "!")
    names = " ".join(f.value for f in e.fields)
    assert "ignore" in names and "toxic" in names   # sub-commands are listed
    assert any("Usage" == f.name for f in e.fields)


def test_resolve_by_alias_and_subcommand(bot):
    assert resolve_command(bot, "commands").name == "help"      # alias of help
    assert resolve_command(bot, "/profile").name == "profile"   # slash slash stripped
    assert resolve_command(bot, "antinuke").name == "antinuke"
    assert resolve_command(bot, "nonsense-xyz") is None
