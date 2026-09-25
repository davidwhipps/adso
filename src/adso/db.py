"""SQLite persistence for Adso."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import AdsoError
from .taxonomy_seed import seed_taxonomy

GOODREADS_FIELDS = (
    "title",
    "author",
    "additional_authors",
    "isbn10",
    "isbn13",
    "publisher",
    "binding",
    "number_of_pages",
    "year_published",
    "original_publication_year",
    "rating",
    "average_rating",
    "reading_status",
    "exclusive_shelf",
    "shelves_json",
    "date_read",
    "date_added",
    "my_review",
    "private_notes",
    "read_count",
    "owned_copies",
)

LOCAL_FIELDS = (
    "format",
    "tags_json",
    "loaned_to",
    "local_notes",
)

# Allowed values for the local `format` field. A set format means "I own this
# book in that form"; NULL means not owned. Kept deliberately separate from the
# Goodreads `binding` field, which describes the catalogued edition rather than
# what's actually on the shelf.
VALID_FORMATS = ("physical", "ebook", "audiobook")


def normalize_tags(raw: str | Iterable[str] | None) -> list[str]:
    """Parse user tag input into the canonical stored form.

    Accepts a comma-separated string or an iterable of tags; lowercases, trims,
    and dedupes (keeping first occurrence) so "Philosophy" and "philosophy "
    are one group, mirroring how Goodreads shelves are normalised on import.
    """
    if raw is None:
        return []
    parts = raw.split(",") if isinstance(raw, str) else raw
    tags: list[str] = []
    for part in parts:
        tag = " ".join(str(part).lower().split())
        if tag and tag not in tags:
            tags.append(tag)
    return tags

# Columns indexed for full-text search. These back both the persistent FTS5
# index (see _migrate_search_fts) and the LIKE fallback in adso.catalogue, so
# the two search paths always cover the same fields. Every entry must be a real
# column on `books`. Changing this tuple changes the FTS schema: an existing
# books_fts built from the old columns won't pick up the change until it is
# dropped and rebuilt, so bump the migration if you edit it.
SEARCH_FIELDS = (
    "title",
    "author",
    "additional_authors",
    "isbn10",
    "isbn13",
    "publisher",
    "binding",
    "reading_status",
    "exclusive_shelf",
    "shelves_json",
    "my_review",
    "private_notes",
    "tags_json",
    "loaned_to",
    "local_notes",
    "subjects_json",
    "subject_places_json",
    "subject_times_json",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI may run a single request's dependency and
    # endpoint on different threadpool threads, so the per-request connection has
    # to cross threads. It is still only ever used by one thread at a time (never
    # shared between concurrent requests), so disabling the guard is safe.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets a reader (e.g. the web UI) and a writer (e.g. a cover fetch) work
    # the same file concurrently without "database is locked"; busy_timeout makes
    # any remaining contention wait rather than fail. WAL is persisted on the file.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_path TEXT NOT NULL,
            mode TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            row_count INTEGER NOT NULL DEFAULT 0,
            created_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            unchanged_count INTEGER NOT NULL DEFAULT 0,
            conflict_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goodreads_id TEXT UNIQUE,
            title TEXT NOT NULL,
            author TEXT,
            additional_authors TEXT,
            isbn10 TEXT,
            isbn13 TEXT,
            publisher TEXT,
            binding TEXT,
            number_of_pages INTEGER,
            year_published INTEGER,
            original_publication_year INTEGER,
            rating INTEGER,
            average_rating TEXT,
            reading_status TEXT,
            exclusive_shelf TEXT,
            shelves_json TEXT NOT NULL DEFAULT '[]',
            date_read TEXT,
            date_added TEXT,
            my_review TEXT,
            private_notes TEXT,
            read_count INTEGER,
            owned_copies INTEGER,
            format TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            loaned_to TEXT,
            local_notes TEXT,
            cover_path TEXT,
            cover_source TEXT,
            cover_source_url TEXT,
            cover_status TEXT,
            cover_fetched_at TEXT,
            description TEXT,
            subjects_json TEXT NOT NULL DEFAULT '[]',
            subject_places_json TEXT NOT NULL DEFAULT '[]',
            subject_times_json TEXT NOT NULL DEFAULT '[]',
            metadata_source TEXT,
            metadata_source_url TEXT,
            metadata_status TEXT,
            metadata_fetched_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_goodreads_import_run_id INTEGER,
            FOREIGN KEY(last_goodreads_import_run_id) REFERENCES import_runs(id)
        );

        CREATE TABLE IF NOT EXISTS source_snapshots (
            book_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            field_name TEXT NOT NULL,
            field_value TEXT,
            import_run_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(book_id, source, field_name),
            FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS raw_import_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_run_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            source_record_id TEXT,
            row_index INTEGER NOT NULL,
            raw_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sync_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_run_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_source_value TEXT,
            local_value TEXT,
            incoming_value TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            resolution TEXT,
            resolved_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(import_run_id) REFERENCES import_runs(id) ON DELETE CASCADE,
            FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS duplicate_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL,
            book_id INTEGER NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(group_key, book_id),
            FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
        );

        -- Append-only audit trail of every decision taken on a conflict. The
        -- current state is denormalized onto sync_conflicts (status/resolution);
        -- this table is the full, ordered history with provenance.
        CREATE TABLE IF NOT EXISTS conflict_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conflict_id INTEGER NOT NULL,
            decision TEXT NOT NULL,
            resulting_value TEXT,
            actor TEXT NOT NULL DEFAULT 'cli',
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(conflict_id) REFERENCES sync_conflicts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_conflict_decisions_conflict
            ON conflict_decisions(conflict_id, id);
        """
    )
    _migrate_sync_conflicts(conn)
    _migrate_book_covers(conn)
    _migrate_conflict_decisions(conn)
    _migrate_local_fields_format(conn)
    _migrate_local_tags(conn)
    _migrate_book_metadata(conn)
    _migrate_search_fts(conn)
    _migrate_categories(conn)
    conn.commit()


