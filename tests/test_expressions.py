import io
import random
import types

import discord
import pytest
from PIL import Image

from syke import expressions
from syke.bot import _admin_predicate
from syke.expressions import (
    choose_sources,
    clean_name,
    download_bytes,
    harvest_message,
    host_is_public,
    parse_command_text,
    prepare_emoji_image,
    prepare_sticker_file,
)

from test_commands import FakeCtx, run


PNG_ID = 123456789012345678
GIF_ID = 123456789012345679


def _png(size=(24, 16), colour=(200, 20, 20, 255)) -> bytes:
    image = Image.new("RGBA", size, colour)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _jpg(size=(40, 20)) -> bytes:
    image = Image.new("RGB", size, (10, 80, 200))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _gif(frames=((255, 0, 0), (0, 255, 0), (0, 0, 255)), size=(12, 30)) -> bytes:
    images = [Image.new("RGB", size, colour) for colour in frames]
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:], duration=80, loop=0)
    return buf.getvalue()


class Attachment:
    def __init__(self, data: bytes, filename: str, content_type: str):
        self._data = data
        self.filename = filename
        self.content_type = content_type
        self.size = len(data)
        self.id = 55

    async def read(self):
        return self._data


class ServerEmoji:
    def __init__(self, emoji_id: int, name: str, animated: bool = False):
        self.id = emoji_id
        self.name = name
        self.animated = animated
        self.deleted = False
        self.reason = None

    def __str__(self) -> str:
        return f"<{'a' if self.animated else ''}:{self.name}:{self.id}>"

    async def delete(self, *, reason=None):
        self.deleted = True
        self.reason = reason

    async def edit(self, *, name, reason=None):
        self.reason = reason
        self.name = name
        return self


class Guild:
    def __init__(self, *, perms=None):
        self.id = 42
        self.name = "Lab"
        self.emojis: list[ServerEmoji] = []
        self.stickers: list[ServerEmoji] = []
        self.emoji_limit = 50
        self.sticker_limit = 5
        self.me = types.SimpleNamespace(
            guild_permissions=perms if perms is not None else discord.Permissions(manage_emojis=True)
        )
        self.created: list[tuple] = []
        self.members = {}

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    async def create_custom_emoji(self, *, name, image, reason=None, roles=None):
        emoji = ServerEmoji(1000 + len(self.created), name, image.startswith(b"GIF"))
        self.created.append(("emoji", name, image, reason))
        self.emojis.append(emoji)
        return emoji

    async def create_sticker(self, *, name, description, emoji, file, reason=None):
        data = file.fp.read()
        sticker = ServerEmoji(3000 + len(self.created), name)
        self.created.append(("sticker", name, data, file.filename, emoji, description, reason))
        self.stickers.append(sticker)
        return sticker


def author(uid=1234, **perms):
    return types.SimpleNamespace(
        id=uid, display_name="Tester", guild_permissions=discord.Permissions(**perms),
    )


def message(content="", stickers=(), attachments=(), reactions=()):
    return types.SimpleNamespace(
        content=content, stickers=list(stickers), attachments=list(attachments), reactions=list(reactions),
    )


def sticker(name, sticker_id, fmt="png"):
    ext = {"lottie": "json", "gif": "gif"}.get(fmt, "png")
    return types.SimpleNamespace(
        id=sticker_id, name=name,
        format=types.SimpleNamespace(name=fmt, file_extension=ext),
        url=f"https://cdn.discordapp.com/stickers/{sticker_id}.{ext}",
    )


def http_error(code: int, text: str = "nope"):
    return discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"), {"code": code, "message": text})


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(expressions.asyncio, "sleep", _sleep)


