"""Blackjack and Hi-Lo rules, checked card by card with stacked shoes.

Deal order is casino order: player, dealer up-card, player, dealer hole card,
then any further cards in the order they're drawn.
"""

import random

import pytest

from syke.cardgames import (
    DECKS,
    HILO_EDGE,
    MAX_HANDS,
    Blackjack,
    Card,
    HiLo,
    Shoe,
    hand_value,
)


def C(text: str) -> Card:
    """'AS' -> Ace of spades, '10H' -> ten of hearts."""
    return Card(text[:-1], text[-1])


def game(*cards: str, bet: int = 100) -> Blackjack:
    g = Blackjack(bet, Shoe.stacked([C(c) for c in cards]))
    g.deal()
    return g


def test_hand_values_soft_and_hard():
    assert hand_value([C("AS"), C("6H")]) == (17, True)
    assert hand_value([C("AS"), C("6H"), C("KD")]) == (17, False)
    assert hand_value([C("AS"), C("AH"), C("9C")]) == (21, True)
    assert hand_value([C("KS"), C("QH"), C("2C")]) == (22, False)


def test_six_deck_shoe_and_cut_card():
    shoe = Shoe(rng=random.Random(1))
    assert shoe.remaining == 52 * DECKS
    while not shoe.needs_shuffle:
        shoe.draw()
    assert shoe.remaining == pytest.approx(52 * DECKS * 0.25, abs=1)   # ~75% penetration


def test_player_blackjack_pays_3_to_2():
    g = game("AS", "9H", "KD", "7C")            # dealer 9 up: no peek, no insurance
    assert g.phase == "done"
    assert g.hands[0].outcome == "blackjack"
    assert g.returned == 250 and g.net == 150


def test_dealer_peeks_on_ten_and_takes_the_hand():
    g = game("10S", "KH", "9D", "AC")
    assert g.peeked and g.dealer_blackjack
    assert g.phase == "done" and g.hands[0].outcome == "lose"


def test_blackjack_vs_dealer_blackjack_pushes():
    g = game("AS", "AH", "KD", "KC")            # dealer shows an Ace
    assert g.phase == "insurance" and g.even_money
    g.decide_insurance(False)
    assert g.hands[0].outcome == "push" and g.net == 0


@pytest.mark.parametrize("hole", ["KC", "7C"])
def test_even_money_always_nets_one_to_one(hole):
    g = game("AS", "AH", "KD", hole)
    g.decide_insurance(True)
    assert g.wagered == 150
    assert g.net == 100                          # guaranteed 1:1 either way


def test_insurance_pays_two_to_one():
    g = game("10S", "AH", "8D", "KC")
    assert g.phase == "insurance" and not g.even_money
    assert g.cost("insurance") == 50
    g.decide_insurance(True)
    assert g.dealer_blackjack and g.hands[0].outcome == "lose"
    assert g.insurance_payout == 150 and g.net == 0


def test_declined_insurance_then_play_on():
    g = game("10S", "AH", "8D", "6C")
    g.decide_insurance(False)
    assert g.phase == "player" and not g.dealer_blackjack


def test_dealer_stands_on_soft_17():
    g = game("10S", "6H", "8D", "AC")           # dealer 6 + A = soft 17
    g.stand()
    assert len(g.dealer) == 2                   # S17: no draw
    assert g.hands[0].outcome == "win"


def test_split_any_ten_value_pair():
    # K-Q is splittable. Hand 1: K + 9 = 19; hand 2: Q + 10 = 20; dealer 5+6 draws 7 = 18.
    g = game("KS", "5H", "QD", "6C", "9S", "10H", "7D")
    assert "split" in g.actions()
    g.split()
    assert len(g.hands) == 2 and g.hand.cards == [C("KS"), C("9S")]
    g.stand()
    assert g.hand.cards == [C("QD"), C("10H")]  # second hand dealt when it comes up
    g.stand()
    assert [h.outcome for h in g.hands] == ["win", "win"]
    assert g.wagered == 200 and g.returned == 400


def test_split_aces_one_card_each_and_21_is_not_blackjack():
    g = game("AS", "9H", "AD", "7C", "KS", "5H", "10D")
    g.split()
    assert g.phase == "done"                    # both split Aces auto-stand on one card
    first, second = g.hands
    assert first.total == 21 and not first.blackjack
    assert first.outcome == "win" and first.payout == 200   # even money, not 3:2
    assert second.total == 16 and second.outcome == "win"   # dealer 16 drew 10 and bust


def test_resplit_up_to_four_hands():
    g = game("8S", "6H", "8D", "10C", "8H", "8C", "8S", "2D", "3D", "4D")
    g.split()                                   # hand 1 draws an 8 -> pair again
    g.split()
    g.split()
    assert len(g.hands) == MAX_HANDS
    assert "split" not in g.actions()           # a fifth hand isn't allowed


def test_double_down_draws_one_and_doubles_the_bet():
    g = game("6S", "6H", "5D", "10C", "10S", "9H")
    assert g.cost("double") == 100
    g.double()
    h = g.hands[0]
    assert h.doubled and h.bet == 200 and len(h.cards) == 3 and h.total == 21
    assert h.outcome == "win" and g.returned == 400   # dealer 16 drew 9 -> bust


def test_double_after_split_but_no_surrender():
    g = game("9S", "6H", "9D", "10C", "2S", "3H")
    g.split()
    acts = g.actions()
    assert "double" in acts and "surrender" not in acts


def test_late_surrender_returns_half_and_only_first():
    g = game("10S", "10H", "6D", "7C")
    g.surrender()
    assert g.hands[0].outcome == "surrender" and g.returned == 50
    assert len(g.dealer) == 2                   # nothing left to play against

    g = game("2S", "10H", "3D", "7C", "4S")
    g.hit()
    assert "surrender" not in g.actions()


def test_dealer_skips_drawing_when_every_hand_busts():
    g = game("10S", "6H", "6D", "10C", "KS")
    g.hit()
    assert g.hands[0].outcome == "bust" and len(g.dealer) == 2


def test_illegal_actions_raise():
    g = game("10S", "6H", "6D", "10C")
    with pytest.raises(ValueError):
        g.split()


def test_basic_play_keeps_a_small_house_edge():
    rng = random.Random(3)
    shoe = Shoe(rng=rng)
    wagered = returned = 0
    for _ in range(30_000):
        if shoe.needs_shuffle:
            shoe.shuffle()
        g = Blackjack(100, shoe)
        g.deal()
        if g.phase == "insurance":
            g.decide_insurance(False)
        while g.phase == "player":
            total, soft = hand_value(g.hand.cards)
            stand_on = 18 if soft else (12 if g.upcard.value <= 6 else 17)
            (g.stand if total >= stand_on else g.hit)()
        wagered += g.wagered
        returned += g.returned
    assert 0.95 < returned / wagered < 1.0


# ── Hi-Lo ───────────────────────────────────────────────────────────────────
class FixedRng(random.Random):
    """Deals a scripted sequence of ranks (suits fixed to spades)."""

    def __init__(self, ranks):
        super().__init__(0)
        self.queue = []
        for r in ranks:
            self.queue += [r, "S"]

    def choice(self, seq):
        return self.queue.pop(0)


def test_hilo_odds_and_multiplier():
    h = HiLo(100, FixedRng(["7"]))
    assert h.odds("higher") == pytest.approx(7 / 13)
    assert h.step_multiplier("higher") == pytest.approx(int(HILO_EDGE / (7 / 13) * 100) / 100)


def test_hilo_correct_call_then_cash_out():
    h = HiLo(100, FixedRng(["5", "9"]))
    assert h.guess("higher") is True
    h.cash_out()
    assert h.won and h.payout == int(100 * h.multiplier)
    assert h.payout > 100


def test_hilo_tie_loses():
    h = HiLo(100, FixedRng(["9", "9"]))
    assert h.guess("higher") is False
    assert h.done and h.payout == 0


def test_hilo_rejects_impossible_calls():
    h = HiLo(100, FixedRng(["2"]))
    with pytest.raises(ValueError):
        h.guess("lower")


def test_hilo_keeps_its_edge():
    rng = random.Random(9)
    paid = 0
    for _ in range(50_000):
        h = HiLo(100, rng)
        g = "higher" if h.odds("higher") >= h.odds("lower") else "lower"
        if h.guess(g):
            h.cash_out()
        paid += h.payout
    assert paid < 100 * 50_000
