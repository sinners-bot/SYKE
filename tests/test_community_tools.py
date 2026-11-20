"""Tickets, quote cards, starboard, sticky messages, scheduled announcements and role menus."""

import asyncio
import io
import types
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
import pytest
from PIL import Image

from syke import notices, quotes, rolemenus, starboard, tickets
from syke.bot import Syke
from syke.config import load_settings

GID = 42
NS = types.SimpleNamespace


def make_bot(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "c.db"), ai_provider="none", web_enabled=False)
    bot = Syke(settings)
    bot._connection.user = NS(id=999, mention="<@999>", name="SYKE")
    return bot


class Sent:
    def __init__(self, ident, channel=None, **kwargs):
        self.id = ident
        self.channel = channel
        self.kwargs = kwargs
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class Channel:
    _next = 1000

    def __init__(self, ident=10, name="general", guild=None):
        self.id, self.name, self.guild = ident, name, guild
        self.mention = f"<#{ident}>"
        self.sent: list[Sent] = []
        self.partials: dict[int, Sent] = {}
        self.threads: list = []
        self.messages: dict[int, object] = {}

    def is_nsfw(self):
        return False

    def permissions_for(self, who):
        return discord.Permissions.all()

    async def send(self, content=None, **kwargs):
        Channel._next += 1
        msg = Sent(Channel._next, self, content=content, **kwargs)
        self.sent.append(msg)
        self.partials[msg.id] = msg
        return msg

    def get_partial_message(self, ident):
        return self.partials.setdefault(ident, Sent(ident, self))

    async def fetch_message(self, ident):
        return self.messages[ident]

    async def create_thread(self, **kwargs):
        thread = Thread(5000 + len(self.threads), kwargs["name"], self.guild)
        thread.kwargs = kwargs
        self.threads.append(thread)
        return thread


class Thread(Channel):
    def __init__(self, ident, name, guild):
        super().__init__(ident, name, guild)
        self.users, self.edits, self.history_messages = [], [], []

    async def add_user(self, user):
        self.users.append(user)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def history(self, limit=None, oldest_first=False):
        for m in self.history_messages:
            yield m


class Member:
    def __init__(self, ident, name="member", roles=(), perms=None, top=1):
        self.id, self.name, self.display_name = ident, name, name.title()
        self.mention = f"<@{ident}>"
        self.roles = list(roles)
        self.guild_permissions = perms or discord.Permissions.none()
        self.top_role = NS(position=top)
        self.bot = False
        self.dms = []
        self.display_avatar = NS(url=f"https://cdn/{ident}.png")

    async def send(self, *args, **kwargs):
        self.dms.append((args, kwargs))

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class Role:
    def __init__(self, ident, name="role", position=5, perms=None):
        self.id, self.name, self.position = ident, name, position
        self.permissions = perms or discord.Permissions.none()
        self.managed = False
        self.mention = f"<@&{ident}>"

    def is_default(self):
        return False


class Guild:
    def __init__(self, channels=(), roles=(), members=()):
        self.id, self.name, self.owner_id = GID, "Test Server", 1
        self.channels = {c.id: c for c in channels}
        for c in channels:
            c.guild = self
        self.role_map = {r.id: r for r in roles}
        self.members = {m.id: m for m in members}
        self.me = NS(id=999, top_role=NS(position=50), guild_permissions=discord.Permissions.all())

    def get_channel(self, ident):
        return self.channels.get(ident)

    get_channel_or_thread = get_channel

    def get_role(self, ident):
        return self.role_map.get(ident)

    def get_member(self, ident):
        return self.members.get(ident)

    def get_emoji(self, ident):
        return None


# Tickets

def test_ticket_helpers():
    assert tickets.thread_name(7, NS(name="Lucky.Lu!!")) == "ticket-0007-luckylu"
    assert tickets.duration(59) == "1m" and tickets.duration(3700) == "1h 1m" and tickets.duration(90000) == "1d 1h"
    staff = Member(5, roles=[Role(77)])
    assert tickets.is_staff(staff, {"staff": 77})
    assert not tickets.is_staff(Member(6), {"staff": 77})
    assert tickets.is_staff(Member(6, perms=discord.Permissions(manage_threads=True)), {})


