# Categorization, auto-tagging and a connected library

## Context

"What should I read next?" is Adso's core question, and answering it well depends on a library that is well categorised and connected. Today the categorisation is thin and split into three unconnected systems:

| What | Where | Source | State today |
|---|---|---|---|
| Exclusive shelf (read / to-read / currently-reading / dnf) | `books.exclusive_shelf`, `reading_status` | Goodreads | Read-only. Filterable. |
| Custom Goodreads shelves (`cozy-fantasy`, `speculative-fiction`) | `books.shelves_json`, lowercased, status shelf included | Goodreads `Bookshelves` column | Stored and full-text searchable only. **No filter, no facet, no edit path.** Overwritten on every sync. |
| Local tags | `books.tags_json`, flat and lowercased (`db.normalize_tags`) | User via CLI, web and MCP | The only editable taxonomy. Sync never touches it. |
| Subjects, places, eras | `subjects_json`, `subject_places_json`, `subject_times_json` | Open Library work record (`metadata.py:_clean_subjects`, capped 25/10/10, original case) | Shown as chips on the book page linking to free-text `/?q=`. No filter, no facet. Noisy vocabulary. |
| Genre, series, form, hierarchy, relationships | none | none | Not modelled. Series is only stripped from titles (`covers.py:294`, `metadata.py:199`). |

What Goodreads gives us: the CSV has **no genres**. It contains only the user's own shelves (`Bookshelves`, plus `Bookshelves with positions`, which is dropped but kept in `raw_import_rows`). The series name is embedded in the title as `(Series, #n)`. Real genre signal has to come from the user's shelves, Open Library subjects, and optionally an LLM.

Recommendations: none exist. The closest things are "More by" and "Also tagged" on the book page, and the latter uses only the first tag (`web/app.py:363-381`). There are no LLM calls in the codebase. The prototype (`prototypes/catalogue/build_data.py:68`) blindly merges shelves into tags.

**Decisions (from Q&A):**
- Hybrid engine: deterministic rules first, optional Claude API for the gaps.
- Faceted taxonomy with a genre hierarchy: exactly one primary genre per book, plus secondaries.
- Goodreads shelves act as evidence for suggestions and are never copied silently. The user confirms a shelf-to-category mapping once, and it then applies automatically.

## Design principles

1. **Everything the machine proposes is a suggestion until a human accepts it.** Accepted assignments are local data, and sync, re-runs or re-fetches never overwrite them. This mirrors the `LOCAL_FIELDS` and `sync_conflicts` philosophy already in the codebase.
2. **Provenance on every assignment**: `source` (user / rule / shelf-map / subjects / llm), `confidence`, and `evidence` (for example "shelf: cozy-fantasy", "OL subject: Space opera"), so the user can see *why*.
3. **Rejections are remembered**, so the same suggestion never comes back.
4. **Teach once, apply many**: accepting a mapping ("shelf `cozy-fantasy` means genre Fantasy > Cozy + mood cozy") creates a rule that auto-applies to future books, including new syncs. This is the main lever for low-effort management.
5. **The taxonomy belongs to the user**. It is seeded with a sensible default tree that they can rename, merge, re-parent or delete, with changes cascading safely.

## Data model (new tables, via a new idempotent migration in `db.py`)

Follow the existing `_migrate_*` pattern (`db.py:244-250`). Keep `tags_json` as it is for back-compat, and model tags as the `theme` facet in the new tables (see migration below).