def _migrate_sync_conflicts(conn: sqlite3.Connection) -> None:
    """Add resolution columns to sync_conflicts tables created before v2."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sync_conflicts)")}
    if "resolution" not in existing:
        conn.execute("ALTER TABLE sync_conflicts ADD COLUMN resolution TEXT")
    if "resolved_at" not in existing:
        conn.execute("ALTER TABLE sync_conflicts ADD COLUMN resolved_at TEXT")


def _migrate_book_covers(conn: sqlite3.Connection) -> None:
    """Add cover-art enrichment columns to books tables created before covers."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    for column in ("cover_path", "cover_source", "cover_source_url", "cover_status", "cover_fetched_at"):
        if column not in existing:
            conn.execute(f"ALTER TABLE books ADD COLUMN {column} TEXT")


# Maps an already-resolved conflict's `resolution` keyword to the field whose
# stored value became the active one, so a backfilled audit row can record the
# resulting value the same way a fresh decision would.
_RESOLUTION_RESULT_COLUMN = {
    "kept_local": "local_value",
    "accepted_incoming": "incoming_value",
}


def _migrate_conflict_decisions(conn: sqlite3.Connection) -> None:
    """Seed the audit trail for conflicts resolved before it existed.

    The table itself is created in ``initialize``. On an upgraded catalogue the
    audit history is empty while resolved conflicts already carry a resolution;
    backfill one ``legacy`` decision row per such conflict so the trail is
    complete. The "empty audit + resolved conflicts" state only occurs once, so
    this is naturally idempotent — every decision made from now on writes its own
    row (see record_conflict_decision).
    """
    already_seeded = conn.execute("SELECT 1 FROM conflict_decisions LIMIT 1").fetchone()
    if already_seeded:
        return
    resolved = conn.execute(
        """
        SELECT id, resolution, local_value, incoming_value, resolved_at
        FROM sync_conflicts
        WHERE status = 'resolved' AND resolution IS NOT NULL
        """
    ).fetchall()
    for row in resolved:
        result_column = _RESOLUTION_RESULT_COLUMN.get(row["resolution"])
        resulting_value = row[result_column] if result_column else None
        conn.execute(
            """
            INSERT INTO conflict_decisions
                (conflict_id, decision, resulting_value, actor, created_at)
            VALUES (?, ?, ?, 'legacy', ?)
            """,
            (row["id"], row["resolution"], resulting_value, row["resolved_at"]),
        )


