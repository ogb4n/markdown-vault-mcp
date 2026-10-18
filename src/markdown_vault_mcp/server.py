import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any
from mcp.server import Server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
    EmbeddedResource,
)

from . import tools

# Load environment variables

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("markdown-vault-mcp")

app = Server("markdown-vault-mcp")

@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""

    return tools.list_tools()

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Handle tool calls for command line run."""
    
    if not isinstance(arguments, dict):
        raise RuntimeError("arguments must be dictionary")


    try:
        # Off the event loop: a tool reads files, and on a network mount one search can take minutes,
        # during which a blocked loop answers nothing (not even /healthz or a ping).
        return await asyncio.to_thread(tools.call_tool, name, arguments)
    except Exception as e:
        logger.error(str(e))
        raise RuntimeError(f"Caught Exception. Error: {str(e)}")


async def main():

    # Import here to avoid issues with event loops
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )
