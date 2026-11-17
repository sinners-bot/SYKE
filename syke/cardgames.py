"""Casino-accurate card games for THE STRIP: blackjack and Hi-Lo.

Pure logic only — no Discord, no images — so every rule is unit-testable with a
stacked shoe. The blackjack rules mirror a classic Las Vegas Strip table:

* 6-deck shoe with a cut card (~75% penetration); the shoe is only reshuffled
  between rounds, once the cut card has come out — just like a real table.
* Cards are dealt in casino order: player, dealer up-card, player, dealer hole card.
* Blackjack pays 3 to 2. Dealer stands on all 17s (S17).
* Dealer peeks for blackjack when showing an Ace or a 10-value card.
* Insurance (pays 2 to 1) is offered when the dealer shows an Ace; with a
  player blackjack it's the same bet, called "even money".
* Double down on any first two cards, including after a split (DAS).
* Split any pair — including any two 10-value cards (K-Q, J-10…) — re-splitting
  up to 4 hands. Split Aces get one card each and can't be re-split; a 21 after a
  split is a plain 21, not a blackjack.
* Late surrender: give up half your bet as your first decision, after the peek.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS = ("S", "H", "D", "C")
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}

DECKS = 6
PENETRATION = 0.75            # fraction of the shoe dealt before the cut card comes out
DEALER_HITS_SOFT_17 = False   # S17, the Strip's standard on most tables
MAX_HANDS = 4                 # split / re-split up to four hands
DOUBLE_AFTER_SPLIT = True
RESPLIT_ACES = False


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    @property
    def value(self) -> int:
        if self.rank == "A":
            return 11
        if self.rank in ("10", "J", "Q", "K"):
            return 10
        return int(self.rank)

    @property
    def red(self) -> bool:
        return self.suit in ("H", "D")

    def __str__(self) -> str:
        return f"{self.rank}{SUIT_SYMBOL[self.suit]}"


def hand_value(cards: list[Card]) -> tuple[int, bool]:
    """Best total for a hand and whether it's soft (an Ace still counting as 11)."""
    total = sum(1 if c.rank == "A" else c.value for c in cards)
    if any(c.rank == "A" for c in cards) and total + 10 <= 21:
        return total + 10, True
    return total, False


class Shoe:
    """A multi-deck shoe. `needs_shuffle` turns true once the cut card is reached."""

    def __init__(self, decks: int = DECKS, rng: random.Random | None = None,
                 penetration: float = PENETRATION) -> None:
        self.decks = decks
        self.rng = rng or random.Random()
        self.penetration = penetration
        self.shuffles = 0
        self.cards: list[Card] = []
        self.cut = 0
        self.shuffle()

    @classmethod
    def stacked(cls, cards: list[Card]) -> "Shoe":
        """A shoe that deals exactly `cards`, in order (for tests)."""
        shoe = cls.__new__(cls)
        shoe.decks, shoe.rng, shoe.penetration, shoe.shuffles = 1, random.Random(0), 1.0, 0
        shoe.cards = list(reversed(cards))
        shoe.cut = 0
        return shoe

    def shuffle(self) -> None:
        self.cards = [Card(r, s) for _ in range(self.decks) for s in SUITS for r in RANKS]
        self.rng.shuffle(self.cards)
        self.cut = int(len(self.cards) * (1 - self.penetration))
        self.shuffles += 1

    @property
    def remaining(self) -> int:
        return len(self.cards)

    @property
    def needs_shuffle(self) -> bool:
        return len(self.cards) <= self.cut

    def draw(self) -> Card:
        if not self.cards:  # never strand a round mid-deal
            self.shuffle()
        return self.cards.pop()


@dataclass
class Hand:
    cards: list[Card] = field(default_factory=list)
    bet: int = 0
    doubled: bool = False
    from_split: bool = False
    split_aces: bool = False
    surrendered: bool = False
    done: bool = False
    outcome: str = ""     # blackjack / win / push / lose / bust / surrender
    payout: int = 0       # total returned for this hand (stake included)

    @property
    def total(self) -> int:
        return hand_value(self.cards)[0]

    @property
    def soft(self) -> bool:
        return hand_value(self.cards)[1]

    @property
    def busted(self) -> bool:
        return self.total > 21

    @property
    def blackjack(self) -> bool:
        return len(self.cards) == 2 and self.total == 21 and not self.from_split


