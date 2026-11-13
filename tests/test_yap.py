"""Yap can carry the server's own emojis, stickers and GIFs."""

import time
import types

import discord
import pytest

import syke.bot as botmod
from syke.yap import Voice, build_voice, emoji_id, mine_expressions


# Mining

def test_mine_expressions_orders_by_use():
    emojis, gifs = mine_expressions([
        "gm <:pepe:111> <:pepe:111>",
        "lol <a:dance:222> https://tenor.com/view/cat-gif-999",
        "nice https://media.giphy.com/x.gif and plain https://example.com/page",
    ])
    assert emojis[0] == "<:pepe:111>"          # used most -> first
    assert "<a:dance:222>" in emojis
    assert any("tenor.com/view" in g for g in gifs)
    assert any(g.endswith(".gif") for g in gifs)
    assert all("example.com/page" not in g for g in gifs)   # non-gif link ignored


def test_yap_defaults_are_livelier():
    from syke.config import load_settings
    s = load_settings()
    assert s.yap_chance >= 0.15 and s.yap_cooldown <= 90   # chimes in more often


def test_recent_chat_carries_real_weight():
    from syke.yap import Turn, query_weights
    w = query_weights("pizza tonight?", chat=[Turn("a", "valorant ranked grind")])
    assert w["pizza"] == 1.0                 # the target still dominates
    assert w.get("valorant", 0) >= 0.4       # but the ongoing conversation matters now


def test_gif_keywords_and_pick():
    from syke.yap import gif_keywords, pick_gif
    assert gif_keywords("https://tenor.com/view/happy-dance-excited-gif-12345") == {"happy", "dance", "excited"}
    assert gif_keywords("https://media.giphy.com/media/abc/sad-cat.gif") == {"sad", "cat"}
    gifs = ["https://tenor.com/view/happy-dance-gif-1", "https://tenor.com/view/sad-cat-gif-2"]
    assert pick_gif(gifs, "i am so happy today") == gifs[0]
    assert pick_gif(gifs, "nothing relevant xyz") is None
    assert pick_gif(gifs, "") is None


def test_emoji_id():
    assert emoji_id("<:pepe:111>") == 111
    assert emoji_id("<a:dance:222>") == 222
    assert emoji_id("not-an-emoji") is None


def test_build_voice_captures_expressions():
    v = build_voice(["hello <:pepe:111>", "omg https://tenor.com/view/x-gif-1 lol"])
    assert "<:pepe:111>" in v.emojis
    assert v.gifs and "tenor.com/view" in v.gifs[0]


# Send-path decoration

def _history(messages):
    async def history(limit=50, before=None):
        for m in messages:
            yield m
    return history


def _channel(messages=()):
    sent = []

    async def send(content=None, **kwargs):
        sent.append((content, kwargs))
        return types.SimpleNamespace(id=1)

    ch = types.SimpleNamespace(id=10, history=_history(messages), send=send, sent=sent)
    return ch


def _message(guild, channel):
    replies = []

    async def reply(content=None, **kwargs):
        replies.append((content, kwargs))
        return types.SimpleNamespace(id=2)

    return types.SimpleNamespace(id=99, guild=guild, channel=channel, content="hi",
                                 author=types.SimpleNamespace(id=7, display_name="Ada", bot=False),
                                 reply=reply, replies=replies)


def _seed_voice(bot, gid, **kw):
    bot._voices[gid] = (time.monotonic(), Voice(chain=None, **kw))


async def test_yap_extras_appends_available_emoji(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 1.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 0.0)
    guild = types.SimpleNamespace(id=42, emojis=[types.SimpleNamespace(id=111)], stickers=[])
    _seed_voice(cog.bot, 42, emojis=["<:pepe:111>", "<:gone:999>"], gifs=[])
    msg = _message(guild, _channel())
    content, stickers = await cog.bot.yap_extras(msg, "hello")
    assert content == "hello <:pepe:111>"   # only the emoji still on the server is used
    assert stickers == []


async def test_yap_extras_sends_recent_sticker(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 1.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 0.0)
    sticker = types.SimpleNamespace(id=900, name="wave")
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[sticker])
    past = [types.SimpleNamespace(stickers=[types.SimpleNamespace(id=900)])]
    _seed_voice(cog.bot, 42, emojis=[], gifs=[])
    msg = _message(guild, _channel(past))
    content, stickers = await cog.bot.yap_extras(msg, "hey")
    assert content == "hey"
    assert stickers == [sticker]


async def test_yap_extras_never_appends_gif(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 0.0)
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[])
    _seed_voice(cog.bot, 42, emojis=[], gifs=["https://tenor.com/view/x-gif-1"])
    msg = _message(guild, _channel())
    content, stickers = await cog.bot.yap_extras(msg, "lol")
    assert content == "lol"      # GIFs no longer ride along with the text
    assert stickers == []


async def test_contextual_gif_fits_the_conversation(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 1.0)
    gifs = ["https://tenor.com/view/happy-dance-gif-1", "https://tenor.com/view/sad-cat-gif-2"]
    _seed_voice(cog.bot, 42, emojis=[], gifs=gifs)
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[])
    msg = _message(guild, _channel())
    msg.content = "i am so happy today"
    assert await cog.bot.contextual_gif(msg, "yeah good vibes") == gifs[0]   # picks the fitting one
    # Nothing in the pool matches -> no gif forced.
    msg.content = "quarterly spreadsheet numbers"
    assert await cog.bot.contextual_gif(msg, "boring meeting") is None


async def test_send_yap_sends_gif_as_separate_message(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 1.0)
    gifs = ["https://tenor.com/view/happy-dance-gif-1"]
    _seed_voice(cog.bot, 42, emojis=[], gifs=gifs)
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[])
    ch = _channel()
    msg = _message(guild, ch)
    msg.content = "so happy right now"
    await cog.bot._send_yap(msg, "happy energy", reply_to=False)
    sent = [c for c, _ in ch.sent]
    assert sent == ["happy energy", gifs[0]]   # text first, GIF as its own follow-up message


async def test_send_yap_passes_stickers_to_reply(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 1.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 0.0)
    sticker = types.SimpleNamespace(id=900, name="wave")
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[sticker])
    past = [types.SimpleNamespace(stickers=[types.SimpleNamespace(id=900)])]
    _seed_voice(cog.bot, 42, emojis=[], gifs=[])
    msg = _message(guild, _channel(past))
    await cog.bot._send_yap(msg, "yo", reply_to=True)
    content, kwargs = msg.replies[-1]
    assert content == "yo" and kwargs.get("stickers") == [sticker]
    assert kwargs.get("mention_author") is False
