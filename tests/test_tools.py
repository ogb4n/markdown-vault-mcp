import json

import pytest

from markdown_vault_mcp import tools
from markdown_vault_mcp.repository import RepositoryError


class Repository:
    def __init__(self) -> None:
        self.calls = []

    def read(self, path):
        self.calls.append(("read", path))
        return type("File", (), {"content": "# Note"})()

    def write(self, path, content):
        self.calls.append(("write", path, content))

    def append(self, path, content):
        self.calls.append(("append", path, content))

    def delete(self, path):
        self.calls.append(("delete", path))

    def patch(self, *args):
        self.calls.append(("patch", *args))

    def list_files(self):
        return []

    def list_directory(self, path):
        return []

    def search(self, query, context_length):
        return [{"filename": "note.md", "score": 1, "matches": []}]

    def search_by_tag(self, tag, directory):
        return ["note.md"]

    def get_frontmatter(self, path):
        return {"tags": ["notes"]}

    def recent_changes(self, limit, days):
        return []


@pytest.fixture
def repository(monkeypatch):
    value = Repository()
    monkeypatch.setattr(tools, "_repository", lambda args: value)
    return value


def text(result):
    return result[0].text


def test_tools_are_markdown_namespaced():
    assert {tool.name for tool in tools.list_tools()} >= {
        "markdown_list_repositories",
        "markdown_get_file",
        "markdown_search",
        "markdown_patch",
        "markdown_delete",
    }


def test_get_file_reads_repository(repository):
    assert json.loads(text(tools.get_file({"path": "note.md"}))) == "# Note"
    assert repository.calls == [("read", "note.md")]


def test_write_and_append_delegate_to_repository(repository):
    tools.write({"path": "note.md", "content": "one"})
    tools.append({"path": "note.md", "content": "two"})
    assert repository.calls == [("write", "note.md", "one"), ("append", "note.md", "two")]


def test_delete_requires_explicit_confirmation(repository):
    with pytest.raises(RepositoryError, match="confirm"):
        tools.delete({"path": "note.md", "confirm": False})
    tools.delete({"path": "note.md", "confirm": True})
    assert repository.calls == [("delete", "note.md")]


def test_patch_validates_required_arguments(repository):
    with pytest.raises(RepositoryError, match="target"):
        tools.patch({"path": "note.md", "operation": "append", "target_type": "heading"})

    tools.patch(
        {
            "path": "note.md",
            "operation": "append",
            "target_type": "heading",
            "target": "Log",
            "content": "entry",
        }
    )
    assert repository.calls == [("patch", "note.md", "append", "heading", "Log", "entry")]
