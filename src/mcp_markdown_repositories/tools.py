"""MCP boundary for OneDrive-backed Markdown repositories."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import lru_cache
import json
import os
from typing import Any

from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool

from .repository import GraphClient, GraphMarkdownRepository, OneDriveTokenProvider, RepositoryError


ToolResult = Sequence[TextContent | ImageContent | EmbeddedResource]
Handler = Callable[[dict[str, Any]], ToolResult]


class RepositoryRegistry:
    def __init__(self, roots: dict[str, str], default: str | None) -> None:
        self._roots = roots
        self._default = default
        self._repositories: dict[str, GraphMarkdownRepository] = {}

    def get(self, requested: Any) -> GraphMarkdownRepository:
        if requested is None:
            if self._default:
                name = self._default
            elif len(self._roots) == 1:
                name = next(iter(self._roots))
            else:
                raise RepositoryError("repository is required when several repositories are configured")
        elif isinstance(requested, str):
            name = requested
        else:
            raise RepositoryError("repository must be a string")
        if name not in self._roots:
            raise RepositoryError(f"Unknown repository '{name}'")
        if name not in self._repositories:
            self._repositories[name] = GraphMarkdownRepository(
                name, self._roots[name], GraphClient(OneDriveTokenProvider())
            )
        return self._repositories[name]

    def names(self) -> list[str]:
        return sorted(self._roots)


@lru_cache(maxsize=1)
def registry() -> RepositoryRegistry:
    raw = os.getenv("MARKDOWN_REPOSITORIES")
    if not raw:
        raise RepositoryError(
            "MARKDOWN_REPOSITORIES is required, for example "
            '{"vault":"Documents/ClaudeVault"}'
        )
    try:
        roots = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RepositoryError("MARKDOWN_REPOSITORIES must contain a JSON object") from error
    if not isinstance(roots, dict) or not roots or not all(
        isinstance(name, str) and isinstance(path, str) for name, path in roots.items()
    ):
        raise RepositoryError("MARKDOWN_REPOSITORIES must map repository names to OneDrive paths")
    default = os.getenv("MARKDOWN_DEFAULT_REPOSITORY")
    if default and default not in roots:
        raise RepositoryError("MARKDOWN_DEFAULT_REPOSITORY is not configured in MARKDOWN_REPOSITORIES")
    return RepositoryRegistry(roots, default)


def _text(value: Any) -> ToolResult:
    return [TextContent(type="text", text=json.dumps(value, ensure_ascii=False, indent=2))]


def _message(message: str) -> ToolResult:
    return [TextContent(type="text", text=message)]


def _repository(args: dict[str, Any]) -> GraphMarkdownRepository:
    return registry().get(args.get("repository"))


def _require(args: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if name not in args or not isinstance(args[name], str)]
    if missing:
        raise RepositoryError(f"{', '.join(missing)} argument missing or invalid")


def _integer(args: dict[str, Any], name: str, default: int, minimum: int = 1) -> int:
    value = args.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RepositoryError(f"{name} must be an integer greater than or equal to {minimum}")
    return value


def _repository_schema() -> dict[str, Any]:
    return {
        "repository": {
            "type": "string",
            "description": "Configured repository name. Optional only when one repository is configured.",
        }
    }


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str], handler: Handler) -> tuple[Tool, Handler]:
    return (
        Tool(
            name=name,
            description=description,
            inputSchema={"type": "object", "properties": properties, "required": required},
        ),
        handler,
    )


def list_repositories(args: dict[str, Any]) -> ToolResult:
    _ = args
    return _text(registry().names())


def list_files(args: dict[str, Any]) -> ToolResult:
    return _text([entry.__dict__ for entry in _repository(args).list_files()])


def list_directory(args: dict[str, Any]) -> ToolResult:
    path = args.get("path", "")
    if not isinstance(path, str):
        raise RepositoryError("path must be a string")
    return _text([entry.__dict__ for entry in _repository(args).list_directory(path)])


def get_file(args: dict[str, Any]) -> ToolResult:
    _require(args, "path")
    return _text(_repository(args).read(args["path"]).content)


def get_files(args: dict[str, Any]) -> ToolResult:
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
        raise RepositoryError("paths must be a non-empty list of strings")
    repository = _repository(args)
    return _text({path: repository.read(path).content for path in paths})


def search(args: dict[str, Any]) -> ToolResult:
    _require(args, "query")
    return _text(_repository(args).search(args["query"], _integer(args, "context_length", 100, 0)))


def append(args: dict[str, Any]) -> ToolResult:
    _require(args, "path", "content")
    _repository(args).append(args["path"], args["content"])
    return _message(f"Successfully appended content to {args['path']}")


def write(args: dict[str, Any]) -> ToolResult:
    _require(args, "path", "content")
    _repository(args).write(args["path"], args["content"])
    return _message(f"Successfully wrote {args['path']}")


def patch(args: dict[str, Any]) -> ToolResult:
    _require(args, "path", "operation", "target_type", "target", "content")
    _repository(args).patch(
        args["path"], args["operation"], args["target_type"], args["target"], args["content"]
    )
    return _message(f"Successfully patched {args['path']}")


def delete(args: dict[str, Any]) -> ToolResult:
    _require(args, "path")
    if args.get("confirm") is not True:
        raise RepositoryError("confirm must be true to delete a file")
    _repository(args).delete(args["path"])
    return _message(f"Successfully deleted {args['path']}")


def search_by_tag(args: dict[str, Any]) -> ToolResult:
    _require(args, "tag")
    directory = args.get("directory")
    if directory is not None and not isinstance(directory, str):
        raise RepositoryError("directory must be a string")
    return _text(_repository(args).search_by_tag(args["tag"], directory))


def get_frontmatter(args: dict[str, Any]) -> ToolResult:
    _require(args, "path")
    return _text(_repository(args).get_frontmatter(args["path"]))


def recent_changes(args: dict[str, Any]) -> ToolResult:
    return _text(_repository(args).recent_changes(_integer(args, "limit", 10), _integer(args, "days", 90)))


def _with_repository(properties: dict[str, Any]) -> dict[str, Any]:
    return _repository_schema() | properties


_PATH = {"type": "string", "format": "path", "description": "Path relative to the configured repository root."}
_CONTENT = {"type": "string", "description": "UTF-8 Markdown content."}


_DEFINITIONS = [
    _tool("markdown_list_repositories", "List configured Markdown repositories.", {}, [], list_repositories),
    _tool("markdown_list_files", "Recursively list Markdown files and directories in a repository.", _with_repository({}), [], list_files),
    _tool("markdown_list_directory", "List Markdown files and directories directly inside a repository path.", _with_repository({"path": _PATH}), [], list_directory),
    _tool("markdown_get_file", "Read a Markdown file.", _with_repository({"path": _PATH}), ["path"], get_file),
    _tool("markdown_get_files", "Read several Markdown files in one call.", _with_repository({"paths": {"type": "array", "items": _PATH}}), ["paths"], get_files),
    _tool("markdown_search", "Search text across every Markdown file. Searches are case-insensitive.", _with_repository({"query": {"type": "string"}, "context_length": {"type": "integer", "default": 100}}), ["query"], search),
    _tool("markdown_append", "Append Markdown content, creating the file when it does not exist.", _with_repository({"path": _PATH, "content": _CONTENT}), ["path", "content"], append),
    _tool("markdown_write", "Create or replace a Markdown file. This overwrites existing content.", _with_repository({"path": _PATH, "content": _CONTENT}), ["path", "content"], write),
    _tool("markdown_patch", "Patch a heading, block reference, or a top-level frontmatter field in a Markdown file.", _with_repository({"path": _PATH, "operation": {"type": "string", "enum": ["append", "prepend", "replace", "delete"]}, "target_type": {"type": "string", "enum": ["heading", "block", "frontmatter"]}, "target": {"type": "string"}, "content": _CONTENT}), ["path", "operation", "target_type", "target", "content"], patch),
    _tool("markdown_delete", "Delete a Markdown file. The caller must explicitly confirm the irreversible operation.", _with_repository({"path": _PATH, "confirm": {"type": "boolean"}}), ["path", "confirm"], delete),
    _tool("markdown_search_by_tag", "Find Markdown files with an exact inline or frontmatter tag.", _with_repository({"tag": {"type": "string"}, "directory": _PATH}), ["tag"], search_by_tag),
    _tool("markdown_get_frontmatter", "Read simple YAML frontmatter from a Markdown file.", _with_repository({"path": _PATH}), ["path"], get_frontmatter),
    _tool("markdown_recent_changes", "List recently modified Markdown files.", _with_repository({"limit": {"type": "integer", "default": 10}, "days": {"type": "integer", "default": 90}}), [], recent_changes),
]

TOOL_DEFINITIONS = {tool.name: (tool, handler) for tool, handler in _DEFINITIONS}


def list_tools() -> list[Tool]:
    return [tool for tool, _ in TOOL_DEFINITIONS.values()]


def call_tool(name: str, arguments: dict[str, Any]) -> ToolResult:
    try:
        return TOOL_DEFINITIONS[name][1](arguments)
    except KeyError as error:
        raise ValueError(f"Unknown tool: {name}") from error
