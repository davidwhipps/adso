"""Reusable catalogue query services.

This module is intentionally independent of the CLI so terminal commands,
future web views, and agent tools can all retrieve catalogue records through
the same boundary.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db


@dataclass(frozen=True)
class BookFilters:
    status: str | None = None
    format: str | None = None
    tag: str | None = None
    author: str | None = None
    shelf: str | None = None
    exclude_shelf: str | None = None
    rating: int | None = None
    limit: int | None = None
    # "added" sorts newest-first by date_added; anything else falls back to the
    # default title ordering.
    sort: str | None = None


# The set of columns search covers is defined once in db.SEARCH_FIELDS, shared
# with the persistent FTS5 index so both search paths stay in lockstep.
SEARCH_FIELDS = db.SEARCH_FIELDS


def list_books(conn: sqlite3.Connection, filters: BookFilters | None = None) -> list[dict[str, Any]]:
    filters = filters or BookFilters()
    where, params = _filter_sql(filters)
    limit_sql = _limit_sql(filters)
    rows = conn.execute(
        f"""
        SELECT * FROM books
        {where}
        ORDER BY {_order_sql(filters)}
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [_book_result(row) for row in rows]


def search_books(
    conn: sqlite3.Connection,
    query: str,
    filters: BookFilters | None = None,
) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return list_books(conn, filters)

    filters = filters or BookFilters()
    if _fts_index_available(conn):
        return _search_books_fts(conn, query, filters)

    where, params = _filter_sql(filters)
    search_where, search_params = _search_sql(query)
    if where:
        combined_where = f"{where} AND {search_where}"
    else:
        combined_where = f"WHERE {search_where}"
    limit_sql = _limit_sql(filters)

    rows = conn.execute(
        f"""
        SELECT * FROM books
        {combined_where}
        ORDER BY {_order_sql(filters)}
        {limit_sql}
        """,
        [*params, *search_params],
    ).fetchall()
    return [_book_result(row) for row in rows]


def distinct_statuses(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT reading_status FROM books "
        "WHERE reading_status IS NOT NULL AND reading_status != '' "
        "ORDER BY reading_status COLLATE NOCASE"
    ).fetchall()
    return [row[0] for row in rows]


def distinct_tags(conn: sqlite3.Connection) -> list[str]:
    """Every tag in use, for the catalogue filter dropdown (JSON1 json_each)."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT je.value FROM books, json_each(books.tags_json) je "
            "ORDER BY je.value COLLATE NOCASE"
        ).fetchall()
    except sqlite3.OperationalError:
        # SQLite built without JSON1 (rare); the dropdown just renders empty.
        return []
    return [row[0] for row in rows]


def get_book(conn: sqlite3.Connection, goodreads_id: str) -> dict[str, Any] | None:
    row = db.get_book_by_goodreads_id(conn, goodreads_id)
    if row is None:
        return None
    return _book_result(row)


def _search_books_fts(
    conn: sqlite3.Connection,
    query: str,
    filters: BookFilters,
) -> list[dict[str, Any]]:
    fts_query = _fts_query(query)
    if not fts_query:
        return list_books(conn, filters)

    where, params = _filter_sql(filters, table_prefix="books")
    if where:
        combined_where = f"{where} AND books_fts MATCH ?"
    else:
        combined_where = "WHERE books_fts MATCH ?"
    limit_sql = _limit_sql(filters)

    # An explicit sort beats relevance ordering; otherwise rank by bm25.
    if filters.sort == "added":
        order_sql = _order_sql(filters, table_prefix="books")
    else:
        order_sql = "bm25(books_fts), books.title COLLATE NOCASE, books.author COLLATE NOCASE"
    rows = conn.execute(
        f"""
        SELECT books.*
        FROM books
        JOIN books_fts ON books_fts.rowid = books.id
        {combined_where}
        ORDER BY {order_sql}
        {limit_sql}
        """,
        [*params, fts_query],
    ).fetchall()
    return [_book_result(row) for row in rows]


def _fts_index_available(conn: sqlite3.Connection) -> bool:
    """Whether the persistent FTS5 index exists (built by db._migrate_search_fts).

    Its presence is the real signal that FTS search is usable: the index is only
    created when this SQLite build supports FTS5 and the schema is complete, so a
    missing table means we must fall back to LIKE search.
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books_fts'"
        ).fetchone()
        is not None
    )


def _fts_query(query: str) -> str:
    tokens = []
    token = []
    for char in query:
        if char.isalnum():
            token.append(char)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return " ".join(f'"{token}"' for token in tokens)