class Blackjack:
    """One round at the table. Phases: 'insurance' → 'player' → 'done'."""

    def __init__(self, bet: int, shoe: Shoe) -> None:
        self.shoe = shoe
        self.base_bet = bet
        self.hands: list[Hand] = [Hand(bet=bet)]
        self.dealer: list[Card] = []
        self.active = 0
        self.phase = "dealing"
        self.insurance_bet = 0
        self.insurance_payout = 0
        self.peeked = False
        self.dealer_blackjack = False
        self.acted = False   # any player decision yet (surrender must be the first)

    # -- dealing --
    def deal(self) -> None:
        hand = self.hands[0]
        hand.cards.append(self.shoe.draw())
        self.dealer.append(self.shoe.draw())
        hand.cards.append(self.shoe.draw())
        self.dealer.append(self.shoe.draw())
        if self.upcard.rank == "A":
            self.phase = "insurance"   # offered before the dealer peeks
            return
        self._after_insurance()

    @property
    def upcard(self) -> Card:
        return self.dealer[0]

    @property
    def hand(self) -> Hand:
        return self.hands[min(self.active, len(self.hands) - 1)]

    @property
    def even_money(self) -> bool:
        """Insurance on a player blackjack is offered as 'even money'."""
        return self.phase == "insurance" and self.hands[0].blackjack

    def decide_insurance(self, take: bool) -> None:
        if self.phase != "insurance":
            raise ValueError("insurance isn't on offer")
        if take:
            self.insurance_bet = self.base_bet // 2
        self._after_insurance()

    def _after_insurance(self) -> None:
        if self.upcard.value in (10, 11):
            self.peeked = True
            if hand_value(self.dealer)[0] == 21:
                self._settle()
                return
        if self.hands[0].blackjack:
            self._settle()
            return
        self.phase = "player"

    # -- player decisions --
    def actions(self) -> set[str]:
        if self.phase == "insurance":
            return {"insurance", "no_insurance"}
        if self.phase != "player":
            return set()
        h = self.hand
        acts = {"hit", "stand"}
        if len(h.cards) == 2 and not h.split_aces and (DOUBLE_AFTER_SPLIT or not h.from_split):
            acts.add("double")
        if (len(h.cards) == 2 and h.cards[0].value == h.cards[1].value
                and len(self.hands) < MAX_HANDS and (RESPLIT_ACES or not h.split_aces)):
            acts.add("split")
        if not self.acted and len(self.hands) == 1 and len(h.cards) == 2:
            acts.add("surrender")
        return acts

    def cost(self, action: str) -> int:
        """Extra chips an action puts on the table."""
        if action in ("double", "split"):
            return self.hand.bet
        if action == "insurance":
            return self.base_bet // 2
        return 0

    def _require(self, action: str) -> Hand:
        if action not in self.actions():
            raise ValueError(f"can't {action} right now")
        self.acted = True
        return self.hand

    def hit(self) -> None:
        h = self._require("hit")
        h.cards.append(self.shoe.draw())
        if h.total >= 21:
            h.done = True
        self._advance()

    def stand(self) -> None:
        self._require("stand").done = True
        self._advance()

    def double(self) -> None:
        h = self._require("double")
        h.bet *= 2
        h.doubled = True
        h.cards.append(self.shoe.draw())
        h.done = True
        self._advance()

    def split(self) -> None:
        h = self._require("split")
        aces = h.cards[0].rank == "A"
        second = Hand(cards=[h.cards.pop()], bet=h.bet, from_split=True, split_aces=aces)
        h.from_split = True
        h.split_aces = aces
        self.hands.insert(self.active + 1, second)
        self._advance()

    def surrender(self) -> None:
        h = self._require("surrender")
        h.surrendered = True
        h.done = True
        self._advance()

    def _advance(self) -> None:
        """Deal to split hands as they come up and move on to the next live hand."""
        while self.active < len(self.hands):
            h = self.hands[self.active]
            if len(h.cards) == 1:          # a split hand gets its second card when it's played
                h.cards.append(self.shoe.draw())
                if h.split_aces:
                    h.done = True          # one card only on split Aces
            if not h.done and h.total >= 21:
                h.done = True              # 21 (or bust) stands automatically
            if h.done:
                self.active += 1
                continue
            return
        self._dealer_turn()

    def _dealer_turn(self) -> None:
        live = [h for h in self.hands if not h.busted and not h.surrendered]
        if live:
            while True:
                total, soft = hand_value(self.dealer)
                if total < 17 or (total == 17 and soft and DEALER_HITS_SOFT_17):
                    self.dealer.append(self.shoe.draw())
                    continue
                break
        self._settle()

    def _settle(self) -> None:
        self.phase = "done"
        d_total = hand_value(self.dealer)[0]
        d_bj = len(self.dealer) == 2 and d_total == 21
        self.dealer_blackjack = d_bj
        for h in self.hands:
            if h.surrendered:
                h.outcome, h.payout = "surrender", h.bet // 2
            elif h.busted:
                h.outcome, h.payout = "bust", 0
            elif h.blackjack and not d_bj:
                h.outcome, h.payout = "blackjack", h.bet + (h.bet * 3) // 2
            elif h.blackjack and d_bj:
                h.outcome, h.payout = "push", h.bet
            elif d_bj:
                h.outcome, h.payout = "lose", 0
            elif d_total > 21 or h.total > d_total:
                h.outcome, h.payout = "win", h.bet * 2
            elif h.total == d_total:
                h.outcome, h.payout = "push", h.bet
            else:
                h.outcome, h.payout = "lose", 0
        self.insurance_payout = self.insurance_bet * 3 if (self.insurance_bet and d_bj) else 0

    # -- money --
    @property
    def wagered(self) -> int:
        return sum(h.bet for h in self.hands) + self.insurance_bet

    @property
    def returned(self) -> int:
        return sum(h.payout for h in self.hands) + self.insurance_payout

    @property
    def net(self) -> int:
        return self.returned - self.wagered