def test_transcript_escapes_and_names_mentions():
    author = Member(5, "lu")
    msg = NS(author=author, content="hey <@6> look <script>alert(1)</script> **bold** https://x.y/z",
             mentions=[Member(6, "bo")], role_mentions=[], channel_mentions=[], attachments=[
                 NS(filename="a.png", url="https://cdn/a.png")], embeds=[], created_at=datetime.now(timezone.utc))
    page = tickets.transcript_html("Ticket #0001", "Server", [tickets.message_record(msg)])
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "@Bo" in page and "<b>bold</b>" in page and 'href="https://x.y/z"' in page
    assert "a.png" in page and "1 message(s)" in page


async def test_ticket_open_limit_and_close(tmp_path):
    bot = make_bot(tmp_path)
    cog = tickets.Tickets(bot)
    panel, logs = Channel(10, "support"), Channel(11, "ticket-logs")
    member, staff_role = Member(5, "lu"), Role(77, "Staff")
    guild = Guild([panel, logs], [staff_role], [member])
    bot.storage.update_config(GID, tickets.CONFIG, channel=10, staff=77, log=11)

    ok, text = await cog.open_ticket(guild, member, "my bet didn't pay")
    assert ok and "<#5000>" in text
    thread = panel.threads[0]
    assert thread.kwargs["type"] == discord.ChannelType.private_thread and thread.users == [member]
    assert "<@&77>" in thread.sent[0].kwargs["content"]
    assert (await cog.open_ticket(guild, member, "again"))[0] is False  # one open ticket each

    thread.history_messages = [NS(author=member, content="hello", mentions=[], role_mentions=[], channel_mentions=[],
                                  attachments=[], embeds=[], created_at=datetime.now(timezone.utc))]
    stranger = Member(9)
    assert cog.can_close(thread, stranger)
    assert cog.can_close(thread, member) is None
    await cog.close_ticket(thread, member, "sorted")
    assert logs.sent and logs.sent[0].kwargs["file"].filename == "transcript-0001.html"
    assert member.dms and thread.edits[-1]["archived"] and thread.edits[-1]["locked"]
    assert cog.config(GID)["open"] == {}
    assert (await cog.open_ticket(guild, member, "new problem"))[0]  # closed, so a new one is fine


# Quote cards

def test_clean_text_and_tokens():
    msg = NS(content="**hey** <@5> look at <#7> 😭 <:pog:123> ~~old~~", mentions=[Member(5, "lu")], role_mentions=[],
             channel_mentions=[NS(id=7, name="memes")])
    text = quotes.clean_text(msg)
    assert text == "hey @Lu look at #memes 😭 <:pog:123> old"
    tokens = quotes.tokenize(text)
    emojis = [v for k, v in tokens if k == "emoji"]
    assert emojis[0].endswith("/1f62d.png") and emojis[1] == "https://cdn.discordapp.com/emojis/123.png?size=96"
    assert ("word", "hey") in tokens and ("word", "@Lu") in tokens


def test_twemoji_codes():
    assert quotes.twemoji_code("❤️") == "2764"  # the variation selector is dropped…
    assert quotes.twemoji_code("👩‍💻") == "1f469-200d-1f4bb"  # …but kept in ZWJ sequences


@pytest.mark.parametrize("flip,colour", [(False, False), (True, True)])
def test_quote_card_renders(flip, colour):
    avatar = io.BytesIO()
    Image.new("RGB", (256, 256), (200, 100, 50)).save(avatar, "PNG")
    tokens = quotes.tokenize("the slot machines know me by name 😭")
    smile = Image.new("RGBA", (72, 72), (255, 200, 0, 255))
    png = quotes.render_quote(tokens, "Lucky Lu", "luckylu", avatar.getvalue(),
                              {quotes.emoji_urls(tokens)[0]: smile}, colour=colour, flip=flip)
    card = Image.open(io.BytesIO(png))
    assert card.size == (1200, 630)
    face_x = 1000 if flip else 100
    r, g, b = card.convert("RGB").getpixel((face_x, 300))
    assert (r != g) == colour  # black and white unless colour is on


