"""THE STRIP's casino graphics, drawn from scratch with Pillow.

Everything here is vector-drawn (no image assets besides the bundled fonts) at
2x and downsampled, so edges stay crisp at any Discord zoom level. One design
system runs through all of it: midnight-purple night sky, hot-pink / purple /
cyan neon, gold trim, and green baize for the tables.

Public renderers return PNG bytes:
    blackjack_table, slots_machine, roulette_wheel, coin_flip, hilo_table,
    players_card, emoji_icon
"""

from __future__ import annotations

import io
import math
import random
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .cardgames import Card, hand_value

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
FONTS = {"bold": "Poppins-Bold.ttf", "medium": "Poppins-Medium.ttf", "serif": "DejaVuSerif-Bold.ttf"}

# ── palette ────────────────────────────────────────────────────────────────
NIGHT_TOP = (13, 7, 26)
NIGHT_BOTTOM = (34, 11, 52)
PINK = (255, 62, 165)
PURPLE = (155, 92, 255)
CYAN = (62, 230, 255)
GOLD = (255, 201, 74)
GOLD_DEEP = (184, 128, 28)
CREAM = (252, 249, 242)
INK = (24, 19, 33)
CARD_RED = (206, 28, 52)
FELT_IN = (20, 116, 74)
FELT_OUT = (5, 48, 31)
WIN = (62, 224, 128)
LOSS = (255, 82, 102)
PUSH = (190, 190, 215)
WHITE = (255, 255, 255)

CHIP_DENOMS = [  # value, body colour, accent colour
    (5000, (122, 76, 38), (255, 214, 140)),
    (1000, (245, 176, 30), (40, 30, 10)),
    (500, (120, 52, 200), (255, 230, 255)),
    (100, (26, 24, 30), (255, 255, 255)),
    (25, (24, 150, 82), (255, 255, 255)),
    (5, (210, 30, 50), (255, 255, 255)),
    (1, (238, 236, 230), (40, 70, 160)),
]


@lru_cache(maxsize=64)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / FONTS[kind]), size)
    except OSError:
        return ImageFont.load_default(size)


class Canvas:
    """An RGBA image drawn in logical (1x) units at `scale`x for anti-aliasing.

    A canvas can be a small *region* of a bigger one (`sub`): it keeps the parent's
    coordinate system (via an origin offset), so effects like blurred shadows and
    neon glows only process the pixels around them instead of the whole image.
    """

    def __init__(self, w: int, h: int, scale: int = 2, bg=(0, 0, 0, 0), origin=(0.0, 0.0)) -> None:
        self.w, self.h, self.S = w, h, scale
        self.ox, self.oy = origin
        self.img = Image.new("RGBA", (max(1, int(w * scale)), max(1, int(h * scale))), bg)
        self.d = ImageDraw.Draw(self.img)

    # coordinates (all inputs alternate x, y)
    def p(self, *vals: float) -> list[int]:
        return [int(round((v - (self.ox if i % 2 == 0 else self.oy)) * self.S)) for i, v in enumerate(vals)]

    def layer(self) -> "Canvas":
        return Canvas(self.w, self.h, self.S, origin=(self.ox, self.oy))

    def sub(self, box, pad: float = 0.0) -> "Canvas":
        """A transparent region canvas covering `box` (+pad), in this canvas's coordinates."""
        x0 = max(self.ox, box[0] - pad)
        y0 = max(self.oy, box[1] - pad)
        x1 = min(self.ox + self.w, box[2] + pad)
        y1 = min(self.oy + self.h, box[3] + pad)
        return Canvas(max(1.0, x1 - x0), max(1.0, y1 - y0), self.S, origin=(x0, y0))

    # primitives
    def rrect(self, box, r, fill=None, outline=None, width=1.0) -> None:
        self.d.rounded_rectangle(self.p(*box), radius=int(r * self.S), fill=fill, outline=outline,
                                 width=max(1, int(width * self.S)) if outline else 0)

    def rect(self, box, fill=None, outline=None, width=1.0) -> None:
        self.d.rectangle(self.p(*box), fill=fill, outline=outline,
                         width=max(1, int(width * self.S)) if outline else 0)

    def ellipse(self, box, fill=None, outline=None, width=1.0) -> None:
        self.d.ellipse(self.p(*box), fill=fill, outline=outline,
                       width=max(1, int(width * self.S)) if outline else 0)

    def circle(self, cx, cy, r, fill=None, outline=None, width=1.0) -> None:
        self.ellipse((cx - r, cy - r, cx + r, cy + r), fill, outline, width)

    def polygon(self, pts, fill=None, outline=None) -> None:
        self.d.polygon([tuple(self.p(x, y)) for x, y in pts], fill=fill, outline=outline)

    def line(self, pts, fill, width=1.0) -> None:
        self.d.line([tuple(self.p(x, y)) for x, y in pts], fill=fill, width=max(1, int(width * self.S)),
                    joint="curve")

    def pieslice(self, box, start, end, fill=None, outline=None, width=1.0) -> None:
        self.d.pieslice(self.p(*box), start, end, fill=fill, outline=outline,
                        width=max(1, int(width * self.S)) if outline else 0)

    def text(self, xy, text, kind="bold", size=20, fill=WHITE, anchor="mm", stroke=0.0,
             stroke_fill=None) -> None:
        self.d.text(tuple(self.p(*xy)), text, font=font(kind, int(size * self.S)), fill=fill,
                    anchor=anchor, stroke_width=int(stroke * self.S), stroke_fill=stroke_fill)

    def text_width(self, text, kind="bold", size=20) -> float:
        return font(kind, int(size * self.S)).getlength(text) / self.S

    # compositing
    def _dest(self, other: "Canvas") -> tuple[int, int]:
        return (int(round((other.ox - self.ox) * self.S)), int(round((other.oy - self.oy) * self.S)))

    def over(self, other: "Canvas") -> None:
        self.img.alpha_composite(other.img, self._dest(other))

    def paste(self, im: Image.Image, x: float, y: float) -> None:
        dx, dy = self.p(x, y)
        if dx < 0 or dy < 0:  # alpha_composite can't take negative offsets: crop instead
            im = im.crop((max(0, -dx), max(0, -dy), im.width, im.height))
            dx, dy = max(0, dx), max(0, dy)
        self.img.alpha_composite(im, (dx, dy))

    def blur_over(self, other: "Canvas", radius: float) -> None:
        self.img.alpha_composite(other.img.filter(ImageFilter.GaussianBlur(radius * self.S)), self._dest(other))

    def png(self) -> bytes:
        out = self.img.resize((int(self.w), int(self.h)), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, "PNG", compress_level=6)
        return buf.getvalue()


# ── shared effects ─────────────────────────────────────────────────────────
@lru_cache(maxsize=32)
def _gradient(w: int, h: int, top, bottom) -> Image.Image:
    col = Image.new("RGBA", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    return col.resize((w, h))


def gradient(w: int, h: int, top, bottom) -> Image.Image:
    return _gradient(int(w), int(h), tuple(top), tuple(bottom)).copy()


@lru_cache(maxsize=16)
def _radial(w: int, h: int, inner, outer) -> Image.Image:
    mask = Image.radial_gradient("L").resize((w, h), Image.BICUBIC)
    return Image.composite(Image.new("RGBA", (w, h), outer + (255,)),
                           Image.new("RGBA", (w, h), inner + (255,)), mask)


def radial(w: int, h: int, inner, outer) -> Image.Image:
    return _radial(int(w), int(h), tuple(inner), tuple(outer)).copy()


@lru_cache(maxsize=8)
def _sky(w: int, h: int, scale: int, seed: int) -> Image.Image:
    cv = Canvas(w, h, scale)
    cv.img.alpha_composite(gradient(*cv.img.size, NIGHT_TOP, NIGHT_BOTTOM))
    glow = cv.layer()  # full-size is fine here: the sky is rendered once and cached
    glow.ellipse((-w * .2, h * .72, w * 1.2, h * 1.6), fill=PINK + (70,))
    glow.ellipse((w * .1, h * .82, w * .9, h * 1.4), fill=PURPLE + (90,))
    cv.blur_over(glow, 60)
    rng = random.Random(seed)
    for _ in range(46):
        x, y = rng.uniform(0, w), rng.uniform(0, h * .55)
        cv.circle(x, y, rng.choice((0.8, 1.0, 1.3)), fill=(255, 255, 255, rng.randint(70, 190)))
    return cv.img


def night_sky(cv: Canvas, seed: int = 7) -> None:
    """Midnight gradient with a soft city glow and a few stars (cached per size)."""
    cv.img.alpha_composite(_sky(int(cv.w), int(cv.h), cv.S, seed))


def _text_box(cv: Canvas, xy, text, kind, size, anchor) -> tuple[float, float, float, float]:
    w = cv.text_width(text, kind, size)
    x, y = xy
    x0 = x - w / 2 if anchor[0] == "m" else (x - w if anchor[0] == "r" else x)
    return x0, y - size, x0 + w, y + size


def neon_text(cv: Canvas, xy, text, colour, size, kind="bold", anchor="mm", glow=10.0, core=None) -> None:
    halo = cv.sub(_text_box(cv, xy, text, kind, size, anchor), pad=glow * 3)
    halo.text(xy, text, kind, size, fill=colour + (255,), anchor=anchor, stroke=size * .06,
              stroke_fill=colour + (255,))
    cv.blur_over(halo, glow)
    cv.blur_over(halo, glow / 3)
    cv.text(xy, text, kind, size, fill=core or tuple(min(255, c + 150) for c in colour), anchor=anchor)


def glow_shape(cv: Canvas, box, radius: float, draw) -> None:
    """Blur whatever `draw(layer)` paints inside `box` and composite it as a glow."""
    layer = cv.sub(box, pad=radius * 3)
    draw(layer)
    cv.blur_over(layer, radius)


def shadow(cv: Canvas, box, r, blur=10.0, offset=(0, 6), alpha=150) -> None:
    x0, y0, x1, y1 = box
    sb = (x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1])
    glow_shape(cv, sb, blur, lambda sh: sh.rrect(sb, r, fill=(0, 0, 0, alpha)))


