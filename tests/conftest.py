import asyncio
import inspect
import types
from dataclasses import replace

import pytest

from syke.bot import Syke, SykeCommands
from syke.config import load_settings


def pytest_pyfunc_call(pyfuncitem):
    """Run `async def` tests on a fresh event loop, so no pytest-asyncio plugin is needed."""
    func = pyfuncitem.obj
    if inspect.iscoroutinefunction(func):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(func(**kwargs))
        return True
    return None


@pytest.fixture()
def cog(tmp_path):
    settings = replace(load_settings(), db_path=str(tmp_path / "t.db"), ai_provider="none")
    bot = Syke(settings)
    bot._connection.user = types.SimpleNamespace(id=999, mention="<@999>")
    return SykeCommands(bot)