def _migrate_local_fields_format(conn: sqlite3.Connection) -> None:
    """Replace owned/copy_count/location/shelf_box with a single format column.

    The local panel was simplified to format/loaned_to/local_notes; data in the
    dropped columns is discarded by design. The FTS triggers reference the old
    columns and SQLite refuses to drop a column a trigger mentions, so the index
    and triggers go first — _migrate_search_fts (which runs after this) rebuilds
    them from the current SEARCH_FIELDS.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    to_drop = [c for c in ("owned", "copy_count", "location", "shelf_box") if c in existing]
    if "format" in existing and not to_drop:
        return
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS books_fts_ai;
        DROP TRIGGER IF EXISTS books_fts_ad;
        DROP TRIGGER IF EXISTS books_fts_au;
        DROP TABLE IF EXISTS books_fts;
        """
    )
    if "format" not in existing:
        conn.execute("ALTER TABLE books ADD COLUMN format TEXT")
    for column in to_drop:
        try:
            conn.execute(f"ALTER TABLE books DROP COLUMN {column}")
        except sqlite3.OperationalError as exc:
            raise AdsoError(
                f"Could not migrate the catalogue schema (dropping books.{column}): {exc}",
                hint="Adso needs SQLite 3.35 or newer to upgrade this catalogue.",
            ) from exc


def _migrate_local_tags(conn: sqlite3.Connection) -> None:
    """Add the local tags_json column to catalogues created before tags.

    tags_json joins SEARCH_FIELDS, so the FTS index and triggers must be
    rebuilt over the new column set — drop them here and _migrate_search_fts
    (which runs after this) recreates them.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    if "tags_json" in existing:
        return
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS books_fts_ai;
        DROP TRIGGER IF EXISTS books_fts_ad;
        DROP TRIGGER IF EXISTS books_fts_au;
        DROP TABLE IF EXISTS books_fts;
        """
    )
    conn.execute("ALTER TABLE books ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")


def _migrate_book_metadata(conn: sqlite3.Connection) -> None:
    """Add Open Library metadata columns to catalogues created before them.

    The subject columns join SEARCH_FIELDS, so the FTS index and triggers must
    be rebuilt over the new column set — drop them here and _migrate_search_fts
    (which runs after this) recreates them.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    columns = {
        "description": "TEXT",
        "subjects_json": "TEXT NOT NULL DEFAULT '[]'",
        "subject_places_json": "TEXT NOT NULL DEFAULT '[]'",
        "subject_times_json": "TEXT NOT NULL DEFAULT '[]'",
        "metadata_source": "TEXT",
        "metadata_source_url": "TEXT",
        "metadata_status": "TEXT",
        "metadata_fetched_at": "TEXT",
    }
    missing = {name: ddl for name, ddl in columns.items() if name not in existing}
    if not missing:
        return
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS books_fts_ai;
        DROP TRIGGER IF EXISTS books_fts_ad;
        DROP TRIGGER IF EXISTS books_fts_au;
        DROP TABLE IF EXISTS books_fts;
        """
    )
    for name, ddl in missing.items():
        conn.execute(f"ALTER TABLE books ADD COLUMN {name} {ddl}")