def _order_sql(filters: BookFilters, table_prefix: str | None = None) -> str:
    """ORDER BY clause for the requested sort (title default, "added" newest-first).

    date_added is an ISO YYYY-MM-DD string, so plain string comparison sorts
    chronologically; NULLs (rare, pre-Goodreads rows) sink to the end.
    """
    prefix = f"{table_prefix}." if table_prefix else ""
    title_order = f"{prefix}title COLLATE NOCASE, {prefix}author COLLATE NOCASE"
    if filters.sort == "added":
        return f"{prefix}date_added IS NULL, {prefix}date_added DESC, {title_order}"
    return title_order


def _filter_sql(filters: BookFilters, table_prefix: str | None = None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    prefix = f"{table_prefix}." if table_prefix else ""

    if filters.status:
        clauses.append(f"{prefix}reading_status = ?")
        params.append(filters.status)
    if filters.format:
        clauses.append(f"{prefix}format = ?")
        params.append(filters.format)
    if filters.tag:
        # tags_json holds a JSON array of normalised (lowercase) tags, so an
        # exact-tag match is a LIKE on the quoted element — no JSON1 needed.
        clauses.append(f"{prefix}tags_json LIKE ?")
        params.append(f'%"{filters.tag.lower().strip()}"%')
    if filters.author:
        clauses.append(
            f"({prefix}author LIKE ? COLLATE NOCASE OR {prefix}additional_authors LIKE ? COLLATE NOCASE)"
        )
        author_query = f"%{filters.author}%"
        params.extend([author_query, author_query])
    if filters.shelf:
        clauses.append(f"{prefix}exclusive_shelf = ? COLLATE NOCASE")
        params.append(filters.shelf.strip())
    if filters.exclude_shelf:
        # Catalogue is a catch-all: keep everything that isn't on the excluded
        # shelf, including books with no exclusive_shelf recorded at all.
        clauses.append(
            f"({prefix}exclusive_shelf IS NULL OR {prefix}exclusive_shelf != ? COLLATE NOCASE)"
        )
        params.append(filters.exclude_shelf.strip())
    if filters.rating is not None:
        if filters.rating == 0:
            # Goodreads exports unrated as 0, but an empty "My Rating" cell
            # parses to NULL; both mean unrated.
            clauses.append(f"({prefix}rating = 0 OR {prefix}rating IS NULL)")
        else:
            clauses.append(f"{prefix}rating = ?")
            params.append(filters.rating)

    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _search_sql(query: str) -> tuple[str, list[Any]]:
    clauses = [f"{field} LIKE ? COLLATE NOCASE" for field in SEARCH_FIELDS]
    params = [f"%{query}%" for _ in SEARCH_FIELDS]
    return "(" + " OR ".join(clauses) + ")", params


def _limit_sql(filters: BookFilters) -> str:
    if filters.limit is None:
        return ""
    if filters.limit < 1:
        raise ValueError("limit must be at least 1")
    return f"LIMIT {int(filters.limit)}"


def _book_result(row: sqlite3.Row) -> dict[str, Any]:
    data = db.row_to_catalogue_dict(row)
    return {
        "id": data["id"],
        "goodreads_id": data["goodreads_id"],
        "title": data["title"],
        "author": data["author"],
        "additional_authors": data["additional_authors"],
        "isbn10": data["isbn10"],
        "isbn13": data["isbn13"],
        "publisher": data["publisher"],
        "binding": data["binding"],
        "number_of_pages": data["number_of_pages"],
        "year_published": data["year_published"],
        "original_publication_year": data["original_publication_year"],
        "rating": data["rating"],
        "average_rating": data["average_rating"],
        "reading_status": data["reading_status"],
        "exclusive_shelf": data["exclusive_shelf"],
        "shelves": data["shelves"],
        "date_read": data["date_read"],
        "date_added": data["date_added"],
        "my_review": data["my_review"],
        "private_notes": data["private_notes"],
        "read_count": data["read_count"],
        "owned_copies": data["owned_copies"],
        "format": data["format"],
        "tags": data["tags"],
        "loaned_to": data["loaned_to"],
        "local_notes": data["local_notes"],
        "description": data.get("description"),
        "subjects": data.get("subjects", []),
        "subject_places": data.get("subject_places", []),
        "subject_times": data.get("subject_times", []),
        "cover_path": data.get("cover_path"),
        "cover_status": data.get("cover_status"),
        "cover_url": f"/covers/{data['goodreads_id']}" if data.get("goodreads_id") else None,
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }
