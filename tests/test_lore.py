from syke import lore

DAY = 86400.0


def rows_with(joke_uses=12, people=4):
    rows, mid = [], 1
    for i in range(300):  # ordinary chat, few reactions
        rows.append((mid, 1, 100 + i % 6, f"u{i % 6}", f"just normal chat about the game number {i % 7}",
                     1_600_000_000 + i * 3600, 0))
        mid += 1
    for i in range(joke_uses):  # the running bit: several people, weeks apart, lots of reactions
        author = 100 + i % people
        rows.append((mid, 1, author, f"u{i % people}", f"lmao that's so big chungus energy {i}",
                     1_600_000_000 + i * 8 * DAY, 4))
        mid += 1
    rows.sort(key=lambda r: r[5])
    return rows


def test_finds_the_running_joke_and_who_started_it():
    found = lore.mine(rows_with())
    phrases = [s["phrase"] for s in found]
    assert phrases and "chungus" in phrases[0]
    assert "chungus" not in phrases[1:] or "big chungus" not in phrases[1:]        # one joke, listed once
    top = found[0]
    assert top["first"][5] == 1_600_000_000 and top["people"] == 4 and top["uses"] >= 12
    assert not any("game" in p or "normal chat" in p for p in phrases)              # ordinary chat isn't lore


def test_needs_more_than_one_person_and_week():
    rows = [(i, 1, 7, "solo", "big chungus energy", 1_600_000_000 + i, 5) for i in range(20)]
    assert lore.mine(rows) == []


def test_skips_commands_optouts_and_slurs():
    rows = rows_with()
    assert lore.mine(rows, optouts=frozenset(range(100, 106))) == []
    assert lore.mine(rows, is_command=lambda t: "chungus" in t) == [] or \
        all("chungus" not in s["phrase"] for s in lore.mine(rows, is_command=lambda t: "chungus" in t))
    slur_rows = [(r[0], r[1], r[2], r[3], r[4].replace("big chungus", "paki"), r[5], r[6]) for r in rows]
    assert all("paki" not in s["phrase"] for s in lore.mine(slur_rows))


def test_tokens_ignore_links_mentions_and_emoji():
    assert lore.tokens("LOL <@123> https://x.y/z <:pog:99> Big Chungus!") == ["lol", "big", "chungus"]


def test_offline_story_reads_well():
    s = lore.mine(rows_with())[0]
    text = lore.offline_story(s)
    assert "First said by **u0**" in text and "4** people" in text