def star(cv: Canvas, cx, cy, r, fill, points: int = 5) -> None:
    pts = []
    for i in range(points * 2):
        a = math.pi / points * i - math.pi / 2
        rr = r if i % 2 == 0 else r * .45
        pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
    cv.polygon(pts, fill=fill)


def pill(cv: Canvas, cx, cy, text, colour, size=18, text_colour=WHITE, pad=14) -> None:
    w = cv.text_width(text, "bold", size) + pad * 2
    h = size * 1.7
    shadow(cv, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), h / 2, blur=6, offset=(0, 3), alpha=120)
    cv.rrect((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), h / 2, fill=colour)
    cv.text((cx, cy + size * .04), text, "bold", size, fill=text_colour)


def header(cv: Canvas, title: str, right: str = "") -> None:
    """The neon sign strip across the top of every casino image."""
    cv.rect((0, 0, cv.w, 66), fill=(8, 4, 16, 235))
    cv.line([(0, 66), (cv.w, 66)], fill=GOLD_DEEP + (255,), width=2)
    neon_text(cv, (28, 34), "THE STRIP", PINK, 30, anchor="lm", glow=9)
    x = 28 + cv.text_width("THE STRIP", "bold", 30) + 22
    cv.circle(x - 11, 34, 3.5, fill=GOLD)
    cv.text((x, 35), title, "bold", 24, fill=GOLD, anchor="lm")
    if right:
        cv.text((cv.w - 26, 35), right, "medium", 16, fill=(200, 190, 230), anchor="rm")


def chip_face(cv: Canvas, cx, cy, r, body, accent, label: str = "") -> None:
    """A casino chip seen from above: edge spots, inlay ring and a printed value."""
    cv.circle(cx, cy, r, fill=body)
    for i in range(8):
        a = math.radians(i * 45 + 22.5)
        x, y = cx + math.cos(a) * r * .82, cy + math.sin(a) * r * .82
        span = math.radians(9)
        pts = [(cx + math.cos(a + t) * r * .99, cy + math.sin(a + t) * r * .99) for t in (-span, span)]
        pts += [(cx + math.cos(a + t) * r * .66, cy + math.sin(a + t) * r * .66) for t in (span, -span)]
        cv.polygon(pts, fill=accent)
        del x, y
    cv.circle(cx, cy, r * .64, fill=body, outline=accent, width=max(1.0, r * .05))
    cv.circle(cx, cy, r * .5, outline=accent + (160,) if len(accent) == 3 else accent, width=max(.8, r * .02))
    if label:
        size = r * (.52 if len(label) <= 2 else .4 if len(label) <= 3 else .32)
        cv.text((cx, cy + r * .02), label, "bold", size, fill=accent)


def chip_stack(cv: Canvas, cx, base_y, amount: int, r=26.0) -> None:
    """A little 3D stack of chips making up `amount` (largest denominations first)."""
    chips = []
    left = amount
    for value, body, accent in CHIP_DENOMS:
        while left >= value and len(chips) < 9:
            chips.append((value, body, accent))
            left -= value
    if not chips:
        chips = [CHIP_DENOMS[-1]]
    chips.reverse()  # smallest at the bottom
    th = r * .22
    shadow(cv, (cx - r, base_y - r * .5, cx + r, base_y + r * .5), r, blur=5, offset=(0, 3), alpha=140)
    for i, (value, body, accent) in enumerate(chips):
        y = base_y - i * th
        dark = tuple(int(c * .6) for c in body)
        cv.ellipse((cx - r, y - r * .42 + th, cx + r, y + r * .42 + th), fill=dark)
        cv.rect((cx - r, y, cx + r, y + th), fill=dark)
        for k in range(6):  # edge spots on the side
            sx = cx - r + (k + .5) * (2 * r / 6)
            cv.rect((sx - r * .07, y + th * .15, sx + r * .07, y + th * .85), fill=accent)
        cv.ellipse((cx - r, y - r * .42, cx + r, y + r * .42), fill=body)
        cv.ellipse((cx - r * .62, y - r * .26, cx + r * .62, y + r * .26), outline=accent, width=1.2)
    top_value = chips[-1][0]
    top_y = base_y - (len(chips) - 1) * th
    cv.text((cx, top_y + 1), short(top_value), "bold", r * .36, fill=chips[-1][2])