```sql
-- Controlled vocabulary: one row per category node, across all facets.
CREATE TABLE categories (
  id INTEGER PRIMARY KEY, facet TEXT NOT NULL,          -- form | genre | theme | setting | era | audience
  slug TEXT NOT NULL, label TEXT NOT NULL,
  parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,   -- hierarchy (genre mainly)
  description TEXT, status TEXT NOT NULL DEFAULT 'active',          -- active | proposed | merged
  merged_into_id INTEGER REFERENCES categories(id),
  created_at ..., UNIQUE(facet, slug));
CREATE TABLE category_aliases (category_id, alias TEXT, PRIMARY KEY(alias, category_id));  -- "sci-fi", "science fiction"

-- Accepted assignments (human-confirmed or auto-applied by an accepted rule).
CREATE TABLE book_categories (
  book_id, category_id, role TEXT NOT NULL DEFAULT 'secondary',     -- primary | secondary
  source TEXT NOT NULL, rule_id INTEGER NULL, confidence REAL, evidence_json TEXT,
  created_at ..., PRIMARY KEY(book_id, category_id));
CREATE UNIQUE INDEX one_primary_genre ON book_categories(book_id) WHERE role='primary';

-- Review queue (the human-in-the-loop surface).
CREATE TABLE category_suggestions (
  id, book_id, category_id NULL, proposed_label TEXT NULL, facet, role,
  source, confidence REAL, evidence_json, status TEXT DEFAULT 'pending',   -- pending | accepted | rejected | superseded
  run_id, created_at, decided_at, UNIQUE(book_id, facet, COALESCE(category_id, proposed_label)));

-- Learned mappings: shelf / OL subject / alias -> category.
CREATE TABLE category_rules (
  id, match_kind TEXT,         -- goodreads_shelf | ol_subject | tag
  match_value TEXT, category_id, role, auto_apply INTEGER DEFAULT 1, created_at);

-- Book-to-book relationships.
CREATE TABLE series (id, name UNIQUE);
CREATE TABLE book_series (book_id PRIMARY KEY, series_id, position REAL);
CREATE TABLE book_relations (book_a, book_b, kind TEXT,  -- same_series | companion | responds_to | similar | read_after
  source, confidence, status, PRIMARY KEY(book_a, book_b, kind));
```

**Primary vs secondary:** the `genre` facet has at most one `primary` per book, enforced by the partial unique index, plus any number of secondaries. Other facets are multi-valued and have no primary. Filtering by a parent genre rolls up its descendants through a recursive CTE, so "Fantasy" includes "Fantasy > Cozy".

**Default seed taxonomy** (`src/adso/taxonomy_seed.py`, small and editable):
- **Form:** fiction, nonfiction, poetry, graphic, drama.
- **Genre:** about 12 top-level genres with 2 to 5 children each (for example Speculative > Science Fiction > Space Opera; Literary; Mystery & Crime; History; Philosophy; Science; Memoir & Biography). Aliases come from common Goodreads shelf names and Open Library subject strings.
- **Theme / mood:** open vocabulary. Existing tags migrate here.
- **Setting and era:** derived from `subject_places` and `subject_times`, normalised through aliases.

## Suggestion engine (`src/adso/categorize.py`)

Each book goes through a pipeline of pluggable "suggesters". Each suggester returns `(facet, category | proposed_label, role, confidence, evidence)`:

1. **RuleSuggester** (deterministic, offline): applies `category_rules` to `shelves_json` (minus status shelves), `subjects_json` and existing tags. It also does alias matching against `category_aliases`. Accepted rules with `auto_apply` write straight to `book_categories` with `source='rule'`. Everything else goes to the queue.
2. **ShelfDiscovery**: when a custom shelf has no rule yet, it proposes a *mapping* rather than per-book suggestions ("Map shelf `cozy-fantasy` (14 books) to Genre: Fantasy > Cozy?"). One decision covers many books.
3. **SubjectSuggester**: Open Library subjects are noisy, so it uses frequency across the library plus alias matching, with lower confidence.
4. **SeriesExtractor**: parses `(Series, #n)` from the raw Goodreads title (reusing the regex from `covers.py`/`metadata.py`) and fills `series` and `book_series`, which creates `same_series` relations. It is high confidence, and auto-accept is configurable.
5. **LLMSuggester** (optional, used only when `ANTHROPIC_API_KEY` is set and the `[ai]` extra is installed):
   - Sends batches of books (title, author, year, description, subjects, shelves) together with the **current taxonomy** as a constrained schema, via structured output or a tool call.
   - Asks for: a primary genre and up to 3 secondaries, form, up to 5 themes, and any *proposed new categories* with a justification.
   - Proposed new categories land as `categories.status='proposed'` and need approval before any book uses them. This keeps the taxonomy from sprawling.
   - Prompt caching on the taxonomy block; a cheap default model; `--dry-run` with a cost estimate.
   - The model is only asked about books that the rules left without a primary genre.

