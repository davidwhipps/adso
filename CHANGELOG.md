# Changelog

All notable changes to Adso are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **A starter taxonomy built for readers, not bookshops.** The genre tree is
  flat and descriptive: Psychological Fiction, Novel of Ideas, Family Saga,
  Coming of Age, Social Novel, Satire & Comic Fiction, Autofiction,
  Experimental & Metafiction, Historical, War, Political & Dystopian, Magical
  Realism & Fabulism, Myth & Retellings, Love Stories, Crime & Mystery,
  Thriller & Espionage, Science Fiction, Fantasy, Horror & Gothic and
  Adventure, plus a reworked nonfiction list (Religion & Mythology, Science &
  Nature, Writing & Literature, Travel & Place…). Speculative Fiction,
  Literary Fiction and Classics are gone, and subgenres are left to you.
  - New **Tradition** facet (American, British & Irish, Continental European,
    Russian, Latin American, Japanese & East Asian, Translated), proposed from
    Open Library subjects such as "Russian literature" or "Translations into
    English". "Romance literature" (the Romance languages) now maps here, not
    to a romance genre.
  - New **Era** facet, filled in automatically from the original publication
    year. An era you set by hand wins; one you remove stays removed.
  - Themes seeded from what books are about: Family, Love & Marriage,
    Friendship, Grief & Loss, War (World War I, World War II) and Writers &
    Artists. WWII is a theme, so it covers novels and history alike.
  - Forms gain Diaries & Letters.
  - Existing catalogues are reshaped once, on upgrade: seed categories are
    renamed or moved into the new tree, unused old ones are removed, and any
    you used (a rule or a book you filed) stay. Suggestions are then rebuilt
    against the new tree.
