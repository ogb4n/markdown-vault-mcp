"""Local Markdown repository.

The module deliberately knows nothing about MCP.  It exposes a small file
repository API over a directory; where that directory comes from (a bind
mount, a synchronised copy) is not its concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any


TAG_PATTERN = re.compile(r"(?<![\w/])#([\w/-]+)")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class RepositoryError(RuntimeError):
    """A repository operation could not be completed."""


class RepositoryNotFoundError(RepositoryError):
    """The requested path does not exist."""


class RepositoryConflictError(RepositoryError):
    """The file changed on disk while it was being edited."""


class RepositoryPathError(RepositoryError):
    """A path escaped, or was invalid for, the configured repository."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    is_directory: bool
    modified_at: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class FileContent:
    content: str
    etag: str | None


class LocalMarkdownRepository:
    """A Markdown-only repository rooted at one local directory."""

    def __init__(self, name: str, root: str | Path) -> None:
        self.name = name
        self._root = Path(root).resolve()
        if not self._root.is_dir():
            raise RepositoryPathError(f"Repository root '{root}' is not a directory")

    def list_directory(self, path: str = "") -> list[FileEntry]:
        directory = self._resolve(_normalise_path(path))
        if not directory.is_dir():
            raise RepositoryNotFoundError(f"'{path}' is not a directory")
        entries = [
            self._entry(child)
            for child in directory.iterdir()
            if not _is_hidden(child.name) and child.exists() and (child.is_dir() or _is_markdown(child.name))
        ]
        return sorted(entries, key=lambda entry: (not entry.is_directory, entry.path.casefold()))

    def list_files(self) -> list[FileEntry]:
        result: list[FileEntry] = []
        for directory, subdirectories, files in os.walk(self._root):
            # Pruning in place keeps .obsidian, .trash and .git out of the walk.
            subdirectories[:] = [name for name in subdirectories if not _is_hidden(name)]
            parent = Path(directory)
            result.extend(self._entry(parent / name) for name in subdirectories if (parent / name).exists())
            result.extend(
                self._entry(parent / name)
                for name in files
                if _is_markdown(name) and not _is_hidden(name) and (parent / name).exists()
            )
        return sorted(result, key=lambda entry: entry.path.casefold())

    def read(self, path: str) -> FileContent:
        target = self._resolve(_markdown_path(path))
        try:
            data = target.read_bytes()
            stat = target.stat()
        except FileNotFoundError as error:
            raise RepositoryNotFoundError("Repository path not found") from error
        except (IsADirectoryError, UnicodeDecodeError, OSError) as error:
            raise RepositoryError(f"Cannot read '{path}': {error}") from error
        return FileContent(content=data.decode("utf-8"), etag=_etag(stat))

    def write(self, path: str, content: str, etag: str | None = None) -> None:
        target = self._resolve(_markdown_path(path))
        if etag is not None:
            current = self._current_etag(target)
            if current != etag:
                raise RepositoryConflictError("File changed on disk; read it again before retrying")
        target.parent.mkdir(parents=True, exist_ok=True)
        # A sibling temporary file then rename: a synchroniser watching the
        # directory never uploads a half-written note.
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            with open(temporary, "wb") as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise RepositoryError(f"Cannot write '{path}': {error}") from error

    def append(self, path: str, content: str) -> None:
        try:
            current = self.read(path)
        except RepositoryNotFoundError:
            self.write(path, content)
            return
        self.write(path, current.content + content, current.etag)

    def delete(self, path: str) -> None:
        target = self._resolve(_markdown_path(path))
        try:
            target.unlink()
        except FileNotFoundError as error:
            raise RepositoryNotFoundError("Repository path not found") from error
        except OSError as error:
            raise RepositoryError(f"Cannot delete '{path}': {error}") from error

    def search(self, query: str, context_length: int = 100) -> list[dict[str, Any]]:
        if not query:
            raise RepositoryError("Search query must not be empty")
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        for entry in self.list_files():
            if entry.is_directory:
                continue
            content = self.read(entry.path).content
            folded = content.casefold()
            matches = []
            start = 0
            while True:
                index = folded.find(needle, start)
                if index < 0:
                    break
                matches.append(
                    {
                        "context": content[max(0, index - context_length): index + len(query) + context_length],
                        "match_position": {"start": index, "end": index + len(query)},
                    }
                )
                start = index + max(1, len(query))
            if matches:
                results.append({"filename": entry.path, "score": len(matches), "matches": matches})
        return sorted(results, key=lambda result: (-result["score"], result["filename"].casefold()))

    def search_by_tag(self, tag: str, directory: str | None = None) -> list[str]:
        expected = tag.removeprefix("#")
        if not expected:
            raise RepositoryError("Tag must not be empty")
        directory = _normalise_path(directory or "")
        matches = []
        for entry in self.list_files():
            if entry.is_directory:
                continue
            if directory and not entry.path.startswith(f"{directory}/"):
                continue
            if expected in _tags(self.read(entry.path).content):
                matches.append(entry.path)
        return matches

    def get_frontmatter(self, path: str) -> dict[str, Any]:
        return _frontmatter(self.read(path).content)

    def patch(self, path: str, operation: str, target_type: str, target: str, content: str) -> None:
        current = self.read(path)
        updated = patch_markdown(current.content, operation, target_type, target, content)
        self.write(path, updated, current.etag)

    def recent_changes(self, limit: int, days: int) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC).timestamp() - days * 24 * 60 * 60
        recent = [
            {"filename": entry.path, "modified_at": entry.modified_at, "size": entry.size}
            for entry in self.list_files()
            if not entry.is_directory
            and entry.modified_at
            and datetime.fromisoformat(entry.modified_at).timestamp() >= cutoff
        ]
        return sorted(recent, key=lambda item: item["modified_at"], reverse=True)[:limit]

    def _resolve(self, path: str) -> Path:
        target = (self._root / path).resolve() if path else self._root
        # resolve() follows symlinks, so a link pointing outside is caught here
        # and not only a literal '..'.
        if not target.is_relative_to(self._root):
            raise RepositoryPathError("Path must stay within its configured repository")
        return target

    def _current_etag(self, target: Path) -> str | None:
        try:
            return _etag(target.stat())
        except FileNotFoundError:
            return None

    def _entry(self, path: Path) -> FileEntry:
        stat = path.stat()
        return FileEntry(
            path=path.relative_to(self._root).as_posix(),
            is_directory=path.is_dir(),
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            size=None if path.is_dir() else stat.st_size,
        )


