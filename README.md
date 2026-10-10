# MCP Markdown Repositories

An MCP server that reads and writes the Markdown files of a local directory the
way the Obsidian MCP does, without Obsidian running. Point it at a vault that is
mounted or synchronised on the machine; Obsidian picks the changes up the next
time it opens the vault.

The server never accepts a filesystem path from a client, only a repository name
and a path relative to that repository root.

## What it provides

| Tool | Purpose |
| --- | --- |
| `markdown_list_repositories` | List configured repository names |
| `markdown_list_files`, `markdown_list_directory` | Browse Markdown files and directories |
| `markdown_get_file`, `markdown_get_files` | Read Markdown |
| `markdown_search`, `markdown_search_by_tag` | Search text or exact tags |
| `markdown_get_frontmatter`, `markdown_recent_changes` | Inspect metadata |
| `markdown_append`, `markdown_write`, `markdown_patch` | Create and edit notes |
| `markdown_delete` | Delete an explicitly confirmed Markdown file |

`markdown_patch` supports headings (with `Parent::Child` paths), Obsidian block
IDs, and simple top-level YAML frontmatter fields.

Not provided, because they need the Obsidian application: Dataview and
JsonLogic queries, periodic notes, and commands.

## Guarantees

- Only `.md` files are exposed. Hidden files and directories (`.obsidian`,
  `.trash`, `.git`) are neither listed nor readable.
- `..`, absolute paths and symbolic links that resolve outside the root are
  refused.
- Writes go to a temporary sibling file that is then renamed, so a synchroniser
  never uploads a half-written note.
- `markdown_patch` and `markdown_append` carry the modification stamp read with
  the file; if the file changed on disk in between, they fail with a conflict
  instead of overwriting it. `markdown_write` replaces unconditionally.

## Run it

```bash
cp .env.example .env
# set MCP_HTTP_TOKEN, VAULT_DIR and VAULT_UID_GID (the owner of the vault)
docker compose up -d --build
```

It serves Streamable HTTP at `/mcp`, protected by
`Authorization: Bearer <MCP_HTTP_TOKEN>`; `/healthz` is the only public endpoint.
The port is published on `127.0.0.1` unless `MCP_HTTP_BIND` says otherwise, so
put a private network or a TLS reverse proxy in front of it before opening it to
another machine. Never put the token in the URL.

The container runs as `VAULT_UID_GID`, which must be able to write the vault. If
the vault is a FUSE mount (rclone, sshfs), the mount needs `allow_other` for
Docker to see it.

When a reverse proxy preserves the public `Host` header, set
`MCP_HTTP_ALLOWED_HOSTS=markdown.example.net` (and `MCP_HTTP_ALLOWED_ORIGINS`
only when a browser client needs CORS). These allow-lists are empty by default to
keep the DNS-rebinding protection enabled.

A remote MCP client needs:

```json
{
  "url": "http://host:27125/mcp",
  "headers": { "Authorization": "Bearer <MCP_HTTP_TOKEN>" }
}
```

For a local stdio client, install the package and run `mcp-markdown-repositories`
with `MARKDOWN_REPOSITORIES='{"vault":"/path/to/vault"}'`. Several repositories
can be named in that JSON object; `MARKDOWN_DEFAULT_REPOSITORY` picks the one
used when a call omits `repository`.

## Operational notes

- Full-text search reads every Markdown file on each call: slow on a very large
  vault, and slower still over a network mount.
- `markdown_delete` requires `confirm: true` and deletes for good: it does not
  use Obsidian's trash.

## Development

```bash
uv sync
uv run pytest -q
uv run pyright
```

`docker build --target test .` runs both checks in the image.
