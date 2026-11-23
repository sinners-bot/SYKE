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


async def test_yap_text_mode_only(cog, monkeypatch):
    # No sticker/gif roll -> a single text message, emoji inline (still just "text").
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 1.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 0.0)
    guild = types.SimpleNamespace(id=42, emojis=[types.SimpleNamespace(id=111)], stickers=[])
    _seed_voice(cog.bot, 42, emojis=["<:pepe:111>", "<:gone:999>"], gifs=[])
    ch = _channel()
    await cog.bot._send_yap(_message(guild, ch), "hello", reply_to=False)
    assert len(ch.sent) == 1                                 # exactly one message
    content, kwargs = ch.sent[0]
    assert content == "hello"                                # a repeated line goes out exactly as said
    assert "stickers" not in kwargs                          # no sticker alongside
    msg = _message(guild, ch)
    await cog.bot._send_yap(msg, "hello", reply_to=True)
    assert msg.replies[0][0] == "hello <:pepe:111>"          # replies may carry a still-available emoji


async def test_random_yap_repeats_a_real_message_word_for_word(cog, monkeypatch):
    from dataclasses import replace
    bot = cog.bot
    bot.settings = replace(bot.settings, ai_provider="openai", openai_api_key="k")

    async def no_ai(*a, **k):
        raise AssertionError("random yaps must not be written by the AI")

    monkeypatch.setattr("syke.ai._call_openai", no_ai)
    said = ["pizza from luigis is lowkey elite", "who wants pizza tonight fr", "valorant ranked is pain",
            "bro the bus was late again", "anyone up for movies later"]
    bot.storage.add_corpus(42, [(i + 1, 10, 100 + i, text) for i, text in enumerate(said)])
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[])
    msg = _message(guild, _channel())
    msg.content = "i want pizza so bad"
    for _ in range(5):
        line = await bot.contextual_yap(msg)
        assert line in said and "pizza" in line             # verbatim, and on topic


def test_style_samples_and_emoji_vocab():
    from syke.yap import FILLER_EMOJI, build_voice, vocabulary
    texts = ["nah fr 😭", "lmaooo 💀💀", "ts pmo icl", "https://tenor.com/view/x-gif-1", "<@5> come vc",
             "pizza time 🍕", "pizza time 🍕", "who tryna hop on 🔥", "who tryna hop on 🔥 rn"] * 3
    voice = build_voice(texts)
    assert "ts pmo icl" in voice.samples
    assert not any("tenor" in t or "<@" in t for t in voice.samples)        # no links or pings
    vocab = vocabulary(voice, "funny")
    assert not FILLER_EMOJI & set(vocab.emojis) and "🍕" in vocab.emojis   # 😭/💀 never offered as the go-to


def test_reply_prompt_shows_how_the_server_types(monkeypatch):
    from dataclasses import replace
    from syke import ai
    from syke.config import load_settings
    from syke.yap import Turn, build_voice
    settings = replace(load_settings(), ai_provider="openai", openai_api_key="k")
    voice = build_voice(["ts pmo icl", "who tryna hop on rn", "nah bc why is it raining again"] * 5)
    seen = {}

    async def fake(s, system, user, temperature=1.0):
        seen["system"], seen["user"] = system, user
        return '{"reply": "bro its always raining here 🌧️"}'

    monkeypatch.setattr(ai, "_call_openai", fake)
    import asyncio
    out = asyncio.run(ai.compose_reply(settings, [(1.0, "rain again ugh")], Turn("Mo", "why is it raining"),
                                       said="weather", voice=voice))
    assert out == "bro its always raining here 🌧️"
    assert "HOW THEY TYPE" in seen["user"] and "ts pmo icl" in seen["user"]
    assert "Don't fall back on 😭, 💀 or 😂" in seen["system"]


async def test_yap_sticker_only(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 1.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 0.0)
    sticker = types.SimpleNamespace(id=900, name="wave")
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[sticker])
    past = [types.SimpleNamespace(stickers=[types.SimpleNamespace(id=900)])]
    _seed_voice(cog.bot, 42, emojis=[], gifs=[])
    ch = _channel(past)
    await cog.bot._send_yap(_message(guild, ch), "hey", reply_to=False)
    assert len(ch.sent) == 1                                 # one message, nothing else
    content, kwargs = ch.sent[0]
    assert content is None and kwargs.get("stickers") == [sticker]   # sticker only, no text


async def test_yap_gif_only(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 1.0)
    gifs = ["https://tenor.com/view/happy-dance-gif-1"]
    _seed_voice(cog.bot, 42, emojis=[], gifs=gifs)
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[])
    ch = _channel()
    msg = _message(guild, ch)
    msg.content = "so happy right now, dance time"
    await cog.bot._send_yap(msg, "happy dance energy", reply_to=False)
    assert len(ch.sent) == 1                                 # just the GIF, no separate text
    content, kwargs = ch.sent[0]
    assert content == gifs[0] and "stickers" not in kwargs
    msg.content = "so happy"                                 # one loose word isn't enough to swap for a GIF
    await cog.bot._send_yap(msg, "good energy", reply_to=False)
    assert ch.sent[-1][0] == "good energy"


async def test_replies_to_people_are_always_words(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 0.5)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 0.5)
    _seed_voice(cog.bot, 42, emojis=[], gifs=["https://tenor.com/view/happy-dance-gif-1"])
    sticker = types.SimpleNamespace(id=900, name="wave")
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[sticker])
    past = [types.SimpleNamespace(stickers=[types.SimpleNamespace(id=900)])]
    msg = _message(guild, _channel(past))
    msg.content = "syke are you happy dance"
    for _ in range(10):
        await cog.bot._send_yap(msg, "always happy dance mode", reply_to=True)
    assert all(content == "always happy dance mode" and not kwargs.get("stickers") for content, kwargs in msg.replies)


async def test_yap_gif_fits_context(cog):
    from syke.yap import Voice
    gifs = ["https://tenor.com/view/happy-dance-gif-1", "https://tenor.com/view/sad-cat-gif-2"]
    voice = Voice(chain=None, gifs=gifs)
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[])
    msg = _message(guild, _channel())
    msg.content = "i am so happy today"
    assert await cog.bot.yap_gif(msg, "happy dance vibes", voice) == gifs[0]
    assert await cog.bot.yap_gif(msg, "yeah good vibes", voice) is None      # one word in common: not enough
    msg.content = "quarterly spreadsheet numbers"
    assert await cog.bot.yap_gif(msg, "boring meeting", voice) is None


async def test_yap_sticker_only_reply(cog, monkeypatch):
    monkeypatch.setattr(botmod, "YAP_EMOJI_CHANCE", 0.0)
    monkeypatch.setattr(botmod, "YAP_STICKER_CHANCE", 1.0)
    monkeypatch.setattr(botmod, "YAP_GIF_CHANCE", 0.0)
    sticker = types.SimpleNamespace(id=900, name="wave")
    guild = types.SimpleNamespace(id=42, emojis=[], stickers=[sticker])
    past = [types.SimpleNamespace(stickers=[types.SimpleNamespace(id=900)])]
    _seed_voice(cog.bot, 42, emojis=[], gifs=[])
    msg = _message(guild, _channel(past))
    ch = msg.channel
    await cog.bot._send_yap(msg, "yo", reply_to=False)        # SYKE jumping in on his own
    content, kwargs = ch.sent[-1]
    assert content is None and kwargs.get("stickers") == [sticker]   # sticker, no text
