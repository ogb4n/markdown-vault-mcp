import asyncio
from unittest.mock import patch

import pytest
from mcp.types import TextContent

from markdown_vault_mcp import server, tools


def run(awaitable):
    return asyncio.run(awaitable)


def test_registered_tools_are_markdown_repository_tools():
    names = {tool.name for tool in tools.list_tools()}
    assert "markdown_list_repositories" in names
    assert "markdown_get_file" in names
    assert "markdown_search" in names


def test_call_tool_rejects_non_dict_arguments():
    with pytest.raises(RuntimeError, match="arguments must be dictionary"):
        run(server.call_tool("markdown_get_file", None))


def test_call_tool_dispatches_to_tools_module():
    expected = [TextContent(type="text", text="ok")]
    with patch("markdown_vault_mcp.server.tools.call_tool", return_value=expected) as call:
        assert run(server.call_tool("markdown_get_file", {"path": "note.md"})) == expected
    call.assert_called_once_with("markdown_get_file", {"path": "note.md"})


def test_call_tool_wraps_repository_errors():
    with patch("markdown_vault_mcp.server.tools.call_tool", side_effect=RuntimeError("bad args")):
        with pytest.raises(RuntimeError, match="Caught Exception. Error: bad args"):
            run(server.call_tool("markdown_get_file", {}))


def test_call_tool_leaves_the_event_loop_free():
    import threading
    import time

    ran_on: list[int] = []

    def slow(name, arguments):
        ran_on.append(threading.get_ident())
        time.sleep(0.3)
        return [TextContent(type="text", text="ok")]

    async def scenario() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(ticker())
        with patch("markdown_vault_mcp.server.tools.call_tool", side_effect=slow):
            await server.call_tool("markdown_get_file", {"path": "note.md"})
        task.cancel()
        return ticks

    assert run(scenario()) >= 5
    assert ran_on and ran_on[0] != threading.get_ident()
