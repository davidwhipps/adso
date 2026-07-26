# Talk to your library with an AI agent (MCP)

Adso ships a [Model Context Protocol](https://modelcontextprotocol.io) server, so an
AI agent can query and lightly curate your catalogue in plain language:

> *"What unread sci-fi do I own in physical?"* · *"Summarise my shelf."* ·
> *"Tag everything by Le Guin as favourites."*

It runs entirely **locally over stdio**, reading the same canonical SQLite file
directly — no server to host, no account, and nothing leaves your machine.

- [Install](#install)
- [Point it at your library](#point-it-at-your-library)
- [Register it with an agent](#register-it-with-an-agent)
  - [Claude Code](#claude-code)
  - [OpenAI Codex (terminal + ChatGPT app)](#openai-codex-terminal--chatgpt-app)
  - [Claude Desktop](#claude-desktop)
- [What the agent can (and can't) do](#what-the-agent-can-and-cant-do)
- [Verifying it works](#verifying-it-works)
- [What about claude.ai and ChatGPT (the cloud web apps)?](#what-about-claudeai-and-chatgpt-the-cloud-web-apps)

## Install

The server ships as the `mcp` extra and needs **Python 3.10+** (the core CLI
stays 3.9):

```bash
pipx install "adso[mcp] @ git+https://github.com/davidwhipps/adso.git"
# or, from a clone:  pip install ".[mcp]"
```

The server itself is one command — your agent normally spawns it for you, but you
can run it by hand to check it starts:

```bash
adso mcp        # stdio MCP server; Ctrl-C to stop
```

## Point it at your library

`adso mcp` resolves the database the same way every other command does, in this
order of precedence:

| How | Example | When to use |
| --- | --- | --- |
| `--db` flag | `adso --db /path/to/adso.sqlite mcp` | Explicit, portable, unambiguous — **recommended for agent configs**. |
| `ADSO_DB` env var | `ADSO_DB=/path/to/adso.sqlite adso mcp` | Handy for config files that take an `env` block (e.g. Codex TOML). |
| `--profile` | `adso --profile personal mcp` | If you keep named profiles (`adso config init`). |
| default | `adso mcp` | Uses `adso.sqlite` in the **current directory** — fragile once an agent launches it from elsewhere, so prefer an explicit path. |

Because an agent may spawn the server from any working directory, **always give an
absolute path** (via `--db` or `ADSO_DB`) rather than relying on the default.

## Register it with an agent

### Claude Code

```bash
claude mcp add adso -- adso --db /absolute/path/to/adso.sqlite mcp
```

That writes a `local`-scoped entry (private to you in this project). Use
`-s user` to make it available from any directory. Verify with
`claude mcp get adso`; it should report **✔ Connected**.

### OpenAI Codex (terminal + ChatGPT app)

Codex is a **local** agent, so it uses the same stdio server — no tunnel, nothing
exposed. Both the terminal `codex` CLI and the Codex agent **inside the ChatGPT
desktop app** read the same config file: `~/.codex/config.toml`.

Add a server stanza:

```toml
[mcp_servers.adso]
command = "/absolute/path/to/.venv/bin/adso"   # or just "adso" if it's on PATH
args = ["mcp"]

[mcp_servers.adso.env]
ADSO_DB = "/absolute/path/to/adso.sqlite"

# Reads run freely; gate the writes behind a confirmation prompt.
[mcp_servers.adso.tools.add_tags_tool]
approval_mode = "approve"
[mcp_servers.adso.tools.remove_tags_tool]
approval_mode = "approve"
[mcp_servers.adso.tools.set_format_tool]
approval_mode = "approve"
[mcp_servers.adso.tools.set_loaned_tool]
approval_mode = "approve"
```

Then **restart the ChatGPT app** (or start a fresh `codex` session) so it reloads
the config. Confirm with `codex mcp list` — `adso` should appear as `enabled`.

> Prefer a command? `codex mcp add adso -- adso --db /absolute/path/to/adso.sqlite mcp`
> does the same thing interactively.

### Claude Desktop

Settings → Developer → Edit Config, then add:

```json
{
  "mcpServers": {
    "adso": {
      "command": "adso",
      "args": ["--db", "/absolute/path/to/adso.sqlite", "mcp"]
    }
  }
}
```

Restart Claude Desktop to pick it up.

## What the agent can (and can't) do

**Eight tools.** Four reads — search the catalogue, fetch a book, summarise your
library, list your shelves/tags/formats — and four curated writes: add tags,
remove tags, set a book's owned format, and record a loan.

The guardrails matter as much as the tools:

- **Private by default.** Read output is assembled from an explicit allowlist, so
  your Goodreads *private notes* — and any column added to the schema later — are
  never exposed to the agent. It is deliberately not a "return every column" dump.
- **Your own notes are read-only.** Your `local notes` are visible to the agent,
  but no tool can overwrite them, so prose you wrote yourself stays safe.
- **Writes touch only your local fields** (tags, format, loaned-to) — the ones
  sync never overwrites. Imports, sync, conflict resolution, and duplicate merges
  are all off-limits.
- **You approve every action.** MCP clients prompt before running a tool; the
  `approval_mode = "approve"` entries above make the write tools ask first, and
  the canonical SQLite catalogue always stays the source of truth.

## Verifying it works

- **Claude Code / Claude Desktop** — run `/mcp` in a session to list connected
  servers; `adso` and its tools should be listed.
- **Codex** — `codex mcp list` shows `adso` as `enabled`.
- **A quick end-to-end check** — ask the agent *"how many books are in my library
  and what's the shelf breakdown?"* (that calls `library_stats`), or
  *"tag A Distant Mirror as medieval"* (which should trigger an approval prompt).

## What about claude.ai and ChatGPT (the cloud web apps)?

`adso mcp` is a **local stdio** server, which only local agents can spawn —
Claude Code, Claude Desktop, and Codex (terminal or the ChatGPT desktop app,
whose embedded Codex runs on your machine).

The **cloud web apps** — claude.ai and plain ChatGPT threads — run their MCP
client in the cloud, so they can only reach a **remote MCP server** over HTTPS.
They can't connect to a process on your Mac. Supporting them would mean running
adso with an HTTP transport and exposing it to the internet behind
authentication — which also puts your live library on a public endpoint. That
isn't shipped today, and for a personal catalogue the local agents above are the
safer, simpler path.