def test_long_quotes_shrink_and_cut():
    size, lines = quotes.fit(quotes.tokenize("word " * 400))
    assert size == quotes.MIN_SIZE and lines[-1][-1] == ("word", "…")
    size, _ = quotes.fit(quotes.tokenize("hi"))
    assert size == quotes.MAX_SIZE


# Starboard

class Reaction:
    def __init__(self, emoji, users):
        self.emoji, self._users = emoji, users

    async def users(self, limit=None):
        for u in self._users:
            yield u


async def test_starboard_posts_updates_and_removes(tmp_path):
    bot = make_bot(tmp_path)
    cog = starboard.Starboard(bot)
    source, board = Channel(10, "general"), Channel(20, "hall")
    guild = Guild([source, board])
    author = Member(5, "lu")
    fans = [Member(i) for i in range(6, 9)]
    msg = NS(id=77, author=author, content="peak comedy", attachments=[], embeds=[], reference=None, channel=source,
             created_at=datetime.now(timezone.utc), jump_url="https://discord.com/channels/42/10/77",
             reactions=[Reaction("⭐", [author] + fans[:2])])
    source.messages[77] = msg
    config = {"channel": 20, "threshold": 3}
    await cog._update(guild, board, source, 77, "⭐", config)
    assert not board.sent  # the author's own star doesn't count: 2 of 3
    msg.reactions = [Reaction("⭐", [author] + fans)]
    await cog._update(guild, board, source, 77, "⭐", config)
    assert len(board.sent) == 1 and "**3**" in board.sent[0].kwargs["content"]
    msg.reactions = [Reaction("⭐", fans + [Member(i) for i in range(10, 20)])]
    await cog._update(guild, board, source, 77, "⭐", config)
    assert len(board.sent) == 1 and "🌟 **13**" in board.sent[0].edits[-1]["content"]
    msg.reactions = [Reaction("⭐", fans[:1])]
    await cog._update(guild, board, source, 77, "⭐", config)
    assert board.sent[0].deleted and cog.posts(GID) == {}


def test_starboard_emoji_keys():
    assert starboard.emoji_key("⭐") == "⭐"
    assert starboard.emoji_key(discord.PartialEmoji(name="pog", id=123)) == "123"
    assert starboard.star_tier(3) == "⭐" and starboard.star_tier(10) == "🌟" and starboard.star_tier(30) == "💫"


# Announcements and sticky messages

VIENNA = ZoneInfo("Europe/Vienna")
NOW = datetime(2026, 11, 20, 23, 55, tzinfo=VIENNA)  # a Friday


@pytest.mark.parametrize("text,expected", [
    ("in 2h", datetime(2026, 11, 21, 1, 55, tzinfo=VIENNA)),
    ("2h30m", datetime(2026, 11, 21, 2, 25, tzinfo=VIENNA)),
    ("18:00", datetime(2026, 11, 21, 18, 0, tzinfo=VIENNA)),
    ("6pm", datetime(2026, 11, 21, 18, 0, tzinfo=VIENNA)),
    ("tomorrow 6:30pm", datetime(2026, 11, 21, 18, 30, tzinfo=VIENNA)),
    ("fri 20:00", datetime(2026, 11, 27, 20, 0, tzinfo=VIENNA)),
    ("saturday at 9am", datetime(2026, 11, 21, 9, 0, tzinfo=VIENNA)),
    ("24.12 18:00", datetime(2026, 12, 24, 18, 0, tzinfo=VIENNA)),
    ("1.1", datetime(2027, 1, 1, 9, 0, tzinfo=VIENNA)),
    ("2027-01-05 08:30", datetime(2027, 1, 5, 8, 30, tzinfo=VIENNA)),
    ("18", None), ("25:00", None), ("whenever", None), ("31.02 10:00", None),
])
def test_parse_when(text, expected):
    assert notices.parse_when(text, NOW) == expected


def test_repeats():
    jan31 = datetime(2027, 1, 31, 18, 0, tzinfo=VIENNA)
    assert notices.next_time(jan31, "monthly", jan31) == datetime(2027, 2, 28, 18, 0, tzinfo=VIENNA)
    assert notices.next_time(jan31, "once", jan31) is None
    assert notices.next_time(jan31, "daily", datetime(2027, 2, 3, 12, tzinfo=VIENNA)).day == 3
    assert notices.split_repeat("fri 20:00 weekly") == ("fri 20:00", "weekly")
    assert notices.split_repeat("tomorrow 6pm every day") == ("tomorrow 6pm", "daily")
    assert notices.split_repeat("in 2h") == ("in 2h", "once")


