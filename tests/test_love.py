"""!marry / !divorce / !partner / !gift: money taken only on a yes, refunded when the game says no."""

import types
from dataclasses import replace

from syke import economy as econ
from syke import love
from syke.bot import Syke
from syke.config import load_settings
from test_economy import FakeAuthor, FakeCtx

GID = 42


class FakeGame:
    """Stands in for THE STRIP's /bot/api with the same answers (see hometown server/bot_api.js)."""

    def __init__(self):
        self.calls, self.down = [], False
        self.married: dict[str, str] = {}
        self.rings: dict[str, dict] = {}
        self.pockets: dict[str, list] = {}
        self.refuse_marry = False

    async def call(self, op, **b):
        self.calls.append((op, b))
        if self.down:
            return dict(love.OFFLINE)
        a, t = b.get("user_id"), b.get("to")
        if op == "catalog":
            return {"ok": True, "rings": love.RINGS, "gifts": love.GIFTS}
        if op == "check":
            if a in self.married and self.married[a] != t:
                return {"ok": False, "error": "taken", "message": "You're married to someone else."}
            if self.married.get(a) == t:
                return {"ok": False, "error": "already", "message": "You're already married to each other."}
            return {"ok": True, "rings": self.rings.get(a, {})}
        if op == "marry":
            if self.refuse_marry:
                return {"ok": False, "error": "taken_them", "message": "They're already with someone."}
            if b.get("use_owned"):
                self.rings[a].pop(b["ring"])
            self.married[a], self.married[t] = t, a
            return {"ok": True, "status": "married", "partner": {"id": t}}
        if op == "status":
            p = self.married.get(a)
            return {"ok": True, "hidden": False, "personal": "Married" if p else "Single",
                    "partner": {"id": p, "name": "x", "online": False} if p else None, "status": "married" if p else None,
                    "label": "Married", "married_at": 1_700_000_000 if p else None}
        if op == "divorce":
            p = self.married.pop(a, None)
            if not p:
                return {"ok": False, "error": "single", "message": "You're not with anyone."}
            self.married.pop(p, None)
            return {"ok": True, "kind": "divorce", "with": {"id": p}}
        if op == "gift":
            self.pockets.setdefault(t, []).append(b["gift"])
            return {"ok": True, "gift": b["gift"]}
        raise AssertionError(op)


class Member(FakeAuthor):
    bot = False


async def setup(tmp_path, game=None):
    settings = replace(load_settings(), db_path=str(tmp_path / "l.db"), ai_provider="none", web_enabled=False)
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>", name="SYKE")
    eco = econ.Economy(bot)
    await bot.add_cog(eco)
    game = game or FakeGame()
    cog = love.Love(bot, game=game)
    await bot.add_cog(cog)
    return cog, game, bot.storage


def cash(db, uid):
    return db.econ_wallet(GID, uid)["cash"]


class Press:
    """Just enough of an interaction for the proposal buttons."""

    def __init__(self, uid):
        self.user = types.SimpleNamespace(id=uid)
        self.edited, self.private = [], []
        self.response = types.SimpleNamespace(defer=self._defer, send_message=self._send, edit_message=self._edit)

    async def _defer(self):
        pass

    async def _send(self, text, **kw):
        self.private.append(text)

    async def _edit(self, **kw):
        self.edited.append(kw)

    async def edit_original_response(self, **kw):
        self.edited.append(kw)


def view_of(ctx):
    return ctx.sent[-1].kwargs["view"]


def test_lookups():
    assert love.find_ring(None) == "paper" and love.find_ring("") == "paper"
    assert love.find_ring("gold") == "gold" and love.find_ring("Gold band") == "gold"
    assert love.find_ring("silver") == "promise" and love.find_ring("diamond ring") == "diamond"
    assert love.find_ring("bottle cap") == "paper" and love.find_ring("yacht") is None
    assert love.find_gift("roses") == "roses" and love.find_gift("rose") == "roses"
    assert love.find_gift("chocolate") == "candy" and love.find_gift("bear") == "teddy"
    assert love.find_gift("") is None and love.find_gift("car") is None


async def test_free_ring_proposal_and_yes(tmp_path):
    cog, game, db = await setup(tmp_path)
    ctx, bo = FakeCtx(uid=1, gid=GID), Member(2, "Bo")
    await cog.marry.callback(cog, ctx, bo)
    assert "proposal" in ctx.replies[-1].title.lower()
    press = Press(2)
    await view_of(ctx).yes.callback(press)
    assert "married" in press.edited[-1]["embed"].title.lower()
    assert game.married == {"1": "2", "2": "1"}


