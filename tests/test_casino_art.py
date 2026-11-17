"""The casino art renders at the right size, with real transparency where it matters."""

import io
import random

import pytest
from PIL import Image

from syke import casino_art as art
from syke.cardgames import Blackjack, Card, HiLo, Shoe
from syke.economy import ROULETTE_RED


def png(data: bytes) -> Image.Image:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return Image.open(io.BytesIO(data))


def C(text: str) -> Card:
    return Card(text[:-1], text[-1])


def test_fonts_are_bundled():
    for name in art.FONTS.values():
        assert (art.FONT_DIR / name).is_file()
    assert art.font("bold", 20).getname()[0] == "Poppins"


@pytest.mark.parametrize("cards", [
    ["10S", "6H", "8D", "AC"],          # mid-hand
    ["AS", "9H", "KD", "7C"],           # natural, settled
    ["10S", "AH", "8D", "6C"],          # insurance offer
])
def test_blackjack_table_renders(cards):
    g = Blackjack(250, Shoe.stacked([C(c) for c in cards]))
    g.deal()
    im = png(art.blackjack_table(g, player_name="Tester", shoe_left=300))
    assert im.size == (art.BJ_W, art.BJ_H)


def test_blackjack_table_with_four_split_hands():
    g = Blackjack(100, Shoe.stacked([C(c) for c in
                                     ["8S", "6H", "8D", "10C", "8H", "8C", "8S", "2D", "3D", "4D", "5D", "KD"]]))
    g.deal()
    for _ in range(3):
        g.split()
    assert len(g.hands) == 4
    png(art.blackjack_table(g))


def test_other_games_render():
    assert png(art.slots_machine(["7️⃣", "7️⃣", "7️⃣"], bet=100, won=2500, multiplier=25)).size == (1100, 700)
    assert png(art.roulette_wheel(0, space="red", bet=10, won=0, red=ROULETTE_RED)).size == (1200, 700)
    assert png(art.coin_flip("tails", pick="heads", bet=10, won=0)).size == (1000, 560)
    h = HiLo(100, random.Random(2))
    assert png(art.hilo_table(h, higher_mult=1.5, lower_mult=2.0)).size == (1200, 700)


def test_players_card_tiers_and_avatar_fallback():
    assert art.tier_for(0)[0] == "BRONZE"
    assert art.tier_for(10_000)[0] == "GOLD"
    assert art.tier_for(5_000_000)[0] == "BLACK"
    im = png(art.players_card(name="Ekoramm", cash=1, chips=2, bank=3, rank=1, member_no=42,
                              avatar=b"not an image"))
    assert im.size == (1000, 560)


@pytest.mark.parametrize("kind", art.EMOJI_NAMES)
def test_emoji_icons_are_transparent_128px(kind):
    im = png(art.emoji_icon(kind)).convert("RGBA")
    assert im.size == (128, 128)
    assert im.getpixel((0, 0))[3] == 0                         # corners stay see-through
    assert len(art.emoji_icon(kind)) < 256 * 1024              # Discord's emoji size limit


def test_card_images_are_cached():
    a = art.card_image(Card("Q", "H"), 100)
    b = art.card_image(Card("Q", "H"), 100)
    assert a is b


def test_region_canvas_keeps_parent_coordinates():
    cv = art.Canvas(200, 100, scale=1)
    sub = cv.sub((50, 20, 80, 40))
    sub.rect((50, 20, 80, 40), fill=(255, 0, 0, 255))
    cv.over(sub)
    assert cv.img.getpixel((60, 30))[:3] == (255, 0, 0)
    assert cv.img.getpixel((10, 10))[3] == 0
