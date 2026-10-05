"""Render a SYKE report from a fake server, no Discord needed.

    python -m syke.preview            # judge the default demo member
    python -m syke.preview --user 3   # judge someone else
    python -m syke.preview --ai       # use your OPENAI/ANTHROPIC key from .env
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import timezone

from .ai import write_roast
from .config import load_settings
from .demo import PERSONAS, build_demo_server
from .profile import analyze_server, build_profile
from .render import render_leaderboard, render_report


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
