<p align="left">
  <img src="assets/adso-logo.png" alt="Adso logo" width="200">
</p>

# Adso

<p align="left">
  <a href="https://github.com/davidwhipps/adso/actions/workflows/ci.yml"><img src="https://github.com/davidwhipps/adso/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+">
</p>

> **Goodreads is where your friends are. Adso is where your library lives.**

**Adso is a digital home for your library** — a sovereign, local-first copy you own, fed from Goodreads, that outlives any single app or service.

Stay on Goodreads for the network, but keep a synced copy in Adso that is yours. A single SQLite file on your own machine, always exportable to CSV/JSON, with no account, no ads, and no company that can sunset it.

It follows the ["file over app"](https://stephango.com/file-over-app) idea coined by Obsidian CEO, Steph Ango. In this case, a single [SQLite](https://www.sqlite.org/) file is one of the longest-lived formats there is to store your catalogue — and it's lightning fast because it's local. Your whole library is one file on disk: search is instant, everything works offline, and it's yours to query, script, or back up however you like.

**And you can talk to it.** Because your whole library is one local file, an AI agent can search and curate it in plain language over [MCP](MCP.md) — in Claude Code, Claude Desktop, or OpenAI's Codex (including the Codex agent inside the ChatGPT app), running locally with nothing exposed: *"what unread sci-fi do I own in physical?"* See [Talk to your library with an AI agent](#talk-to-your-library-with-an-ai-agent-mcp).

v1 is CLI-first and SQLite-backed. Because every interface is just an adapter over that one canonical catalogue, the same core powers what's next without rewriting the sync model: a **local web UI** (v2 — where visual conflict resolution lives), catalogue **enrichment** and **duplicate cleanup**, **more connectors** beyond Goodreads and Notion, and an **assistant layer** for audits and recommendations that builds on the [AI-agent interface](#talk-to-your-library-with-an-ai-agent-mcp) shipped today.

## Quick Start

The quickest way to try Adso is [pipx](https://pipx.pypa.io/), which installs the `adso` command onto your PATH so it works from any directory and any terminal — no virtual environment to create or activate:

```bash
pipx install "git+https://github.com/davidwhipps/adso.git"

adso init
adso import goodreads goodreads_library_export.csv
adso list
adso report summary
```

Don't have pipx? `brew install pipx` (macOS) or see the [pipx install guide](https://pipx.pypa.io/stable/installation/). Prefer `uv`? `uv tool install "git+https://github.com/davidwhipps/adso.git"` does the same thing. To work from a clone instead, see [Installation](#installation).

By default Adso uses `adso.sqlite` in the current directory. Pass `--db path/to/adso.sqlite` before the command to use another database.

No Goodreads export handy? Try the synthetic sample data. From a clone it's already in `examples/`; if you installed with pipx, grab it first:

```bash
curl -O https://raw.githubusercontent.com/davidwhipps/adso/main/examples/goodreads_sample.csv

adso import goodreads goodreads_sample.csv
adso list
adso show 100001
```

For a fuller walkthrough, see [examples/demo.md](examples/demo.md).

## Installation

The core CLI has **no required runtime dependencies** — it installs with nothing beyond the standard library and runs on **Python 3.9+**.

**Recommended — pipx** (puts `adso` on your PATH, isolated from the rest of your Python):

```bash
pipx install "git+https://github.com/davidwhipps/adso.git"
```

Optional features each add their own extra (these need **Python 3.10+**): `covers` for cover art, `web` for the local web UI, `notion` for Notion export, `mcp` for the [AI-agent (MCP) server](#talk-to-your-library-with-an-ai-agent-mcp). Add them in the install:

```bash
pipx install "adso[web,covers,notion,mcp] @ git+https://github.com/davidwhipps/adso.git"
```

**From a clone** (or if you'd rather manage your own environment), use a virtual environment so Adso's dependencies stay isolated:

```bash
git clone https://github.com/davidwhipps/adso.git
cd adso
python3 -m venv .venv
. .venv/bin/activate          # the `adso` command lives here while this is active
pip install .                 # add extras like: pip install ".[web,covers,notion]"
```

> The `adso` command is only available while that venv is activated — open a new terminal and you'll need to re-run `. .venv/bin/activate` first (or call `.venv/bin/adso` directly). Installing with pipx avoids this by putting `adso` on your PATH for good.

For a pinned, reproducible environment, run `pip install -r requirements-lock.txt` before `pip install .`.

## How It Works

- **SQLite is canonical** — your local catalogue is the source of truth.
- **Goodreads CSV exports** are preserved raw and normalized into the catalogue.
- **Your local fields** (format, tags, loaned-to, notes) are protected during sync.
- **Goodreads updates apply safely** only when your local value hasn't changed since the last sync — otherwise the change is held as a conflict rather than silently overwriting your data.
- **Cosmetic drift is ignored** — community ratings, edition relabels, ISBNs, page counts, and title casing refresh quietly, while real title/author changes stay tracked. An empty Goodreads value never erases stored data.
- **Open Library enrichment** — covers, plus descriptions, subjects, and place/time facets fetched politely (no API key); books the CSV left without ISBNs get them backfilled from the matched edition.

## Talk to your library with an AI agent (MCP)

Your whole catalogue is one local SQLite file — so you can hand it to an AI agent and ask for what you want in plain language: *"what unread sci-fi do I own in physical?"*, *"summarise my shelf"*, *"tag everything by Le Guin as favourites"*. Adso speaks the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), so **Claude Code, Claude Desktop, and OpenAI's Codex** — in the terminal or inside the ChatGPT desktop app — can read and lightly curate your library.

It runs entirely **locally over stdio**, reading the same canonical SQLite file directly — no server to host, no account, and nothing leaves your machine.

```bash
pip install ".[mcp]"       # needs Python 3.10+
adso mcp                   # stdio MCP server — your agent spawns this for you
```

Register it in one line — e.g. with Claude Code:

```bash
claude mcp add adso -- adso --db /absolute/path/to/adso.sqlite mcp
```

**→ Full setup for Claude Code, Codex, and Claude Desktop is in [MCP.md](MCP.md).**

**Eight tools, safe by design.** The agent can search the catalogue, fetch a book, summarise your library, and list your shelves, tags, and formats — plus four curated writes: add/remove tags, set a book's owned format, and record a loan. The guardrails matter as much as the tools:

- **Private by default.** Tool output is assembled from an explicit allowlist, so your Goodreads *private notes* — and any field added to the schema later — are never exposed to the agent. It is deliberately not a "return every column" dump.
- **Your own notes are read-only.** Your `local notes` are visible to the agent but there's no tool to overwrite them, so prose you wrote yourself stays safe.
- **Writes touch only your local fields** (tags, format, loaned-to) — the ones sync never overwrites. Imports, sync, conflict resolution, and duplicate merges are all off-limits.
- **You approve every action.** MCP clients prompt for confirmation before running any tool; the canonical SQLite catalogue stays the source of truth.

## Commands

```bash
adso init                                  # create the catalogue
adso doctor                                # check setup, suggest next steps
adso import goodreads path/to/export.csv   # first load (alias of sync)
adso sync goodreads path/to/export.csv     # later refreshes (same safe operation)
adso list --status Read --format physical  # browse, with filters
adso search "winter society" --limit 10
adso show GOODREADS_ID
adso edit GOODREADS_ID --format physical --tags "philosophy, medieval"
adso fetch-metadata --limit 200            # descriptions & subjects from Open Library, in batches
adso conflicts                             # list open conflicts (--all shows decided ones too)
adso resolve CONFLICT_ID --accept-incoming # or --keep-local / --set / --ignore / --review-later / --reopen
adso report summary --output reports/summary.md
adso export csv  --output exports/catalogue.csv
adso export json --output exports/catalogue.json
```

`import` and `sync` run the same safe, idempotent operation — the name just reads naturally for the first load versus a later refresh. Both preserve raw import rows and write a conflict report whenever a Goodreads update would overwrite one of your local changes.

## Book covers

Adso can fetch cover art and store it locally beside your database in a `covers/` folder. Covers are enrichment only — never sourced from Goodreads, never part of conflict resolution.

```bash
pip install ".[covers]"
adso fetch-covers                        # fill in missing covers
adso fetch-covers --limit 10 --dry-run   # preview without writing
adso set-cover GOODREADS_ID --url https://example.com/cover.jpg
```

Covers resolve from free public APIs (Open Library, then Apple Books) — no account or key needed — and are fetched automatically after import/sync (pass `--no-covers` to skip). A manual cover is never overwritten by an automatic fetch.

## Optional & experimental

These work but sit outside the core v1 surface:

- **Local web UI** — `pip install ".[web]"`, then `adso serve` opens a browser view over the same catalogue (visual conflict resolution, import, activity, reports, local-field editing, and CSV/JSON/Notion export). Early preview; a proper write-up comes with v2.
- **Configuration profiles** — `adso config init` lets you bundle a database path and connector settings under named profiles and switch with `--profile`. Handy if you keep more than one library.
- **Notion export** — `pip install ".[notion]"` adds `adso export notion`, an optional adapter that mirrors the catalogue into a Notion database. Experimental; the local SQLite catalogue always stays canonical.

## Development

Work from a clone inside an activated virtual environment (see [Installation](#installation)), then install editable with all extras:

```bash
pip install -e ".[web,notion,covers,dev]"   # editable install — code edits take effect immediately
python -m unittest discover -s tests         # run the test suite
ruff check .                                 # lint
```

With an editable install the `adso` command reflects your changes as you edit, so there's no reinstall step. It's still only on your PATH while the venv is activated — run `. .venv/bin/activate` in new terminals, or call `.venv/bin/adso` directly.

CI (`.github/workflows/ci.yml`) runs the tests across Python 3.9–3.13, checks a clean-checkout install, and runs ruff on every push and pull request.

## License

Adso is released under the [MIT License](LICENSE). Copyright (c) 2026 David Whipps.
