"""FastAPI application factory for the Adso local web UI.

The app is intentionally a thin presentation layer: every route delegates to
the existing catalogue services in :mod:`adso.catalogue`, which run in-process
against the same SQLite file the CLI uses. No catalogue or sync logic is
duplicated here.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import activity as activity_service
from .. import categorize as categorize_service
from .. import conflicts as conflicts_service
from .. import covers as covers_service
from .. import db
from .. import dedupe as dedupe_service
from .. import exports as exports_service
from .. import metadata as metadata_service
from .. import reports as reports_service
from .. import sync as sync_service
from ..catalogue import (
    BookFilters,
    distinct_tags,
    get_book,
    list_books,
    search_books,
)
from ..config import ResolvedConfig, mask_secret
from ..notion import NotionConfigError, export_to_notion
from .library import (
    COVER_SHAPES,
    SHELF_LABELS,
    SORT_LABELS,
    SORTS,
    TO_READ_SHELF,
    LibraryParams,
    build_library,
    sort_books,
)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Muted tints for the generated placeholder shown when a book has no cover.
_PLACEHOLDER_TINTS = ("#6b7280", "#7c6f64", "#5f7470", "#6d6875", "#785964", "#4a6670")


def _placeholder_svg(label: str) -> str:
    """Build a small SVG cover placeholder (title initials on a tinted block).

    Generated inline so missing covers need no external request and work offline.
    """
    text = (label or "?").strip()
    words = [w for w in text.split() if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    initials = escape(initials)  # keep the SVG well-formed for titles like "& Sons"
    tint = _PLACEHOLDER_TINTS[sum(ord(c) for c in text) % len(_PLACEHOLDER_TINTS)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300" width="200" height="300">'
        f'<rect width="200" height="300" fill="{tint}"/>'
        f'<text x="100" y="150" fill="#ffffff" font-family="system-ui, sans-serif" '
        'font-size="72" font-weight="600" text-anchor="middle" dominant-baseline="central">'
        f"{initials}</text></svg>"
    )


class _AssetVersion:
    """Renders as a static file's mtime, re-read each time a template uses it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __str__(self) -> str:
        try:
            return str(int(self.path.stat().st_mtime))
        except OSError:
            return "0"


def _short_au_date(value: object) -> str:
    """Render a stored date as short Australian format (dd/mm/yy).

    Stored dates are ISO-ish strings (yyyy-mm-dd, occasionally yyyy/mm/dd);
    anything unparseable falls back to the raw string, and empty values render
    as an em dash.
    """
    if not value:
        return "—"
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%d/%m/%y")
        except ValueError:
            continue
    return text


# The exact book fields the JSON API (/api/books and /api/books/{id}) may
# serialize. It mirrors catalogue._book_result *minus* private_notes: an explicit
# allowlist, so any column added to _book_result later stays invisible to HTTP
# callers until it is named here — private-by-default, the same rule the MCP
# server applies via mcp_server.AGENT_BOOK_FIELDS. Kept as a dedicated web list
# (rather than reusing AGENT_BOOK_FIELDS) so the API can go on exposing the
# non-sensitive fields the agent surface intentionally omits — id, cover_url,
# cover_status, cover_path, timestamps — without coupling the two shapes together.
# The HTML pages and CLI still use the full _book_result, so this narrows only the
# JSON API, and only by dropping private_notes.
API_BOOK_FIELDS = (
    "id",
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
    "cover_path",
    "cover_status",
    "cover_url",
    "created_at",
    "updated_at",
    "primary_genre",
    "categories",
    "series",
)

# Guard: fields the JSON API must never serialize, asserted against
# API_BOOK_FIELDS at import time so a careless edit can't silently re-expose them.
_FORBIDDEN_API_BOOK_FIELDS = frozenset({"private_notes"})
assert _FORBIDDEN_API_BOOK_FIELDS.isdisjoint(API_BOOK_FIELDS)


def _book_to_api_dict(book: dict[str, object]) -> dict[str, object]:
    """Project a catalogue record onto the JSON-API allowlist (drops private_notes)."""
    return {field: book.get(field) for field in API_BOOK_FIELDS}


