"""Anti-nuke / anti-raid logic, tested without a live Discord."""

import types
from dataclasses import replace

import pytest

from syke.bot import Syke
from syke.config import load_settings
from syke.security import DEFAULTS, Security, cfg, trips
from test_commands import run


# Pure window helper

def test_trips_counts_only_inside_window():
    now = 100.0
    assert trips([99.0, 99.5, 100.0], now, 10, 3) is True
    assert trips([80.0, 99.5, 100.0], now, 10, 3) is False   # one is too old
    assert trips([100.0, 100.0], now, 10, 3) is False        # not enough
    assert trips([], now, 10, 1) is False


@pytest.fixture()
def sec(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "s.db"), ai_provider="none", web_enabled=False)
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    return Security(bot)


def _guild(gid=42, owner_id=1):
    return types.SimpleNamespace(id=gid, owner_id=owner_id)


# Anti-nuke counting + exemptions

def test_register_nuke_trips_at_limit(sec):
    config = dict(DEFAULTS, an_limit=3, an_window=10)
    t = 0.0
    assert sec.register_nuke(42, 500, "channel_delete", config, now=t) is False
    assert sec.register_nuke(42, 500, "channel_delete", config, now=t + 1) is False
    assert sec.register_nuke(42, 500, "channel_delete", config, now=t + 2) is True   # 3rd within 10s


def test_register_nuke_separates_actors_and_kinds(sec):
    config = dict(DEFAULTS, an_limit=3, an_window=10)
    # Same actor, different action kinds don't add up together.
    sec.register_nuke(42, 500, "channel_delete", config, now=0)
    sec.register_nuke(42, 500, "role_delete", config, now=1)
    assert sec.register_nuke(42, 500, "channel_delete", config, now=2) is False
    # A different actor has their own count.
    assert sec.register_nuke(42, 600, "channel_delete", config, now=2) is False


def test_an_exempt_protects_owner_bot_and_whitelist(sec):
    guild = _guild(owner_id=1)
    config = dict(DEFAULTS, whitelist=[777])
    assert sec.an_exempt(guild, None, config) is True                        # unknown actor
    assert sec.an_exempt(guild, types.SimpleNamespace(id=1), config) is True   # server owner
    assert sec.an_exempt(guild, types.SimpleNamespace(id=999), config) is True # the bot itself
    assert sec.an_exempt(guild, types.SimpleNamespace(id=777), config) is True # whitelisted
    assert sec.an_exempt(guild, types.SimpleNamespace(id=500), config) is False # a stranger


def test_strip_roles_drops_only_dangerous_manageable_roles(sec):
    class Perms:
        def __init__(self, **kw):
            self._kw = kw

        def __getattr__(self, name):
            return self._kw.get(name, False)

    def role(rid, pos, perms, default=False, managed=False):
        return types.SimpleNamespace(id=rid, position=pos, permissions=Perms(**perms),
                                     is_default=lambda: default, managed=managed)

    admin = role(2, 5, {"administrator": True})
    banner = role(3, 4, {"ban_members": True})
    plain = role(4, 3, {})
    too_high = role(5, 20, {"manage_guild": True})   # above the bot's top role
    removed = []

    async def remove_roles(*roles, reason=None):
        removed.extend(roles)

    member = types.SimpleNamespace(roles=[admin, banner, plain, too_high], remove_roles=remove_roles)
    guild = types.SimpleNamespace(me=types.SimpleNamespace(top_role=types.SimpleNamespace(position=10)))

    n = run(sec.strip_roles(guild, member, "test"))
    assert n == 2
    assert admin in removed and banner in removed
    assert plain not in removed and too_high not in removed


# Anti-raid

def test_is_young_respects_age_gate(sec):
    from datetime import datetime, timedelta, timezone
    old = types.SimpleNamespace(created_at=datetime.now(timezone.utc) - timedelta(days=100))
    fresh = types.SimpleNamespace(created_at=datetime.now(timezone.utc) - timedelta(hours=2))
    assert sec.is_young(old, dict(DEFAULTS, ar_min_age_days=0)) is True    # gate off -> all count
    assert sec.is_young(old, dict(DEFAULTS, ar_min_age_days=7)) is False   # established account
    assert sec.is_young(fresh, dict(DEFAULTS, ar_min_age_days=7)) is True  # brand new


def test_join_flood_trips_raid_mode_and_kicks(sec):
    kicked = []
    saved = {}

    def get_config(gid, key):
        return dict(DEFAULTS, antiraid=True, ar_count=3, ar_window=10, ar_action="kick", **saved)

    def update_config(gid, key, **changes):
        saved.update(changes)
        return get_config(gid, key)

    def modlog_channel(gid):
        return None

    sec.bot.storage.get_config = get_config
    sec.bot.storage.update_config = update_config
    sec.bot.storage.modlog_channel = modlog_channel

    async def kick(reason=None):
        kicked.append(True)

    from datetime import datetime, timezone

    def member(mid):
        return types.SimpleNamespace(id=mid, bot=False, guild=_guild(),
                                     created_at=datetime.now(timezone.utc), kick=kick)

    # Three fast joins should trip raid mode and sweep the burst.
    for mid in (10, 11, 12):
        run(sec.on_member_join(member(mid)))

    assert saved.get("raid_mode") is True
    assert len(kicked) >= 3
