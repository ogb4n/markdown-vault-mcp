# MCP Markdown Repositories

An MCP server for Markdown repositories stored in OneDrive. It talks directly
to Microsoft Graph: Obsidian, its Local REST API plugin, and a permanently
powered-on desktop are no longer part of the architecture.

Each configured repository is a OneDrive directory. The server never accepts a
filesystem path from a client, only a repository name and a path relative to
that repository root.

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

`markdown_patch` supports headings (with `Parent::Child` paths), Obsidian
block IDs, and simple top-level YAML frontmatter fields. Writes use the OneDrive
ETag read with the file, so a concurrent remote change returns a conflict
instead of silently overwriting it.

Only `.md` files are exposed. Paths containing `..` or an absolute path are
rejected, and the configured OneDrive roots cannot be escaped.

## OneDrive setup

The server uses delegated OAuth so it works with both personal and work/school
OneDrive accounts.

1. Create a Microsoft Entra application registration that supports the account
   type you use (include personal Microsoft accounts for OneDrive Personal).
2. Enable public client/device-code flow and add delegated Microsoft Graph
   permission `Files.ReadWrite`. Do not grant broader permissions unless you
   deliberately want them.
3. Copy the application (client) ID into `.env`.

Start with the supplied configuration:

```bash
cp .env.example .env
# set a random MCP_HTTP_TOKEN and ONEDRIVE_CLIENT_ID
# scope MARKDOWN_REPOSITORIES to the smallest directories that contain the notes
```

The repository mapping is JSON because it can name several isolated note
collections:

```dotenv
MARKDOWN_REPOSITORIES={"claude-vault":"ClaudeVault","work-notes":"Documents/Work/Notes"}
MARKDOWN_DEFAULT_REPOSITORY=claude-vault
```

Build the image, then perform the one-time device-code authorization from a
terminal:

```bash
docker compose build
docker compose --profile auth run --rm onedrive-auth
```

The command displays a Microsoft URL and code. Complete it in a browser. The
refresh token is stored in the Docker volume `onedrive-tokens`, with mode
`0600`, and is automatically rotated by the server. Treat that volume as a
secret.

Start the durable service:

```bash
docker compose up -d markdown-repositories
```

It serves Streamable HTTP at `/mcp`, protected by
`Authorization: Bearer <MCP_HTTP_TOKEN>`; `/healthz` is the only public
endpoint. The compose file publishes the port on loopback only. Put a TLS
reverse proxy or private network in front of it before making it reachable by
agents outside the server. Never put the bearer token in the URL.

When a reverse proxy preserves the public `Host` header, set
`MCP_HTTP_ALLOWED_HOSTS=markdown.example.net` in its service environment (and
set `MCP_HTTP_ALLOWED_ORIGINS=https://markdown.example.net` only when a browser
client needs CORS). These allow-lists are intentionally empty by default for
remote hosts to keep the DNS-rebinding protection enabled.

A generic remote MCP client needs an endpoint like:

```json
{
  "url": "https://markdown.example.net/mcp",
  "headers": {
    "Authorization": "Bearer <MCP_HTTP_TOKEN>"
  }
}
```

For a local stdio client, install the package and run
`mcp-markdown-repositories`; it uses the same environment variables.

## Operational notes

- The OneDrive root is a security boundary. Give the app access to the account
  it needs, but expose only the smallest vault directories through
  `MARKDOWN_REPOSITORIES`.
- Full-text search is deliberately provider-independent: it lists the
  configured Markdown files and searches their UTF-8 contents. This gives
  predictable results for Markdown, but is slower on a very large vault.
- `markdown_write` replaces a file. Use `markdown_append` or
  `markdown_patch` for an existing note whenever possible.
- `markdown_delete` requires `confirm: true` and deletes through Microsoft
  Graph. Confirm the deletion policy of the target OneDrive account before
  relying on its recycle bin.

## Development

```bash
uv sync
uv run pytest -q
uv run pyright
```

The project uses MCP 1.x because its server registration API is stable for
this implementation. The HTTP transport is tested separately from the
OneDrive adapter; the latter has no test credentials and is exercised through
mocked Graph responses.