# ── Hi-Lo ──────────────────────────────────────────────────────────────────
HILO_ORDER = {r: i for i, r in enumerate(("2", "3", "4", "5", "6", "7", "8", "9", "10",
                                          "J", "Q", "K", "A"), start=2)}
HILO_EDGE = 0.97       # each correct call pays 97% of fair odds
HILO_MAX_STEPS = 12


class HiLo:
    """Call whether the next card is higher or lower (Aces high, ties lose). Each correct
    call multiplies your winnings by fair odds less a small edge; cash out any time."""

    def __init__(self, bet: int, rng: random.Random | None = None) -> None:
        self.bet = bet
        self.rng = rng or random.Random()
        self.cards: list[Card] = [self._draw()]
        self.multiplier = 1.0
        self.steps = 0
        self.done = False
        self.won: bool | None = None

    def _draw(self) -> Card:
        return Card(self.rng.choice(RANKS), self.rng.choice(SUITS))

    @property
    def current(self) -> Card:
        return self.cards[-1]

    def odds(self, guess: str) -> float:
        cur = HILO_ORDER[self.current.rank]
        if guess == "higher":
            n = sum(1 for v in HILO_ORDER.values() if v > cur)
        else:
            n = sum(1 for v in HILO_ORDER.values() if v < cur)
        return n / 13

    def step_multiplier(self, guess: str) -> float:
        p = self.odds(guess)
        return math.floor(HILO_EDGE / p * 100) / 100 if p else 0.0

    def guess(self, guess: str) -> bool:
        if self.done:
            raise ValueError("round is over")
        if guess not in ("higher", "lower") or self.odds(guess) == 0:
            raise ValueError(f"can't call {guess} on a {self.current.rank}")
        step = self.step_multiplier(guess)
        before = HILO_ORDER[self.current.rank]
        card = self._draw()
        self.cards.append(card)
        after = HILO_ORDER[card.rank]
        right = after > before if guess == "higher" else after < before
        if not right:
            self.done, self.won = True, False
            return False
        self.multiplier = round(self.multiplier * step, 4)
        self.steps += 1
        if self.steps >= HILO_MAX_STEPS:
            self.cash_out()
        return True

    def cash_out(self) -> None:
        if self.done:
            return
        self.done, self.won = True, True

    @property
    def payout(self) -> int:
        return int(self.bet * self.multiplier) if self.won else 0