def test_names_links_and_harvest_round_trip():
    assert clean_name("cool cat.png", limit=32) == "cool_cat"
    assert clean_name("!!", limit=32) is None
    assert clean_name("my-sticker", limit=30) == "my_sticker"

    parsed = parse_command_text(
        "add cool cat <:foo:123456789012345678> <a:bar:123456789012345679> "
        "https://cdn.example.com/a.png https://example.com/page.html "
        "https://discord.com/channels/42/7/99"
    )
    assert parsed.name == "cool cat"
    assert [emoji.name for emoji in parsed.harvest.emojis] == ["foo", "bar"]
    assert parsed.harvest.emojis[1].animated
    assert [image.url for image in parsed.harvest.images] == ["https://cdn.example.com/a.png"]
    assert parsed.message_link.endswith("/42/7/99")

    reply = message(
        content="hi <:foo:123456789012345678> https://files.example.com/cat.jpg",
        stickers=[sticker("sad cat", 55)],
        reactions=[types.SimpleNamespace(emoji=types.SimpleNamespace(id=123456789012345671, name="react", animated=True))],
        attachments=[Attachment(_png(), "pic.png", "image/png")],
    )
    found = harvest_message(reply)
    assert [emoji.name for emoji in found.emojis] == ["foo", "react"]
    assert found.stickers[0].name == "sad cat"
    assert [image.filename for image in found.images] == ["pic.png", "cat.jpg"]

    command = parsed.harvest
    target = found
    assert [emoji.name for emoji in choose_sources("both", command, target, "!").emojis] == ["foo", "bar", "react"]
    files = choose_sources("emoji", expressions.Harvest(images=command.images), target, "!")
    assert [image.filename for image in files.images] == ["a.png"]
    stolen = choose_sources("emoji", expressions.Harvest(), target, "!")
    assert [emoji.name for emoji in stolen.emojis] == ["foo", "react"]
    assert stolen.images == [] and "picture" in (stolen.note or "")
    picture = harvest_message(message(attachments=[Attachment(_png(), "only.png", "image/png")]))
    assert choose_sources("both", expressions.Harvest(), picture, "!").hint == "image"
    assert choose_sources("sticker", expressions.Harvest(), picture, "!").images[0].filename == "only.png"
    assert choose_sources("emoji", expressions.Harvest(), harvest_message(message(stickers=[sticker("s", 1)])), "!").hint == "stickers"


def test_private_links_are_refused_before_download(cog, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("private host was fetched")

    monkeypatch.setattr(expressions, "_fetch", explode)
    assert host_is_public("127.0.0.1") is False
    assert host_is_public("10.1.2.3") is False
    assert host_is_public("1.1.1.1") is True
    with pytest.raises(expressions.ImageProblem):
        run(download_bytes("http://127.0.0.1/secret.png"))
    with pytest.raises(expressions.ImageProblem):
        run(download_bytes("http://user:pass@example.com/a.png"))
    with pytest.raises(expressions.ImageProblem):
        run(download_bytes("file:///etc/passwd"))

    blocked = run(expressions.handle(
        cog.bot, Guild(), author(), kind="emoji", action="add", text="http://127.0.0.1/a.png", prefix="!",
    ))
    assert blocked.title == "Couldn't add those"
    assert "public image" in blocked.description


def test_image_prep_fits_discord_limits():
    png = _png()
    assert prepare_emoji_image(png)[0] == png

    gif = _gif()
    assert prepare_emoji_image(gif) == (gif, "emoji.gif")

    rng = random.Random(1)
    noisy = Image.new("RGB", (360, 360))
    noisy.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(360 * 360)])
    raw = io.BytesIO()
    noisy.save(raw, format="PNG")
    original = raw.getvalue()
    shrunk, _name = prepare_emoji_image(original)
    assert len(original) > expressions.EMOJI_MAX_BYTES
    assert len(shrunk) <= expressions.EMOJI_MAX_BYTES

    sticker_bytes, sticker_name = prepare_sticker_file(_jpg())
    assert sticker_name == "sticker.png"
    with Image.open(io.BytesIO(sticker_bytes)) as image:
        assert image.size == (320, 320)
        assert image.getpixel((0, 0))[3] == 0

    animated, animated_name = prepare_sticker_file(_gif())
    assert animated_name == "sticker.gif"
    assert len(animated) <= expressions.STICKER_MAX_BYTES
    with Image.open(io.BytesIO(animated)) as image:
        assert image.size == (320, 320)
        assert image.n_frames >= 2


