"""The text-mangling toys: deterministic with a seeded rng, and ping-safe."""

import random

from syke.textfun import clean_source, drunk, keysmash, mock, scramble, uwu, zalgo


def test_mock_alternates_caps():
    assert mock("hello world") == "hElLo WoRlD"   # toggle runs continuously across letters
    assert mock("ABC!") == "aBc!"


def test_scramble_keeps_ends_and_is_reversible_length():
    out = scramble("scramble these longwords", random.Random(1))
    for orig, new in zip("scramble these longwords".split(), out.split()):
        assert new[0] == orig[0] and new[-1] == orig[-1]
        assert sorted(new) == sorted(orig)      # same letters, maybe reordered
    assert scramble("hi ok cat", random.Random(1)) == "hi ok cat"   # short words untouched


def test_uwu_softens_rl_and_adds_face():
    out = uwu("really lovely", random.Random(0))
    assert "r" not in out.split(" uwu")[0].replace("really", "")  # r/l became w
    assert out.endswith((" uwu", " owo", " >w<", " :3", " ^w^", " (◕ᴥ◕)"))


def test_drunk_changes_text_but_stays_a_string():
    out = drunk("i am totally fine and sober", random.Random(3))
    assert isinstance(out, str) and out != "i am totally fine and sober"


def test_zalgo_adds_combining_marks():
    base = "cursed"
    out = zalgo(base, rng=random.Random(2))
    assert len(out) > len(base)
    assert all(c in out for c in base)   # original letters still present


def test_keysmash_degrades_and_appends_tail():
    out = keysmash("the quick brown fox jumps", random.Random(5))
    assert " " in out and out != "the quick brown fox jumps"


def test_clean_source_strips_pings():
    assert "@everyone" not in clean_source("hey @everyone look")
    assert "<@123>" not in clean_source("yo <@123> sup")
    assert clean_source("  spaced  ") == "spaced"
    assert len(clean_source("x" * 1000)) <= 400