def patch_markdown(source: str, operation: str, target_type: str, target: str, content: str) -> str:
    if operation not in {"append", "prepend", "replace", "delete"}:
        raise RepositoryError("operation must be append, prepend, replace, or delete")
    if target_type == "heading":
        return _patch_heading(source, operation, target, content)
    if target_type == "block":
        return _patch_block(source, operation, target, content)
    if target_type == "frontmatter":
        return _patch_frontmatter(source, operation, target, content)
    raise RepositoryError("target_type must be heading, block, or frontmatter")


def _patch_heading(source: str, operation: str, target: str, content: str) -> str:
    target_parts = tuple(part.strip() for part in target.split("::") if part.strip())
    if not target_parts:
        raise RepositoryError("Heading target must not be empty")
    lines = source.splitlines(keepends=True)
    stack: list[str] = []
    start = -1
    level = 0
    for index, line in enumerate(lines):
        match = HEADING_PATTERN.match(line.rstrip("\r\n"))
        if not match:
            continue
        heading_level = len(match.group(1))
        stack = stack[: heading_level - 1] + [match.group(2)]
        if tuple(stack) == target_parts or (len(target_parts) == 1 and match.group(2) == target_parts[0]):
            start, level = index, heading_level
            break
    if start < 0:
        raise RepositoryNotFoundError(f"Heading '{target}' not found")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = HEADING_PATTERN.match(lines[index].rstrip("\r\n"))
        if match and len(match.group(1)) <= level:
            end = index
            break
    body = "".join(lines[start + 1:end])
    replacement = _apply_patch(body, operation, content)
    if replacement and not replacement.endswith(("\n", "\r")):
        replacement += "\n"
    return "".join(lines[:start + 1]) + replacement + "".join(lines[end:])