def test_steal_emoji_sticker_and_reaction(cog, monkeypatch):
    png = _png()
    gif = _gif()
    fetched: list[str] = []

    async def fake_download(url, *, allow_json=False):
        fetched.append(url)
        return gif if url.endswith(".gif?quality=lossless") or ".gif" in url else png

    monkeypatch.setattr(expressions, "download_bytes", fake_download)
    guild = Guild()
    reply = message(
        content=f"<:foo:{PNG_ID}>",
        stickers=[sticker("sad cat", 77)],
        reactions=[types.SimpleNamespace(emoji=types.SimpleNamespace(id=GIF_ID, name="party", animated=True))],
    )
    embed = run(expressions.handle(
        cog.bot, guild, author(), kind="both", action="add", reply=reply, prefix="!",
    ))
    assert embed.title == "Yoinked ✅"
    assert [row[1] for row in guild.created] == ["foo", "party", "sad_cat"]
    assert guild.created[1][2].startswith(b"GIF")
    assert "sad_cat" in embed.description
    assert cog.bot.storage.expression_added_by(42, "emoji", guild.emojis[0].id) == 1234
    assert len(fetched) == 3

    twins = message(content=f"<:kek:{PNG_ID + 10}> <:kek:{PNG_ID + 11}>")
    twin = run(expressions.handle(cog.bot, guild, author(), kind="emoji", action="add", reply=twins, prefix="!"))
    assert twin.title == "Yoinked ✅"
    assert [emoji.name for emoji in guild.emojis if emoji.name.startswith("kek")] == ["kek", "kek_2"]

    again = run(expressions.handle(
        cog.bot, guild, author(), kind="both", action="add", reply=reply, text="renamed", prefix="!",
    ))
    assert again.title == "Already on this server"
    assert len(guild.created) == 5


def test_add_from_file_link_and_reply(cog, monkeypatch):
    png = _png()
    jpg = _jpg()

    async def fake_download(url, *, allow_json=False):
        assert url == "https://cdn.example.com/cat.png"
        return png

    monkeypatch.setattr(expressions, "download_bytes", fake_download)
    guild = Guild()
    embed = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add", text="cool cat",
        attachments=[Attachment(jpg, "ignored.jpg", "image/jpeg")], prefix="!",
    ))
    assert embed.title == "Added ✅"
    assert guild.created[0][0] == "emoji" and guild.created[0][1] == "cool_cat"

    sticker_embed = run(expressions.handle(
        cog.bot, guild, author(), kind="sticker", action="add", text="wide",
        attachments=[Attachment(jpg, "wide.jpg", "image/jpeg")], prefix="!",
    ))
    assert sticker_embed.title == "Added ✅"
    kind, name, data, filename, emoji_tag, description, _reason = guild.created[-1]
    assert (kind, name, filename, emoji_tag, description) == ("sticker", "wide", "sticker.png", "⭐", "Added with SYKE")
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (320, 320)

    link = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add",
        text="https://cdn.example.com/cat.png", prefix="!",
    ))
    assert link.title == "Added ✅"
    assert guild.created[-1][1] == "cat"

    picture = message(attachments=[Attachment(png, "only.png", "image/png")])
    hinted = run(expressions.handle(cog.bot, guild, author(), kind="both", action="add", reply=picture, prefix="!"))
    assert hinted.title == "That's a picture, not an emoji yet"
    added = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add", text="only", reply=picture, prefix="!",
    ))
    assert added.title == "Added ✅" and guild.created[-1][1] == "only"