def short(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n // 1000}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


# ── suits & cards ──────────────────────────────────────────────────────────
def suit(cv: Canvas, kind: str, cx, cy, s, colour, flip=False) -> None:
    """Draw a suit symbol `s` tall centred on (cx, cy); `flip` draws it upside down."""
    f = -1 if flip else 1

    def P(x, y):
        return (cx + x * s, cy + f * y * s)

    def circ(x, y, r):
        px, py = P(x, y)
        cv.circle(px, py, r * s, fill=colour)

    def poly(pts):
        cv.polygon([P(x, y) for x, y in pts], fill=colour)

    if kind == "H":
        circ(-.235, -.17, .27)
        circ(.235, -.17, .27)
        poly([(-.495, -.11), (.495, -.11), (.0, .5)])
    elif kind == "D":
        pts = []
        corners = [(0, -.5), (.38, 0), (0, .5), (-.38, 0)]
        for i in range(4):
            (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
            for k in range(8):
                t = k / 8
                bow = math.sin(math.pi * t) * .05
                mx, my = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                pts.append((mx - mx * bow * 2.2, my - my * bow * 2.2))
        poly(pts)
    elif kind == "S":
        circ(-.235, .1, .255)
        circ(.235, .1, .255)
        poly([(-.485, .05), (.485, .05), (0, -.5)])
        poly([(-.045, .12), (.045, .12), (.17, .5), (-.17, .5)])
    else:  # clubs
        circ(0, -.24, .215)
        circ(-.235, .08, .215)
        circ(.235, .08, .215)
        circ(0, -.02, .13)
        poly([(-.045, .02), (.045, .02), (.16, .5), (-.16, .5)])


PIPS = {
    2: [(.5, 0), (.5, 1)],
    3: [(.5, 0), (.5, .5), (.5, 1)],
    4: [(0, 0), (1, 0), (0, 1), (1, 1)],
    5: [(0, 0), (1, 0), (.5, .5), (0, 1), (1, 1)],
    6: [(0, 0), (1, 0), (0, .5), (1, .5), (0, 1), (1, 1)],
    7: [(0, 0), (1, 0), (.5, .25), (0, .5), (1, .5), (0, 1), (1, 1)],
    8: [(0, 0), (1, 0), (.5, .25), (0, .5), (1, .5), (.5, .75), (0, 1), (1, 1)],
    9: [(0, 0), (1, 0), (0, 1 / 3), (1, 1 / 3), (.5, .5), (0, 2 / 3), (1, 2 / 3), (0, 1), (1, 1)],
    10: [(0, 0), (1, 0), (.5, 1 / 6), (0, 1 / 3), (1, 1 / 3), (0, 2 / 3), (1, 2 / 3), (.5, 5 / 6),
         (0, 1), (1, 1)],
}


def _crown(cv: Canvas, cx, cy, w, colour, points=5) -> None:
    h = w * .55
    pts = [(cx - w / 2, cy + h / 2)]
    for i in range(points * 2 - 1):
        x = cx - w / 2 + w * i / (points * 2 - 2)
        y = cy - h / 2 if i % 2 == 0 else cy + h * .05
        pts.append((x, y))
    pts.append((cx + w / 2, cy + h / 2))
    cv.polygon(pts, fill=colour)
    for i in range(points):
        x = cx - w / 2 + w * i / (points - 1)
        cv.circle(x, cy - h / 2, w * .06, fill=colour)
    cv.rect((cx - w / 2, cy + h / 2 - w * .08, cx + w / 2, cy + h / 2), fill=tuple(int(c * .8) for c in colour))


def card_image(card: Card | None, w: int = 130, scale: int = 2) -> Image.Image:
    """A full playing card (or the STRIP card back when `card` is None) as an RGBA image
    already at `scale`x, ready to be pasted onto a canvas with the same scale. Cached —
    treat the result as read-only."""
    key = (card.rank, card.suit) if card is not None else None
    return _card_image(key, int(w), scale)


@lru_cache(maxsize=512)
def _card_image(key, w: int, scale: int) -> Image.Image:
    card = Card(*key) if key is not None else None
    h = int(w * 1.4)
    cv = Canvas(w, h, scale)
    r = w * .085
    if card is None:
        cv.rrect((0, 0, w, h), r, fill=CREAM)
        m = w * .07
        cv.rrect((m, m, w - m, h - m), r * .6, fill=(58, 16, 96))
        lat = cv.layer()
        step = w * .11
        for i in range(-12, 24):
            x = m + i * step
            lat.line([(x, m), (x + (h - 2 * m), h - m)], fill=PINK + (120,), width=w * .012)
            lat.line([(x, h - m), (x + (h - 2 * m), m)], fill=PURPLE + (150,), width=w * .012)
        mask = Image.new("L", cv.img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(cv.p(m, m, w - m, h - m), radius=int(r * .6 * scale), fill=255)
        cv.img.paste(lat.img, (0, 0), Image.composite(lat.img.getchannel("A"), Image.new("L", mask.size, 0), mask))
        cv.circle(w / 2, h / 2, w * .25, fill=(28, 8, 48), outline=GOLD, width=w * .02)
        cv.text((w / 2, h / 2 + w * .015), "S", "bold", w * .3, fill=GOLD)
        cv.rrect((m, m, w - m, h - m), r * .6, outline=GOLD + (200,), width=w * .012)
        return cv.img

    colour = CARD_RED if card.red else INK
    cv.rrect((0, 0, w, h), r, fill=CREAM, outline=(205, 198, 186), width=w * .008)
    # corner indices (top-left, and the same rotated in the bottom-right)
    idx = Canvas(w, h, scale)
    rank_size = w * (.2 if card.rank != "10" else .17)
    idx.text((w * .13, h * .1), card.rank, "serif", rank_size, fill=colour)
    suit(idx, card.suit, w * .13, h * .1 + w * .19, w * .13, colour)
    cv.over(idx)
    cv.img.alpha_composite(idx.img.rotate(180))

    if card.rank == "A":
        big = w * (.62 if card.suit == "S" else .48)
        suit(cv, card.suit, w / 2, h / 2, big, colour)
    elif card.rank in ("J", "Q", "K"):
        fx0, fy0, fx1, fy1 = w * .22, h * .14, w * .78, h * .86
        cv.rrect((fx0, fy0, fx1, fy1), w * .03, fill=(250, 238, 214), outline=GOLD_DEEP, width=w * .014)
        cv.rrect((fx0 + w * .03, fy0 + w * .03, fx1 - w * .03, fy1 - w * .03), w * .02,
                 outline=colour + (150,), width=w * .008)
        mid = (fy0 + fy1) / 2
        cv.line([(fx0 + w * .05, mid), (fx1 - w * .05, mid)], fill=GOLD_DEEP + (160,), width=w * .006)
        _crown(cv, w / 2, mid - h * .2, w * (.3 if card.rank == "K" else .24 if card.rank == "Q" else .18),
               GOLD, points=5 if card.rank == "K" else 3)
        cv.text((w / 2, mid - h * .03), card.rank, "serif", w * .3, fill=colour)
        suit(cv, card.suit, w / 2, mid + h * .17, w * .16, colour)
        suit(cv, card.suit, fx0 + w * .1, fy0 + w * .1, w * .07, colour)
        suit(cv, card.suit, fx1 - w * .1, fy1 - w * .1, w * .07, colour, flip=True)
    else:
        n = int(card.rank)
        px0, px1 = w * .3, w * .7
        py0, py1 = h * .17, h * .83
        size = w * (.19 if n <= 8 else .17)
        for fx, fy in PIPS[n]:
            x = px0 + (px1 - px0) * fx
            y = py0 + (py1 - py0) * fy
            suit(cv, card.suit, x, y, size, colour, flip=fy > .5)
    return cv.img


def place_card(cv: Canvas, card: Card | None, x: float, y: float, w: float) -> None:
    h = w * 1.4
    shadow(cv, (x, y, x + w, y + h), w * .085, blur=7, offset=(2, 7), alpha=150)
    cv.paste(card_image(card, int(w), cv.S), x, y)


# ── blackjack ──────────────────────────────────────────────────────────────
def arc_text(cv: Canvas, text: str, cx, cy, radius, size, colour, kind="bold", spread=1.0) -> None:
    """Text printed along the bottom arc of a circle (like the felt print on a real table)."""
    f = font(kind, int(size * cv.S))
    widths = [f.getlength(ch) / cv.S for ch in text]
    total = sum(widths) * spread
    angle = -total / radius / 2
    for ch, wd in zip(text, widths):
        step = wd * spread / radius
        a = angle + step / 2
        x = cx + math.sin(a) * radius
        y = cy + math.cos(a) * radius
        glyph = Image.new("RGBA", (int(size * 2 * cv.S), int(size * 2 * cv.S)), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((glyph.width / 2, glyph.height / 2), ch, font=f, fill=colour, anchor="mm")
        glyph = glyph.rotate(math.degrees(a), resample=Image.BICUBIC)
        cv.img.alpha_composite(glyph, (int(x * cv.S - glyph.width / 2), int(y * cv.S - glyph.height / 2)))
        angle += step


OUTCOME_STYLE = {
    "blackjack": ("BLACKJACK!", GOLD, INK),
    "win": ("WIN", WIN, INK),
    "push": ("PUSH", PUSH, INK),
    "lose": ("LOSE", LOSS, WHITE),
    "bust": ("BUST", LOSS, WHITE),
    "surrender": ("SURRENDER", PUSH, INK),
}


def _total_label(cards: list[Card]) -> str:
    total, soft = hand_value(cards)
    if len(cards) == 2 and total == 21:
        return "21"
    return f"{total - 10}/{total}" if soft and total != 21 else str(total)


BJ_W, BJ_H = 1200, 820
# Felt print & rail are concentric arcs centred far above the table, like the
# half-moon table seen from the dealer's side.
ARC_CY = -755


@lru_cache(maxsize=2)
def _felt(W: int, H: int, S: int) -> Image.Image:
    """The static part of the blackjack table (felt, grain, rail, print, chip tray)."""
    cv = Canvas(W, H, S)
    cv.img.alpha_composite(radial(*cv.img.size, FELT_IN, FELT_OUT))
    # padded leather rail along the players' edge; dark wood beyond it
    a, b = 1900, 1515
    box = (W / 2 - a, ARC_CY - b, W / 2 + a, ARC_CY + b)
    outside = Image.new("L", cv.img.size, 255)
    ImageDraw.Draw(outside).ellipse(cv.p(*box), fill=0)
    cv.img.paste(Image.new("RGBA", cv.img.size, (34, 16, 8, 255)), (0, 0), outside)
    cv.ellipse(box, outline=(52, 26, 12), width=36)
    cv.ellipse((box[0] + 4, box[1] + 4, box[2] - 4, box[3] - 4), outline=(96, 54, 24), width=6)
    cv.ellipse((box[0] + 17, box[1] + 17, box[2] - 17, box[3] - 17), outline=(140, 86, 40, 200), width=2)
    # felt print
    arc_text(cv, "BLACKJACK PAYS 3 TO 2", W / 2, ARC_CY, 1100, 30, GOLD + (235,), spread=1.18)
    arc_text(cv, "DEALER MUST DRAW TO 16 AND STAND ON ALL 17s", W / 2, ARC_CY, 1133, 15,
             (226, 242, 230, 200), kind="medium", spread=1.3)
    arc_text(cv, "INSURANCE PAYS 2 TO 1", W / 2, ARC_CY, 1159, 15, GOLD + (190,), kind="bold", spread=1.4)
    # the dealer's chip tray
    tx0, tx1, ty0, ty1 = W / 2 - 200, W / 2 + 200, 76, 122
    shadow(cv, (tx0, ty0, tx1, ty1), 10, blur=6, offset=(0, 4), alpha=150)
    cv.rrect((tx0, ty0, tx1, ty1), 10, fill=(30, 15, 8), outline=GOLD_DEEP, width=2)
    wells = [CHIP_DENOMS[1], CHIP_DENOMS[2], CHIP_DENOMS[3], CHIP_DENOMS[3], CHIP_DENOMS[4],
             CHIP_DENOMS[4], CHIP_DENOMS[5], CHIP_DENOMS[5], CHIP_DENOMS[6], CHIP_DENOMS[6]]
    ww = (tx1 - tx0 - 20) / len(wells)
    for i, (_, body, accent) in enumerate(wells):
        x = tx0 + 10 + i * ww
        cv.rrect((x + 2, ty0 + 7, x + ww - 2, ty1 - 7), 5, fill=body)
        for k in range(7):
            yy = ty0 + 10 + k * 4.4
            cv.line([(x + 3, yy), (x + ww - 3, yy)], fill=tuple(int(c * .65) for c in body) + (255,), width=1.2)
        cv.rect((x + ww * .42, ty0 + 7, x + ww * .58, ty1 - 7), fill=accent + (120,))
    return cv.img


def betting_circle(cv: Canvas, cx, cy, r) -> None:
    cv.circle(cx, cy, r, fill=(0, 0, 0, 40), outline=GOLD + (210,), width=2.5)
    cv.circle(cx, cy, r - 6, outline=GOLD + (90,), width=1)


def blackjack_table(game, *, player_name: str = "You", shoe_left: int | None = None,
                    reveal: bool | None = None) -> bytes:
    """Render a blackjack round: dealer on top, player hand(s) below, bets in betting circles."""
    W, H = BJ_W, BJ_H
    cv = Canvas(W, H)
    cv.img.alpha_composite(_felt(W, H, cv.S))
    header(cv, "BLACKJACK", f"6 DECKS  •  {shoe_left} CARDS IN SHOE" if shoe_left is not None else "6 DECKS")

    done = game.phase == "done"
    show_hole = done if reveal is None else reveal
    # dealer
    dw = 108
    dealer_cards = list(game.dealer)
    n = len(dealer_cards)
    dx = dw * .62
    x0 = W / 2 - (dw + dx * (n - 1)) / 2
    dy = 136
    for i, c in enumerate(dealer_cards):
        place_card(cv, c if (i != 1 or show_hole) else None, x0 + i * dx, dy, dw)
    cv.text((x0 - 26, dy + 34), "DEALER", "bold", 17, fill=(232, 246, 236, 230), anchor="rm")
    if dealer_cards:
        d_total = hand_value(dealer_cards)[0]
        if show_hole:
            label = "BLACKJACK" if (len(dealer_cards) == 2 and d_total == 21) else _total_label(dealer_cards)
        else:
            label = "A" if dealer_cards[0].rank == "A" else str(dealer_cards[0].value)
        bx = x0 + dw + dx * (n - 1) + 26
        bust = show_hole and d_total > 21
        pill(cv, bx + cv.text_width(label, "bold", 18) / 2 + 14, dy + 34,
             label + (" BUST" if bust else ""), LOSS if bust else (8, 30, 20), 18)

    # player hands
    hands = game.hands
    count = len(hands)
    pw = {1: 116, 2: 104, 3: 88, 4: 78}[count]
    ph = pw * 1.4
    slot = W / count
    py = 468
    for i, hand in enumerate(hands):
        cx = slot * i + slot / 2 + 26
        cards = hand.cards
        k = len(cards)
        room = slot - 96
        pdx = min(pw * .58, (room - pw) / max(1, k - 1)) if k > 1 else 0
        span = pw + pdx * (k - 1)
        hx0 = cx - span / 2
        if not done and game.phase == "player" and i == game.active:
            box = (hx0 - 12, py - 14, hx0 + span + 12, py + ph + 12)
            glow_shape(cv, box, 8, lambda g, b=box: g.rrect(b, 16, outline=CYAN + (255,), width=5))
            cv.rrect(box, 16, outline=CYAN + (235,), width=2)
        for j, c in enumerate(cards):
            place_card(cv, c, hx0 + j * pdx, py, pw)
        # the bet sits in a betting circle beside the cards
        cr = 30 if count <= 2 else 25
        bcx, bcy = hx0 - cr - 14, py + ph - cr - 8
        betting_circle(cv, bcx, bcy, cr + 6)
        chip_stack(cv, bcx, bcy + cr * .25, hand.bet, r=cr * .82)
        cv.text((bcx, bcy + cr + 22), short(hand.bet), "bold", 14, fill=(240, 240, 240))
        if cards:
            label = "BLACKJACK" if hand.blackjack else _total_label(cards)
            pill(cv, cx, py - 26, label, LOSS if hand.busted else (8, 30, 20), 17)
        tags = []
        if hand.doubled:
            tags.append("DOUBLED")
        if count > 1:
            tags.append(f"HAND {i + 1}")
        tag_y = py + ph + 30
        if done and hand.outcome:
            text, bg, fg = OUTCOME_STYLE[hand.outcome]
            pill(cv, cx, py + ph + 30, text, bg, 20 if count < 3 else 16, text_colour=fg)
            tag_y = py + ph + 62
        if tags:
            cv.text((cx, tag_y), "  •  ".join(tags), "medium", 13, fill=(225, 242, 230, 215))

    # insurance bet, placed on the insurance line
    if game.insurance_bet:
        ix, iy = W / 2 + 330, 262
        betting_circle(cv, ix, iy, 30)
        chip_stack(cv, ix, iy + 6, game.insurance_bet, r=21)
        cv.text((ix, iy + 50), "INSURANCE", "bold", 13, fill=GOLD)

    # footer banner
    cv.rect((0, H - 52, W, H), fill=(6, 4, 12, 230))
    cv.line([(0, H - 52), (W, H - 52)], fill=GOLD_DEEP + (255,), width=2)
    if done:
        net = game.net
        if net > 0:
            text, colour = f"YOU WIN  +{net:,}", WIN
        elif net == 0:
            text, colour = "PUSH  —  BET RETURNED", PUSH
        else:
            text, colour = f"DEALER WINS  {net:,}", LOSS
        neon_text(cv, (W / 2, H - 26), text, colour, 25, glow=8)
    else:
        hint = ("INSURANCE?  DEALER SHOWS AN ACE" if not game.even_money else "EVEN MONEY?") \
            if game.phase == "insurance" else f"{player_name.upper()} TO ACT"
        cv.text((W / 2, H - 25), hint, "bold", 17, fill=(225, 220, 245))
    return cv.png()


# ── slots ──────────────────────────────────────────────────────────────────
def slot_symbol(cv: Canvas, sym: str, cx, cy, s) -> None:
    """Draw one reel symbol `s` across, centred on (cx, cy)."""
    if sym == "🍒":
        cv.line([(cx - s * .2, cy + s * .05), (cx + s * .02, cy - s * .38), (cx + s * .22, cy + s * .02)],
                fill=(40, 120, 40), width=s * .055)
        cv.polygon([(cx + s * .02, cy - s * .38), (cx + s * .32, cy - s * .44), (cx + s * .22, cy - s * .28)],
                   fill=(70, 170, 60))
        for ox in (-.2, .22):
            x, y = cx + s * ox, cy + s * .2
            cv.circle(x, y, s * .2, fill=(170, 10, 30))
            cv.circle(x - s * .02, y - s * .02, s * .18, fill=(225, 30, 50))
            cv.circle(x - s * .07, y - s * .08, s * .05, fill=(255, 190, 200))
    elif sym == "🔔":
        body = [(cx - s * .34, cy + s * .22), (cx - s * .27, cy - s * .05), (cx - s * .2, cy - s * .27),
                (cx, cy - s * .38), (cx + s * .2, cy - s * .27), (cx + s * .27, cy - s * .05),
                (cx + s * .34, cy + s * .22)]
        cv.polygon(body, fill=GOLD_DEEP)
        cv.polygon([(x - s * .02 if x < cx else x - s * .02, y - s * .02) for x, y in body], fill=GOLD)
        cv.rrect((cx - s * .4, cy + s * .18, cx + s * .4, cy + s * .28), s * .05, fill=GOLD_DEEP)
        cv.circle(cx, cy + s * .33, s * .08, fill=(150, 100, 20))
        cv.circle(cx, cy - s * .42, s * .05, fill=GOLD_DEEP)
        cv.polygon([(cx - s * .16, cy - s * .2), (cx - s * .1, cy - s * .26), (cx - s * .18, cy + s * .12),
                    (cx - s * .24, cy + s * .1)], fill=(255, 240, 180, 200))
    elif sym == "🍋":
        pts = []
        for i in range(48):
            a = 2 * math.pi * i / 48
            rx = s * .38 * (1 + .14 * abs(math.cos(a)) ** 6)
            pts.append((cx + math.cos(a) * rx, cy + math.sin(a) * s * .27))
        cv.polygon(pts, fill=(222, 176, 0))
        cv.polygon([(x - (x - cx) * .06, y - (y - cy) * .1 - s * .02) for x, y in pts], fill=(255, 222, 40))
        cv.ellipse((cx - s * .22, cy - s * .17, cx + s * .02, cy - s * .07), fill=(255, 248, 190, 200))
    elif sym == "💎":
        top = cy - s * .3
        girdle = cy - s * .1
        tip = cy + s * .38
        cv.polygon([(cx - s * .24, top), (cx + s * .24, top), (cx + s * .42, girdle), (cx, tip),
                    (cx - s * .42, girdle)], fill=(20, 150, 220))
        cv.polygon([(cx - s * .24, top), (cx + s * .24, top), (cx + s * .42, girdle), (cx - s * .42, girdle)],
                   fill=(110, 220, 255))
        cv.polygon([(cx - s * .1, top), (cx + s * .1, top), (cx + s * .16, girdle), (cx - s * .16, girdle)],
                   fill=(200, 245, 255))
        cv.polygon([(cx - s * .16, girdle), (cx + s * .16, girdle), (cx, tip)], fill=(60, 190, 245))
        cv.polygon([(cx - s * .42, girdle), (cx - s * .16, girdle), (cx, tip)], fill=(30, 120, 200))
    elif sym == "7️⃣":
        cv.text((cx + s * .02, cy + s * .03), "7", "bold", s * .92, fill=(120, 0, 20), stroke=s * .05,
                stroke_fill=GOLD_DEEP)
        cv.text((cx, cy), "7", "bold", s * .92, fill=(232, 22, 52), stroke=s * .035, stroke_fill=GOLD)
    else:  # 🎰 — the STRIP jackpot badge
        cv.circle(cx, cy, s * .4, fill=(40, 10, 70), outline=GOLD, width=s * .045)
        for i in range(10):
            a = math.radians(i * 36 - 90)
            cv.circle(cx + math.cos(a) * s * .32, cy + math.sin(a) * s * .32, s * .022, fill=GOLD)
        cv.text((cx, cy + s * .02), "S", "bold", s * .42, fill=PINK, stroke=s * .02, stroke_fill=(255, 220, 240))


@lru_cache(maxsize=4)
def _reel_parts(size: tuple[int, int], scale: int) -> tuple[Image.Image, Image.Image, Image.Image]:
    """Cached cylinder shading, top/bottom shadow and rounded mask for one reel strip."""
    w, h = size
    cyl = Image.new("RGBA", size)
    shade = Image.new("RGBA", size)
    cd, sd = ImageDraw.Draw(cyl), ImageDraw.Draw(shade)
    for y in range(h):
        t = y / h
        v = int(110 + 145 * math.sin(math.pi * t))
        cd.line([(0, y), (w, y)], fill=(v, v, min(255, v + 6), 255))
        a = int(200 * max(0.0, abs(t - .5) * 2 - .32) / .68)
        sd.line([(0, y), (w, y)], fill=(10, 0, 20, a))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=14 * scale, fill=255)
    return cyl, shade, mask


def slots_machine(reels: list[str], *, bet: int, won: int, multiplier: int, seed: int = 0) -> bytes:
    W, H = 1100, 700
    cv = Canvas(W, H)
    night_sky(cv, seed=11)
    header(cv, "SLOTS", "3 REELS  •  1 LINE")
    # cabinet
    cx0, cy0, cx1, cy1 = 150, 92, W - 150, H - 28
    shadow(cv, (cx0, cy0, cx1, cy1), 40, blur=18, offset=(0, 12), alpha=180)
    frame = gradient(int((cx1 - cx0) * cv.S), int((cy1 - cy0) * cv.S), (255, 226, 130), GOLD_DEEP)
    fmask = Image.new("L", frame.size, 0)
    ImageDraw.Draw(fmask).rounded_rectangle((0, 0, frame.size[0] - 1, frame.size[1] - 1),
                                            radius=40 * cv.S, fill=255)
    cv.img.paste(frame, tuple(cv.p(cx0, cy0)), fmask)
    cv.rrect((cx0 + 16, cy0 + 16, cx1 - 16, cy1 - 16), 30, fill=(70, 12, 50))
    # marquee with bulbs
    mx0, my0, mx1, my1 = cx0 + 40, cy0 + 32, cx1 - 40, cy0 + 132
    cv.rrect((mx0, my0, mx1, my1), 22, fill=(20, 6, 30), outline=GOLD, width=3)
    bulbs = cv.sub((mx0, my0, mx1, my1), pad=16)
    per_row = 22
    for i in range(per_row):
        x = mx0 + 14 + i * (mx1 - mx0 - 28) / (per_row - 1)
        for y in (my0 + 12, my1 - 12):
            col = (255, 236, 150) if i % 2 == 0 else PINK
            bulbs.circle(x, y, 5, fill=col + (255,))
    cv.blur_over(bulbs, 5)
    cv.over(bulbs)
    title = "STRIP SLOTS"
    tx = (mx0 + mx1) / 2
    ty = (my0 + my1) / 2 + 1
    neon_text(cv, (tx, ty), title, PINK, 40, glow=11)
    half = cv.text_width(title, "bold", 40) / 2
    for sx in (tx - half - 34, tx + half + 34):
        glow_shape(cv, (sx - 14, ty - 14, sx + 14, ty + 14), 6, lambda g, x=sx: star(g, x, ty, 14, GOLD + (255,)))
        star(cv, sx, ty, 13, (255, 236, 160))
    # reel window
    rx0, ry0, rx1, ry1 = cx0 + 52, cy0 + 162, cx1 - 52, cy0 + 432
    cv.rrect((rx0 - 10, ry0 - 10, rx1 + 10, ry1 + 10), 22, fill=(16, 6, 22), outline=GOLD_DEEP, width=3)
    rw = (rx1 - rx0 - 2 * 14) / 3
    rng = random.Random(hash(tuple(reels)) ^ seed)
    from .economy import SLOT_SYMBOLS  # local import: economy imports this module
    mid = (ry0 + ry1) / 2
    span = (ry1 - ry0) / 3
    for i, sym in enumerate(reels):
        x0 = rx0 + i * (rw + 14)
        reel = cv.sub((x0, ry0, x0 + rw, ry1))
        cyl, shade, mask = _reel_parts(reel.img.size, cv.S)
        reel.img.alpha_composite(cyl)
        for k, (yc, symbol) in enumerate([(mid - span * 1.05, rng.choice(SLOT_SYMBOLS)), (mid, sym),
                                          (mid + span * 1.05, rng.choice(SLOT_SYMBOLS))]):
            slot_symbol(reel, symbol, x0 + rw / 2, yc, span * (.9 if k == 1 else .78))
        reel.img.alpha_composite(shade)
        reel.img.putalpha(Image.composite(reel.img.getchannel("A"), Image.new("L", mask.size, 0), mask))
        cv.over(reel)
    # payline
    glow_shape(cv, (rx0 - 6, mid - 3, rx1 + 6, mid + 3), 6,
               lambda g: g.line([(rx0 - 6, mid), (rx1 + 6, mid)], fill=PINK + (255,), width=5))
    cv.line([(rx0 - 6, mid), (rx1 + 6, mid)], fill=(255, 170, 215, 230), width=2)
    for x, d in ((rx0 - 28, 1), (rx1 + 28, -1)):
        cv.polygon([(x, mid - 12), (x, mid + 12), (x + 16 * d, mid)], fill=PINK)
    # result panel
    py0 = ry1 + 34
    cv.rrect((cx0 + 52, py0, cx1 - 52, py0 + 120), 20, fill=(14, 4, 20), outline=GOLD_DEEP, width=2)
    third = (cx1 - cx0 - 104) / 3
    cols = [cx0 + 52 + third * (k + .5) for k in range(3)]
    cv.text((cols[0], py0 + 38), "BET", "medium", 17, fill=(200, 185, 220))
    cv.text((cols[0], py0 + 78), f"{bet:,}", "bold", 30, fill=WHITE)
    cv.text((cols[2], py0 + 38), "PAYS", "medium", 17, fill=(200, 185, 220))
    cv.text((cols[2], py0 + 78), f"×{multiplier}" if multiplier else "—", "bold", 30, fill=GOLD)
    if won > bet:
        neon_text(cv, (cols[1], py0 + 44), "BIG WIN" if multiplier >= 12 else "WINNER", WIN, 26, glow=8)
        cv.text((cols[1], py0 + 84), f"+{won - bet:,}", "bold", 30, fill=WHITE)
    elif won == bet:
        cv.text((cols[1], py0 + 60), "PUSH", "bold", 30, fill=PUSH)
    else:
        cv.text((cols[1], py0 + 44), "NO WIN", "bold", 24, fill=LOSS)
        cv.text((cols[1], py0 + 84), f"-{bet:,}", "bold", 26, fill=(230, 200, 210))
    return cv.png()


# ── roulette ───────────────────────────────────────────────────────────────
WHEEL_ORDER = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1,
               20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]


def roulette_wheel(number: int, *, space: str, bet: int, won: int, red: set[int]) -> bytes:
    W, H = 1200, 700
    cv = Canvas(W, H)
    night_sky(cv, seed=5)
    header(cv, "ROULETTE", "SINGLE ZERO  •  EUROPEAN WHEEL")
    cx, cy, R = 360, 384, 286
    shadow(cv, (cx - R, cy - R, cx + R, cy + R), R, blur=20, offset=(0, 14), alpha=200)
    # wooden bowl
    cv.circle(cx, cy, R, fill=(70, 34, 14))
    cv.circle(cx, cy, R * .965, fill=(120, 66, 28))
    cv.circle(cx, cy, R * .93, fill=(48, 22, 8))
    # pockets: rotate so the winning number sits at 12 o'clock
    n = len(WHEEL_ORDER)
    seg = 360 / n
    win_i = WHEEL_ORDER.index(number)
    start = -90 - seg / 2 - win_i * seg
    ro, ri = R * .88, R * .6
    for i, num in enumerate(WHEEL_ORDER):
        a0 = start + i * seg
        colour = (24, 140, 70) if num == 0 else ((200, 22, 40) if num in red else (20, 18, 24))
        cv.pieslice((cx - ro, cy - ro, cx + ro, cy + ro), a0, a0 + seg, fill=colour)
        cv.pieslice((cx - ro, cy - ro, cx + ro, cy + ro), a0, a0 + seg, outline=GOLD_DEEP, width=1.4)
    # numbers
    for i, num in enumerate(WHEEL_ORDER):
        a = math.radians(start + (i + .5) * seg)
        rr = R * .79
        x, y = cx + math.cos(a) * rr, cy + math.sin(a) * rr
        g = Image.new("RGBA", (60 * cv.S, 40 * cv.S), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((g.width / 2, g.height / 2), str(num), font=font("bold", 17 * cv.S),
                               fill=WHITE, anchor="mm")
        g = g.rotate(-math.degrees(a) - 90, resample=Image.BICUBIC, expand=True)
        cv.img.alpha_composite(g, (int(x * cv.S - g.width / 2), int(y * cv.S - g.height / 2)))
    cv.d = ImageDraw.Draw(cv.img)
    # pocket ring (where the ball rests) + highlight of the winner
    cv.circle(cx, cy, ri, fill=(34, 16, 6))
    # outline the winning pocket in glowing gold (an annular sector, so its number stays visible)
    a0 = start + win_i * seg
    sector = [(cx + math.cos(math.radians(a0 + seg * t / 10)) * ro, cy + math.sin(math.radians(a0 + seg * t / 10)) * ro)
              for t in range(11)]
    sector += [(cx + math.cos(math.radians(a0 + seg * t / 10)) * ri, cy + math.sin(math.radians(a0 + seg * t / 10)) * ri)
               for t in range(10, -1, -1)]
    sector.append(sector[0])
    xs, ys = [x for x, _ in sector], [y for _, y in sector]
    glow_shape(cv, (min(xs), min(ys), max(xs), max(ys)), 8,
               lambda g: g.line(sector, fill=GOLD + (255,), width=8))
    cv.line(sector, fill=GOLD, width=3.5)
    # cone + turret
    cone = radial(int(ri * 1.86 * cv.S), int(ri * 1.86 * cv.S), (170, 104, 50), (70, 34, 12))
    cmask = Image.new("L", cone.size, 0)
    ImageDraw.Draw(cmask).ellipse((0, 0, cone.size[0] - 1, cone.size[1] - 1), fill=255)
    cv.img.paste(cone, (int((cx - ri * .93) * cv.S), int((cy - ri * .93) * cv.S)), cmask)
    cv.d = ImageDraw.Draw(cv.img)
    for k in range(8):
        a = math.radians(k * 45 + 22.5)
        cv.line([(cx + math.cos(a) * ri * .2, cy + math.sin(a) * ri * .2),
                 (cx + math.cos(a) * ri * .7, cy + math.sin(a) * ri * .7)], fill=(120, 70, 26), width=3)
    for k in range(4):
        a = math.radians(k * 90)
        cv.line([(cx, cy), (cx + math.cos(a) * ri * .42, cy + math.sin(a) * ri * .42)], fill=GOLD, width=8)
        cv.circle(cx + math.cos(a) * ri * .42, cy + math.sin(a) * ri * .42, 9, fill=GOLD)
    cv.circle(cx, cy, ri * .14, fill=GOLD, outline=GOLD_DEEP, width=3)
    # ball in the winning pocket
    by = cy - R * .66
    cv.circle(cx + 2, by + 4, 13, fill=(0, 0, 0, 120))
    cv.circle(cx, by, 12, fill=(244, 244, 248))
    cv.circle(cx - 4, by - 4, 4, fill=WHITE)
    # pointer
    cv.polygon([(cx - 16, cy - R - 18), (cx + 16, cy - R - 18), (cx, cy - R + 8)], fill=GOLD)
    # result panel
    px = 760
    colour = (24, 160, 80) if number == 0 else ((214, 28, 48) if number in red else (30, 28, 36))
    glow_shape(cv, (px + 58, 158, px + 242, 342), 26, lambda g: g.circle(px + 150, 250, 92, fill=colour + (255,)))
    cv.circle(px + 150, 250, 88, fill=colour, outline=GOLD, width=4)
    cv.text((px + 150, 252), str(number), "bold", 82, fill=WHITE)
    name = "GREEN" if number == 0 else ("RED" if number in red else "BLACK")
    tags = [name] if number == 0 else [name, "EVEN" if number % 2 == 0 else "ODD",
                                       "LOW 1-18" if number <= 18 else "HIGH 19-36"]
    cv.text((px + 150, 372), "  •  ".join(tags), "bold", 19, fill=(230, 222, 245))
    cv.rrect((px, 418, px + 300, 560), 18, fill=(12, 5, 20, 230), outline=GOLD_DEEP, width=2)
    cv.text((px + 150, 446), f"YOUR BET  •  {space.upper()}", "medium", 16, fill=(195, 182, 222))
    cv.text((px + 150, 478), f"{bet:,} CHIPS", "bold", 22, fill=WHITE)
    if won > bet:
        neon_text(cv, (px + 150, 526), f"WIN  +{won - bet:,}", WIN, 28, glow=8)
    else:
        neon_text(cv, (px + 150, 526), f"LOSE  -{bet:,}", LOSS, 26, glow=6)
    return cv.png()


# ── coin flip ──────────────────────────────────────────────────────────────
def coin(cv: Canvas, cx, cy, r, face: str, shadowed: bool = True) -> None:
    if shadowed:
        shadow(cv, (cx - r, cy - r, cx + r, cy + r), r, blur=14, offset=(0, 10), alpha=190)
    disc = radial(int(2 * r * cv.S), int(2 * r * cv.S), (255, 232, 140), (196, 132, 24))
    m = Image.new("L", disc.size, 0)
    ImageDraw.Draw(m).ellipse((0, 0, disc.size[0] - 1, disc.size[1] - 1), fill=255)
    cv.img.paste(disc, (int((cx - r) * cv.S), int((cy - r) * cv.S)), m)
    cv.d = ImageDraw.Draw(cv.img)
    for i in range(90):  # reeded edge
        a = math.radians(i * 4)
        cv.line([(cx + math.cos(a) * r * .9, cy + math.sin(a) * r * .9),
                 (cx + math.cos(a) * r * .99, cy + math.sin(a) * r * .99)], fill=(170, 112, 20), width=1.6)
    cv.circle(cx, cy, r * .86, outline=(150, 98, 14), width=r * .025)
    cv.circle(cx, cy, r * .78, outline=(255, 240, 180, 200), width=r * .012)
    for i in range(16):
        a = math.radians(i * 22.5 - 90)
        cv.circle(cx + math.cos(a) * r * .7, cy + math.sin(a) * r * .7, r * .022, fill=(160, 104, 16))
    if face == "heads":
        _crown(cv, cx, cy - r * .28, r * .5, (175, 112, 16), points=5)
        cv.text((cx, cy + r * .17), "S", "bold", r * .58, fill=(150, 92, 10))
        cv.text((cx - r * .01, cy + r * .15), "S", "bold", r * .58, fill=(255, 238, 170))
    else:
        cv.text((cx, cy - r * .02), "7", "bold", r * .62, fill=(150, 92, 10))
        cv.text((cx - r * .01, cy - r * .04), "7", "bold", r * .62, fill=(255, 238, 170))
        cv.text((cx, cy + r * .44), "LUCKY", "bold", r * .13, fill=(150, 92, 10))


def coin_flip(result: str, *, pick: str, bet: int, won: int) -> bytes:
    W, H = 1000, 560
    cv = Canvas(W, H)
    night_sky(cv, seed=3)
    header(cv, "COIN FLIP", "PAYS 1.95×")
    glow_shape(cv, (140, 130, 520, 510), 40, lambda g: g.circle(330, 320, 190, fill=GOLD + (120,)))
    coin(cv, 330, 320, 170, result)
    win = won > 0
    neon_text(cv, (740, 210), result.upper(), GOLD, 64, glow=12)
    cv.text((740, 292), f"YOU CALLED {pick.upper()}", "medium", 20, fill=(205, 192, 230))
    if win:
        neon_text(cv, (740, 372), f"+{won - bet:,}", WIN, 50, glow=10)
        cv.text((740, 428), "CHIPS WON", "bold", 18, fill=(205, 240, 215))
    else:
        neon_text(cv, (740, 372), f"-{bet:,}", LOSS, 50, glow=8)
        cv.text((740, 428), "CHIPS LOST", "bold", 18, fill=(245, 205, 215))
    return cv.png()


# ── hi-lo ──────────────────────────────────────────────────────────────────
def hilo_table(game, *, higher_mult: float, lower_mult: float) -> bytes:
    W, H = 1200, 700
    cv = Canvas(W, H)
    cv.img.alpha_composite(radial(*cv.img.size, (36, 30, 96), (10, 6, 30)))
    header(cv, "HI-LO", "ACES HIGH  •  TIES LOSE")
    history = game.cards[:-1][-7:]
    if history:
        cv.text((40, 112), "PREVIOUS", "bold", 14, fill=(180, 170, 220), anchor="lm")
        for i, c in enumerate(history):
            place_card(cv, c, 40 + i * 52, 130, 74)
    cur = game.current
    cw = 190
    gbox = (W / 2 - cw / 2 - 10, 250 - 10, W / 2 + cw / 2 + 10, 250 + cw * 1.4 + 10)
    gcol = (CYAN if not game.done else (WIN if game.won else LOSS)) + (150,)
    glow_shape(cv, gbox, 22, lambda g: g.rrect(gbox, 24, fill=gcol))
    place_card(cv, cur, W / 2 - cw / 2, 250, cw)
    # multiplier panel
    cv.rrect((W / 2 - 170, 120, W / 2 + 170, 210), 20, fill=(10, 6, 24, 230), outline=GOLD_DEEP, width=2)
    cv.text((W / 2, 145), "MULTIPLIER", "medium", 15, fill=(195, 182, 222))
    neon_text(cv, (W / 2, 182), f"×{game.multiplier:.2f}", GOLD, 34, glow=8)
    if not game.done:
        for x, label, mult, colour in ((230, "HIGHER", higher_mult, WIN), (W - 230, "LOWER", lower_mult, PINK)):
            enabled = mult > 0
            cv.rrect((x - 130, 330, x + 130, 470), 22, fill=(12, 6, 26, 230),
                     outline=(colour if enabled else (90, 90, 110)), width=3)
            arrow_y = 365
            if label == "HIGHER":
                cv.polygon([(x, arrow_y - 18), (x - 20, arrow_y + 8), (x + 20, arrow_y + 8)],
                           fill=colour if enabled else (90, 90, 110))
            else:
                cv.polygon([(x, arrow_y + 18), (x - 20, arrow_y - 8), (x + 20, arrow_y - 8)],
                           fill=colour if enabled else (90, 90, 110))
            cv.text((x, 408), label, "bold", 24, fill=WHITE if enabled else (120, 120, 140))
            cv.text((x, 444), f"×{mult:.2f}" if enabled else "—", "bold", 20,
                    fill=GOLD if enabled else (120, 120, 140))
        cv.rect((0, H - 52, W, H), fill=(6, 4, 12, 210))
        cv.text((W / 2, H - 26), f"BET {game.bet:,}  •  CASH OUT NOW FOR {int(game.bet * game.multiplier):,}",
                "bold", 18, fill=(225, 220, 245))
    else:
        cv.rect((0, H - 64, W, H), fill=(6, 4, 12, 225))
        if game.won:
            neon_text(cv, (W / 2, H - 32), f"CASHED OUT  +{game.payout - game.bet:,}", WIN, 28, glow=8)
        else:
            neon_text(cv, (W / 2, H - 32), f"WRONG CALL  -{game.bet:,}", LOSS, 28, glow=8)
    return cv.png()


# ── the Strip property map ─────────────────────────────────────────────────
def fit(cv: Canvas, text: str, kind: str, max_w: float, size: float, floor: float = 10) -> float:
    while size > floor and cv.text_width(text, kind, size) > max_w:
        size -= 1
    return size


@lru_cache(maxsize=4)
def _facade(w: int, h: int, scale: int) -> Image.Image:
    """A brick shopfront (cached): dark brick gradient with mortar lines."""
    cv = Canvas(w, h, scale)
    cv.img.alpha_composite(gradient(*cv.img.size, (58, 26, 36), (22, 10, 20)))
    for row, y in enumerate(range(0, h, 13)):
        cv.line([(0, y), (w, y)], fill=(0, 0, 0, 55), width=1)
        off = 0 if row % 2 else 16
        for x in range(off, w, 32):
            cv.line([(x, y), (x, y + 13)], fill=(0, 0, 0, 45), width=1)
    mask = Image.new("L", cv.img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, cv.img.width - 1, cv.img.height - 1), radius=14 * scale, fill=255)
    cv.img.putalpha(mask)
    return cv.img


def _storefront(cv: Canvas, x, y, w, h, store: dict) -> None:
    colour = tuple(store["colour"])
    owned = store.get("owner") is not None
    shadow(cv, (x, y, x + w, y + h), 14, blur=8, offset=(0, 6), alpha=170)
    cv.paste(_facade(int(w), int(h), cv.S), x, y)
    # neon sign
    sx0, sy0, sx1, sy1 = x + 14, y + 14, x + w - 14, y + 66
    cv.rrect((sx0, sy0, sx1, sy1), 10, fill=(10, 4, 14, 235))
    glow_shape(cv, (sx0, sy0, sx1, sy1), 6, lambda g: g.rrect((sx0, sy0, sx1, sy1), 10,
                                                              outline=colour + (255,), width=4))
    cv.rrect((sx0, sy0, sx1, sy1), 10, outline=tuple(min(255, c + 70) for c in colour), width=1.6)
    name = store["name"].upper()
    size = fit(cv, name, "bold", sx1 - sx0 - 22, 24)
    lit = colour if owned else tuple(int(c * .75) for c in colour)
    neon_text(cv, ((sx0 + sx1) / 2, (sy0 + sy1) / 2 + 1), name, lit, size, glow=6 if owned else 3)
    # window + door
    wx0, wy0, wx1, wy1 = x + 18, y + 80, x + w - 18, y + h - 74
    if owned:
        cv.img.alpha_composite(gradient(int((wx1 - wx0) * cv.S), int((wy1 - wy0) * cv.S), (255, 206, 128), (168, 82, 40)),
                               tuple(cv.p(wx0, wy0)))
    else:
        cv.rect((wx0, wy0, wx1, wy1), fill=(26, 18, 30))
    cv.rect((wx0, wy0, wx1, wy1), outline=(20, 12, 18), width=3)
    dw = (wx1 - wx0) * .24
    cv.rect(((wx0 + wx1) / 2 - dw / 2, wy0 + 10, (wx0 + wx1) / 2 + dw / 2, wy1), fill=(30, 16, 22) if owned else (14, 8, 16),
            outline=(12, 6, 10), width=2)
    cv.line([(wx0, (wy0 + wy1) / 2), ((wx0 + wx1) / 2 - dw / 2, (wy0 + wy1) / 2)], fill=(20, 12, 18), width=2)
    cv.line([((wx0 + wx1) / 2 + dw / 2, (wy0 + wy1) / 2), (wx1, (wy0 + wy1) / 2)], fill=(20, 12, 18), width=2)
    if not owned:  # a hung FOR SALE placard
        px, py = (wx0 + wx1) / 2, (wy0 + wy1) / 2
        cv.line([(px - 30, wy0), (px - 22, py - 14)], fill=(200, 200, 200), width=1.2)
        cv.line([(px + 30, wy0), (px + 22, py - 14)], fill=(200, 200, 200), width=1.2)
        cv.rrect((px - 46, py - 16, px + 46, py + 14), 4, fill=(232, 36, 54), outline=WHITE, width=1.5)
        cv.text((px, py), "FOR SALE", "bold", 13, fill=WHITE)
    # info strip
    iy = y + h - 60
    cv.rrect((x + 10, iy, x + w - 10, y + h - 10), 10, fill=(8, 4, 12, 220))
    if owned:
        who = store["owner"][:18]
        cv.text((x + 22, iy + 16), who, "bold", fit(cv, who, "bold", w - 120, 15), fill=WHITE, anchor="lm")
        for k in range(5):
            star(cv, x + w - 76 + k * 13, iy + 16, 5.5, GOLD if k < store["level"] else (70, 60, 80))
        cv.text((x + 22, iy + 37), f"+{store['income']:,} / HR", "medium", 13, fill=(160, 240, 190), anchor="lm")
    else:
        cv.text((x + 22, iy + 16), f"{store['price']:,}", "bold", 16, fill=GOLD, anchor="lm")
        cv.text((x + 22, iy + 37), f"EARNS {store['income']:,} / HR", "medium", 12, fill=(200, 190, 220), anchor="lm")


def strip_map(stores: list[dict]) -> bytes:
    """The Strip's storefronts as a neon street: who owns what, and what's for sale."""
    W, H = 1200, 900
    cv = Canvas(W, H)
    night_sky(cv, seed=21)
    header(cv, "PROPERTY", f"{sum(1 for s in stores if s.get('owner'))} / {len(stores)} STOREFRONTS OWNED")
    cols, gap, margin = 4, 16, 22
    tw = (W - 2 * margin - (cols - 1) * gap) / cols
    th = (H - 66 - 2 * 18 - 2 * gap) / 3
    tiles = list(stores) + [None]
    for i, store in enumerate(tiles[:12]):
        x = margin + (i % cols) * (tw + gap)
        y = 66 + 18 + (i // cols) * (th + gap)
        if store is None:  # the Strip's own sign fills the last lot
            glow_shape(cv, (x, y, x + tw, y + th), 18, lambda g: g.rrect((x + 20, y + 20, x + tw - 20, y + th - 20),
                                                                         20, outline=PINK + (200,), width=4))
            cv.rrect((x + 20, y + 20, x + tw - 20, y + th - 20), 20, outline=(255, 150, 205, 220), width=1.8)
            neon_text(cv, (x + tw / 2, y + th / 2 - 26), "THE STRIP", PINK, 38, glow=10)
            for k in range(3):
                star(cv, x + tw / 2 - 30 + k * 30, y + th / 2 + 18, 8, GOLD)
            cv.text((x + tw / 2, y + th / 2 + 52), "OWN A PIECE OF IT", "bold", 14, fill=(225, 214, 245))
            continue
        _storefront(cv, x, y, tw, th, store)
    return cv.png()


# ── players club card (wallet) ─────────────────────────────────────────────
TIERS = [  # (minimum net worth, name, base colour, accent)
    (1_000_000, "BLACK", (18, 16, 22), (220, 220, 230)),
    (250_000, "DIAMOND", (30, 110, 160), (190, 240, 255)),
    (50_000, "PLATINUM", (120, 126, 140), (245, 245, 250)),
    (10_000, "GOLD", (176, 124, 26), (255, 236, 170)),
    (2_000, "SILVER", (130, 136, 150), (240, 242, 248)),
    (0, "BRONZE", (130, 74, 36), (255, 214, 170)),
]


def tier_for(net: int) -> tuple[str, tuple, tuple]:
    for minimum, name, base, accent in TIERS:
        if net >= minimum:
            return name, base, accent
    return TIERS[-1][1:]


def players_card(*, name: str, cash: int, chips: int, bank: int, rank: int | None, member_no: int,
                 avatar: bytes | None = None, streak: int = 0) -> bytes:
    W, H = 1000, 560
    cv = Canvas(W, H)
    net = cash + chips + bank
    tier, base, accent = tier_for(net)
    cx0, cy0, cx1, cy1 = 40, 30, W - 40, H - 30
    shadow(cv, (cx0, cy0, cx1, cy1), 34, blur=16, offset=(0, 12), alpha=170)
    dark = tuple(max(0, int(c * .45)) for c in base)
    card = gradient(int((cx1 - cx0) * cv.S), int((cy1 - cy0) * cv.S), base, dark)
    sheen = Image.new("RGBA", card.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    for k in range(0, card.size[0] + card.size[1], 6 * cv.S):
        sd.line([(k, 0), (k - card.size[1], card.size[1])], fill=(255, 255, 255, 10), width=2 * cv.S)
    sd.polygon([(card.size[0] * .55, 0), (card.size[0] * .78, 0), (card.size[0] * .45, card.size[1]),
                (card.size[0] * .22, card.size[1])], fill=(255, 255, 255, 26))
    card.alpha_composite(sheen)
    m = Image.new("L", card.size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, card.size[0] - 1, card.size[1] - 1), radius=34 * cv.S, fill=255)
    cv.img.paste(card, tuple(cv.p(cx0, cy0)), m)
    cv.d = ImageDraw.Draw(cv.img)
    cv.rrect((cx0, cy0, cx1, cy1), 34, outline=accent + (140,), width=2)
    # brand + tier
    neon_text(cv, (cx0 + 44, cy0 + 56), "THE STRIP", PINK, 34, anchor="lm", glow=8)
    cv.text((cx0 + 46, cy0 + 96), "PLAYERS CLUB", "bold", 16, fill=accent, anchor="lm")
    cv.text((cx1 - 44, cy0 + 56), tier, "bold", 30, fill=accent, anchor="rm")
    cv.text((cx1 - 44, cy0 + 92), "MEMBER", "medium", 14, fill=accent + (200,), anchor="rm")
    # EMV chip
    ex, ey = cx0 + 52, cy0 + 150
    cv.rrect((ex, ey, ex + 78, ey + 60), 10, fill=(230, 190, 90), outline=(150, 110, 40), width=1.5)
    for k in (1, 2):
        cv.line([(ex, ey + 20 * k), (ex + 78, ey + 20 * k)], fill=(170, 125, 45), width=1.4)
    cv.line([(ex + 39, ey), (ex + 39, ey + 60)], fill=(170, 125, 45), width=1.4)
    # avatar
    ax, ay, ar = cx1 - 110, cy0 + 182, 58
    cv.circle(ax, ay, ar + 4, fill=accent)
    if avatar:
        try:
            im = Image.open(io.BytesIO(avatar)).convert("RGBA").resize((int(ar * 2 * cv.S),) * 2, Image.LANCZOS)
            am = Image.new("L", im.size, 0)
            ImageDraw.Draw(am).ellipse((0, 0, im.size[0] - 1, im.size[1] - 1), fill=255)
            cv.img.paste(im, (int((ax - ar) * cv.S), int((ay - ar) * cv.S)), am)
            cv.d = ImageDraw.Draw(cv.img)
        except Exception:
            avatar = None
    if not avatar:
        cv.circle(ax, ay, ar, fill=dark)
        cv.text((ax, ay + 2), (name[:1] or "?").upper(), "bold", 52, fill=accent)
    # net worth
    cv.text((cx0 + 52, cy0 + 262), "NET WORTH", "medium", 15, fill=accent + (210,), anchor="lm")
    cv.text((cx0 + 50, cy0 + 308), f"{net:,}", "bold", 54, fill=WHITE, anchor="lm")
    # balances row with drawn icons
    row_y = cy0 + 388
    cols = [(cx0 + 52, "CASH", cash, "cash"), (cx0 + 300, "CHIPS", chips, "chip"), (cx0 + 548, "BANK", bank, "bank")]
    for x, label, value, icon in cols:
        _mini_icon(cv, icon, x + 16, row_y + 18, 34)
        cv.text((x + 44, row_y + 6), label, "medium", 13, fill=accent + (210,), anchor="lm")
        cv.text((x + 44, row_y + 32), f"{value:,}", "bold", 24, fill=WHITE, anchor="lm")
    # embossed name + member number
    digits = f"{member_no % 10**16:016d}"
    groups = " ".join(digits[i:i + 4] for i in range(0, 16, 4))
    cv.text((cx0 + 52, cy1 - 42), name.upper()[:24], "bold", 22, fill=(255, 255, 255, 235), anchor="lm")
    cv.text((cx1 - 44, cy1 - 42), groups, "medium", 17, fill=accent + (200,), anchor="rm")
    extra = []
    if rank:
        extra.append(f"#{rank} ON THE STRIP")
    if streak:
        extra.append(f"{streak}-DAY STREAK")
    if extra:
        cv.text((cx1 - 44, cy0 + 300), "  •  ".join(extra), "bold", 15, fill=accent, anchor="rm")
    return cv.png()


def _mini_icon(cv: Canvas, kind: str, cx, cy, s) -> None:
    if kind == "cash":
        for k in (2, 1, 0):
            ox = k * s * .06
            cv.rrect((cx - s * .5 + ox, cy - s * .3 - ox, cx + s * .5 + ox, cy + s * .3 - ox), s * .06,
                     fill=(40, 150, 80) if k else (64, 190, 104), outline=(20, 90, 40), width=1)
        cv.circle(cx, cy, s * .16, outline=(220, 255, 225), width=1.5)
        cv.text((cx, cy + s * .01), "$", "bold", s * .3, fill=(230, 255, 235))
    elif kind == "chip":
        chip_face(cv, cx, cy, s * .5, (120, 40, 190), (255, 255, 255))
        cv.text((cx, cy + 1), "S", "bold", s * .3, fill=GOLD)
    elif kind == "bank":
        cv.circle(cx, cy, s * .5, fill=(140, 146, 160), outline=(70, 74, 84), width=2)
        cv.circle(cx, cy, s * .34, fill=(100, 104, 116))
        for k in range(6):
            a = math.radians(k * 60)
            cv.line([(cx, cy), (cx + math.cos(a) * s * .3, cy + math.sin(a) * s * .3)], fill=(220, 224, 232),
                    width=2)
        cv.circle(cx, cy, s * .1, fill=(220, 224, 232))


# ── custom emoji set ───────────────────────────────────────────────────────
EMOJI_NAMES = ("cash", "chip", "bank", "coin", "cards", "slots", "roulette", "crown", "dice")


def emoji_icon(kind: str) -> bytes:
    """A 128x128 transparent PNG for one of SYKE's custom economy emojis."""
    cv = Canvas(128, 128, scale=4)
    c = 64
    if kind == "cash":
        for k in (2, 1, 0):
            o = k * 7
            cv.rrect((14 + o, 38 - o, 106 + o, 92 - o), 8, fill=(36, 140, 74) if k else (70, 196, 110),
                     outline=(16, 84, 40), width=2.5)
        cv.rrect((22, 45, 98, 85), 5, outline=(200, 250, 210), width=2)
        cv.circle(60, 65, 15, fill=(46, 160, 84), outline=(220, 255, 228), width=2.5)
        cv.text((60, 66), "$", "bold", 22, fill=(235, 255, 238))
    elif kind == "chip":
        chip_face(cv, c, c, 56, (120, 40, 200), (255, 255, 255))
        cv.circle(c, c, 30, fill=(70, 16, 120))
        cv.text((c, c + 1), "S", "bold", 34, fill=GOLD)
    elif kind == "bank":
        cv.rrect((10, 10, 118, 118), 16, fill=(110, 116, 130), outline=(60, 64, 74), width=3)
        cv.circle(c, c, 42, fill=(150, 156, 170), outline=(70, 74, 84), width=4)
        cv.circle(c, c, 28, fill=(96, 100, 112))
        for k in range(6):
            a = math.radians(k * 60 + 30)
            cv.line([(c, c), (c + math.cos(a) * 36, c + math.sin(a) * 36)], fill=(230, 232, 240), width=5)
            cv.circle(c + math.cos(a) * 36, c + math.sin(a) * 36, 5, fill=(230, 232, 240))
        cv.circle(c, c, 9, fill=GOLD)
        for y in (30, 98):
            cv.rect((112, y - 8, 122, y + 8), fill=(70, 74, 84))
    elif kind == "coin":
        coin(cv, c, c, 56, "heads", shadowed=False)
    elif kind == "cards":
        back = card_image(None, 62, 4).rotate(14, resample=Image.BICUBIC, expand=True)
        cv.img.alpha_composite(back, (14 * 4, 10 * 4))
        front = card_image(Card("A", "S"), 62, 4).rotate(-10, resample=Image.BICUBIC, expand=True)
        cv.img.alpha_composite(front, (44 * 4, 18 * 4))
    elif kind == "slots":
        cv.rrect((14, 16, 114, 118), 14, fill=(200, 30, 70), outline=GOLD, width=4)
        cv.rrect((24, 28, 104, 50), 6, fill=(30, 8, 40))
        cv.text((c, 39), "STRIP", "bold", 14, fill=PINK)
        cv.rrect((24, 58, 104, 94), 6, fill=CREAM)
        for k in range(3):
            cv.text((37 + k * 27, 77), "7", "bold", 30, fill=(220, 20, 50), stroke=1.2, stroke_fill=GOLD_DEEP)
        cv.line([(118, 40), (122, 72)], fill=(160, 160, 170), width=4)
        cv.circle(120, 34, 8, fill=(220, 30, 50))
    elif kind == "roulette":
        cv.circle(c, c, 58, fill=(110, 60, 24))
        for i in range(18):
            cv.pieslice((14, 14, 114, 114), i * 20, i * 20 + 20, fill=(200, 22, 40) if i % 2 else (22, 20, 26))
        cv.pieslice((14, 14, 114, 114), -100, -80, fill=(24, 140, 70))
        cv.circle(c, c, 30, fill=(150, 90, 40))
        for k in range(4):
            a = math.radians(k * 90 + 45)
            cv.line([(c, c), (c + math.cos(a) * 22, c + math.sin(a) * 22)], fill=GOLD, width=5)
        cv.circle(c, c, 8, fill=GOLD)
        cv.circle(c + 30, c - 32, 7, fill=WHITE)
    elif kind == "crown":
        _crown(cv, c, c + 4, 104, GOLD, points=5)
        for k, col in enumerate((PINK, CYAN, PINK)):
            cv.circle(c - 30 + k * 30, c + 18, 7, fill=col)
    elif kind == "dice":
        for ox, oy, rot in ((10, 30, -12), (50, 18, 14)):
            die = Canvas(72, 72, 4)
            die.rrect((4, 4, 68, 68), 14, fill=CREAM, outline=(180, 170, 160), width=2)
            pips = {(-12): [(22, 22), (50, 50), (36, 36)], 14: [(22, 22), (50, 22), (22, 50), (50, 50), (36, 36)]}
            for px, py in pips[rot]:
                die.circle(px, py, 6, fill=CARD_RED if rot > 0 else INK)
            im = die.img.rotate(rot, resample=Image.BICUBIC, expand=True)
            cv.img.alpha_composite(im, (ox * 4, oy * 4))
    else:
        raise ValueError(f"unknown emoji {kind!r}")
    return cv.png()


@lru_cache(maxsize=16)
def emoji_data_uri(kind: str) -> str:
    """The emoji icon as a data: URI, for the web dashboard."""
    import base64
    return "data:image/png;base64," + base64.b64encode(emoji_icon(kind)).decode()
