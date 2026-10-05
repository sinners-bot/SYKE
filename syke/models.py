from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Msg:
    """A platform-agnostic chat message, so analysis never touches discord.py objects."""

    author_id: int
    author_name: str
    content: str
    created_at: datetime  # timezone-aware
    channel_id: int = 0
    laugh_reactions: int = 0
    total_reactions: int = 0