async def test_announce_loop_posts_and_reschedules(tmp_path):
    bot = make_bot(tmp_path)
    cog = notices.Notices(bot)
    news = Channel(10, "news")
    guild = Guild([news])
    bot.get_guild = lambda gid: guild if gid == GID else None
    now = datetime.now(bot.tz_for(GID)).timestamp()
    items = {
        "1": {"channel": 10, "text": "once", "at": now - 5, "repeat": "once"},
        "2": {"channel": 10, "text": "daily", "at": now - 5, "repeat": "daily"},
        "3": {"channel": 10, "text": "missed", "at": now - 7200, "repeat": "once"},
        "4": {"channel": 10, "text": "later", "at": now + 3600, "repeat": "once"},
    }
    bot.storage.set_config(GID, notices.ANNOUNCE, {"next": 5, "items": items})
    await notices.Notices.announce_loop.coro(cog)
    assert [m.kwargs["content"] for m in news.sent] == ["once", "daily"]
    left = bot.storage.get_config(GID, notices.ANNOUNCE)["items"]
    assert set(left) == {"2", "4"} and left["2"]["at"] > now + 80000


async def test_schedule_respects_everyone_permission(tmp_path):
    bot = make_bot(tmp_path)
    cog = notices.Notices(bot)
    guild = Guild([Channel(10)])
    card = cog.schedule(guild, Member(5), guild.get_channel(10), NOW, "weekly", "@everyone game night")
    assert "#1" in card.title
    item = bot.storage.get_config(GID, notices.ANNOUNCE)["items"]["1"]
    assert item["everyone"] is False and item["repeat"] == "weekly"


async def test_sticky_reposts_at_the_bottom(tmp_path):
    bot = make_bot(tmp_path)
    cog = notices.Notices(bot)
    channel = Channel(10)
    guild = Guild([channel])
    bot.storage.set_config(GID, notices.STICKY, {"10": {"text": "read the rules", "last": 555}})
    await cog._restick(guild, channel, delay=0)
    assert channel.partials[555].deleted
    first = channel.sent[-1]
    assert first.kwargs["embed"].footer.text == notices.STICKY_FOOTER
    assert bot.storage.get_config(GID, notices.STICKY)["10"]["last"] == first.id
    await cog._restick(guild, channel, delay=0)
    assert first.deleted and channel.sent[-1] is not first


# Role menus

def test_role_rules():
    guild = Guild()
    assert rolemenus.role_issue(Role(1, position=5), guild) is None
    assert "moderator" in rolemenus.role_issue(Role(1, perms=discord.Permissions(manage_messages=True)), guild)
    assert "above" in rolemenus.role_issue(Role(1, position=60), guild)
    assert "your own" in rolemenus.role_issue(Role(1, position=5), guild, Member(7, top=3))
    assert rolemenus.role_issue(Role(1, position=5), guild, Member(1, top=3)) is None  # the owner


def test_emoji_parsing():
    assert rolemenus.parse_emoji("🎮") == ("🎮", None)
    assert rolemenus.parse_emoji("<:pog:123>") == ("<:pog:123>", None)
    assert rolemenus.parse_emoji("gamers")[1]


def setup_menu(bot, exclusive=True, style="buttons"):
    red, blue = Role(101, "Red"), Role(102, "Blue")
    guild = Guild([Channel(10)], [red, blue])
    menu = {"channel": 10, "message": 900, "title": "Colours", "style": style, "exclusive": exclusive,
            "items": [{"role": 101, "emoji": "🔴", "label": None}, {"role": 102, "emoji": "🔵", "label": None}]}
    bot.storage.set_config(GID, rolemenus.CONFIG, {"next": 2, "menus": {"1": menu}})
    return guild, red, blue, menu


