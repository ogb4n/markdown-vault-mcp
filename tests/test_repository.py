import os

import pytest

from mcp_markdown_repositories.repository import (
    LocalMarkdownRepository,
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryPathError,
    _frontmatter,
    _tags,
    patch_markdown,
)


SOURCE = """---
title: Demo
tags: [notes, project]
---
# Overview
Before.
## Log
First.^line-one
# Other
After.
"""


def test_patch_heading_keeps_the_following_section():
    patched = patch_markdown(SOURCE, "append", "heading", "Overview::Log", "Second.")
    assert "First.^line-one\nSecond.\n# Other" in patched


def test_patch_heading_accepts_unique_bare_heading():
    patched = patch_markdown(SOURCE, "replace", "heading", "Overview", "Replaced.\n")
    assert "# Overview\nReplaced.\n# Other" in patched


def test_patch_block_and_frontmatter():
    block = patch_markdown(SOURCE, "replace", "block", "line-one", "Changed.^line-one")
    frontmatter = patch_markdown(block, "replace", "frontmatter", "title", "Updated")
    assert "Changed.^line-one" in frontmatter
    assert "title: Updated" in frontmatter


def test_patch_requires_existing_targets():
    with pytest.raises(RepositoryNotFoundError):
        patch_markdown(SOURCE, "append", "heading", "Missing", "x")


def test_frontmatter_and_tags_are_extracted_without_obsidian():
    assert _frontmatter(SOURCE)["title"] == "Demo"
    assert _tags(SOURCE) == {"notes", "project"}


def test_paths_cannot_escape_a_repository():
    from mcp_markdown_repositories.repository import _normalise_path

    with pytest.raises(RepositoryPathError):
        _normalise_path("../outside.md")


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "note.md").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "root.md").write_text("Racine #idee\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "workspace.md").write_text("secret", encoding="utf-8")
    return LocalMarkdownRepository("vault", tmp_path)


def test_listing_hides_non_markdown_and_dot_directories(vault):
    assert [entry.path for entry in vault.list_files()] == ["Projects", "Projects/note.md", "root.md"]
    assert [entry.path for entry in vault.list_directory("")] == ["Projects", "root.md"]


def test_hidden_paths_are_not_readable(vault):
    with pytest.raises(RepositoryPathError):
        vault.read(".obsidian/workspace.md")


def test_symlink_pointing_outside_the_vault_is_refused(vault, tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("hors coffre", encoding="utf-8")
    os.symlink(outside, tmp_path / "lien.md")
    with pytest.raises(RepositoryPathError):
        vault.read("lien.md")


def test_write_creates_parents_and_leaves_no_temporary_file(vault, tmp_path):
    vault.write("Nouveau/dossier/page.md", "Contenu é\n")
    assert (tmp_path / "Nouveau/dossier/page.md").read_text(encoding="utf-8") == "Contenu é\n"
    assert not list(tmp_path.rglob("*.tmp"))


def test_write_with_a_stale_etag_is_a_conflict(vault, tmp_path):
    first = vault.read("root.md")
    (tmp_path / "root.md").write_text("Modifié ailleurs, plus long\n", encoding="utf-8")
    with pytest.raises(RepositoryConflictError):
        vault.write("root.md", "Mon edition", first.etag)


def test_append_creates_a_missing_file_and_extends_an_existing_one(vault):
    vault.append("journal.md", "un\n")
    vault.append("journal.md", "deux\n")
    assert vault.read("journal.md").content == "un\ndeux\n"


def test_patch_updates_the_file_on_disk(vault):
    vault.patch("Projects/note.md", "replace", "frontmatter", "title", "Autre")
    assert vault.get_frontmatter("Projects/note.md")["title"] == "Autre"


def test_search_and_tags_cover_nested_files(vault):
    assert [hit["filename"] for hit in vault.search("first")] == ["Projects/note.md"]
    assert vault.search_by_tag("#idee") == ["root.md"]
    assert vault.search_by_tag("notes", "Projects") == ["Projects/note.md"]


def test_delete_removes_the_file_and_reports_a_missing_one(vault, tmp_path):
    vault.delete("root.md")
    assert not (tmp_path / "root.md").exists()
    with pytest.raises(RepositoryNotFoundError):
        vault.delete("root.md")


def test_recent_changes_lists_files_by_modification_time(vault):
    names = [change["filename"] for change in vault.recent_changes(10, 1)]
    assert set(names) == {"Projects/note.md", "root.md"}