def _patch_block(source: str, operation: str, target: str, content: str) -> str:
    target = target.removeprefix("^")
    expression = re.compile(rf"(?m)^.*\^{re.escape(target)}\s*$")
    match = expression.search(source)
    if not match:
        raise RepositoryNotFoundError(f"Block '{target}' not found")
    return source[:match.start()] + _apply_patch(match.group(0), operation, content) + source[match.end():]


def _patch_frontmatter(source: str, operation: str, target: str, content: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", target):
        raise RepositoryPathError("Frontmatter target must be a top-level YAML key")
    if not source.startswith("---\n"):
        if operation == "delete":
            raise RepositoryNotFoundError(f"Frontmatter field '{target}' not found")
        frontmatter, body = "---\n---\n", source
    else:
        closing = source.find("\n---\n", 4)
        if closing < 0:
            raise RepositoryError("Unterminated YAML frontmatter")
        frontmatter, body = source[:closing + 5], source[closing + 5:]
    lines = frontmatter.splitlines(keepends=True)
    expression = re.compile(rf"^{re.escape(target)}:\s*(.*)$")
    index = next((index for index, line in enumerate(lines) if expression.match(line)), None)
    if index is None:
        if operation == "delete":
            raise RepositoryNotFoundError(f"Frontmatter field '{target}' not found")
        lines.insert(-1, f"{target}: {content.rstrip()}\n")
    else:
        match = expression.match(lines[index])
        if match is None:
            raise RepositoryError("Invalid frontmatter field")
        current = match.group(1)
        value = _apply_patch(current, operation, content).rstrip("\n")
        if operation == "delete":
            del lines[index]
        else:
            lines[index] = f"{target}: {value}\n"
    return "".join(lines) + body


def _apply_patch(current: str, operation: str, content: str) -> str:
    if operation == "append":
        return current + content
    if operation == "prepend":
        return content + current
    if operation == "replace":
        return content
    return ""


def _frontmatter(content: str) -> dict[str, Any]:
    if not content.startswith("---\n"):
        return {}
    closing = content.find("\n---\n", 4)
    if closing < 0:
        return {}
    result: dict[str, Any] = {}
    for line in content[4:closing].splitlines():
        if not line or line.startswith((" ", "\t", "#")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            result[key.strip()] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        else:
            result[key.strip()] = value.strip("'\"")
    return result


def _tags(content: str) -> set[str]:
    tags = {match.group(1) for match in TAG_PATTERN.finditer(content)}
    frontmatter_tags = _frontmatter(content).get("tags")
    if isinstance(frontmatter_tags, list):
        tags.update(str(tag).removeprefix("#") for tag in frontmatter_tags)
    elif isinstance(frontmatter_tags, str):
        tags.add(frontmatter_tags.removeprefix("#"))
    return tags


def _etag(stat: os.stat_result) -> str:
    return f"{stat.st_mtime_ns}-{stat.st_size}"


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _normalise_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RepositoryPathError("Path must stay within its configured repository")
    if any(_is_hidden(part) for part in candidate.parts):
        raise RepositoryPathError("Hidden files and directories are not exposed")
    normalised = str(candidate)
    return "" if normalised == "." else normalised.strip("/")


def _markdown_path(path: str) -> str:
    path = _normalise_path(path)
    if not path or not _is_markdown(path):
        raise RepositoryPathError("Only .md files can be manipulated")
    return path


def _is_markdown(path: str) -> bool:
    return path.casefold().endswith(".md")