def _migrate_categories(conn: sqlite3.Connection) -> None:
    """Create the category, suggestion and series tables, then seed defaults.

    Categorisation is local data in its own tables rather than JSON columns on
    `books`: sync never touches it, and a book can carry many categories with
    per-assignment provenance. Everything here is CREATE ... IF NOT EXISTS, so
    it is safe on every start and on upgraded catalogues.

    - categories: the user-owned vocabulary, one row per node in a facet
      (form / genre / audience / theme); genre nodes nest via parent_id.
    - category_aliases: alternative names, also the matching vocabulary for
      Goodreads shelves and Open Library subjects.
    - book_categories: accepted assignments. role is 'primary' (at most one per
      book, genre only) or 'secondary'; source says who put it there (user,
      rule, suggestion) and rule_id links rule-made rows to their rule so
      unmapping a shelf takes its assignments with it.
    - category_exclusions: a category the user removed from a book, so rules
      never quietly add it back.
    - category_rules: accepted "teach once" mappings (shelf/subject/tag value
      -> category), applied to every matching book on each categorize run.
    - category_suggestions: the human-in-the-loop review queue. kind='map'
      proposes a rule; kind='primary' proposes a book's primary genre. Rejected
      rows are kept so the same suggestion is never raised again.
    - series / book_series: series membership and reading order, parsed from
      Goodreads titles ("Red Dragon (Hannibal, #1)") or set by the user. A
      user row with a NULL series_id means "not part of a series", so title
      parsing doesn't put it back.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS adso_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facet TEXT NOT NULL,
            slug TEXT NOT NULL,
            label TEXT NOT NULL,
            parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            position INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_identity
            ON categories(facet, COALESCE(parent_id, 0), slug);
        CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);

        CREATE TABLE IF NOT EXISTS category_aliases (
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            PRIMARY KEY(category_id, alias)
        );

        CREATE TABLE IF NOT EXISTS category_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_kind TEXT NOT NULL,
            match_value TEXT NOT NULL,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            created_by TEXT NOT NULL DEFAULT 'cli',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(match_kind, match_value, category_id)
        );

        CREATE TABLE IF NOT EXISTS book_categories (
            book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'secondary',
            source TEXT NOT NULL,
            rule_id INTEGER REFERENCES category_rules(id) ON DELETE CASCADE,
            evidence TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(book_id, category_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_book_categories_one_primary
            ON book_categories(book_id) WHERE role = 'primary';
        CREATE INDEX IF NOT EXISTS idx_book_categories_category
            ON book_categories(category_id);

        CREATE TABLE IF NOT EXISTS category_exclusions (
            book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(book_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS category_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            match_kind TEXT,
            match_value TEXT,
            book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
            category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
            proposed_facet TEXT,
            proposed_label TEXT,
            confidence REAL NOT NULL DEFAULT 0,
            book_count INTEGER NOT NULL DEFAULT 0,
            evidence TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_at TEXT,
            decided_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_category_suggestions_map
            ON category_suggestions(match_kind, match_value) WHERE kind = 'map';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_category_suggestions_primary
            ON category_suggestions(book_id, category_id) WHERE kind = 'primary';
        -- kind='assign': "put this book in this category", proposed by an agent
        -- (MCP) rather than derived from shelves; new-category proposals carry
        -- proposed_facet/proposed_label with a NULL category_id.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_category_suggestions_assign
            ON category_suggestions(book_id, COALESCE(category_id, 0), COALESCE(proposed_label, ''))
            WHERE kind = 'assign';

        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE
        );

        CREATE TABLE IF NOT EXISTS book_series (
            book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
            series_id INTEGER REFERENCES series(id) ON DELETE CASCADE,
            position REAL,
            source TEXT NOT NULL DEFAULT 'title'
        );
        CREATE INDEX IF NOT EXISTS idx_book_series_series ON book_series(series_id);
        """
    )
    suggestion_columns = {row["name"] for row in conn.execute("PRAGMA table_info(category_suggestions)")}
    if "proposed_by" not in suggestion_columns:
        # Who raised a suggestion: 'adso' for the rule engine, or the agent/tool
        # that proposed it (e.g. 'mcp'), shown to the reviewer.
        conn.execute("ALTER TABLE category_suggestions ADD COLUMN proposed_by TEXT")
    seed_taxonomy(conn)


