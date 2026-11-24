"""What SYKE sees: pictures on the message he answers, the message it replies to, and GIF stills."""

import asyncio
import datetime as dt
import types
from dataclasses import replace

import httpx
import pytest

from syke import ai, vision
from syke.config import load_settings
from syke.yap import Turn, gif_name

NS = types.SimpleNamespace


def _author(uid=7, name="Mo"):
    return NS(id=uid, display_name=name, name=name.lower(), bot=False)


def _msg(content="", attachments=(), stickers=(), embeds=(), author=None, reference=None, history=(), mid=1,
         created=None):
    channel = NS(id=10)

    def hist(limit=None, before=None):
        async def gen():
            for m in history:
                yield m
        return gen()

    channel.history = hist
    return NS(id=mid, content=content, attachments=list(attachments), stickers=list(stickers),
              embeds=list(embeds), author=author or _author(), reference=reference, channel=channel,
              created_at=created or dt.datetime.now(dt.timezone.utc))


def _image(url="https://cdn.discordapp.com/attachments/1/2/fit.png", kind="image/png"):
    return NS(content_type=kind, filename=url.rsplit("/", 1)[-1], size=1000, url=url, proxy_url=url + "?proxy")


def test_gif_names_read_the_link():
    assert gif_name("https://tenor.com/view/crying-cat-sad-gif-12345") == "crying cat sad"
    assert gif_name("https://giphy.com/gifs/happy-dance-party-abc123XYZ") == "happy dance party"


def test_pictures_on_one_message():
    gif = NS(url="https://tenor.com/view/side-eye-gif-99", type="gifv",
             thumbnail=NS(url="https://media.tenor.com/x/side-eye.png"), image=NS(url=None))
    sticker = NS(name="wave", url="https://media.discordapp.net/stickers/5.png", format=NS(name="png"))
    lottie = NS(name="dance", url="https://x/6.json", format=NS(name="lottie"))
    video = NS(content_type="video/mp4", filename="clip.mp4", size=10, url="u", proxy_url="u")
    m = _msg("https://tenor.com/view/side-eye-gif-99", [_image(), video], [sticker, lottie], [gif])
    pics = vision.pictures_in(m)
    assert [p.label for p in pics] == ["Mo's image", "Mo's sticker (wave)", "Mo's gif (side eye)"]
    assert pics[0].url.endswith("?proxy")
    giphy = _msg("https://giphy.com/gifs/happy-dance-abcdEFGH1234")
    assert vision.pictures_in(giphy)[0].url == "https://media.giphy.com/media/abcdEFGH1234/giphy_s.gif"


async def test_gather_looks_at_the_reply_and_a_picture_posted_just_before():
    photo = _msg(attachments=[_image()], mid=1)
    rate = _msg("syke rate my fit", history=[photo], mid=2)
    pics = await vision.gather(None, rate)
    assert [p.label for p in pics] == ["Mo's image"]                           # posted just before
    old = _msg(attachments=[_image()], mid=3, created=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10))
    assert await vision.gather(None, _msg("syke rate", history=[old], mid=4)) == []   # too long ago
    theirs = _msg(attachments=[_image("https://cdn.discordapp.com/a/b/setup.jpg")], author=_author(8, "Zara"))
    reply = _msg("syke look at this", reference=NS(resolved=theirs), mid=5)
    assert [p.label for p in await vision.gather(None, reply)] == ["Zara's image (the message they're replying to)"]


async def test_gif_preview_is_waited_for(monkeypatch):
    monkeypatch.setattr(vision, "EMBED_WAIT", 0)
    later = _msg("https://tenor.com/view/crying-cat-gif-1",
                 embeds=[NS(url="https://tenor.com/view/crying-cat-gif-1", type="gifv",
                            thumbnail=NS(url="https://media.tenor.com/c.png"), image=None)])
    first = _msg("https://tenor.com/view/crying-cat-gif-1")

    async def fetch_message(mid):
        return later

    first.channel.fetch_message = fetch_message
    later.channel = first.channel
    pics = await vision.gather(None, first)
    assert pics and pics[0].label == "Mo's gif (crying cat)"


def _settings():
    return replace(load_settings(), ai_provider="openai", openai_api_key="k")


def test_reply_sends_the_pictures_and_says_what_they_are(monkeypatch):
    seen = {}

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None, images=None):
        seen.update(system=system, user=user, images=images)
        return '{"reply": "that fit is giving clearance rack"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    pics = [vision.Picture("https://cdn/x.png", "Mo's image")]
    out = asyncio.run(ai.compose_reply(_settings(), [(1.0, "fits here are mid")], Turn("Mo", "syke rate my fit [image]"),
                                       pictures=pics))
    assert out == "that fit is giving clearance rack"
    assert seen["images"] == ["https://cdn/x.png"] and "PICTURES (attached, in this order): 1. Mo's image" in seen["user"]
    assert "never someone's face" in seen["system"]


def test_openai_body_carries_low_detail_images(monkeypatch):
    sent = []

    class Client:
        def __init__(self, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers, json):
            sent.append(json)
            return NS(raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(ai.httpx, "AsyncClient", Client)
    asyncio.run(ai._call_openai(_settings(), "s", "u", images=["https://cdn/x.png"]))
    content = sent[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "u"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://cdn/x.png", "detail": "low"}}
    asyncio.run(ai._call_openai(_settings(), "s", "u"))
    assert sent[1]["messages"][1]["content"] == "u"                           # no pictures: plain text as before


def test_unloadable_pictures_fall_back_to_text(monkeypatch):
    calls = []

    async def fake(s, system, user, temperature=1.0, timeout=30, model=None, images=None):
        calls.append(images)
        if images:
            request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            raise httpx.HTTPStatusError("bad image", request=request, response=httpx.Response(400, request=request))
        assert "couldn't be loaded" in user
        return '{"reply": "ok"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    settings = replace(_settings(), openai_chat_model="gpt-5.4-mini", openai_model="gpt-4o-mini")
    assert asyncio.run(ai._call_chat(settings, "s", "u", images=["https://cdn/x.png"])) == '{"reply": "ok"}'
    assert calls == [["https://cdn/x.png"], None]                             # no pointless model switch first


def test_chat_text_names_gifs(cog):
    m = _msg("lmao https://tenor.com/view/crying-cat-sad-gif-12345")
    assert cog.bot.turn_text(m) == "lmao https://tenor.com/view/crying-cat-sad-gif-12345 [gif: crying cat sad]"


async def test_picture_only_reply_to_a_yap_gets_answered(cog, monkeypatch):
    bot = cog.bot
    answered = []

    async def replied(message):
        return "rate my setup then"

    async def answer(message, said):
        answered.append((message.id, said))

    async def nothing(message):
        return None

    monkeypatch.setattr(bot, "replied_yap", replied)
    monkeypatch.setattr(bot, "answer_yap_reply", answer)
    monkeypatch.setattr(bot, "process_commands", nothing)
    m = _msg(attachments=[_image()], mid=9)
    m.guild = NS(id=42)
    await bot.on_message(m)
    assert answered == [(9, "rate my setup then")]

    async def not_a_reply(message):
        return None

    monkeypatch.setattr(bot, "replied_yap", not_a_reply)
    plain = _msg(attachments=[_image()], mid=10)
    plain.guild = NS(id=42)
    await bot.on_message(plain)
    assert answered == [(9, "rate my setup then")]                            # a photo nobody aimed at SYKE: ignored
