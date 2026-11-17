"""THE STRIP casino games: pure rules, no storage. strip_casino.py moves the chips.

* Slot machines: five themed machines, each with its own reel strip (symbol weights), pay table and limits.
  Every reel stops independently on a weighted symbol; a machine pays on the line of three. The return to
  player of every machine is computed exactly from its table (rtp()) and kept below 100%.
* Roulette: single-zero wheel. A bet is a set of numbers; it pays 36 / (size of the set) times the stake
  in total (straight 36x, split 18x, street 12x, corner 9x, six-line 6x, dozen/column 3x, even money 2x),
  and zero loses every outside bet, exactly like a European table.
* Table blackjack: one shoe, one dealer, up to five seats playing in turn. Same rules as cardgames.py
  (6 decks, S17, 3:2, peek, double on any two incl. after split, split to four hands, one card on split
  aces). A round is rebuilt from (secret seed, bets, actions), so it survives restarts.
"""

from __future__ import annotations

import itertools
import random
import re

from .cardgames import Hand, Shoe, hand_value, DEALER_HITS_SOFT_17, MAX_HANDS, DOUBLE_AFTER_SPLIT, RESPLIT_ACES

# ---------------------------------------------------------------------------------------------- slots
# reels: symbol -> weight (each reel the same strip). pays: what a line of three returns, as a total
# multiple of the bet: "triple" = three of a symbol; "any" = every reel shows one of the listed symbols
# (checked after triples, best first); "pair" = exactly two of a symbol anywhere on the line.
SLOT_MACHINES: dict[str, dict] = {
    "lucky7s": {
        "name": "Lucky 7s", "limits": (10, 100_000), "jackpot": "triple7",
        "reels": {"cherry": 30, "lemon": 24, "bar": 18, "bell": 14, "seven": 9, "triple7": 5},
        "triple": {"triple7": 250, "seven": 50, "bell": 16, "bar": 10, "lemon": 6, "cherry": 5},
        "any": [(("seven", "triple7"), 12), (("bar", "bell", "seven", "triple7"), 2)],
        "pair": {"cherry": 2},
    },
    "desertgold": {
        "name": "Desert Gold", "limits": (10, 100_000), "jackpot": "goldrush",
        "reels": {"cactus": 32, "horseshoe": 24, "scorpion": 18, "nugget": 13, "goldbar": 9, "goldrush": 4},
        "triple": {"goldrush": 650, "goldbar": 65, "nugget": 33, "scorpion": 16, "horseshoe": 10, "cactus": 7},
        "any": [(("nugget", "goldbar", "goldrush"), 5)],
        "pair": {"horseshoe": 2},
    },
    "neon": {
        "name": "Neon Nights", "limits": (10, 50_000), "jackpot": "neon7",
        "reels": {"heart": 28, "star": 26, "cocktail": 20, "dice": 14, "crown": 8, "neon7": 4},
        "triple": {"neon7": 300, "crown": 60, "dice": 25, "cocktail": 15, "star": 9, "heart": 7},
        "any": [(("heart", "star"), 3)],
        "pair": {"crown": 3},
    },
    "deadman": {
        "name": "Dead Man's Jackpot", "limits": (25, 100_000), "jackpot": "deadskull",
        "reels": {"candle": 34, "raven": 24, "rose": 18, "tombstone": 13, "acespade": 8, "deadskull": 3},
        "triple": {"deadskull": 1300, "acespade": 100, "tombstone": 40, "rose": 20, "raven": 10, "candle": 6},
        "any": [(("tombstone", "acespade", "deadskull"), 6)],
        "pair": {"candle": 1},
    },
    "vinnie": {
        "name": "Vinnie's Lucky Machine", "limits": (50, 250_000), "jackpot": "vinnie",
        "reels": {"dollar": 30, "cigar": 24, "fedora": 18, "ring": 14, "briefcase": 9, "vinnie": 5},
        "triple": {"vinnie": 360, "briefcase": 55, "ring": 22, "fedora": 14, "cigar": 7, "dollar": 5},
        "any": [(("ring", "briefcase", "vinnie"), 6)],
        "pair": {"dollar": 2},
    },
}


