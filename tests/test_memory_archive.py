import gzip
import json

import pytest

from syke import memory_archive as arc
from syke.storage import Storage

from test_web import _session_cookie, dash, serving  # noqa: F401  (fixture reuse)
from syke import web_auth as auth


def make(tmp_path, name="a.db"):
    path = str(tmp_path / name)
    return Storage(path), path


def seed(store, gid=1, n=12):
    rows = [(1000 + i, 10 + i % 2, 7 + i % 3, f"message {i} ✨", 1_600_000_000.0 + i * 86400, f"user{i % 3}", i % 2, i)
            for i in range(n)]
    store.add_corpus(gid, rows)
    store.save_labels(gid, {1000: frozenset({"funny", "loud"})})


def test_export_then_import_round_trip(tmp_path):
    store, db = make(tmp_path)
    seed(store)
    path, count, size = arc.export_guild(store, db, 1, "Home")
    assert count == 12 and size == path.stat().st_size and path.parent.name == "exports"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        header = json.loads(f.readline())
        lines = [json.loads(line) for line in f]
    assert header["type"] == "syke-memory" and header["messages"] == 12 and header["guild_name"] == "Home"
    assert sum("m" in line for line in lines) == 12 and any("l" in line for line in lines)

    other, _ = make(tmp_path, "b.db")
    read, added, h = arc.import_file(other, path, 2)
    assert (read, added) == (12, 12) and h["guild_id"] == 1
    again = arc.import_file(other, path.read_bytes(), 2)        # bytes work too, and nothing duplicates
    assert again[:2] == (12, 0)
    got = other._read("SELECT content, author_name, total_reactions FROM corpus WHERE guild_id=2 AND message_id=1005", ())
    assert got == [("message 5 ✨", "user2", 5)]
    assert other._read("SELECT traits FROM message_labels WHERE guild_id=2", ()) == [("funny,loud",)]


def test_stats_and_listing(tmp_path):
    store, db = make(tmp_path)
    seed(store)
    info = arc.stats(store, 1)
    assert info["messages"] == 12 and info["authors"] == 3 and info["channels"] == 2
    assert info["first"] == 1_600_000_000.0
    path, _, _ = arc.export_guild(store, db, 1, "Home")
    assert [n for n, _, _ in arc.list_exports(db, 1)] == [path.name]
    assert arc.list_exports(db, 999) == []


def test_rejects_other_files_and_paths(tmp_path):
    store, db = make(tmp_path)
    assert arc.safe_path(db, "../a.db") is None and arc.safe_path(db, "a.db") is None
    with pytest.raises(ValueError):
        arc.import_file(store, gzip.compress(b'{"type":"nope"}\n'), 1)
    with pytest.raises(ValueError):
        arc.import_file(store, gzip.compress(b'{"type":"syke-memory","version":99}\n'), 1)


def test_human_size():
    assert arc.human_size(500) == "500 B" and arc.human_size(2048) == "2.0 KB"
    assert arc.human_size(5 * 1024 ** 3) == "5.0 GB"


async def test_dashboard_export_and_download(dash):  # noqa: F811
    async with serving(dash) as (cl, web, bot, guild):
        seed(bot.storage, guild.id)
        resp = await cl.post(f"/g/{guild.id}/memory/export", data={}, allow_redirects=False)
        assert resp.status == 302 and "/login" in resp.headers["Location"]      # logged out → no export
        cl.session.cookie_jar.update_cookies(_session_cookie())
        resp = await cl.post(f"/g/{guild.id}/memory/export", data={"csrf": auth.csrf_token("unit-test-secret", 42)},
                             allow_redirects=False)
        assert resp.status == 302 and "Exported" in resp.headers["Location"]
        name = arc.list_exports(bot.settings.db_path, guild.id)[0][0]
        got = await cl.get(f"/g/{guild.id}/memory/export/{name}")
        assert got.status == 200 and "attachment" in got.headers["Content-Disposition"]
        assert arc.read_header(await got.read())["messages"] == 12
        assert (await cl.get(f"/g/{guild.id}/memory/export/..%2Fw.db")).status == 404
        cl.session.cookie_jar.clear()
        assert (await cl.get(f"/g/{guild.id}/memory/export/{name}", allow_redirects=False)).status == 302