**Confidence policy:** auto-accept happens only through rules the user has approved (and series, if configured). Everything else waits in the queue. Pending suggestions are ranked by (books affected × confidence) so that the highest-leverage decisions come first.

**Triggering:** `adso categorize` can be run manually. It also runs automatically at the end of sync, after metadata fetch, in the same place as the covers and metadata hooks (`cli.py:277-288`), but the automatic run uses rules only and never calls the LLM unless `--ai` or a config flag is set. New books from Goodreads therefore get categorised by learned rules immediately, and anything left over waits in the queue.

## Human-in-the-loop surfaces

**CLI** (`cli.py`):
- `adso categorize [--ai] [--limit] [--dry-run] [--book ID]`
- `adso review` walks the queue interactively (accept / reject / change / skip / "always map this shelf"), similar to the existing conflicts flow in `conflicts.py`.
- `adso taxonomy list | add | rename | move | merge | delete | alias`
- `adso rules list | delete`
- `adso edit ID --genre "Fantasy > Cozy" --also "Literary" --theme "grief"`
- `list` and `search` gain `--genre`, `--category`, `--series` and `--gr-shelf` filters.

**Web** (`web/app.py`, `web/library.py`, templates):
- **Review page** (`/review`): grouped cards. Shelf-mapping proposals come first, then bulk proposals ("apply Science Fiction to these 9 books"), then single-book suggestions. Each card shows its evidence chips, with accept, reject and edit buttons plus a "remember as rule" toggle. The badge count sits in the sidebar.
- **Book page**: a primary genre pill, secondary pills and theme chips, each showing its source icon (user / rule / AI). Includes inline change and remove, and an "Also in this series" strip.
- **Sidebar**: a genre tree with roll-up counts replaces the flat tag list. Custom Goodreads shelves become a filterable facet at last, and subjects become facets too, not just free-text links.
- **Taxonomy page** (`/taxonomy`): rename, drag to re-parent, merge (which rewrites assignments and rules and keeps the old slug as an alias), with the affected book count shown before confirming.

**MCP** (`mcp_server.py`): new tools
- `list_taxonomy`
- `categorize_book` (writes *suggestions* with `source='agent'`, never direct assignments)
- `list_suggestions`
- `review_suggestion` (accept or reject; only when the user explicitly asks the agent to)
- `books_by_category`
- `related_books`

`list_facets` is extended with genres, series and custom shelves, and `AGENT_BOOK_FIELDS` gains `genres`, `primary_genre` and `series`. This lets Claude Desktop act as the classifier without an API key in Adso, which complements the built-in path.

## Toward "what should I read next" (phase 3, built on this graph)

- `related_books(book)`: scores books by shared primary and secondary genre (weighted by hierarchy distance), shared themes, same author, same series (next unread position), and relations.
- `adso next` and the web **Next up** panel rank the to-read pile against a *taste profile* built from high-rated read books' categories. Each suggestion comes with an explanation ("because you rated 5★ three Space Opera books; next in series *The Expanse*"). There are also "paths": unexplored neighbour genres next to strongly rated ones.
- Insight views: genre distribution of read vs to-read (to spot a backlog skewed away from what you actually finish), and DNF rate by genre.

## Delivery phases (separate PRs)