def slot_pay(machine: str, reels: list[str]) -> tuple[int, str]:
    """(total multiple of the bet, what hit: 'jackpot' / 'triple' / 'any' / 'pair' / '')."""
    m = SLOT_MACHINES[machine]
    a, b, c = reels
    if a == b == c:
        mult = m["triple"].get(a, 0)
        return mult, ("jackpot" if a == m["jackpot"] else "triple") if mult else ""
    for syms, mult in sorted(m["any"], key=lambda x: -x[1]):
        if all(r in syms for r in reels):
            return mult, "any"
    for sym, mult in m["pair"].items():
        if reels.count(sym) == 2:
            return mult, "pair"
    return 0, ""


def slot_spin(machine: str, rng) -> list[str]:
    strip = SLOT_MACHINES[machine]["reels"]
    syms, weights = list(strip), list(strip.values())
    return [rng.choices(syms, weights=weights, k=1)[0] for _ in range(3)]


def slot_rtp(machine: str) -> float:
    """Exact return to player: the sum over every line of P(line) x pay."""
    strip = SLOT_MACHINES[machine]["reels"]
    tot = sum(strip.values())
    rtp = 0.0
    for line in itertools.product(strip, repeat=3):
        p = 1.0
        for s in line:
            p *= strip[s] / tot
        rtp += p * slot_pay(machine, list(line))[0]
    return rtp


def slot_config() -> dict:
    """What the game shows: names, symbols in strip order, pay tables, limits, jackpot symbol, RTP."""
    return {k: {"name": m["name"], "limits": list(m["limits"]), "jackpot": m["jackpot"], "symbols": list(m["reels"]),
                "triple": m["triple"], "any": [[list(s), x] for s, x in m["any"]], "pair": m["pair"],
                "rtp": round(slot_rtp(k), 4)} for k, m in SLOT_MACHINES.items()}


# ---------------------------------------------------------------------------------------------- roulette
WHEEL_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
_OUTSIDE = {
    "red": WHEEL_RED, "black": set(range(1, 37)) - WHEEL_RED,
    "even": {n for n in range(1, 37) if n % 2 == 0}, "odd": {n for n in range(1, 37) if n % 2},
    "low": set(range(1, 19)), "high": set(range(19, 37)),
    "dozen1": set(range(1, 13)), "dozen2": set(range(13, 25)), "dozen3": set(range(25, 37)),
    "col1": set(range(1, 37, 3)), "col2": set(range(2, 37, 3)), "col3": set(range(3, 37, 3)),
}
_GROUP = re.compile(r"^(split|street|corner|line):(\d{1,2})(?:-(\d{1,2}))?$")


def roulette_numbers(space: str) -> frozenset | None:
    """The numbers a bet covers, or None if it isn't a bet on this table.

    Spaces: '0'..'36', red/black/even/odd/low/high, dozen1-3, col1-3,
    'split:a-b' (two neighbours on the layout, incl. 0-1/0-2/0-3), 'street:n' (n, n+1, n+2; n = 1,4,..34),
    'corner:n' (n, n+1, n+3, n+4; n not in the right-hand column, n <= 32), 'line:n' (two streets; n = 1,4,..31).
    """
    s = str(space or "").strip().lower()
    if s.isdigit():
        n = int(s)
        return frozenset({n}) if 0 <= n <= 36 and s == str(n) else None
    if s in _OUTSIDE:
        return frozenset(_OUTSIDE[s])
    m = _GROUP.match(s)
    if not m:
        return None
    kind, a = m.group(1), int(m.group(2))
    b = int(m.group(3)) if m.group(3) is not None else None
    if kind == "split":
        if b is None or not (0 <= a < b <= 36):
            return None
        ok = (a == 0 and b in (1, 2, 3)) or (a >= 1 and (b == a + 3 or (b == a + 1 and a % 3 != 0)))
        return frozenset({a, b}) if ok else None
    if b is not None:
        return None
    if kind == "street":
        return frozenset({a, a + 1, a + 2}) if 1 <= a <= 34 and a % 3 == 1 else None
    if kind == "corner":
        return frozenset({a, a + 1, a + 3, a + 4}) if 1 <= a <= 32 and a % 3 != 0 else None
    if kind == "line":
        return frozenset(range(a, a + 6)) if 1 <= a <= 31 and a % 3 == 1 else None
    return None


def roulette_mult(space: str, number: int) -> int:
    """Total returned per chip staked (0 = lost)."""
    nums = roulette_numbers(space)
    if not nums or number not in nums:
        return 0
    return 36 // len(nums)


