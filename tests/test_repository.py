import pytest

from mcp_markdown_repositories.repository import (
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
