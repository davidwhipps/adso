"""Model Context Protocol server for Adso.

Exposes the local catalogue to MCP-speaking agents (Claude Desktop, Claude
Code, …) over stdio. It is a thin, tool-shaped skin over the same
``adso.catalogue`` / ``adso.db`` service layer the CLI and web UI use — no new
data path — reached through ``adso mcp``.

Two design rules matter here:

* **Allowlist output.** Tool results are built by ``_book_to_agent_dict`` from an
  explicit field list (``AGENT_BOOK_FIELDS``), so anything not named — notably
  ``private_notes``, and any column added later — is invisible to the agent by
  default. This is deliberately *not* the catalogue's ``SELECT *`` shape.
* **Curated writes only.** The write tools touch the ``LOCAL_FIELDS`` sync never
  overwrites (tags, format, loaned_to). ``local_notes`` is readable but not
  writable, and Goodreads/source columns are never mutated.

The tools are plain functions that take a connection so they can be unit-tested
directly; ``build_server`` wraps each in a FastMCP ``@tool`` that opens a
short-lived connection per call.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import catalogue, db

SERVER_NAME = "adso"

# The exact fields an agent may see for a book. Built from catalogue._book_result
# but deliberately omitting: private_notes (sensitive), internal id / cover_path /
# cover_url (implementation detail), created_at / updated_at (noise). Anything not
# listed here — including future columns — is private by default.
AGENT_BOOK_FIELDS = (
    "goodreads_id",
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
    "shelves",
    "date_read",
    "date_added",
    "my_review",
    "read_count",
    "owned_copies",
    "format",
    "tags",
    "loaned_to",
    "local_notes",
    "description",
    "subjects",
    "subject_places",
    "subject_times",
    "cover_status",
)

# Guard: a field an agent must never see, asserted against AGENT_BOOK_FIELDS at
# import time so a careless edit to the allowlist can't silently expose it.
_FORBIDDEN_BOOK_FIELDS = frozenset({"private_notes"})
assert _FORBIDDEN_BOOK_FIELDS.isdisjoint(AGENT_BOOK_FIELDS)

DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200


def _book_to_agent_dict(book: dict[str, Any]) -> dict[str, Any]:
    """Project a catalogue record onto the agent-visible allowlist."""
    return {field: book.get(field) for field in AGENT_BOOK_FIELDS}


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_SEARCH_LIMIT
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(int(limit), MAX_SEARCH_LIMIT)


def _normalized_format(value: str | None) -> str | None:
    """Validate a local ``format``, treating empty/'none' as 'not owned' (NULL)."""
    if value is None:
        return None
    cleaned = value.strip().lower()
    if cleaned in ("", "none"):
        return None
    if cleaned not in db.VALID_FORMATS:
        raise ValueError(
            f"Unknown format {value!r}. Valid formats: "
            f"{', '.join(db.VALID_FORMATS)}, or empty/'none' for not owned."
        )
    return cleaned


# --- Read tools --------------------------------------------------------------


def search_books(
    conn: sqlite3.Connection,
    query: str = "",
    *,
    shelf: str | None = None,
    format: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    rating: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Full-text search / filtered browse over the catalogue (allowlisted output)."""
    if rating is not None and not 0 <= rating <= 5:
        raise ValueError("rating must be an integer from 0 (unrated) to 5")
    filters = catalogue.BookFilters(
        shelf=(shelf or "").strip() or None,
        format=(format or "").strip().lower() or None,
        tag=(tag or "").strip() or None,
        author=(author or "").strip() or None,
        rating=rating,
        limit=_clamp_limit(limit),
    )
    books = catalogue.search_books(conn, query or "", filters)
    return {"count": len(books), "books": [_book_to_agent_dict(b) for b in books]}


def get_book(conn: sqlite3.Connection, goodreads_id: str) -> dict[str, Any]:
    """One book's full allowlisted record, by Goodreads ID."""
    book = catalogue.get_book(conn, goodreads_id)
    if book is None:
        raise ValueError(f"No book found for Goodreads ID {goodreads_id}")
    return _book_to_agent_dict(book)