def roulette_colour(n: int) -> str:
    return "green" if n == 0 else ("red" if n in WHEEL_RED else "black")


# ---------------------------------------------------------------------------------------------- table blackjack
class TableRound:
    """One blackjack round at a shared table. Seats play in seat order; phase 'player' -> 'done'.

    bets: [(seat, bet)] sorted by seat. Moves are (seat, action) and must be made by the seat whose turn it is.
    """

    def __init__(self, seed: int, bets: list[tuple[int, int]], shoe: Shoe | None = None) -> None:
        self.shoe = shoe or Shoe(rng=random.Random(seed))
        self.seats = [s for s, _ in bets]
        self.hands: dict[int, list[Hand]] = {s: [Hand(bet=b)] for s, b in bets}
        self.active: dict[int, int] = {s: 0 for s in self.seats}
        self.dealer: list = []
        self.turn = 0                 # index into self.seats
        self.phase = "player"
        self.dealer_blackjack = False
        for _ in range(2):            # casino order: each seat, then the dealer, twice
            for s in self.seats:
                self.hands[s][0].cards.append(self.shoe.draw())
            self.dealer.append(self.shoe.draw())
        if self.dealer[0].value in (10, 11) and hand_value(self.dealer)[0] == 21:
            self._settle()            # the dealer peeks: blackjack, round over
            return
        for s in self.seats:
            if self.hands[s][0].blackjack:
                self.hands[s][0].done = True
        self._advance()

    # -- whose turn --
    @property
    def seat(self) -> int | None:
        return self.seats[self.turn] if self.phase == "player" and self.turn < len(self.seats) else None

    def hand_of(self, seat: int) -> Hand:
        hs = self.hands[seat]
        return hs[min(self.active[seat], len(hs) - 1)]

    def actions(self) -> set[str]:
        if self.seat is None:
            return set()
        s = self.seat
        h = self.hand_of(s)
        acts = {"hit", "stand"}
        if len(h.cards) == 2 and not h.split_aces and (DOUBLE_AFTER_SPLIT or not h.from_split):
            acts.add("double")
        if (len(h.cards) == 2 and h.cards[0].value == h.cards[1].value and len(self.hands[s]) < MAX_HANDS
                and (RESPLIT_ACES or not h.split_aces)):
            acts.add("split")
        return acts

    def cost(self, action: str) -> int:
        return self.hand_of(self.seat).bet if action in ("double", "split") and self.seat is not None else 0

    def act(self, seat: int, action: str) -> None:
        if seat != self.seat:
            raise ValueError("not your turn")
        if action not in self.actions():
            raise ValueError(f"can't {action} right now")
        h = self.hand_of(seat)
        if action == "hit":
            h.cards.append(self.shoe.draw())
            if h.total >= 21:
                h.done = True
        elif action == "stand":
            h.done = True
        elif action == "double":
            h.bet *= 2; h.doubled = True
            h.cards.append(self.shoe.draw()); h.done = True
        elif action == "split":
            aces = h.cards[0].rank == "A"
            second = Hand(cards=[h.cards.pop()], bet=h.bet, from_split=True, split_aces=aces)
            h.from_split = True; h.split_aces = aces
            self.hands[seat].insert(self.active[seat] + 1, second)
        self._advance()

    def _advance(self) -> None:
        while self.turn < len(self.seats):
            s = self.seats[self.turn]
            hs = self.hands[s]
            while self.active[s] < len(hs):
                h = hs[self.active[s]]
                if len(h.cards) == 1:                      # a split hand gets its second card when it's played
                    h.cards.append(self.shoe.draw())
                    if h.split_aces:
                        h.done = True
                if not h.done and h.total >= 21:
                    h.done = True
                if h.done:
                    self.active[s] += 1
                    continue
                return
            self.turn += 1
        self._dealer_turn()

    def _dealer_turn(self) -> None:
        live = [h for hs in self.hands.values() for h in hs if not h.busted and not h.blackjack]
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
        for hs in self.hands.values():
            for h in hs:
                h.done = True
                if h.busted:
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

    # -- money per seat --
    def wagered(self, seat: int) -> int:
        return sum(h.bet for h in self.hands[seat])

    def returned(self, seat: int) -> int:
        return sum(h.payout for h in self.hands[seat]) if self.phase == "done" else 0


def card_code(c) -> str:
    return f"{c.rank}{c.suit}"