async def test_exclusive_menu_swaps_roles(tmp_path):
    bot = make_bot(tmp_path)
    cog = rolemenus.RoleMenus(bot)
    guild, red, blue, _ = setup_menu(bot)
    member = Member(5, roles=[red])
    text = await cog.toggle(guild, member, 1, 102)
    assert member.roles == [blue] and "swapped from Red" in text
    assert await cog.toggle(guild, member, 1, 102) == "Removed **Blue**."
    assert "no longer exists" in await cog.toggle(guild, member, 1, 999)


async def test_dropdown_sets_exactly_the_picked_roles(tmp_path):
    bot = make_bot(tmp_path)
    cog = rolemenus.RoleMenus(bot)
    guild, red, blue, _ = setup_menu(bot, exclusive=False, style="dropdown")
    member = Member(5, roles=[red])
    await cog.set_roles(guild, member, 1, {102})
    assert member.roles == [blue]
    await cog.set_roles(guild, member, 1, set())
    assert member.roles == []


async def test_menu_views_use_restart_proof_ids(tmp_path):
    bot = make_bot(tmp_path)
    guild, _, _, menu = setup_menu(bot)
    view = rolemenus.menu_view(1, menu, guild)
    ids = [item.custom_id for item in view.children]
    assert ids == ["syke:rm:1:101", "syke:rm:1:102"]
    menu["style"] = "dropdown"
    select = rolemenus.menu_view(1, menu, guild).children[0].item
    assert select.custom_id == "syke:rms:1" and select.max_values == 1 and select.min_values == 0
    assert rolemenus.RoleButton.__discord_ui_compiled_template__.fullmatch("syke:rm:3:44")
    menu["style"] = "reactions"
    assert rolemenus.menu_view(1, menu, guild) is None
    assert "one at a time" in rolemenus.menu_embed(menu).footer.text


# Command callbacks

class Ctx:
    def __init__(self, guild, author, channel, interaction=None):
        self.guild, self.author, self.channel = guild, author, channel
        self.interaction = interaction
        self.prefix, self.clean_prefix = "!", "!"
        self.message = NS(reference=None)
        self.replies = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(kwargs.get("embed") or content)
        return Sent(1)

    async def defer(self, **kwargs):
        return None


async def test_announce_add_from_prefix_text(tmp_path):
    bot = make_bot(tmp_path)
    cog = notices.Notices(bot)
    news = Channel(10, "news")
    guild = Guild([news])
    ctx = Ctx(guild, Member(5, perms=discord.Permissions(manage_guild=True)), news)
    await cog.announce_add.callback(cog, ctx, news, when="tomorrow 18:00 weekly | Game night\\nBring snacks")
    item = bot.storage.get_config(GID, notices.ANNOUNCE)["items"]["1"]
    assert item["repeat"] == "weekly" and item["text"] == "Game night\nBring snacks"
    await cog.announce_add.callback(cog, ctx, news, when="someday | hi")
    assert "couldn't read" in ctx.replies[-1].description
    await cog.announce_add.callback(cog, ctx, news, when="18:00")
    assert "after a `|`" in ctx.replies[-1].description


async def test_sticky_and_rolemenu_commands(tmp_path):
    bot = make_bot(tmp_path)
    channel = Channel(10)
    red = Role(101, "Red")
    guild = Guild([channel], [red])
    admin = Member(1, perms=discord.Permissions.all(), top=40)
    ctx = Ctx(guild, admin, channel)
    sticky = notices.Notices(bot)
    await sticky.sticky_set.callback(sticky, ctx, None, text="Read #rules first")
    assert channel.sent[-1].kwargs["embed"].description == "Read #rules first"
    menus = rolemenus.RoleMenus(bot)
    await menus.rolemenu_create.callback(menus, ctx, "Games", "buttons", False, None, description="")
    assert "Menu #1 posted" in ctx.replies[-1].title
    await menus.rolemenu_add.callback(menus, ctx, 1, red, "Redstone")  # a word, not an emoji: it's the label
    item = menus.menu(GID, 1)["items"][0]
    assert item == {"role": 101, "emoji": None, "label": "Redstone"}
    edited = channel.partials[menus.menu(GID, 1)["message"]].edits[-1]
    assert edited["view"].children[0].item.label == "Redstone"