def _sqlite_has_fts5(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite build can create FTS5 tables (it's a compile option)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.adso_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.adso_fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _migrate_search_fts(conn: sqlite3.Connection) -> None:
    """Build a persistent FTS5 search index kept current by triggers.

    Earlier, search rebuilt a temp FTS table from every book on each query —
    correct but O(catalogue) per call, which would bite repeated queries behind
    the web UI (DAV-146). Instead we keep one external-content FTS5 index
    (``books_fts`` reads its column values straight from ``books``) and maintain
    it with AFTER INSERT/UPDATE/DELETE triggers, so a search is just an index
    lookup. Built once here; if it already exists the triggers have kept it fresh
    and there's nothing to do. Skipped entirely on SQLite builds without FTS5,
    where adso.catalogue falls back to LIKE search.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books_fts'"
    ).fetchone():
        return
    if not _sqlite_has_fts5(conn):
        return
    # The index and its triggers read every SEARCH_FIELDS column off `books`; on
    # an unexpectedly incomplete schema, skip rather than crash — search falls
    # back to LIKE in adso.catalogue.
    book_columns = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    if not set(SEARCH_FIELDS) <= book_columns:
        return

    cols = ", ".join(SEARCH_FIELDS)
    new_values = ", ".join(f"new.{field}" for field in SEARCH_FIELDS)
    old_values = ", ".join(f"old.{field}" for field in SEARCH_FIELDS)

    conn.executescript(
        f"""
        CREATE VIRTUAL TABLE books_fts USING fts5(
            {cols},
            content='books',
            content_rowid='id'
        );

        CREATE TRIGGER books_fts_ai AFTER INSERT ON books BEGIN
            INSERT INTO books_fts (rowid, {cols}) VALUES (new.id, {new_values});
        END;

        CREATE TRIGGER books_fts_ad AFTER DELETE ON books BEGIN
            INSERT INTO books_fts (books_fts, rowid, {cols})
            VALUES ('delete', old.id, {old_values});
        END;

        CREATE TRIGGER books_fts_au AFTER UPDATE ON books BEGIN
            INSERT INTO books_fts (books_fts, rowid, {cols})
            VALUES ('delete', old.id, {old_values});
            INSERT INTO books_fts (rowid, {cols}) VALUES (new.id, {new_values});
        END;
        """
    )
    # Populate from any rows that already exist (an upgraded catalogue).
    conn.execute("INSERT INTO books_fts (books_fts) VALUES ('rebuild')")


def create_import_run(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_path: str,
    mode: str,
    row_count: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO import_runs (source, source_path, mode, row_count)
        VALUES (?, ?, ?, ?)
        """,
        (source, source_path, mode, row_count),
    )
    return int(cur.lastrowid)


def update_import_run_counts(
    conn: sqlite3.Connection,
    import_run_id: int,
    *,
    created: int,
    updated: int,
    unchanged: int,
    conflicts: int,
) -> None:
    conn.execute(
        """
        UPDATE import_runs
        SET created_count = ?, updated_count = ?, unchanged_count = ?, conflict_count = ?
        WHERE id = ?
        """,
        (created, updated, unchanged, conflicts, import_run_id),
    )


def list_import_runs(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    return conn.execute(
        f"SELECT * FROM import_runs ORDER BY id DESC {limit_sql}"
    ).fetchall()


def get_book_by_goodreads_id(conn: sqlite3.Connection, goodreads_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM books WHERE goodreads_id = ?", (goodreads_id,))
    return cur.fetchone()


def get_book(conn: sqlite3.Connection, book_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,))
    return cur.fetchone()


def iter_books(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    yield from conn.execute("SELECT * FROM books ORDER BY title COLLATE NOCASE, author COLLATE NOCASE")


def row_to_catalogue_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["shelves"] = json.loads(data.pop("shelves_json") or "[]")
    data["tags"] = json.loads(data.pop("tags_json") or "[]")
    data["subjects"] = json.loads(data.pop("subjects_json") or "[]")
    data["subject_places"] = json.loads(data.pop("subject_places_json") or "[]")
    data["subject_times"] = json.loads(data.pop("subject_times_json") or "[]")
    return data


def serialize_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def insert_raw_row(
    conn: sqlite3.Connection,
    *,
    import_run_id: int,
    source: str,
    row_index: int,
    source_record_id: str,
    raw: dict[str, str],
    normalized: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO raw_import_rows
            (import_run_id, source, source_record_id, row_index, raw_json, normalized_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            import_run_id,
            source,
            source_record_id,
            row_index,
            json.dumps(raw, sort_keys=True),
            json.dumps(normalized, sort_keys=True),
        ),
    )


def insert_book_from_goodreads(
    conn: sqlite3.Connection,
    normalized: dict[str, Any],
    *,
    import_run_id: int,
) -> int:
    columns = ["goodreads_id", *GOODREADS_FIELDS, "last_goodreads_import_run_id"]
    values = [normalized.get("goodreads_id")]
    values.extend(normalized.get(field) for field in GOODREADS_FIELDS)
    values.append(import_run_id)
    placeholders = ", ".join("?" for _ in columns)
    cur = conn.execute(
        f"INSERT INTO books ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    book_id = int(cur.lastrowid)
    upsert_source_snapshots(conn, book_id, "goodreads", normalized, import_run_id)
    return book_id


def update_book_goodreads_fields(
    conn: sqlite3.Connection,
    book_id: int,
    updates: dict[str, Any],
    *,
    import_run_id: int,
) -> None:
    if not updates:
        return
    assignments = [f"{field} = ?" for field in updates]
    values = list(updates.values())
    assignments.append("last_goodreads_import_run_id = ?")
    values.append(import_run_id)
    assignments.append("updated_at = CURRENT_TIMESTAMP")
    values.append(book_id)
    conn.execute(
        f"UPDATE books SET {', '.join(assignments)} WHERE id = ?",
        values,
    )


def update_local_fields(
    conn: sqlite3.Connection,
    goodreads_id: str,
    updates: dict[str, Any],
) -> None:
    invalid = [field for field in updates if field not in LOCAL_FIELDS]
    if invalid:
        raise ValueError(f"Unsupported local fields: {', '.join(invalid)}")
    if "format" in updates and updates["format"] not in (None, *VALID_FORMATS):
        raise ValueError(
            f"Unsupported format {updates['format']!r}; "
            f"expected one of {', '.join(VALID_FORMATS)}, or empty for not owned"
        )
    if "tags_json" in updates:
        # Accept a list (normalised here) or an already-serialized JSON array;
        # either way the stored value is canonical normalize_tags output.
        value = updates["tags_json"]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(f"tags_json must be a JSON array, got {value!r}") from None
        if not isinstance(value, list):
            raise ValueError("tags_json must be a list of tags")
        updates["tags_json"] = json.dumps(normalize_tags(value))
    if not updates:
        return
    book = get_book_by_goodreads_id(conn, goodreads_id)
    if book is None:
        raise ValueError(f"No book found for Goodreads ID {goodreads_id}")
    assignments = [f"{field} = ?" for field in updates]
    values = list(updates.values())
    assignments.append("updated_at = CURRENT_TIMESTAMP")
    values.append(book["id"])
    conn.execute(f"UPDATE books SET {', '.join(assignments)} WHERE id = ?", values)
    conn.commit()


def set_cover(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    cover_path: str | None,
    cover_source: str | None,
    cover_source_url: str | None,
    cover_status: str,
) -> None:
    """Record the outcome of a cover-art fetch for one book.

    Covers are local enrichment, not a Goodreads-sourced field, so this writes
    directly to the books row without touching source_snapshots/conflicts.
    """
    conn.execute(
        """
        UPDATE books
        SET cover_path = ?,
            cover_source = ?,
            cover_source_url = ?,
            cover_status = ?,
            cover_fetched_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (cover_path, cover_source, cover_source_url, cover_status, book_id),
    )
    conn.commit()


def set_metadata(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    description: str | None,
    subjects: list[str],
    subject_places: list[str],
    subject_times: list[str],
    metadata_source: str | None,
    metadata_source_url: str | None,
    metadata_status: str,
) -> None:
    """Record the outcome of an Open Library metadata fetch for one book.

    Like covers, metadata is local enrichment, not a Goodreads-sourced field,
    so this writes directly to the books row without touching
    source_snapshots/conflicts.
    """
    conn.execute(
        """
        UPDATE books
        SET description = ?,
            subjects_json = ?,
            subject_places_json = ?,
            subject_times_json = ?,
            metadata_source = ?,
            metadata_source_url = ?,
            metadata_status = ?,
            metadata_fetched_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            description,
            json.dumps(subjects),
            json.dumps(subject_places),
            json.dumps(subject_times),
            metadata_source,
            metadata_source_url,
            metadata_status,
            book_id,
        ),
    )
    conn.commit()


def clear_metadata(conn: sqlite3.Connection, book_id: int) -> None:
    """Reset all metadata fields for one book (so the next fetch reconsiders it)."""
    conn.execute(
        """
        UPDATE books
        SET description = NULL,
            subjects_json = '[]',
            subject_places_json = '[]',
            subject_times_json = '[]',
            metadata_source = NULL,
            metadata_source_url = NULL,
            metadata_status = NULL,
            metadata_fetched_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (book_id,),
    )
    conn.commit()


def backfill_isbns(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    isbn10: str | None = None,
    isbn13: str | None = None,
) -> bool:
    """Fill empty ISBN columns from enrichment data; never overwrite a value.

    Goodreads stays authoritative for ISBNs (they are informational sync
    fields); enrichment only fills vacuums, enforced here in the SQL. Returns
    whether anything changed.
    """
    changed = 0
    if isbn13:
        changed += conn.execute(
            "UPDATE books SET isbn13 = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND (isbn13 IS NULL OR isbn13 = '')",
            (isbn13, book_id),
        ).rowcount
    if isbn10:
        changed += conn.execute(
            "UPDATE books SET isbn10 = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND (isbn10 IS NULL OR isbn10 = '')",
            (isbn10, book_id),
        ).rowcount
    if changed:
        conn.commit()
    return bool(changed)


def clear_cover(conn: sqlite3.Connection, book_id: int) -> None:
    """Reset all cover fields for one book (so the next fetch reconsiders it)."""
    conn.execute(
        """
        UPDATE books
        SET cover_path = NULL,
            cover_source = NULL,
            cover_source_url = NULL,
            cover_status = NULL,
            cover_fetched_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (book_id,),
    )
    conn.commit()


def get_source_snapshot(
    conn: sqlite3.Connection,
    book_id: int,
    source: str,
    field_name: str,
) -> str | None:
    cur = conn.execute(
        """
        SELECT field_value FROM source_snapshots
        WHERE book_id = ? AND source = ? AND field_name = ?
        """,
        (book_id, source, field_name),
    )
    row = cur.fetchone()
    return row["field_value"] if row else None


def upsert_source_snapshots(
    conn: sqlite3.Connection,
    book_id: int,
    source: str,
    normalized: dict[str, Any],
    import_run_id: int,
    fields: Iterable[str] = GOODREADS_FIELDS,
) -> None:
    for field in fields:
        conn.execute(
            """
            INSERT INTO source_snapshots
                (book_id, source, field_name, field_value, import_run_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(book_id, source, field_name)
            DO UPDATE SET
                field_value = excluded.field_value,
                import_run_id = excluded.import_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (book_id, source, field, serialize_value(normalized.get(field)), import_run_id),
        )


def add_conflict(
    conn: sqlite3.Connection,
    *,
    import_run_id: int,
    book_id: int,
    source: str,
    field_name: str,
    old_source_value: Any,
    local_value: Any,
    incoming_value: Any,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_conflicts
            (import_run_id, book_id, source, field_name, old_source_value, local_value, incoming_value)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_run_id,
            book_id,
            source,
            field_name,
            serialize_value(old_source_value),
            serialize_value(local_value),
            serialize_value(incoming_value),
        ),
    )


def get_conflict(conn: sqlite3.Connection, conflict_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, b.title, b.author, b.goodreads_id
        FROM sync_conflicts c
        JOIN books b ON b.id = c.book_id
        WHERE c.id = ?
        """,
        (conflict_id,),
    ).fetchone()


def list_conflicts(
    conn: sqlite3.Connection,
    *,
    status: str | tuple[str, ...] | None = "pending",
    book_id: int | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        statuses = (status,) if isinstance(status, str) else tuple(status)
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"c.status IN ({placeholders})")
        params.extend(statuses)
    if book_id is not None:
        clauses.append("c.book_id = ?")
        params.append(book_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT c.*, b.title, b.author, b.goodreads_id
        FROM sync_conflicts c
        JOIN books b ON b.id = c.book_id
        {where}
        ORDER BY b.title COLLATE NOCASE, c.field_name
        """,
        params,
    ).fetchall()


# Conflict status buckets. "open" still needs a decision (pending = act now,
# review_later = the user deferred it); "closed" has one (resolved/ignored).
OPEN_CONFLICT_STATUSES = ("pending", "review_later")
DECIDED_CONFLICT_STATUSES = ("resolved", "ignored")


def set_conflict_status(
    conn: sqlite3.Connection,
    conflict_id: int,
    *,
    status: str,
    resolution: str | None,
) -> None:
    """Set the current (denormalized) state of a conflict.

    Pass status='pending' with resolution=None to reopen a decided conflict;
    that also clears resolved_at. Any other status stamps resolved_at now.
    """
    if status == "pending":
        conn.execute(
            "UPDATE sync_conflicts SET status = 'pending', resolution = NULL, resolved_at = NULL WHERE id = ?",
            (conflict_id,),
        )
        return
    conn.execute(
        """
        UPDATE sync_conflicts
        SET status = ?, resolution = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, resolution, conflict_id),
    )


def set_conflict_resolution(
    conn: sqlite3.Connection,
    conflict_id: int,
    *,
    resolution: str,
) -> None:
    """Mark a conflict resolved with a value resolution (back-compat wrapper)."""
    set_conflict_status(conn, conflict_id, status="resolved", resolution=resolution)


def reopen_conflict(conn: sqlite3.Connection, conflict_id: int) -> None:
    """Return a decided conflict to pending so it can be decided again."""
    set_conflict_status(conn, conflict_id, status="pending", resolution=None)


def record_conflict_decision(
    conn: sqlite3.Connection,
    conflict_id: int,
    *,
    decision: str,
    resulting_value: Any = None,
    actor: str = "cli",
    note: str | None = None,
) -> None:
    """Append one immutable row to a conflict's decision/audit trail."""
    conn.execute(
        """
        INSERT INTO conflict_decisions
            (conflict_id, decision, resulting_value, actor, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (conflict_id, decision, serialize_value(resulting_value), actor, note),
    )


def list_conflict_decisions(conn: sqlite3.Connection, conflict_id: int) -> list[sqlite3.Row]:
    """Return a conflict's decision history, oldest first."""
    return conn.execute(
        "SELECT * FROM conflict_decisions WHERE conflict_id = ? ORDER BY id",
        (conflict_id,),
    ).fetchall()


def pending_conflict_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM sync_conflicts WHERE status = 'pending'").fetchone()
    return int(row[0])


def deferred_conflict_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM sync_conflicts WHERE status = 'review_later'").fetchone()
    return int(row[0])


# Default values that mean "no local enrichment set" for each local field, used
# when merging duplicates to decide whether the keeper already has a value.
_LOCAL_FIELD_EMPTY = {
    "format": None,
    "tags_json": "[]",
    "loaned_to": None,
    "local_notes": None,
}


def insert_duplicate_link(
    conn: sqlite3.Connection,
    *,
    group_key: str,
    book_id: int,
    reason: str | None = None,
) -> None:
    """Record a book as a pending member of a duplicate group (idempotent)."""
    conn.execute(
        """
        INSERT INTO duplicate_links (group_key, book_id, reason)
        VALUES (?, ?, ?)
        ON CONFLICT(group_key, book_id) DO NOTHING
        """,
        (group_key, book_id, reason),
    )


def list_duplicate_links(
    conn: sqlite3.Connection,
    *,
    status: str | None = "pending",
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("d.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT d.*, b.title, b.author, b.goodreads_id, b.reading_status,
               b.format, b.loaned_to, b.local_notes,
               b.cover_path, b.last_goodreads_import_run_id
        FROM duplicate_links d
        JOIN books b ON b.id = d.book_id
        {where}
        ORDER BY b.title COLLATE NOCASE, d.book_id
        """,
        params,
    ).fetchall()


def set_duplicate_group_status(
    conn: sqlite3.Connection,
    group_key: str,
    *,
    status: str,
) -> None:
    conn.execute(
        "UPDATE duplicate_links SET status = ? WHERE group_key = ?",
        (status, group_key),
    )


def dismissed_or_merged_group_keys(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT group_key FROM duplicate_links WHERE status IN ('dismissed', 'merged')"
    ).fetchall()
    return {row["group_key"] for row in rows}


def pending_duplicate_group_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT group_key) FROM duplicate_links WHERE status = 'pending'"
    ).fetchone()
    return int(row[0])


def merge_books(conn: sqlite3.Connection, *, keep_id: int, drop_id: int) -> None:
    """Fold the ``drop`` book into ``keep`` and delete the duplicate.

    Local enrichment (LOCAL_FIELDS) from ``drop`` fills any field the keeper has
    left empty, and the keeper inherits the dropped record's cover if it has none.
    Child rows for ``drop`` are deleted explicitly (not relying on cascade) before
    the book itself; raw_import_rows are import logs keyed on the run, not the
    book, so they are left intact.
    """
    keep = get_book(conn, keep_id)
    drop = get_book(conn, drop_id)
    if keep is None or drop is None:
        raise ValueError(f"merge_books needs two existing books (keep={keep_id}, drop={drop_id})")

    updates: dict[str, Any] = {}
    for field, empty in _LOCAL_FIELD_EMPTY.items():
        if keep[field] == empty and drop[field] != empty:
            updates[field] = drop[field]
    if not keep["cover_path"] and drop["cover_path"]:
        updates["cover_path"] = drop["cover_path"]
        updates["cover_status"] = drop["cover_status"]

    if updates:
        assignments = [f"{field} = ?" for field in updates]
        values = list(updates.values())
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        values.append(keep_id)
        conn.execute(f"UPDATE books SET {', '.join(assignments)} WHERE id = ?", values)

    # Categories and series are local enrichment too: the keeper gains any the
    # dropped record had, but its own primary genre and series win.
    conn.execute(
        """
        INSERT OR IGNORE INTO book_categories
            (book_id, category_id, role, source, rule_id, evidence, created_at)
        SELECT ?, category_id,
               CASE WHEN role = 'primary' AND EXISTS (
                   SELECT 1 FROM book_categories WHERE book_id = ? AND role = 'primary'
               ) THEN 'secondary' ELSE role END,
               source, rule_id, evidence, created_at
        FROM book_categories WHERE book_id = ?
        """,
        (keep_id, keep_id, drop_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO book_series (book_id, series_id, position, source)
        SELECT ?, series_id, position, source FROM book_series WHERE book_id = ?
        """,
        (keep_id, drop_id),
    )
    for table in ("book_categories", "category_exclusions", "category_suggestions", "book_series"):
        conn.execute(f"DELETE FROM {table} WHERE book_id = ?", (drop_id,))
    conn.execute("DELETE FROM source_snapshots WHERE book_id = ?", (drop_id,))
    conn.execute("DELETE FROM sync_conflicts WHERE book_id = ?", (drop_id,))
    conn.execute("DELETE FROM duplicate_links WHERE book_id = ?", (drop_id,))
    conn.execute("DELETE FROM books WHERE id = ?", (drop_id,))
