# Changelog

All notable changes to Adso are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Goodreads covers**: `fetch-covers` now tries the book's own public Goodreads page first (the `.xml` variant of the URL, which isn't behind Goodreads' bot check), then Goodreads search suggestions, accepting only an exact Goodreads ID match. Open Library and Apple Books remain as fallbacks. Goodreads' "no photo" placeholder is ignored. On a real 1,067-book library this found all 138 covers that were previously `not_found`. Run `adso fetch-covers --refresh` to replace earlier title-search covers with the exact edition's cover. Manual covers are still never touched.
- **Goodreads auto-sync on macOS** (`adso service install-sync|uninstall-sync`,
  `adso goodreads ingest|open|remind`): a LaunchAgent watches `~/Downloads` (or
  `--watch-dir`), and when a `goodreads_library_export*.csv` lands it backs up the
  catalogue, files the CSV under `exports/goodreads/goodreads-YYYY-MM-DD.csv`, runs
  the usual safe sync and posts a notification with the counts. A weekly reminder
  (`--day`/`--hour`, or `--no-reminder`) prompts the one manual step, clicking
  **Export Library**. Goodreads rejects headless browsers (HTTP 403), so Adso
  doesn't try to click it for you. An export downloaded *before* the last
  Goodreads sync is filed away but not synced, so an old file lying in Downloads
  can't bring back outdated Goodreads values. An export byte-identical to the
  one last synced is moved to the Trash instead of being synced and filed again.
- **Always-on web UI on macOS** (`adso service install|status|restart|uninstall`):
  a per-user LaunchAgent keeps `adso serve` running at `http://127.0.0.1:8420`
  (starts at login, restarts on crash). The web UI now ships a favicon, an
  apple-touch-icon and a web app manifest, so Safari's **File → Add to Dock** turns
  it into a standalone Adso app.
- **MCP server** (`adso mcp`): serve the catalogue to LLM agents (Claude Desktop,
  Claude Code) over stdio. Eight tools — `search_books`, `get_book`,
  `library_stats`, `list_facets`, plus curated writes `add_tags`, `remove_tags`,
  `set_format`, `set_loaned`. Output is built from an explicit allowlist so
  `private_notes` (and any future column) is never exposed; writes reach only the
  local fields sync never overwrites, and `local_notes` stays read-only. Install
  with the optional extra: `pip install -e '.[mcp]'`. See `docs/agents/mcp.md`.
- Web UI: edit your **local catalogue fields** inline on a book's page —
  local-only, never synced to Goodreads.
- Conflict decisions now support **ignore** and **review-later** alongside
  keep-local / accept-incoming / custom, and any decision can be **reopened**.
- Every decision is recorded in an append-only audit trail with provenance
  (which interface decided), surfaced in `adso conflicts --all`, the conflict
  report, and the web UI's "Recently decided" section.
- `adso resolve` gains `--ignore`, `--review-later`, and `--reopen`.
- `show` and the CSV/JSON exports now surface the publisher, binding, page count,
  and publication years that were already imported and synced.
- Web UI **Export** page: download the catalogue as CSV or JSON, and run a
  Notion export with a dry-run preview before writing.
- Web UI **report views** (`/reports/summary`, `/reports/conflicts`) and a
  **latest-sync status card** on the Activity page linking to them.
- `exports.catalogue_csv_string` / `catalogue_json_string` (in-memory
  serializers reused by the file exports and the web downloads).
- **Open Library metadata enrichment**: `adso fetch-metadata` (and an automatic
  pass after import/sync, opt out with `--no-metadata`) fetches work
  descriptions, subjects, and place/time facets — shown on the book page as a
  synopsis plus searchable badges, included in exports (`subjects` joins the
  CSV; JSON carries everything). Books the Goodreads CSV left without ISBNs get
  them backfilled from the matched Open Library edition, and a sync guard now
  ensures an empty incoming Goodreads value never erases stored data.
- **Local tags**: group books your own way (e.g. `philosophy`) with
  comma-separated tags on the book page or `adso edit --tags`. Tags are
  local-only (never synced), searchable, filterable in the catalogue and via
  `adso list/search --tag` / `/api/books?tag=…`, included in exports, and sent
  to Notion as a `Tags` multi-select.

### Changed
- **Local catalogue fields simplified** to `format` (physical / ebook /
  audiobook — set means owned, empty means not owned), `tags`, `loaned_to`, and
  `local_notes`. The `owned`, `copy_count`, `location`, and `shelf_box` columns
  are dropped from the schema (existing catalogues migrate automatically; any
  values in the dropped columns are discarded). The catalogue's owned/location
  filters, `adso list/search --owned/--location`, and
  `adso edit --owned/--copy-count/--location/--shelf-box` are replaced by
  `--format`, and the Notion export now writes a `Format` select property
  instead of Owned / Location / Shelf-Box.
- `import` and `sync` are now documented as the same safe, idempotent operation,
  and `import` writes a conflict report just like `sync` (previously it could
  record conflicts silently).
- Search now uses a persistent FTS5 index maintained by triggers instead of
  rebuilding the index from scratch on every query.

### Removed
- Dropped the legacy `goodreads_to_notion.py` compatibility shim; use the `adso`
  CLI instead.

## [0.1.0] - 2026-06-05

First tagged release — a self-hosted technical preview. Clone it, install it, and
run it against your own Goodreads export.

### Added
- Local-first CLI (`adso`) backed by a canonical SQLite catalogue.
- Goodreads CSV import and sync, preserving raw import rows and protecting local
  physical-library fields, with conflict reporting instead of silent overwrites.
- Catalogue commands: `init`, `doctor`, `import`, `sync`, `list`, `search`,
  `show`, `edit`, `conflicts`, `resolve`, `report`, and `export`.
- Exports to CSV and JSON; optional Notion export adapter (`adso export notion`,
  `[notion]` extra) that reads from SQLite as the source of truth.
- Optional local web UI (`adso serve`, `[web]` extra) over the same catalogue,
  including visual conflict resolution, browser import, and an activity view.
- Packaging for self-hosted installs: MIT `LICENSE`, license metadata and a
  PEP 517 build backend in `pyproject.toml`, and a pinned `requirements-lock.txt`
  for reproducible installs.

[Unreleased]: https://github.com/davidwhipps/adso/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/davidwhipps/adso/releases/tag/v0.1.0