def test_permissions_slots_and_management(cog):
    guild = Guild(perms=discord.Permissions.none())
    embed = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add",
        attachments=[Attachment(_png(), "a.png", "image/png")], text="nope", prefix="!",
    ))
    assert embed.title == "I need Manage Expressions"
    assert not guild.created

    guild.me.guild_permissions = discord.Permissions(create_expressions=True)
    added = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add",
        attachments=[Attachment(_png(), "a.png", "image/png")], text="allowed", prefix="!",
    ))
    assert added.title == "Added ✅"
    made = guild.emojis[0]
    blocked = run(expressions.handle(cog.bot, guild, author(), kind="emoji", action="remove", text="allowed", prefix="!"))
    assert blocked.title == "I need Manage Expressions"
    assert made.deleted is False

    guild.me.guild_permissions = discord.Permissions(manage_emojis=True)
    stranger = run(expressions.handle(cog.bot, guild, author(99), kind="emoji", action="remove", text="allowed", prefix="!"))
    assert stranger.title == "Not yours"
    own = run(expressions.handle(cog.bot, guild, author(), kind="emoji", action="remove", text="allowed", prefix="!"))
    assert own.title == "Removed" and made.deleted
    assert cog.bot.storage.expression_added_by(42, "emoji", made.id) is None

    other = ServerEmoji(8, "foreign")
    guild.emojis.append(other)
    admin = author(50, manage_guild=True)
    renamed = run(expressions.handle(cog.bot, guild, admin, kind="emoji", action="rename", text="foreign fresh", prefix="!"))
    assert renamed.title == "Renamed" and other.name == "fresh"
    assert "`foreign` is now" in renamed.description

    guild.emoji_limit = 1
    guild.emojis[:] = [ServerEmoji(1, "full")]
    full = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add",
        attachments=[Attachment(_png(), "more.png", "image/png")], text="more", prefix="!",
    ))
    assert "out of static emoji slots" in full.description
    assert "1/1 static · 0/1 animated" in full.footer.text

    listed = run(expressions.handle(cog.bot, guild, author(), kind="emoji", action="list", prefix="!"))
    assert "full" in listed.description
    assert listed.title == "Custom emojis · 1/1 static · 0/1 animated"


def test_partial_failure_stops_when_slots_run_out(cog, monkeypatch):
    async def fake_download(url, *, allow_json=False):
        return _png()

    monkeypatch.setattr(expressions, "download_bytes", fake_download)
    guild = Guild()
    calls = {"n": 0}

    async def create_custom_emoji(*, name, image, reason=None, roles=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise http_error(30008, "Maximum number of emojis reached")
        emoji = ServerEmoji(4000 + calls["n"], name, False)
        guild.emojis.append(emoji)
        return emoji

    guild.create_custom_emoji = create_custom_emoji
    reply = message(content=f"<:one:{PNG_ID}> <:two:{GIF_ID}> <:three:123456789012345670>")
    embed = run(expressions.handle(cog.bot, guild, author(), kind="emoji", action="add", reply=reply, prefix="!"))
    assert embed.title == "Added some"
    assert "out of static emoji slots" in embed.description
    assert calls["n"] == 2


def test_static_and_animated_slots_are_counted_separately(cog):
    guild = Guild()
    guild.emojis = [ServerEmoji(1, "still"), ServerEmoji(2, "spin", animated=True)]
    listed = run(expressions.handle(cog.bot, guild, author(), kind="emoji", action="list", prefix="!"))
    assert listed.title == "Custom emojis · 1/50 static · 1/50 animated"

    empty = run(expressions.handle(cog.bot, Guild(), author(), kind="emoji", action="list", prefix="!"))
    assert empty.title == "No custom emojis (0/50 static · 0/50 animated)"

    guild.emoji_limit = 1
    guild.emojis[:] = [ServerEmoji(1, "full")]
    gif = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add",
        attachments=[Attachment(_gif(), "dance.gif", "image/gif")], text="dance", prefix="!",
    ))
    assert gif.title == "Added ✅"
    assert "1/1 static · 1/1 animated" in gif.footer.text
    assert guild.emojis[-1].animated

    guild.emojis[:] = [ServerEmoji(2, "spin", animated=True)]
    before = len(guild.created)
    mixed = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add",
        attachments=[
            Attachment(_gif(), "another.gif", "image/gif"),
            Attachment(_png(), "plain.png", "image/png"),
        ],
        prefix="!",
    ))
    assert mixed.title == "Added some"
    assert "out of animated emoji slots" in mixed.description
    assert "plain" in mixed.description
    assert [row[1] for row in guild.created[before:]] == ["plain"]

    guild.emojis[:] = [ServerEmoji(1, "full"), ServerEmoji(3, "spin", animated=True)]
    before = len(guild.created)
    both = run(expressions.handle(
        cog.bot, guild, author(), kind="emoji", action="add",
        attachments=[Attachment(_png(), "more.png", "image/png")], text="more", prefix="!",
    ))
    assert "out of emoji slots" in both.description
    assert "static" not in both.description
    assert len(guild.created) == before


