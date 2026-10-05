import types
from dataclasses import replace

import pytest

from syke.bot import Syke, SykeCommands
from syke.config import load_settings


@pytest.fixture()
def cog(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "t.db"), ai_provider="none")
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>")
    return SykeCommands(bot)