async def test_paid_ring_charged_only_on_yes(tmp_path):
    cog, game, db = await setup(tmp_path)
    db.econ_touch(GID, 1, 500)
    db.econ_adjust(GID, 1, cash=3000)
    before = cash(db, 1)
    ctx, bo = FakeCtx(uid=1, gid=GID), Member(2, "Bo")
    await cog.marry.callback(cog, ctx, bo, ring="gold")
    assert cash(db, 1) == before                      # asking is free
    other = Press(3)
    await view_of(ctx).yes.callback(other)            # a stranger can't accept
    assert other.private and not game.married
    await view_of(ctx).yes.callback(Press(2))
    assert cash(db, 1) == before - 2500 and game.married["1"] == "2"


async def test_refund_when_the_game_says_no(tmp_path):
    cog, game, db = await setup(tmp_path)
    db.econ_touch(GID, 1, 500)
    db.econ_adjust(GID, 1, cash=3000)
    before = cash(db, 1)
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.marry.callback(cog, ctx, Member(2, "Bo"), ring="gold")
    game.refuse_marry = True
    press = Press(2)
    await view_of(ctx).yes.callback(press)
    assert cash(db, 1) == before and "off" in press.edited[-1]["embed"].title.lower()


async def test_owned_ring_is_used_not_bought(tmp_path):
    cog, game, db = await setup(tmp_path)
    game.rings["1"] = {"diamond": 1}
    db.econ_touch(GID, 1, 500)
    before = cash(db, 1)
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.marry.callback(cog, ctx, Member(2, "Bo"), ring="diamond")   # 15,000 they don't have: fine, they own one
    await view_of(ctx).yes.callback(Press(2))
    assert game.married["1"] == "2" and cash(db, 1) == before and game.rings["1"] == {}
    assert game.calls[-1][1]["use_owned"] is True


async def test_cant_afford_ring_never_posts_a_proposal(tmp_path):
    cog, game, db = await setup(tmp_path)
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.marry.callback(cog, ctx, Member(2, "Bo"), ring="diamond")
    assert "not enough" in ctx.replies[-1].title.lower() and "view" not in ctx.sent[-1].kwargs


async def test_no_button_and_already_taken(tmp_path):
    cog, game, db = await setup(tmp_path)
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.marry.callback(cog, ctx, Member(2, "Bo"))
    press = Press(2)
    await view_of(ctx).no.callback(press)
    assert not game.married and "not this time" in press.edited[-1]["embed"].title.lower()
    game.married = {"1": "5", "5": "1"}
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.marry.callback(cog, ctx, Member(2, "Bo"))
    assert "can't" in ctx.replies[-1].title.lower()


async def test_divorce_is_one_sided_and_confirmed(tmp_path):
    cog, game, db = await setup(tmp_path)
    game.married = {"1": "2", "2": "1"}
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.divorce.callback(cog, ctx)
    view = view_of(ctx)
    await view.confirm.callback(Press(1))
    assert game.married == {}
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.divorce.callback(cog, ctx)
    assert "nothing to end" in ctx.replies[-1].title.lower()


async def test_partner_card(tmp_path):
    cog, game, db = await setup(tmp_path)
    game.married = {"1": "2", "2": "1"}
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.partner.callback(cog, ctx)
    assert "<@2>" in ctx.replies[-1].description and "Married" in ctx.replies[-1].description
    ctx = FakeCtx(uid=3, gid=GID)
    await cog.partner.callback(cog, ctx)
    assert "Single" in ctx.replies[-1].description


async def test_gifts_charge_and_land_in_the_game(tmp_path):
    cog, game, db = await setup(tmp_path)
    db.econ_touch(GID, 1, 500)
    before = cash(db, 1)
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.gift.callback(cog, ctx, Member(2, "Bo"), gift="roses")
    assert cash(db, 1) == before - 60 and game.pockets["2"] == ["roses"]
    await cog.gift.callback(cog, ctx, Member(2, "Bo"), gift="flower")
    assert cash(db, 1) == before - 60 and game.pockets["2"] == ["roses", "flower"]
    await cog.gift.callback(cog, ctx, None, gift=None)            # no args: the chapel's shelf
    assert "chapel" in ctx.replies[-1].title.lower()


async def test_game_down_costs_nothing(tmp_path):
    cog, game, db = await setup(tmp_path)
    db.econ_touch(GID, 1, 500)
    before = cash(db, 1)
    game.down = True
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.gift.callback(cog, ctx, Member(2, "Bo"), gift="roses")
    assert cash(db, 1) == before and "answering" in ctx.replies[-1].description


async def test_off_without_a_game_url(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "o.db"), ai_provider="none", web_enabled=False,
                       strip_game_url="")
    bot = Syke(settings)
    cog = love.Love(bot)
    ctx = FakeCtx(uid=1, gid=GID)
    await cog.partner.callback(cog, ctx)
    assert "closed" in ctx.replies[-1].title.lower()
