"""The slur counter: spellings it catches, words it must not, censoring, and the tally."""

import pytest

from syke import slurs


@pytest.mark.parametrize("text,label,count", [
    ("nigga what", "n-word", 1), ("NIGGER", "n-word", 1), ("my niggas", "n-word", 1), ("n1gga n1gga", "n-word", 2),
    ("niqqa please", "n-word", 1), ("niggaz", "n-word", 1), ("nigguh", "n-word", 1),
    ("stupid paki", "paki", 1), ("pakis", "paki", 1), ("a chink", "chink", 1), ("spics", "spic", 1),
    ("kike", "kike", 1), ("wetback", "wetback", 1), ("du kanake", "kanake", 1), ("sand nigger", "sand n-word", 1),
])
def test_catches_slurs(text, label, count):
    assert slurs.count_slurs(text)[label] == count


@pytest.mark.parametrize("text", ["Niger is a country", "she sniggered", "pakistan is big", "spicy food",
                                  "a spice rack", "packaging", "nightingale", "the snigger", "chinkapin oak?no",
                                  "kikeriki", "night niggle"])
def test_ignores_normal_words(text):
    found = slurs.count_slurs(text)
    assert "n-word" not in found and "paki" not in found and "spic" not in found and "kike" not in found


def test_sand_variant_isnt_double_counted():
    assert slurs.count_slurs("sand nigger") == {"sand n-word": 1}


def test_censor():
    assert slurs.censor("paki") == "p***" and slurs.censor("n-word") == "n-word"
    assert slurs.censor("porch monkey") == "p**** m*****"


def test_tally_ranks_and_skips_optouts_and_commands():
    rows = [(1, "Ada", "nigga nigga", 100.0), (1, "Ada", "paki", 200.0), (2, "Bo", "nigger", 50.0),
            (3, "Cy", "nigga", 10.0), (2, "Bo", "!racist nigga", 60.0)]
    people = slurs.tally(rows, optouts={3}, is_command=lambda t: t.startswith("!"))
    assert people[1]["total"] == 3 and people[1]["words"]["n-word"] == 2 and people[1]["first"] == 100.0
    assert people[2]["total"] == 1 and 3 not in people
    assert slurs.breakdown(people[1]["words"]) == "n-word ×2, p*** ×1"
