"""Work out which invite someone used when they joined.

Discord never says who invited a member. SYKE keeps the use-count of every invite and, when
someone joins, the count that went up is the one they used.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# How long a deleted invite is still a suspect, in case it was used up as someone joined.
INVITE_GONE_SECONDS = 15
# Give Discord a moment to bump the use count before we read invites again.
INVITE_SETTLE_SECONDS = 1.0


@dataclass(frozen=True)
class InviteUse:
    code: str
    uses: int
    max_uses: int  # 0 means unlimited
    inviter_id: int | None
    inviter_name: str
    vanity: bool = False


def invite_from_discord(invite, *, vanity: bool = False) -> InviteUse:
    inviter = getattr(invite, "inviter", None)
    inviter_id = getattr(inviter, "id", None) if inviter is not None else None
    name = ""
    if inviter is not None:
        name = (
            getattr(inviter, "display_name", None)
            or getattr(inviter, "global_name", None)
            or getattr(inviter, "name", None)
            or ""
        )
    return InviteUse(
        code=str(invite.code),
        uses=int(getattr(invite, "uses", None) or 0),
        max_uses=int(getattr(invite, "max_uses", None) or 0),
        inviter_id=int(inviter_id) if inviter_id is not None else None,
        inviter_name=str(name),
        vanity=vanity,
    )


def remember_invite(cache: dict[str, InviteUse], invite: InviteUse) -> None:
    """Add or refresh an invite without letting a stale event rewind its use count."""
    current = cache.get(invite.code)
    if current is None or invite.uses >= current.uses:
        cache[invite.code] = invite
        return
    cache[invite.code] = replace(
        current,
        inviter_id=current.inviter_id if current.inviter_id is not None else invite.inviter_id,
        inviter_name=current.inviter_name or invite.inviter_name,
        max_uses=invite.max_uses or current.max_uses,
        vanity=current.vanity or invite.vanity,
    )


def settle_invites(before: dict[str, InviteUse], after: list[InviteUse]) -> tuple[InviteUse | None, dict[str, InviteUse]]:
    """The invite this join used, and the cache to keep afterwards.

    One invite gaining several uses only spends one of them, so another join waiting behind
    this one can claim the rest. Two invites growing at once is a tie, and nobody is blamed.
    A single-use invite that vanished (it was used up) counts when nothing else grew.
    """
    live = {inv.code: inv for inv in after}
    grown: list[tuple[int, InviteUse]] = []
    for inv in after:
        prev = before.get(inv.code)
        prev_uses = prev.uses if prev else 0
        delta = inv.uses - prev_uses
        if delta > 0:
            grown.append((delta, inv))

    chosen: InviteUse | None = None
    if len(grown) == 1:
        delta, inv = grown[0]
        chosen = inv
        if delta > 1:
            live[inv.code] = replace(inv, uses=inv.uses - (delta - 1))
    elif not grown:
        missing = [inv for code, inv in before.items() if code not in live and not inv.vanity]
        exhausted = [inv for inv in missing if inv.max_uses > 0 and inv.uses + 1 >= inv.max_uses]
        if len(missing) == 1 and len(exhausted) == 1:
            chosen = exhausted[0]
    return chosen, live


def _when(joined_at: float | None) -> str:
    if not joined_at:
        return ""
    stamp = int(joined_at)
    return f" on <t:{stamp}:F> (<t:{stamp}:R>)"


def join_lines(target_name: str, *, self_lookup: bool, owner: bool, row) -> tuple[str, str, bool]:
    """Title, description, and whether this is a real answer (rather than a shrug).

    `row` is `(inviter_id, inviter_name, code, kind, joined_at)` or None when SYKE never
    saw the join. `kind` is `user`, `vanity`, or `unknown`.
    """
    if row is None:
        if owner and self_lookup:
            body = (
                "You own this server. If you created it, nobody invited you. "
                "SYKE also only remembers people who join while it's here with **Manage Server**."
            )
        elif owner:
            body = (
                f"**{target_name}** owns this server. If they created it, nobody invited them. "
                "SYKE also only remembers people who join while it's here with **Manage Server**."
            )
        elif self_lookup:
            body = (
                "SYKE didn't see you join. It only remembers people who arrive while it's "
                "in the server with **Manage Server**, and the Server Members intent has to be on."
            )
        else:
            body = (
                f"SYKE didn't see **{target_name}** join. It only remembers people who arrive while it's "
                "in the server with **Manage Server**, and the Server Members intent has to be on."
            )
        return "No witnesses 🔬", body, False

    inviter_id, inviter_name, code, kind, joined_at = row
    when = _when(joined_at)
    link = f" `discord.gg/{code}`" if code else ""
    who = "You" if self_lookup else f"**{target_name}**"
    were = "were" if self_lookup else "was"

    if kind == "vanity":
        via = f"the vanity invite{link}" if code else "this server's vanity invite"
        return "Vanity invite 📨", f"{who} came in through {via}{when}. No one person to point at.", True

    if kind == "user" and inviter_id is not None:
        name = inviter_name or "someone"
        via = f" with{link}" if code else ""
        return "Invited by 📨", f"{who} {were} invited by **{name}**{via}{when}.", True

    if code:
        return "Invited by 📨", f"{who} used{link}{when}, but that invite doesn't say who created it.", True

    if self_lookup:
        body = (
            f"You joined{when}, but SYKE couldn't tell which invite did it. "
            "Two people may have walked in at once, or you came in through Server Discovery."
        )
    else:
        body = (
            f"**{target_name}** joined{when}, but SYKE couldn't tell which invite did it. "
            "Two people may have walked in at once, or they used Server Discovery."
        )
    return "No witnesses 🔬", body, False
