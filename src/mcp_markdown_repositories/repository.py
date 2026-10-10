"""OneDrive-backed Markdown repository.

The module deliberately knows nothing about MCP.  It exposes a small file
repository API; Microsoft Graph is only its infrastructure adapter.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import PurePosixPath
import re
import time
from typing import Any
from urllib.parse import quote

import requests


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"
TAG_PATTERN = re.compile(r"(?<![\w/])#([\w/-]+)")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class RepositoryError(RuntimeError):
    """A repository operation could not be completed."""


class RepositoryNotFoundError(RepositoryError):
    """The requested path does not exist."""


class RepositoryConflictError(RepositoryError):
    """The remote file changed while it was being edited."""


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


class OneDriveTokenProvider:
    """Obtains an access token from a static token or a renewable token file."""

    def __init__(self) -> None:
        self._access_token = os.getenv("ONEDRIVE_ACCESS_TOKEN")
        self._refresh_token = os.getenv("ONEDRIVE_REFRESH_TOKEN")
        self._refresh_token_file = os.getenv("ONEDRIVE_REFRESH_TOKEN_FILE")
        self._client_id = os.getenv("ONEDRIVE_CLIENT_ID")
        self._client_secret = os.getenv("ONEDRIVE_CLIENT_SECRET")
        self._tenant_id = os.getenv("ONEDRIVE_TENANT_ID", "consumers")
        self._expires_at = 0.0

        if self._refresh_token_file and not self._refresh_token:
            try:
                self._refresh_token = open(self._refresh_token_file, encoding="utf-8").read().strip()
            except OSError as error:
                raise RepositoryError(
                    f"Cannot read ONEDRIVE_REFRESH_TOKEN_FILE: {error}"
                ) from error

    def access_token(self) -> str:
        if self._access_token and not self._refresh_token:
            return self._access_token
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        if not self._refresh_token or not self._client_id:
            raise RepositoryError(
                "Configure ONEDRIVE_ACCESS_TOKEN, or ONEDRIVE_CLIENT_ID with "
                "ONEDRIVE_REFRESH_TOKEN(_FILE)."
            )

        response = requests.post(
            f"{LOGIN_ROOT}/{self._tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "scope": "offline_access Files.ReadWrite",
            },
            timeout=(3, 20),
        )
        if not response.ok:
            raise RepositoryError(f"OneDrive token refresh failed: {response.status_code}")
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str):
            raise RepositoryError("OneDrive token refresh returned no access token")
        self._access_token = token
        self._expires_at = time.monotonic() + int(payload.get("expires_in", 3600))
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            self._refresh_token = rotated
            self._persist_refresh_token(rotated)
        return token

    def _persist_refresh_token(self, token: str) -> None:
        if not self._refresh_token_file:
            return
        destination = PurePosixPath(self._refresh_token_file)
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                stream.write(token)
                stream.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        except OSError as error:
            raise RepositoryError(f"Cannot persist rotated OneDrive refresh token: {error}") from error


class GraphClient:
    def __init__(self, token_provider: OneDriveTokenProvider) -> None:
        self._tokens = token_provider

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        request_headers = {"Authorization": f"Bearer {self._tokens.access_token()}"}
        if headers:
            request_headers.update(headers)
        response = requests.request(
            method,
            f"{GRAPH_ROOT}{path}",
            params=params,
            data=content,
            headers=request_headers,
            timeout=(3, 30),
        )
        if response.status_code == 404:
            raise RepositoryNotFoundError("Repository path not found")
        if response.status_code == 412:
            raise RepositoryConflictError("File changed on OneDrive; read it again before retrying")
        if not response.ok:
            message = _graph_error_message(response)
            raise RepositoryError(f"OneDrive request failed ({response.status_code}): {message}")
        return response


class GraphMarkdownRepository:
    """A Markdown-only repository rooted at one configured OneDrive directory."""

    def __init__(self, name: str, root: str, client: GraphClient) -> None:
        self.name = name
        self._root = _normalise_root(root)
        self._client = client
        self._root_id: str | None = None

    def list_directory(self, path: str = "") -> list[FileEntry]:
        item = self._item(path)
        if "folder" not in item:
            raise RepositoryPathError(f"'{path}' is not a directory")
        entries = []
        for child in self._children(item["id"]):
            is_directory = "folder" in child
            if is_directory or _is_markdown(str(child.get("name", ""))):
                entries.append(_entry(path, child, is_directory))
        return sorted(entries, key=lambda entry: (not entry.is_directory, entry.path.casefold()))

    def list_files(self) -> list[FileEntry]:
        root = self._item("")
        result: list[FileEntry] = []
        pending: list[tuple[str, str]] = [("", root["id"])]
        while pending:
            parent_path, parent_id = pending.pop()
            for child in self._children(parent_id):
                name = str(child.get("name", ""))
                path = _join_path(parent_path, name)
                if "folder" in child:
                    result.append(_entry(parent_path, child, True))
                    pending.append((path, child["id"]))
                elif _is_markdown(name):
                    result.append(_entry(parent_path, child, False))
        return sorted(result, key=lambda entry: entry.path.casefold())

    def read(self, path: str) -> FileContent:
        path = _markdown_path(path)
        item = self._item(path)
        response = self._client.request("GET", f"/me/drive/items/{item['id']}/content")
        return FileContent(
            content=response.content.decode("utf-8"),
            etag=response.headers.get("ETag") or item.get("eTag"),
        )

    def write(self, path: str, content: str, etag: str | None = None) -> None:
        path = _markdown_path(path)
        self._ensure_parent_directories(path)
        headers = {"Content-Type": "text/markdown; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        self._client.request(
            "PUT",
            f"/me/drive/root:/{self._encoded_full_path(path)}:/content",
            content=content.encode("utf-8"),
            headers=headers,
        )

    def append(self, path: str, content: str) -> None:
        try:
            current = self.read(path)
        except RepositoryNotFoundError:
            self.write(path, content)
            return
        self.write(path, current.content + content, current.etag)

    def delete(self, path: str) -> None:
        item = self._item(_markdown_path(path))
        self._client.request("DELETE", f"/me/drive/items/{item['id']}")

    def search(self, query: str, context_length: int = 100) -> list[dict[str, Any]]:
        if not query:
            raise RepositoryError("Search query must not be empty")
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        for entry in self.list_files():
            if entry.is_directory:
                continue
            file = self.read(entry.path)
            matches = []
            start = 0
            folded = file.content.casefold()
            while True:
                index = folded.find(needle, start)
                if index < 0:
                    break
                matches.append(
                    {
                        "context": file.content[max(0, index - context_length): index + len(query) + context_length],
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
            if directory and not entry.path.startswith(f"{directory}/"):
                continue
            content = self.read(entry.path).content
            if expected in _tags(content):
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
        recent = []
        for entry in self.list_files():
            if entry.is_directory or not entry.modified_at:
                continue
            try:
                modified = datetime.fromisoformat(entry.modified_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if modified >= cutoff:
                recent.append({"filename": entry.path, "modified_at": entry.modified_at, "size": entry.size})
        return sorted(recent, key=lambda item: item["modified_at"], reverse=True)[:limit]

    def _item(self, path: str) -> dict[str, Any]:
        path = _normalise_path(path)
        if not path:
            return self._root_item()
        response = self._client.request("GET", f"/me/drive/root:/{self._encoded_full_path(path)}")
        return response.json()

    def _root_item(self) -> dict[str, Any]:
        if self._root_id:
            return {"id": self._root_id, "folder": {}}
        response = self._client.request("GET", f"/me/drive/root:/{self._encoded_full_path('')}")
        item = response.json()
        if "folder" not in item:
            raise RepositoryPathError("Configured OneDrive repository root is not a directory")
        self._root_id = item["id"]
        return item

    def _children(self, item_id: str) -> Iterator[dict[str, Any]]:
        path = f"/me/drive/items/{item_id}/children"
        params: dict[str, str] | None = {"$select": "id,name,folder,file,size,lastModifiedDateTime,eTag"}
        while path:
            response = self._client.request("GET", path, params=params)
            payload = response.json()
            yield from payload.get("value", [])
            next_link = payload.get("@odata.nextLink")
            path = next_link.removeprefix(GRAPH_ROOT) if isinstance(next_link, str) else ""
            params = None

    def _ensure_parent_directories(self, path: str) -> None:
        parts = path.split("/")[:-1]
        current_id = self._root_item()["id"]
        for part in parts:
            child = next((item for item in self._children(current_id) if item.get("name") == part), None)
            if child is None:
                response = self._client.request(
                    "POST",
                    f"/me/drive/items/{current_id}/children",
                    content=json.dumps({"name": part, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                child = response.json()
            if "folder" not in child:
                raise RepositoryPathError(f"'{part}' is a file, not a directory")
            current_id = child["id"]

    def _full_path(self, path: str) -> str:
        return _join_path(self._root, path) if path else self._root

    def _encoded_full_path(self, path: str) -> str:
        return quote(self._full_path(path), safe="/")


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


def _entry(parent: str, item: dict[str, Any], is_directory: bool) -> FileEntry:
    return FileEntry(
        path=_join_path(parent, str(item["name"])),
        is_directory=is_directory,
        modified_at=item.get("lastModifiedDateTime"),
        size=item.get("size"),
    )


def _normalise_root(path: str) -> str:
    path = _normalise_path(path)
    if not path:
        raise RepositoryPathError("A OneDrive repository root must be configured")
    return path


def _normalise_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RepositoryPathError("Path must stay within its configured repository")
    normalised = str(candidate)
    return "" if normalised == "." else normalised.strip("/")


def _markdown_path(path: str) -> str:
    path = _normalise_path(path)
    if not path or not _is_markdown(path):
        raise RepositoryPathError("Only .md files can be manipulated")
    return path


def _is_markdown(path: str) -> bool:
    return path.casefold().endswith(".md")


def _join_path(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part.strip("/"))


def _graph_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    except ValueError:
        pass
    return "unexpected response"