def create_app(db_path: str | Path, *, config: ResolvedConfig | None = None) -> FastAPI:
    """Build a FastAPI app bound to the SQLite database at ``db_path``.

    ``config`` carries the resolved profile + Notion target (from
    :func:`adso.config.load`), so the export surface can show the active target
    and drive a Notion export. When it is ``None`` the Notion affordance renders
    as "not configured" and never attempts a network write.
    """

    db_path = str(db_path)
    # Covers are stored beside the database; resolve relative cover_path values
    # against this root when serving them.
    cover_root = Path(db_path).resolve().parent

    def _notion_target() -> dict[str, object]:
        """How to describe the Notion export target on the export page."""
        configured = bool(config and config.notion_api_key and config.notion_database_id)
        return {
            "configured": configured,
            "profile": (config.profile if config else None) or "(none)",
            "target": (config.notion_target if config else None) or "(unnamed)",
            "database": mask_secret(config.notion_database_id) if config else "(unset)",
        }
    app = FastAPI(
        title="Adso",
        description="Local-first book catalogue web UI.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Cache-bust the stylesheet and script by their mtimes so browsers pick up a
    # rebuilt asset after an upgrade (or an edit mid-session) instead of serving
    # a stale cached copy. Read at render time; a stat per page is negligible.
    templates.env.globals["css_version"] = _AssetVersion(STATIC_DIR / "app.css")
    templates.env.globals["js_version"] = _AssetVersion(STATIC_DIR / "adso.js")
    templates.env.globals.update(sorts=SORTS, sort_labels=SORT_LABELS, shelf_labels=SHELF_LABELS)
    templates.env.filters["audate"] = _short_au_date
    templates.env.filters["series_pos"] = categorize_service.format_position
    templates.env.globals["facet_labels"] = categorize_service.FACET_LABELS

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Initialize the schema ONCE at startup. Doing it per request turned every
    # read (including the ~30 cover thumbnails a catalogue page fires at once)
    # into a write/commit, which contended on the SQLite write lock and returned
    # 500s under load. Requests now use read-only connections.
    _init_conn = db.connect(db_path)
    db.initialize(_init_conn)
    _init_conn.close()

    def get_conn() -> Iterator[sqlite3.Connection]:
        # A fresh connection per request keeps SQLite thread-safe under the
        # uvicorn worker threadpool. The schema is already initialized above.
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _nav_ctx(conn: sqlite3.Connection) -> dict:
        """Shared top-bar context: the combined Review badge count.

        Every full-page render needs the pending conflict + duplicate counts for
        the "More" menu badge; centralising it here keeps each route from
        recomputing the pair by hand (and defines the single aggregate the badge
        now shows).
        """
        pending = conflicts_service.pending_count(conn)
        dupes = dedupe_service.pending_count(conn)
        categories = categorize_service.pending_card_count(conn)
        return {
            "pending_count": pending,
            "duplicate_count": dupes,
            "category_count": categories,
            "review_count": pending + dupes + categories,
        }

    def _cat_ctx(conn: sqlite3.Connection, book: dict) -> dict:
        """What the book's category block (_book_categories.html) renders."""
        return {
            "bc": categorize_service.book_categories(conn, int(book["id"])),
            "choices": categorize_service.category_choices(conn),
        }

    def _rating_param(raw: str | None) -> int | None:
        # The catalogue form submits rating="" when "Any rating" is selected,
        # so this must be parsed by hand — a plain `int | None` query param
        # 422s on the empty string.
        if raw is None or raw.strip() == "":
            return None
        try:
            value = int(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="rating must be an integer from 0 to 5")
        if not 0 <= value <= 5:
            raise HTTPException(status_code=422, detail="rating must be between 0 and 5")
        return value

    def _filters(
        status: str | None,
        format: str | None,
        tag: str | None,
        author: str | None,
        rating: int | None,
        limit: int | None,
        exclude_shelf: str | None = None,
        sort: str | None = None,
    ) -> BookFilters:
        return BookFilters(
            status=status or None,
            format=format if format in db.VALID_FORMATS else None,
            tag=(tag or "").strip() or None,
            author=author or None,
            exclude_shelf=exclude_shelf,
            rating=rating,
            limit=limit,
            sort=sort if sort == "added" else None,
        )

    def _query_books(
        conn: sqlite3.Connection,
        q: str,
        filters: BookFilters,
    ) -> list[dict]:
        if q.strip():
            return search_books(conn, q, filters)
        return list_books(conn, filters)

    def _book_or_404(conn: sqlite3.Connection, goodreads_id: str) -> dict:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        book["ar"] = COVER_SHAPES.ratio(cover_root, book.get("cover_path"))
        return book

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        q: str = Query("", description="Search query"),
        shelf: str | None = Query(None),
        smart: str | None = Query(None),
        tag: str | None = Query(None),
        category: str | None = Query(None, description="Category id (includes subcategories)"),
        gr_shelf: str | None = Query(None, description="Any Goodreads shelf"),
        series: str | None = Query(None),
        status: str | None = Query(None),
        format: str | None = Query(None),
        author: str | None = Query(None),
        rating: str | None = Query(None),
        sort: str | None = Query(None),
        view: str | None = Query(None),
        book: str | None = Query(None, description="Goodreads ID to open in the book sidebar"),
    ) -> HTMLResponse:
        params = LibraryParams.clean(
            q=q, shelf=shelf, smart=smart, tag=tag, category=category, gr_shelf=gr_shelf, series=series,
            status=status, format=format, author=author, rating=_rating_param(rating), sort=sort,
            view=view, book=book,
        )
        library = build_library(conn, params, cover_root)
        # The book sidebar survives a reload (and a trip to the full page and
        # back) because its book rides in the URL.
        open_book = get_book(conn, params.book) if params.book else None
        if open_book is not None:
            open_book["ar"] = COVER_SHAPES.ratio(cover_root, open_book.get("cover_path"))
        return templates.TemplateResponse(
            request,
            "catalogue.html",
            {
                "lib": library,
                "p": library.params,
                "open_book": open_book,
                "all_tags": distinct_tags(conn),
                **(_cat_ctx(conn, open_book) if open_book is not None else {}),
                **_nav_ctx(conn),
            },
        )

    @app.get("/to-read")
    def to_read(
        q: str = Query(""),
        tag: str | None = Query(None),
        sort: str | None = Query(None),
    ) -> RedirectResponse:
        # To Read is now a shelf in the library sidebar; keep old links working.
        params = LibraryParams.clean(q=q, tag=tag, sort=sort, shelf=TO_READ_SHELF)
        return RedirectResponse(params.query(), status_code=307)

    @app.get("/book/{goodreads_id}/inspect", response_class=HTMLResponse)
    def book_inspect(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        """The book sidebar's contents, fetched when a cover is opened."""
        return templates.TemplateResponse(
            request,
            "_inspector.html",
            {"book": (book := _book_or_404(conn, goodreads_id)), "all_tags": distinct_tags(conn),
             **_cat_ctx(conn, book)},
        )

    @app.get("/book/{goodreads_id}", response_class=HTMLResponse)
    def book_detail(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        book = _book_or_404(conn, goodreads_id)
        by_author = [
            b for b in list_books(conn, BookFilters(author=book["author"]))
            if b["goodreads_id"] != goodreads_id and b["author"] == book["author"]
        ] if book.get("author") else []
        cat_ctx = _cat_ctx(conn, book)
        # The rest of the series, in reading order, ahead of the other strips.
        in_series: list[dict] = []
        if cat_ctx["bc"]["series"]:
            in_series = [
                b for b in list_books(conn, BookFilters(series=cat_ctx["bc"]["series"]["name"]))
                if b["goodreads_id"] != goodreads_id
            ]
            by_author = [b for b in by_author if b not in in_series]
        also_tagged: list[dict] = []
        if book["tags"]:
            seen = {goodreads_id, *(b["goodreads_id"] for b in by_author), *(b["goodreads_id"] for b in in_series)}
            also_tagged = [
                b for b in sort_books(list_books(conn, BookFilters(tag=book["tags"][0])), "added")
                if b["goodreads_id"] not in seen
            ]
        return templates.TemplateResponse(
            request,
            "book_detail.html",
            {
                "book": book,
                "all_tags": distinct_tags(conn),
                "by_author": sort_books(by_author, "year")[:12],
                "also_tagged": also_tagged[:12],
                "in_series": in_series[:24],
                **cat_ctx,
                "shelf_label": SHELF_LABELS.get(book.get("exclusive_shelf") or "", book.get("reading_status") or ""),
                **_nav_ctx(conn),
            },
        )

    def _bulk_ids(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
        books = [get_book(conn, i) for i in dict.fromkeys(ids)]
        return [b for b in books if b is not None]

    @app.post("/books/bulk/tags")
    def bulk_tag(
        conn: sqlite3.Connection = Depends(get_conn),
        ids: list[str] = Form(...),
        tag: str = Form(...),
    ) -> dict:
        """Add one tag to many books (the library's selection bar)."""
        target = db.normalize_tags(tag)
        if not target:
            raise HTTPException(status_code=422, detail="tag must not be blank")
        updated = 0
        for book in _bulk_ids(conn, ids):
            if target[0] not in book["tags"]:
                db.update_local_fields(conn, book["goodreads_id"], {"tags_json": [*book["tags"], target[0]]})
                updated += 1
        return {"updated": updated, "tag": target[0]}

    @app.post("/books/bulk/format")
    def bulk_format(
        conn: sqlite3.Connection = Depends(get_conn),
        ids: list[str] = Form(...),
        format: str = Form(""),
    ) -> dict:
        """Set (or clear) the owned format on many books at once."""
        value = format.strip() or None
        if value not in (None, *db.VALID_FORMATS):
            raise HTTPException(status_code=422, detail=f"Unsupported format {format!r}")
        books = _bulk_ids(conn, ids)
        for book in books:
            db.update_local_fields(conn, book["goodreads_id"], {"format": value})
        return {"updated": len(books), "format": value}

    # The local-catalogue fields (format, loaned_to, local_notes, tags) are all
    # LOCAL_FIELDS, which sync never touches, so editing them is always safe —
    # db.update_local_fields enforces that boundary (including rejecting unknown
    # format values). Every field is edited inline and autosaves on change; each
    # endpoint below re-renders just that one field's fragment.
    _EDIT_SCOPES = ("detail", "insp", "shelf", "table")

    def _scope(scope: str) -> str:
        # `scope` namespaces the element ids so a book can be edited in several
        # places at once (detail card, shelf popover, table popover). It rides in
        # from our own templates, but it lands in ids/hx-attributes, so clamp it
        # to the known set rather than reflect arbitrary input.
        return scope if scope in _EDIT_SCOPES else "detail"

    def _field_fragment(
        request: Request,
        conn: sqlite3.Connection,
        goodreads_id: str,
        template: str,
        scope: str,
        *,
        saved: bool = False,
        error: str | None = None,
        oob: bool = False,
    ) -> HTMLResponse:
        """Re-render a single local-field control with the stored value."""
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        return templates.TemplateResponse(
            request,
            template,
            {"book": book, "scope": _scope(scope), "saved": saved, "error": error, "oob": oob},
        )

    @app.get("/book/{goodreads_id}/local/panel", response_class=HTMLResponse)
    def local_panel(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        scope: str = Query("shelf"),
    ) -> HTMLResponse:
        """The quick-edit popover body for a list item, loaded lazily on open."""
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        return templates.TemplateResponse(
            request,
            "_local_panel.html",
            {"book": book, "scope": _scope(scope), "all_tags": distinct_tags(conn)},
        )

    @app.post("/book/{goodreads_id}/format", response_class=HTMLResponse)
    def local_format(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        format: str | None = Form(None),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        # Always emit the out-of-band table-cell swap so the Format badge on the
        # list stays in sync, whether the save took or was rejected.
        try:
            db.update_local_fields(conn, goodreads_id, {"format": (format or "").strip() or None})
        except ValueError as exc:
            return _field_fragment(
                request, conn, goodreads_id, "_field_format.html", scope, error=str(exc), oob=True
            )
        return _field_fragment(
            request, conn, goodreads_id, "_field_format.html", scope, saved=True, oob=True
        )

    @app.post("/book/{goodreads_id}/loaned", response_class=HTMLResponse)
    def local_loaned(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        loaned_to: str | None = Form(None),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        try:
            db.update_local_fields(
                conn, goodreads_id, {"loaned_to": (loaned_to or "").strip() or None}
            )
        except ValueError as exc:
            return _field_fragment(
                request, conn, goodreads_id, "_field_loaned.html", scope, error=str(exc)
            )
        return _field_fragment(request, conn, goodreads_id, "_field_loaned.html", scope, saved=True)

    @app.post("/book/{goodreads_id}/notes", response_class=HTMLResponse)
    def local_notes(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        local_notes: str | None = Form(None),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        try:
            db.update_local_fields(
                conn, goodreads_id, {"local_notes": (local_notes or "").strip() or None}
            )
        except ValueError as exc:
            return _field_fragment(
                request, conn, goodreads_id, "_field_notes.html", scope, error=str(exc)
            )
        return _field_fragment(request, conn, goodreads_id, "_field_notes.html", scope, saved=True)

    def _tags_fragment(
        request: Request,
        conn: sqlite3.Connection,
        goodreads_id: str,
        scope: str,
    ) -> HTMLResponse:
        """Re-render just the editable tag chips for one scope."""
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        return templates.TemplateResponse(
            request,
            "_local_tags.html",
            {"book": book, "scope": _scope(scope), "all_tags": distinct_tags(conn)},
        )

    @app.post("/book/{goodreads_id}/tags/add", response_class=HTMLResponse)
    def tag_add(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        tag: str | None = Form(None),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        # Append the raw input; update_local_fields runs normalize_tags, which
        # lowercases, trims, and dedupes — so a blank or duplicate is a no-op.
        db.update_local_fields(conn, goodreads_id, {"tags_json": [*book["tags"], tag or ""]})
        return _tags_fragment(request, conn, goodreads_id, scope)

    @app.post("/book/{goodreads_id}/tags/remove", response_class=HTMLResponse)
    def tag_remove(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        tag: str = Form(...),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        # Normalise the incoming tag the same way stored tags are, so the match is
        # case/whitespace-insensitive.
        target = db.normalize_tags(tag)
        remaining = [t for t in book["tags"] if [t] != target]
        db.update_local_fields(conn, goodreads_id, {"tags_json": remaining})
        return _tags_fragment(request, conn, goodreads_id, scope)

    # Categories are local data in their own tables (see adso.categorize): the
    # user's edits here are final — a run never overrides them, and a removed
    # category is remembered so rules don't add it back to this book.
    def _categories_fragment(
        request: Request,
        conn: sqlite3.Connection,
        goodreads_id: str,
        scope: str,
        action: Callable[[int], object] | None = None,
    ) -> HTMLResponse:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        error = None
        if action is not None:
            try:
                action(int(book["id"]))
            except categorize_service.CategoryError as exc:
                error = str(exc)
        return templates.TemplateResponse(
            request,
            "_book_categories.html",
            {"book": book, "scope": _scope(scope), "error": error, **_cat_ctx(conn, book)},
        )

    @app.post("/book/{goodreads_id}/categories/add", response_class=HTMLResponse)
    def category_add(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        category: str = Form(""),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        if not category.strip():
            return _categories_fragment(request, conn, goodreads_id, scope)
        return _categories_fragment(
            request, conn, goodreads_id, scope,
            lambda book_id: categorize_service.add_book_category(conn, book_id, category),
        )

    @app.post("/book/{goodreads_id}/categories/remove", response_class=HTMLResponse)
    def category_remove(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        category: str = Form(...),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        return _categories_fragment(
            request, conn, goodreads_id, scope,
            lambda book_id: categorize_service.remove_book_category(conn, book_id, category),
        )

    @app.post("/book/{goodreads_id}/categories/primary", response_class=HTMLResponse)
    def category_primary(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        category: str = Form(...),
        scope: str = Form("detail"),
    ) -> HTMLResponse:
        return _categories_fragment(
            request, conn, goodreads_id, scope,
            lambda book_id: categorize_service.set_primary_genre(conn, book_id, category),
        )

    @app.get("/covers/{goodreads_id}")
    def cover(
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        size: str | None = Query(None, description="'thumb' for a small cached copy"),
    ) -> Response:
        row = db.get_book_by_goodreads_id(conn, goodreads_id)
        if row is not None and row["cover_path"]:
            file_path = cover_root / row["cover_path"]
            if file_path.is_file():
                if size == "thumb":
                    # Dense views (wall, table, strips) load hundreds of covers
                    # at once; serve a cached small copy when one can be made.
                    file_path = covers_service.cover_thumbnail(file_path) or file_path
                return FileResponse(
                    file_path,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        label = row["title"] if row is not None else goodreads_id
        return Response(
            content=_placeholder_svg(label),
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/books")
    def api_books(
        conn: sqlite3.Connection = Depends(get_conn),
        q: str = Query("", description="Search query"),
        status: str | None = Query(None),
        format: str | None = Query(None),
        tag: str | None = Query(None),
        author: str | None = Query(None),
        rating: str | None = Query(None),
        category: str | None = Query(None, description="Category, e.g. 'genre:Fantasy' (includes subcategories)"),
        gr_shelf: str | None = Query(None, description="Any Goodreads shelf"),
        series: str | None = Query(None, description="Series name (reading order)"),
        limit: int | None = Query(None, ge=1),
    ) -> dict:
        filters = replace(
            _filters(status, format, tag, author, _rating_param(rating), limit),
            category=(category or "").strip() or None,
            gr_shelf=(gr_shelf or "").strip() or None,
            series=(series or "").strip() or None,
        )
        try:
            books = _query_books(conn, q, filters)
        except categorize_service.CategoryError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        categorize_service.attach_categories(conn, books)
        return {"count": len(books), "books": [_book_to_api_dict(b) for b in books]}

    @app.get("/api/books/{goodreads_id}")
    def api_book(
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        return _book_to_api_dict(categorize_service.attach_categories(conn, [book])[0])

    def _review_context(conn: sqlite3.Connection) -> dict:
        """Everything the consolidated Review page renders: field conflicts and
        suspected duplicate records, namespaced so the two sections don't collide.
        """
        conflict_groups = conflicts_service.list_open_conflicts(conn)
        duplicate_groups = dedupe_service.list_open_duplicates(conn)
        return {
            "category_cards": [_with_options(conn, c) for c in categorize_service.list_suggestion_cards(conn)],
            "choices": categorize_service.category_choices(conn),
            "conflict_groups": conflict_groups,
            "conflict_total": sum(len(group["conflicts"]) for group in conflict_groups),
            "decided": conflicts_service.list_decided_conflicts(conn),
            "deferred_count": conflicts_service.deferred_count(conn),
            "duplicate_groups": duplicate_groups,
            "duplicate_total": len(duplicate_groups),
            **_nav_ctx(conn),
        }

    def _with_options(conn: sqlite3.Connection, card: dict) -> dict:
        """Give a primary-genre card its candidate genres to choose between."""
        if card["kind"] == "primary":
            genres = categorize_service.book_categories(conn, card["book_id"])["by_facet"]["genre"]
            card["options"] = [
                {"id": g["id"], "path": g["path"], "ref": f"genre:{g['path']}"}
                for g in genres if g["source"] != "derived"
            ]
        return card

    def _card_after(
        request: Request, conn: sqlite3.Connection, card_id: int, remaining: list[int], message: str
    ) -> HTMLResponse:
        """What replaces a card after a decision.

        Deciding one source of a grouped card (``only``) leaves the rest of the
        card, which re-renders in the same slot (the client targets the card
        element, so its id may change to the new lead source). Otherwise the
        card collapses to a one-line outcome.
        """
        if remaining:
            for card in categorize_service.list_suggestion_cards(conn):
                if any(m["id"] in remaining for m in card["members"]):
                    return templates.TemplateResponse(
                        request, "_category_card.html",
                        {"card": _with_options(conn, card), "choices": categorize_service.category_choices(conn)},
                    )
        return templates.TemplateResponse(
            request, "_category_card_resolved.html", {"card_id": card_id, "message": message, **_nav_ctx(conn)}
        )

    @app.post("/categories/suggestions/{suggestion_id}/accept", response_class=HTMLResponse)
    def accept_category_suggestion(
        request: Request,
        suggestion_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
        as_category: str = Form(""),
        only: str = Form(""),
    ) -> HTMLResponse:
        try:
            members = categorize_service.suggestion_group(conn, suggestion_id)
            outcome = categorize_service.accept_suggestion(
                conn, suggestion_id, as_category=as_category.strip() or None, only=bool(only), actor="web"
            )
        except categorize_service.CategoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if outcome["kind"] == "map":
            message = (
                f"Mapped to {outcome['category']}: now on {outcome['books']} book(s), "
                "and on new ones after each sync."
            )
        elif outcome["kind"] == "primary":
            message = f"Primary genre set: {outcome['category']}."
        else:
            message = f"Added {outcome['category']}."
        remaining = [m for m in members if m != suggestion_id] if only else []
        return _card_after(request, conn, suggestion_id, remaining, message)

    @app.post("/categories/suggestions/{suggestion_id}/reject", response_class=HTMLResponse)
    def reject_category_suggestion(
        request: Request,
        suggestion_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
        only: str = Form(""),
    ) -> HTMLResponse:
        try:
            members = categorize_service.suggestion_group(conn, suggestion_id)
            item = categorize_service.reject_suggestion(conn, suggestion_id, only=bool(only), actor="web")
        except categorize_service.CategoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if item["kind"] == "primary":
            message = f"Rejected {item['target']} as the primary genre."
        else:
            message = f"Rejected: {item['target']} won't be suggested for these again."
        remaining = [m for m in members if m != suggestion_id] if only else []
        return _card_after(request, conn, suggestion_id, remaining, message)

    # ------------------------------------------------------------- taxonomy
    # The Categories page edits the user's vocabulary. Every action is a plain
    # form POST that redirects back with a message, so it works without JS;
    # merge and delete confirm in the browser with their impact first.

    def _category_ref(conn: sqlite3.Connection, category_id: int) -> str:
        taxonomy = categorize_service.Taxonomy(conn)
        if category_id not in taxonomy.by_id:
            raise HTTPException(status_code=404, detail=f"No category {category_id}")
        return f"{taxonomy.get(category_id).facet}:{taxonomy.path(category_id)}"

    def _taxonomy_redirect(message: str = "", error: str = "", anchor: str = "") -> RedirectResponse:
        query = urlencode({k: v for k, v in (("msg", message), ("error", error)) if v})
        return RedirectResponse(f"/taxonomy{'?' + query if query else ''}{anchor}", status_code=303)

    def _taxonomy_action(action: Callable[[], str], anchor: str = "") -> RedirectResponse:
        try:
            return _taxonomy_redirect(message=action(), anchor=anchor)
        except categorize_service.CategoryError as exc:
            detail = f"{exc} {exc.hint}" if exc.hint and "adso " not in exc.hint else str(exc)
            return _taxonomy_redirect(error=detail, anchor=anchor)

    @app.get("/taxonomy", response_class=HTMLResponse)
    def taxonomy_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        msg: str = Query(""),
        error: str = Query(""),
    ) -> HTMLResponse:
        aliases: dict[int, list[str]] = {}
        for row in conn.execute("SELECT category_id, alias FROM category_aliases ORDER BY alias"):
            aliases.setdefault(row["category_id"], []).append(row["alias"])
        facets = categorize_service.taxonomy_tree(conn)
        for facet in facets:
            for node in facet["categories"]:
                node["ref"] = f"{facet['facet']}:{node['path']}"
                node["aliases"] = aliases.get(node["id"], [])
                node.update(categorize_service.category_impact(conn, node["id"]))
        return templates.TemplateResponse(
            request,
            "taxonomy.html",
            {
                "facets": facets,
                "choices": categorize_service.category_choices(conn),
                "rules": categorize_service.list_rules(conn),
                "match_kinds": categorize_service.MATCH_KIND_LABELS,
                "msg": msg,
                "error": error,
                **_nav_ctx(conn),
            },
        )

    @app.post("/taxonomy/add")
    def taxonomy_add(
        conn: sqlite3.Connection = Depends(get_conn),
        facet: str = Form("genre"),
        parent: str = Form(""),
        name: str = Form(...),
    ) -> RedirectResponse:
        reference = f"{parent} > {name}" if parent.strip() else f"{facet}:{name}"

        def action() -> str:
            category = categorize_service.add_category(conn, reference)
            return f"Added {categorize_service.Taxonomy(conn).display(category.id)}."

        return _taxonomy_action(action)

    @app.post("/taxonomy/{category_id}/rename")
    def taxonomy_rename(
        category_id: int, conn: sqlite3.Connection = Depends(get_conn), label: str = Form(...)
    ) -> RedirectResponse:
        ref = _category_ref(conn, category_id)
        return _taxonomy_action(
            lambda: f"Renamed to {categorize_service.rename_category(conn, ref, label).label}; the old name still matches.",
            f"#cat-{category_id}",
        )

    @app.post("/taxonomy/{category_id}/move")
    def taxonomy_move(
        category_id: int, conn: sqlite3.Connection = Depends(get_conn), parent: str = Form("")
    ) -> RedirectResponse:
        ref = _category_ref(conn, category_id)

        def action() -> str:
            moved = categorize_service.move_category(conn, ref, parent.strip() or None)
            return f"Moved to {categorize_service.Taxonomy(conn).display(moved.id)}."

        return _taxonomy_action(action, f"#cat-{category_id}")

    @app.post("/taxonomy/{category_id}/merge")
    def taxonomy_merge(
        category_id: int, conn: sqlite3.Connection = Depends(get_conn), target: str = Form(...)
    ) -> RedirectResponse:
        ref = _category_ref(conn, category_id)

        def action() -> str:
            result = categorize_service.merge_categories(conn, ref, target)
            return f"Merged {result['source']} into {result['target']} ({result['books']} book(s) moved)."

        return _taxonomy_action(action)

    @app.post("/taxonomy/{category_id}/delete")
    def taxonomy_delete(category_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> RedirectResponse:
        ref = _category_ref(conn, category_id)

        def action() -> str:
            result = categorize_service.delete_category(conn, ref)
            return f"Deleted {result['category']} (removed from {result['books']} book(s))."

        return _taxonomy_action(action)

    @app.post("/taxonomy/{category_id}/alias")
    def taxonomy_alias(
        category_id: int, conn: sqlite3.Connection = Depends(get_conn), alias: str = Form(...)
    ) -> RedirectResponse:
        ref = _category_ref(conn, category_id)
        return _taxonomy_action(
            lambda: f"“{alias.strip()}” now also matches {categorize_service.add_alias(conn, ref, alias).label}.",
            f"#cat-{category_id}",
        )

    @app.post("/taxonomy/map")
    def taxonomy_map(
        conn: sqlite3.Connection = Depends(get_conn),
        kind: str = Form("shelf"),
        value: str = Form(...),
        to: str = Form(...),
    ) -> RedirectResponse:
        def action() -> str:
            outcome = categorize_service.add_rule(conn, kind, value, to, actor="web")
            return f"Mapped {kind} “{value.strip()}” to {outcome['category']} ({outcome['books']} book(s))."

        return _taxonomy_action(action, "#rules")

    @app.post("/taxonomy/rules/{rule_id}/delete")
    def taxonomy_unmap(rule_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> RedirectResponse:
        def action() -> str:
            rule = categorize_service.delete_rule(conn, rule_id)
            return f"Removed the rule for {rule['match_label']} “{rule['match_value']}” and its {rule['books']} assignment(s)."

        return _taxonomy_action(action, "#rules")

    @app.get("/api/taxonomy")
    def api_taxonomy(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"facets": categorize_service.taxonomy_tree(conn)}

    @app.get("/review", response_class=HTMLResponse)
    def review_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, "review.html", _review_context(conn))

    # The old split pages now live as sections of Review; keep the URLs working
    # for bookmarks and CLI-printed links.
    @app.get("/conflicts")
    def conflicts_redirect() -> RedirectResponse:
        return RedirectResponse("/review", status_code=307)

    @app.get("/duplicates")
    def duplicates_redirect() -> RedirectResponse:
        return RedirectResponse("/review", status_code=307)

    @app.post("/conflicts/{conflict_id}/resolve", response_class=HTMLResponse)
    def resolve_conflict(
        request: Request,
        conflict_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
        choice: str = Form(...),
        value: str | None = Form(None),
    ) -> HTMLResponse:
        try:
            outcome = conflicts_service.resolve_conflict(
                conn, conflict_id, choice=choice, custom_value=value, actor="web"
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Reopening returns the field to its editable state in place; every other
        # decision collapses it to the decided summary.
        if choice == "reopen":
            return templates.TemplateResponse(
                request,
                "_conflict_field.html",
                {
                    "c": conflicts_service.conflict_field_view(conn, conflict_id),
                    "swap": True,
                    **_nav_ctx(conn),
                },
            )
        return templates.TemplateResponse(
            request,
            "_conflict_field_resolved.html",
            {"swap": True, **outcome, **_nav_ctx(conn)},
        )

    @app.post("/conflicts/book/{book_id}/resolve", response_class=HTMLResponse)
    def resolve_book_conflicts(
        request: Request,
        book_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
        choice: str = Form(...),
    ) -> HTMLResponse:
        try:
            outcome = conflicts_service.resolve_book(conn, book_id, choice=choice, actor="web")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return templates.TemplateResponse(
            request,
            "_conflict_group_resolved.html",
            {**outcome, **_nav_ctx(conn)},
        )

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        runs = activity_service.list_activity(conn)
        return templates.TemplateResponse(
            request,
            "activity.html",
            {
                "runs": runs,
                "latest": runs[0] if runs else None,
                **_nav_ctx(conn),
            },
        )

    @app.get("/import", response_class=HTMLResponse)
    def import_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "import.html",
            {
                **_nav_ctx(conn),
            },
        )

    @app.post("/import", response_class=HTMLResponse)
    def import_upload(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        file: UploadFile = File(...),
    ) -> HTMLResponse:
        filename = os.path.basename(file.filename or "") or "upload.csv"
        context: dict = {"filename": filename}

        if not filename.lower().endswith(".csv"):
            context["error"] = "Please choose a Goodreads CSV export (a .csv file)."
        else:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                    tmp.write(file.file.read())
                    tmp_path = tmp.name
                # Label the run "import" on an empty catalogue, otherwise "sync".
                # Behaviour is identical either way; this just reads naturally in Activity.
                book_count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
                mode = "import" if book_count == 0 else "sync"
                summary = sync_service.import_goodreads_csv(
                    conn, tmp_path, mode=mode, source_label=filename
                )
                context["summary"] = {
                    "mode": summary.mode,
                    "row_count": summary.row_count,
                    "created": summary.created,
                    "updated": summary.updated,
                    "unchanged": summary.unchanged,
                    "conflicts": summary.conflicts,
                    "skipped": summary.skipped,
                }
                # Best-effort cover enrichment; never let a network hiccup
                # break the import the user just performed.
                try:
                    cover_result = covers_service.fetch_covers(conn, cover_root)
                    context["covers"] = {
                        "fetched": cover_result["fetched"],
                        "not_found": cover_result["not_found"],
                        "errors": cover_result["errors"],
                    }
                except covers_service.CoversError:
                    context["covers"] = None
                # Same best-effort posture for Open Library metadata.
                try:
                    metadata_result = metadata_service.fetch_metadata(conn)
                    context["metadata"] = {
                        "fetched": metadata_result["fetched"],
                        "not_found": metadata_result["not_found"],
                        "errors": metadata_result["errors"],
                        "isbn_backfilled": metadata_result["isbn_backfilled"],
                    }
                except metadata_service.MetadataError:
                    context["metadata"] = None
                # Apply the user's accepted category rules to new books (local only).
                categorize_service.categorize(conn)
            except Exception as exc:  # noqa: BLE001 - surface any parse/IO error to the user
                context["error"] = f"Could not import that file: {exc}"
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        context.update(_nav_ctx(conn))
        return templates.TemplateResponse(request, "import.html", context)

    @app.post("/duplicates/scan", response_class=HTMLResponse)
    def scan_duplicates(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        # The "Re-scan" button swaps the whole body, so re-render the full Review
        # page (nav badge included) with the freshly scanned duplicate groups.
        dedupe_service.scan_duplicates(conn)
        return templates.TemplateResponse(request, "review.html", _review_context(conn))

    @app.post("/duplicates/merge", response_class=HTMLResponse)
    def merge_duplicate(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        group_key: str = Form(...),
        keep_id: int = Form(...),
    ) -> HTMLResponse:
        try:
            outcome = dedupe_service.merge_duplicate(conn, group_key, keep_id=keep_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return templates.TemplateResponse(
            request,
            "_duplicate_resolved.html",
            {**outcome, **_nav_ctx(conn)},
        )

    @app.post("/duplicates/dismiss", response_class=HTMLResponse)
    def dismiss_duplicate(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        group_key: str = Form(...),
    ) -> HTMLResponse:
        outcome = dedupe_service.dismiss_duplicate(conn, group_key)
        return templates.TemplateResponse(
            request,
            "_duplicate_resolved.html",
            {**outcome, **_nav_ctx(conn)},
        )

    @app.get("/export", response_class=HTMLResponse)
    def export_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "export.html",
            {
                "notion": _notion_target(),
                "book_count": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
                **_nav_ctx(conn),
            },
        )

    @app.get("/export/catalogue.csv")
    def export_catalogue_csv(conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return Response(
            content=exports_service.catalogue_csv_string(conn),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=adso-catalogue.csv"},
        )

    @app.get("/export/catalogue.json")
    def export_catalogue_json(conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        return Response(
            content=exports_service.catalogue_json_string(conn),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=adso-catalogue.json"},
        )

    @app.post("/export/notion", response_class=HTMLResponse)
    def export_notion(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        dry_run: bool = Form(False),
    ) -> HTMLResponse:
        # A real Notion export is a network write to the user's own database, so
        # the UI offers a dry-run preview first; the actual write is explicit.
        try:
            result = export_to_notion(
                conn,
                api_key=config.notion_api_key if config else None,
                database_id=config.notion_database_id if config else None,
                dry_run=dry_run,
            )
        except NotionConfigError as exc:
            return templates.TemplateResponse(
                request, "_notion_result.html", {"error": str(exc)}
            )
        return templates.TemplateResponse(
            request,
            "_notion_result.html",
            {"result": result, "dry_run": dry_run},
        )

    @app.get("/reports/summary", response_class=HTMLResponse)
    def report_summary(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        return _report_page(request, conn, "Sync summary", reports_service.latest_sync_summary_markdown(conn))

    @app.get("/reports/conflicts", response_class=HTMLResponse)
    def report_conflicts(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        return _report_page(request, conn, "Conflict report", reports_service.latest_conflicts_markdown(conn))

    def _report_page(request: Request, conn: sqlite3.Connection, title: str, body: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "report_title": title,
                "report_body": body,
                **_nav_ctx(conn),
            },
        )

    @app.get("/api/conflicts")
    def api_conflicts(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        groups = conflicts_service.list_open_conflicts(conn)
        return {"pending": conflicts_service.pending_count(conn), "books": groups}

    @app.get("/api/activity")
    def api_activity(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        runs = activity_service.list_activity(conn)
        return {"count": len(runs), "runs": runs}

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "db": db_path}

    return app
