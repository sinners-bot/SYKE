"""Radio picks the right upload of the right song, swaps blocked uploads, and keeps stations on-genre."""

import time
from dataclasses import replace

from test_lavalink import NS, lltrack, rig, settle  # noqa: F401  (fixtures)

from syke import music, music_lists as lists, music_match as m
from syke.music_sources import Track


def upload(title, author, seconds=200, source="youtube", encoded=None):
    data = lltrack(title, f"https://youtu.be/{title[:6]}", source, seconds * 1000, encoded)
    data["info"]["author"] = author
    data["info"]["identifier"] = encoded or title
    return data


def test_the_real_song_beats_covers_loops_and_edits():
    score = m.score_upload
    real = score("Super Shy", "NewJeans", "Super Shy", "NewJeans - Topic", 155)
    assert real > score("Super Shy", "NewJeans", "NewJeans - Super Shy (sped up)", "x", 120)
    assert score("Super Shy", "NewJeans", "Super Shy 1 hour loop", "y", 3600) < 0
    assert score("Super Shy", "NewJeans", "Super Shy cover by Mia", "Mia", 150) < 2
    assert score("Super Shy", "NewJeans", "Cupid", "FIFTY FIFTY", 170) < 0
    assert score("Очень", "JONY", "JONY - Очень (Lyrics Video)", "JONY", 180) > 2      # Cyrillic works
    assert score("Uptown Funk", "Mark Ronson", "Uptown Funk (Live)", "x", 250) < real
    assert m.split_query("Dr. Dre - Still D.R.E.") == ("Still D.R.E.", "Dr. Dre")
    assert not m.plausible_song("Best Phonk Mix 2025 (1 hour)", 3600) and m.plausible_song("Murder In My Mind", 190)


async def test_resolver_picks_the_clean_upload_and_keeps_backups(rig):
    node, cog = rig.node, rig.cog
    node.loads["ytmsearch:NewJeans Super Shy"] = {"loadType": "search", "data": [
        upload("Super Shy", "NewJeans - Topic", 155, encoded="enc-topic")]}
    node.loads["ytsearch:NewJeans - Super Shy"] = {"loadType": "search", "data": [
        upload("NewJeans - Super Shy (sped up)", "edits", 120, encoded="enc-sped"),
        upload("NewJeans (뉴진스) 'Super Shy' Official MV", "HYBE LABELS", 200, encoded="enc-mv"),
        upload("Super Shy 10 hours", "loops", 36000, encoded="enc-loop")]}
    track = Track(title="Super Shy — NewJeans", query="NewJeans - Super Shy", radio="pop")
    assert await cog.ll_resolve(track) == "enc-topic"
    assert track.alternates == ["enc-mv"]                          # no sped-up edit, no loop


async def test_a_blocked_upload_is_swapped_for_another_at_the_same_second(rig, monkeypatch):
    node, voice, p = rig.node, rig.voice, rig.player

    async def no_wait(_s):
        return None
    monkeypatch.setattr(music, "resume_wait", no_wait)
    node.loads["ytmsearch:NewJeans Super Shy"] = {"loadType": "search", "data": [
        upload("Super Shy", "NewJeans - Topic", 155, encoded="enc-topic"),
        upload("Super Shy (Official Audio)", "NewJeans", 155, encoded="enc-audio")]}
    p.queue.append(Track(title="Super Shy — NewJeans", query="NewJeans - Super Shy", radio="pop", duration=155))
    await p.start_or_queue()
    assert node.updates[-1][1]["track"] == {"encoded": "enc-topic"}
    p.base, p.t0 = 40.0, time.monotonic()
    voice._event({"type": "TrackExceptionEvent", "exception": {"message": "This video requires login."}})
    voice._event({"type": "TrackEndEvent", "reason": "loadFailed"})
    await settle()
    body = node.updates[-1][1]
    assert body["track"] == {"encoded": "enc-audio"} and 38000 <= body["position"] <= 40000
    assert p.current.title == "Super Shy — NewJeans"


async def test_radio_mixes_hand_picked_and_ai_picked_songs_for_the_station(rig, monkeypatch):
    cog, p = rig.cog, rig.player
    cog.bot.settings = replace(cog.bot.settings, ai_provider="openai")
    asked = []

    async def ai(settings, system, user, temperature, timeout):
        asked.append(user)
        return {"songs": [{"artist": "Myke Towers", "title": "LALA"},
                          {"artist": "Ozuna", "title": "Taki Taki Remix"}, {"artist": "Anuel AA", "title": "China"},
                          {"artist": "Bad Bunny", "title": "Monaco"}]}
    monkeypatch.setattr(music.ai, "_complete", ai)
    p.radio = "latin"
    await cog.radio_fill(p, 6)
    await settle()
    titles = [t.title for t in p.queue]
    assert len(titles) == 6 and all(t.radio == "latin" and t.source == "Radio" for t in p.queue)
    hand = {f"{t} — {a}" for a, t in lists.GENRES["latin"]}
    assert 3 <= sum(t in hand for t in titles) <= 4 and len(set(titles)) == 6   # half from the list, no repeats
    assert "reggaeton" in asked[0] and "Station: latin" in asked[0]


async def test_mood_stations_use_ai_picks_not_random_searches(rig, monkeypatch):
    cog, p, node = rig.cog, rig.player, rig.node
    cog.bot.settings = replace(cog.bot.settings, ai_provider="openai")

    async def ai(settings, system, user, temperature, timeout):
        return {"songs": [{"artist": f"Artist {i}", "title": f"Sad Song {i}"} for i in range(8)]}
    monkeypatch.setattr(music.ai, "_complete", ai)
    p.radio = "sad"
    await cog.radio_fill(p, 4)
    assert len(p.queue) == 4 and all(t.title.startswith("Sad Song") for t in p.queue)
    assert not [q for q in node.loaded if "sad pop song" in q]               # no vague searches needed


async def test_without_ai_searches_skip_mixes_and_hour_long_videos(rig):
    cog, p, node = rig.cog, rig.player, rig.node
    for seed in lists.RADIO_SEARCHES["gym"]:
        node.loads[f"ytmsearch:{seed}"] = {"loadType": "search", "data": [
            upload("GYM MOTIVATION MIX 2025", "mixes", 3600), upload(f"Real Song for {seed}", "Rapper", 190)]}
    p.radio = "gym"
    await cog.radio_fill(p, 2)
    assert p.queue and all("MIX" not in t.title for t in p.queue)


def test_every_station_has_a_brief_and_fits_discord():
    assert len(lists.GENRES) <= 25
    assert all(g in lists.STATION_BRIEFS for g in lists.all_genres())
    assert lists.resolve_genre("русский") is None and lists.resolve_genre("k-pop") is None and lists.resolve_genre("baile funk") == "brazilian"