1. **Foundation** (done): tables, seed taxonomy, `categorize.py` (rules, shelf/subject/tag proposals, series from titles, provisional primary genres), CLI (`categorize`, `review`, `taxonomy`, `edit --genre/--add-category/--remove-category/--series`), `--category`/`--gr-shelf`/`--series` filters, and categorisation after CLI and web syncs. Where it departs from the design above:
   - Tags stay in `tags_json` rather than moving into the theme facet. They feed proposals like shelves do, and the move to the theme facet is left for phase 2, alongside the web and MCP surfaces that read tags.
   - Setting and era (`subject_places`, `subject_times`) are not mapped yet. The facets are form, genre, audience and theme.
   - `book_relations` is left for phase 4. Series is in.
   - Category labels are not in the FTS index yet.
   - Review is list-then-decide (`adso review`, `adso review ID --accept/--as/--reject/--reopen`), matching `conflicts`/`resolve`, rather than an interactive walk.
2. **Web and MCP** (done). Delivered:
   - Library sidebar: a genre tree, themes, and custom Goodreads shelves, plus series filtering.
   - Book block: category chips showing where each came from, with add, remove and make-primary, a series line, and an "In the series" strip.
   - Review: a Categories section with grouped cards.
   - A `/taxonomy` Categories page.
   - MCP: category fields and filters, plus `list_taxonomy`, `list_category_suggestions`, `suggest_categories` (new `assign` suggestion kind, queue-only) and `review_category_suggestion`.
   - API filters and `/api/taxonomy`.
   - CSV/JSON export columns.

   Not done:
   - The Notion export is unchanged: writing a Genre property would fail on databases that lack it. This needs an opt-in.
   - Subjects are not sidebar facets yet.

   Follow-ups from testing on the real library (same PR):
   - Custom Goodreads shelves are not a sidebar taxonomy. They fold into genres (via mappings) and **tags**: an unmatched shelf is proposed as a tag rule (`category_rules.tag`), applied once per book (`tag_rule_applications`).
   - Subject-only nonfiction genres on fiction become grouped `assign` questions.
   - Seed revision 2 removes the "war", "military" and "juvenile fiction" aliases.
   - Aliases can be removed.
   - Pending proposals are revalidated on each run.
   - Examples show the most recently added books.
3. **AI suggester**: the `[ai]` optional extra with the `anthropic` SDK, batching, prompt caching, cost dry-run, and proposed-category approval.
4. **Relationships and next reads**: `related_books`, the taste profile, `adso next`, the web panel, and insight views.

## Critical files

- `src/adso/db.py`: the migration, new tables, write helpers (`assign_category`, `set_primary_genre`, `merge_categories`), and `row_to_catalogue_dict` extended with genres and series.
- `src/adso/categorize.py` (new) and `src/adso/taxonomy_seed.py` (new).
- `src/adso/catalogue.py`: `BookFilters` gains `genre` (roll-up CTE), `gr_shelf` and `series`, plus facet helpers alongside `distinct_tags()`.
- `src/adso/cli.py`: new commands and the post-sync hook (next to `cli.py:277-288`).
- `src/adso/sync.py`: no field changes. It triggers rules for new or changed books only.
- `src/adso/mcp_server.py`, `src/adso/web/app.py`, `src/adso/web/library.py`, templates, `src/adso/exports.py`, `src/adso/notion.py`.
- Reuse: `normalize_tags`, `update_local_fields` (validation style), the conflict-review UX in `conflicts.py`, the title-series regex in `metadata.py`/`covers.py`, and `_migrate_search_fts` (add genre labels to the FTS index, which needs a migration bump per the note at `db.py:68-73`).

## Verification

- `python -m unittest discover -s tests -v` and `ruff check .`.
- New `tests/test_categorize.py` covers:
  - Seed and migration idempotency on an existing DB.
  - Shelf rules auto-applying on a second sync.
  - Rejected suggestions not reappearing.
  - The one-primary constraint.
  - A parent-genre filter rolling up children.
  - Merge rewriting assignments and rules.
  - Series parsing.
  - The LLM suggester with a mocked client, never writing assignments directly.
- Extend `test_mcp_server.py` and the web tests (with `httpx` installed) for the new tools and routes.
- End to end: `adso import examples/goodreads_sample.csv`, then `adso categorize --dry-run`, `adso review`, then re-sync the same CSV and confirm the accepted mappings auto-apply with no new queue items. Then run `adso web` and check the review page, genre tree and book pills.
