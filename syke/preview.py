"""Render a SYKE report from a fake server, no Discord needed.

    python -m syke.preview            # judge the default demo member
    python -m syke.preview --user 3   # judge someone else
    python -m syke.preview --ai       # use your OPENAI/ANTHROPIC key from .env
"""

from __future__ import annotations

import argparse
import asyncio
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .ai import write_roast
from .config import load_settings
from .models import Msg
from .profile import analyze_server, build_profile
from .render import render_leaderboard, render_report

PERSONAS: dict[int, tuple[str, list[int], list[str]]] = {
    1: ("Zyro", [22, 23, 0, 1, 2], [
        "bro really thought he could solo the raid 💀💀",
        "LMAOOO NO WAY",
        "who let bro cook 😭😭",
        "nah this is actually so funny im crying",
        "ASDFGHJKL I CANT",
        "stfu you're so stupid 😭",
        "bro really thought...",
        "WHY IS EVERYONE AWAKE AT 3AM",
        "ok but hear me out 😏",
        "she's lowkey freaky ngl 😏😏",
        "nah bro is down bad 💀",
        "LMAO ratio + skill issue",
        "who wants to hop on valo",
        "bro really said that with his whole chest 💀",
        "im going to commit a crime??!!",
        "that's actually so dumb lmao",
        "WAIT WHAT",
        "no because why is this so real 😭",
        "chat is this real",
        "bro really thought he was him",
        "touch grass challenge (impossible)",
        "this server is a zoo 💀",
        "go to sleep? never heard of her",
        "imagine being this bad at the game lmao",
    ]),
    2: ("Mira", [9, 10, 11, 14, 15], [
        "Honestly I think the patch notes make sense if you consider the balance data.",
        "I disagree, the evidence actually points the other way because of the drop rates.",
        "Does anyone have a source for that? I'd like to understand the context.",
        "Good morning everyone.",
        "That's a fair point, although I think it's probably more nuanced.",
        "I read the whole thread and I believe we're arguing about different things.",
        "Technically the meta shifted because of the item changes, not the hero buffs.",
        "Can we keep the debate civil please?",
    ]),
    3: ("bubbles", [16, 17, 18, 19, 20], [
        "uwu hiii bestie :3",
        "rawr x3 nuzzles",
        "omg slay periodt ✨✨💅💖",
        "that's so sus amogus ඞ",
        "teehee >w< smol bean",
        "skibidi rizz ohio 💀",
        "mood fr fr",
        "heyyyyy guysss",
        "nom nom 🍪🍪🍪🍪",
    ]),
    4: ("Dex", [12, 13, 19, 20, 21], [
        "gg", "ok", "yeah", "true", "lol", "same", "nice", "k", "sure", "maybe tomorrow",
        "who's on", "brb",
    ]),
    5: ("Vex", [20, 21, 22, 23, 0], [
        "you're actually the worst player i've ever seen",
        "shut up nobody asked 🙄",
        "cope harder",
        "this is garbage game design",
        "how are you this bad wtf",
        "uninstall bozo 🤡",
        "lol",
        "ok that was kinda funny",
    ]),
}


def build_demo_server(seed: int = 7) -> list[Msg]:
    rng = random.Random(seed)
    start = datetime(2026, 5, 3, tzinfo=timezone.utc)
    messages: list[Msg] = []
    for user_id, (name, hours, lines) in PERSONAS.items():
        volume = rng.randint(140, 420) if user_id != 1 else 2847
        for _ in range(volume):
            day = start + timedelta(days=rng.randint(0, 150))
            hour = rng.choice(hours)
            when = day.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59))
            text = rng.choice(lines)
            laughs = rng.choice([0, 0, 0, 1, 2, 5]) if user_id in (1, 5) else rng.choice([0, 0, 1])
            messages.append(Msg(user_id, name, text, when, channel_id=rng.randint(1, 3), laugh_reactions=laughs))
    return messages


async def run(user_id: int, use_ai: bool) -> None:
    settings = load_settings()
    if not use_ai:
        settings = replace(settings, ai_provider="none")

    server = analyze_server(build_demo_server(), timezone.utc, settings.min_messages)
    profile = await write_roast(build_profile(server, user_id), settings)

    print(f"🧠 SYKE REPORT — {profile.name}\n")
    print(render_report(profile))
    print("\n\n🔥 /syke leaderboard trait:chaotic\n")
    print(render_leaderboard(server, "chaotic"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview a SYKE report with fake data.")
    parser.add_argument("--user", type=int, default=1, choices=sorted(PERSONAS))
    parser.add_argument("--ai", action="store_true", help="use the AI provider configured in .env")
    args = parser.parse_args()
    asyncio.run(run(args.user, args.ai))


if __name__ == "__main__":
    main()