def test_message_link_and_command_wiring(cog, monkeypatch):
    png = _png()

    async def fake_download(url, *, allow_json=False):
        return png

    monkeypatch.setattr(expressions, "download_bytes", fake_download)
    guild = Guild()
    wrong = run(expressions.handle(
        cog.bot, guild, author(), kind="both", action="add",
        text="https://discord.com/channels/99/1/2", prefix="!",
    ))
    assert wrong.title == "Can't open that message"

    target = message(content=f"<:linked:{PNG_ID}>")

    async def fetch_message(message_id):
        assert message_id == 99
        return target

    channel = types.SimpleNamespace(id=7, fetch_message=fetch_message)
    guild.get_channel = lambda channel_id: channel if channel_id == 7 else None
    linked = run(expressions.handle(
        cog.bot, guild, author(), kind="both", action="add",
        text="https://discord.com/channels/42/7/99", prefix="!",
    ))
    assert linked.title == "Yoinked ✅"
    assert guild.emojis[0].name == "linked"

    class Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    ctx = FakeCtx()
    ctx.typing = lambda **_kwargs: Typing()
    ctx.guild = guild
    ctx.message = message(content="!steal", attachments=[])
    ctx.message.reference = types.SimpleNamespace(message_id=5, resolved=target, cached_message=None)
    ctx.channel = channel
    run(cog.steal.callback(cog, ctx, text=""))
    assert ctx.replies[-1].title == "Already on this server"

    bare = FakeCtx()
    bare.typing = ctx.typing
    bare.guild = guild
    bare.message = message(content="!steal")
    run(cog.steal.callback(cog, bare, text=""))
    assert bare.replies[-1].title == "Nothing to grab"

    by_name = {command.name: command for command in cog.get_commands()}
    for name in ("steal", "emoji", "sticker"):
        assert _admin_predicate not in by_name[name].checks
    assert cog.emoji.get_command("remove").name == "remove"
    assert cog.sticker.get_command("list").name == "list"


def test_context_menu_steals_the_clicked_message(cog, monkeypatch):
    async def fake_download(url, *, allow_json=False):
        return _png()

    monkeypatch.setattr(expressions, "download_bytes", fake_download)
    run(cog.bot.add_cog(cog))
    menu = cog.bot.tree.get_command("Steal emoji/sticker", type=discord.AppCommandType.message)
    assert menu is not None and menu.guild_only

    guild = Guild()
    sent = {}

    class Response:
        def __init__(self):
            self.deferred = False

        async def defer(self, **_kwargs):
            self.deferred = True

        async def send_message(self, **kwargs):
            sent["early"] = kwargs

    class Followup:
        async def send(self, **kwargs):
            sent["embed"] = kwargs["embed"]

    interaction = types.SimpleNamespace(
        guild=guild, user=author(), response=Response(), followup=Followup(),
    )
    clicked = message(stickers=[sticker("dancing", 88)])
    run(cog.steal_from_message(interaction, clicked))
    assert interaction.response.deferred
    assert sent["embed"].title == "Yoinked ✅"
    assert guild.stickers[0].name == "dancing"


def test_expression_owner_survives_rename_in_storage(cog):
    storage = cog.bot.storage
    storage.remember_expression(42, "emoji", 5, "old", 9)
    storage.remember_expression(42, "emoji", 5, "new", 8)
    assert storage.expression_added_by(42, "emoji", 5) == 9
    storage.rename_expression(42, "emoji", 5, "newer")
    storage.forget_expression(42, "emoji", 5)
    assert storage.expression_added_by(42, "emoji", 5) is None