def library_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate counts: total, owned, and breakdowns by shelf/format/rating."""
    total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    owned = conn.execute(
        "SELECT COUNT(*) FROM books WHERE format IS NOT NULL AND format != ''"
    ).fetchone()[0]
    by_shelf = {
        (row[0] or "(unshelved)"): row[1]
        for row in conn.execute(
            "SELECT exclusive_shelf, COUNT(*) FROM books "
            "GROUP BY exclusive_shelf ORDER BY COUNT(*) DESC"
        ).fetchall()
    }
    by_format = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT format, COUNT(*) FROM books "
            "WHERE format IS NOT NULL AND format != '' GROUP BY format"
        ).fetchall()
    }
    # Goodreads exports unrated as 0, and an empty cell parses to NULL; both mean
    # unrated, so coalesce NULL to 0 before grouping.
    by_rating = {
        int(row[0]): row[1]
        for row in conn.execute(
            "SELECT COALESCE(rating, 0) AS r, COUNT(*) FROM books GROUP BY r ORDER BY r"
        ).fetchall()
    }
    return {
        "total_books": total,
        "owned_books": owned,
        "by_shelf": by_shelf,
        "by_format": by_format,
        "by_rating": by_rating,
    }


def list_facets(conn: sqlite3.Connection) -> dict[str, Any]:
    """Valid filter vocabulary the agent should use: shelves, tags, formats."""
    shelves = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT exclusive_shelf FROM books "
            "WHERE exclusive_shelf IS NOT NULL AND exclusive_shelf != '' "
            "ORDER BY exclusive_shelf COLLATE NOCASE"
        ).fetchall()
    ]
    return {
        "shelves": shelves,
        "tags": catalogue.distinct_tags(conn),
        "formats": list(db.VALID_FORMATS),
    }


# --- Write tools (curated; LOCAL_FIELDS only) --------------------------------


def _require_book(conn: sqlite3.Connection, goodreads_id: str) -> dict[str, Any]:
    book = catalogue.get_book(conn, goodreads_id)
    if book is None:
        raise ValueError(f"No book found for Goodreads ID {goodreads_id}")
    return book


def add_tags(
    conn: sqlite3.Connection, goodreads_id: str, tags: list[str]
) -> dict[str, Any]:
    """Add local tags to a book (union with existing; case-normalized)."""
    book = _require_book(conn, goodreads_id)
    current: list[str] = list(book.get("tags") or [])
    incoming = db.normalize_tags(tags)
    merged = current + [t for t in incoming if t not in current]
    db.update_local_fields(conn, goodreads_id, {"tags_json": merged})
    return {"goodreads_id": goodreads_id, "tags": merged}


def remove_tags(
    conn: sqlite3.Connection, goodreads_id: str, tags: list[str]
) -> dict[str, Any]:
    """Remove local tags from a book (no error if a tag wasn't present)."""
    book = _require_book(conn, goodreads_id)
    current: list[str] = list(book.get("tags") or [])
    to_remove = set(db.normalize_tags(tags))
    remaining = [t for t in current if t not in to_remove]
    db.update_local_fields(conn, goodreads_id, {"tags_json": remaining})
    return {"goodreads_id": goodreads_id, "tags": remaining}


def set_format(
    conn: sqlite3.Connection, goodreads_id: str, format: str | None
) -> dict[str, Any]:
    """Set the owned format (physical/ebook/audiobook), or clear it (not owned)."""
    value = _normalized_format(format)
    _require_book(conn, goodreads_id)
    db.update_local_fields(conn, goodreads_id, {"format": value})
    return {"goodreads_id": goodreads_id, "format": value}


def set_loaned(
    conn: sqlite3.Connection, goodreads_id: str, loaned_to: str | None
) -> dict[str, Any]:
    """Record who a book is loaned to, or clear the loan (pass empty/null)."""
    value = (loaned_to or "").strip() or None
    _require_book(conn, goodreads_id)
    db.update_local_fields(conn, goodreads_id, {"loaned_to": value})
    return {"goodreads_id": goodreads_id, "loaned_to": value}


# --- Server assembly ---------------------------------------------------------


def build_server(db_path: str) -> Any:
    """Build the FastMCP server. Import is lazy so the core CLI needn't ship mcp."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        raise ModuleNotFoundError(
            "The MCP server needs the 'mcp' package. Install it with: "
            "pip install -e '.[mcp]'"
        ) from exc

    # Initialize the schema once, then hand each tool call its own short-lived
    # connection (FastMCP runs tools on a threadpool; a per-call connection keeps
    # SQLite thread-safe, mirroring the web app).
    _init = db.connect(db_path)
    db.initialize(_init)
    _init.close()

    mcp = FastMCP(SERVER_NAME)

    def _run(fn, *args, **kwargs):
        conn = db.connect(db_path)
        try:
            return fn(conn, *args, **kwargs)
        finally:
            conn.close()

    @mcp.tool()
    def search_books_tool(
        query: str = "",
        shelf: str | None = None,
        format: str | None = None,
        tag: str | None = None,
        author: str | None = None,
        rating: int | None = None,
        limit: int | None = None,
    ) -> dict:
        """Search or browse the book catalogue.

        With a `query`, runs full-text search over titles, authors, ISBNs,
        reviews and tags. With an empty query, lists books filtered by the
        optional arguments. Filter values should come from `list_facets`.
        Returns at most 200 books (default 50); narrow with filters or `limit`.
        """
        return _run(
            search_books,
            query,
            shelf=shelf,
            format=format,
            tag=tag,
            author=author,
            rating=rating,
            limit=limit,
        )

    @mcp.tool()
    def get_book_tool(goodreads_id: str) -> dict:
        """Get one book's full record by its Goodreads ID."""
        return _run(get_book, goodreads_id)

    @mcp.tool()
    def library_stats_tool() -> dict:
        """Summarize the library: totals and counts by shelf, format, and rating."""
        return _run(library_stats)

    @mcp.tool()
    def list_facets_tool() -> dict:
        """List the valid shelves, tags, and formats to filter searches by."""
        return _run(list_facets)

    @mcp.tool()
    def add_tags_tool(goodreads_id: str, tags: list[str]) -> dict:
        """Add one or more local tags to a book. Returns the book's full tag list."""
        return _run(add_tags, goodreads_id, tags)

    @mcp.tool()
    def remove_tags_tool(goodreads_id: str, tags: list[str]) -> dict:
        """Remove one or more local tags from a book. Returns the remaining tags."""
        return _run(remove_tags, goodreads_id, tags)

    @mcp.tool()
    def set_format_tool(goodreads_id: str, format: str | None = None) -> dict:
        """Set a book's owned format: 'physical', 'ebook', 'audiobook', or empty to clear."""
        return _run(set_format, goodreads_id, format)

    @mcp.tool()
    def set_loaned_tool(goodreads_id: str, loaned_to: str | None = None) -> dict:
        """Record who a book is loaned to, or pass empty to clear the loan."""
        return _run(set_loaned, goodreads_id, loaned_to)

    return mcp


def run_stdio(db_path: str) -> int:
    """Run the MCP server over stdio (the transport MCP clients spawn)."""
    build_server(db_path).run()
    return 0