- Open Library subjects that describe a list or an edition ("New York Times
  reviewed", reading levels, syllabus projects) are ignored for suggestions
  and recommendation reasons.
- Custom did-not-finish shelves (`attempted`, `abandoned`, `gave-up`) count as
  DNF and are never proposed as tags. A `shortlist` shelf or tag nudges a
  book up in Next up.
- Nothing maps to an era: Era is left out of "Accept as" and mapping-rule
  pickers, subjects such as "19th century" aren't proposed as eras, and a rule
  or "accept as" pointing at an era is refused. You can still set one book's
  era by hand.
- Review cards explain a decision that can't go through instead of doing
  nothing: a card that was already settled (for example withdrawn when
  another decision re-checked the queue) collapses with the reason, and a
  refused accept shows its error on the card. Accepting a leftover
  "subject -> Era" card is refused, and era mapping rules made before era
  became automatic are removed so the publication year applies again.
- Pick lists show categories in use first. The library sidebar gains
  Traditions and Eras, and Next up can filter by them.
- New directions finds neighbours of top-level genres through the books they
  share.

### Added
- **What to read next** (phase 4): `adso next`, `adso related`, `adso insights`,
  a web **Next up** page, a **Related** strip on book pages, and MCP tools
  (`recommend_next`, `explore_paths`, `related_books`, `reading_insights`).
  - Your to-read shelf is ranked against a taste profile built from your own
    ratings (DNFs count against) across genres, themes, tags, subjects and
    authors. Rare and specific features weigh more, and a genre needs a few
    books behind it before it counts fully.
  - Series order: the next unread book moves up, and skipping ahead waits.
  - Owned books and strong community ratings get a nudge, and a variety pass
    keeps one genre from filling the list.
  - Every pick carries plain-language reasons.
  - "New directions" suggests unread neighbours of genres you love. Insights
    show reading by genre and where your pile leans. Book pages replace
    "Also tagged" with "Related".
- **Categories in the web UI, MCP and exports** (phase 2).
  - Library sidebar: a genre tree with roll-up counts, and themes. The library
    also filters by series, in reading order.
  - Book page and book sidebar: categories as chips grouped by facet, showing
    where each came from, with add, remove and "make primary". A series line,
    and an "In the series" strip on the full page.
  - Review page: a Categories section with one card per target. Each card
    offers accept, reject, "map to another category", and rejecting a single
    source; primary-genre questions offer the book's genres as buttons. The
    Review badge counts open cards.
  - A new Categories page (More → Categories) to add, rename, move, merge,
    alias and delete categories, and to add or remove mapping rules. Merges
    and deletes confirm their impact first.
  - MCP:
    - Books now carry `primary_genre`, `categories` and `series`.
    - `search_books` filters by `category`, `goodreads_shelf` and `series`.
    - `list_facets` lists the categories in use, your custom shelves and series.
    - New tools: `list_taxonomy`, `list_category_suggestions`,
      `suggest_categories` and `review_category_suggestion`. Agent proposals only
      reach the review queue, credited to the agent with its reason.
  - `/api/books` gains the same filters and fields, and there's a new
    `/api/taxonomy`.
  - CSV and JSON exports include primary genre, categories, series and series
    position. The Notion export is unchanged, since a missing "Genre" property
    in the target database would fail every export.
  - Goodreads shelves fold into genres and tags rather than being a taxonomy of
    their own.
    - A shelf that isn't a genre is proposed as a tag of the same name. Once
      accepted, it tags those books, and new books on that shelf after each
      sync. Each book is tagged once, so removing the tag sticks.
    - `adso taxonomy map --to tag:NAME` makes such a rule by hand.
    - The Import page explains how shelves are used.
  - Open Library subjects alone no longer put a novel in a nonfiction genre
    (History, Biography, Science and so on). Those become one grouped "is it
    really History?" question per genre.
  - The over-broad aliases "war", "military" (Military History) and
    "juvenile fiction" (Children's) are removed, including from existing
    catalogues. Aliases can now be removed with `adso taxonomy unalias` or on
    the Categories page. Open suggestions are re-checked whenever aliases
    change.
  - Suggestion examples show your most recently added books.

### Changed
- **Less manual categorisation review.** On a 1,000-book test library this cut review
  items from 71 to 37, mapping decisions from 50 to 23, and primary-genre questions
  from 152 to 53.
  - `adso review` groups proposals that point at the same category into one card, so
    a shelf and every Open Library spelling of that genre are one decision. `--only`
    acts on a single source.
  - Primary genres are picked automatically when a book's own shelves settle it, or
    when its competing genres share a parent (Cyberpunk + Space Opera → Science
    Fiction). Automatic picks stay provisional and can be overridden.
  - Categories whose accepted mapping was deleted are asked about again.
  - Date and book-club shelves are no longer proposed.
  - Common shelf spellings (lit-fic, whodunnit, popsci, ww2, hard-sf) now match
    genres.

### Added
- **Categories, reviewed suggestions and series** (`adso categorize`, `adso review`,
  `adso taxonomy`). Books are organised along facets (form, genre, audience,
  theme). Genre is a hierarchy with one primary genre per book. A starter
  taxonomy is seeded once and is then the user's to rename, move, merge and alias.
  Custom Goodreads shelves, Open Library subjects and tags become mapping
  proposals. Accepting one creates a rule that applies across the library and to
  books arriving in later syncs. Rejections are remembered, hand edits are never
  overridden, and categories removed from a book stay removed. Primary genres are
  set when unambiguous and raised for review when not. Series and reading order
  are parsed from Goodreads titles. `adso list`/`search` gain `--category`
  (includes subcategories), `--gr-shelf` (any Goodreads shelf) and `--series`
  (reading order). `adso edit` gains `--genre`, `--add-category`,
  `--remove-category`, `--series` and `--series-position`. `adso show` prints
  categories and series. Categorisation runs, local only, after every CLI and
  web sync.
- **Redesigned library web UI.** One library page replaces the Catalogue and To Read
  pages. A sidebar lists the whole library, shelves, smart views
  (Loaned out, Read but unrated, Recently added, Five stars, Untagged) and tags, each
  with the count clicking it would show, plus a row of tag buttons. Three views:
  a masonry cover grid at each cover's real shape (with a size slider and optional
  titles), a sortable table, and a wall of the whole library on one screen. Sort by
  title, author, date added, rating, year or date read. Clicking a book opens it in a
  sidebar where tags, format, loan and notes save as you edit; it expands to a
  redesigned full book page with previous/next and "more by this author" rows.
  Select several books (click **Select**, `x`, cmd- or shift-click, or table
  checkboxes) to add a tag or set the format in one go. Keyboard: `/` search,
  `h j k l` move, `↵` open, `f` full page, `1 2 3` switch view, `?` for the list.
  New look throughout: warm palette with chartreuse accents, Fraunces and
  Instrument Serif type, a blackletter logotype, dark mode (◐), and view
  transitions between states. Fonts ship with the package (SIL OFL), so it all
  works offline. `/to-read` now redirects to the To read shelf. Rating and shelf
  stay read-only; they come from Goodreads.
- **Cover thumbnails** for the library's wall and table and the book page's
  "more by" rows: `/covers/{id}?size=thumb` serves a small JPEG (360px long edge)
  cached in `covers/.thumbs/` and rebuilt when a cover changes. Made with macOS
  `sips` (or Pillow, if installed); elsewhere the full cover is served as before.
  Covers already 360px or smaller, and small originals that a thumbnail wouldn't
  shrink, are served as they are. On a 1,086-book library the wall drops from
  about 208 MB of images to 32 MB.
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
