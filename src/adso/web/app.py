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
from collections.abc import Iterator
from html import escape
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import activity as activity_service
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
    distinct_statuses,
    distinct_tags,
    get_book,
    list_books,
    search_books,
)
from ..config import ResolvedConfig, mask_secret
from ..notion import NotionConfigError, export_to_notion

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

    def _filters(
        status: str | None,
        format: str | None,
        tag: str | None,
        author: str | None,
        limit: int | None,
    ) -> BookFilters:
        return BookFilters(
            status=status or None,
            format=format if format in db.VALID_FORMATS else None,
            tag=(tag or "").strip() or None,
            author=author or None,
            limit=limit,
        )

    def _query_books(
        conn: sqlite3.Connection,
        q: str,
        filters: BookFilters,
    ) -> list[dict]:
        if q.strip():
            return search_books(conn, q, filters)
        return list_books(conn, filters)

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        q: str = Query("", description="Search query"),
        status: str | None = Query(None),
        format: str | None = Query(None),
        tag: str | None = Query(None),
        author: str | None = Query(None),
        limit: int | None = Query(None, ge=1),
    ) -> HTMLResponse:
        filters = _filters(status, format, tag, author, limit)
        books = _query_books(conn, q, filters)
        return templates.TemplateResponse(
            request,
            "catalogue.html",
            {
                "books": books,
                "q": q,
                "status": status or "",
                "format": format or "",
                "tag": tag or "",
                "author": author or "",
                "statuses": distinct_statuses(conn),
                "formats": db.VALID_FORMATS,
                "tags": distinct_tags(conn),
                "count": len(books),
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": dedupe_service.pending_count(conn),
            },
        )

    @app.get("/book/{goodreads_id}", response_class=HTMLResponse)
    def book_detail(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        return templates.TemplateResponse(
            request,
            "book_detail.html",
            {
                "book": book,
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": dedupe_service.pending_count(conn),
            },
        )

    def _local_card(
        request: Request,
        conn: sqlite3.Connection,
        goodreads_id: str,
        *,
        editing: bool = False,
        saved: bool = False,
        error: str | None = None,
    ) -> HTMLResponse:
        """Render the local-catalogue card, read-only or as an edit form."""
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        template = "_local_fields_form.html" if editing else "_local_fields.html"
        return templates.TemplateResponse(
            request, template, {"book": book, "saved": saved, "error": error}
        )

    @app.get("/book/{goodreads_id}/local", response_class=HTMLResponse)
    def local_fields(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        return _local_card(request, conn, goodreads_id)

    @app.get("/book/{goodreads_id}/local/edit", response_class=HTMLResponse)
    def local_fields_edit(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        return _local_card(request, conn, goodreads_id, editing=True)

    @app.post("/book/{goodreads_id}/local", response_class=HTMLResponse)
    def local_fields_save(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        format: str | None = Form(None),
        loaned_to: str | None = Form(None),
        local_notes: str | None = Form(None),
    ) -> HTMLResponse:
        # These are all LOCAL_FIELDS, which sync never touches, so editing them is
        # always safe — db.update_local_fields enforces that boundary (including
        # rejecting unknown format values). Tags are edited inline via the
        # /tags/add and /tags/remove endpoints, so this form deliberately leaves
        # tags_json untouched.
        def _clean(value: str | None) -> str | None:
            value = (value or "").strip()
            return value or None

        updates = {
            "format": _clean(format),
            "loaned_to": _clean(loaned_to),
            "local_notes": (local_notes or "").strip() or None,
        }
        try:
            db.update_local_fields(conn, goodreads_id, updates)
        except ValueError as exc:
            return _local_card(request, conn, goodreads_id, editing=True, error=str(exc))
        return _local_card(request, conn, goodreads_id, saved=True)

    def _tags_fragment(
        request: Request,
        conn: sqlite3.Connection,
        goodreads_id: str,
    ) -> HTMLResponse:
        """Re-render just the editable tag chips (the Tags <dd>)."""
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        return templates.TemplateResponse(request, "_local_tags.html", {"book": book})

    @app.post("/book/{goodreads_id}/tags/add", response_class=HTMLResponse)
    def tag_add(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        tag: str | None = Form(None),
    ) -> HTMLResponse:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        # Append the raw input; update_local_fields runs normalize_tags, which
        # lowercases, trims, and dedupes — so a blank or duplicate is a no-op.
        db.update_local_fields(conn, goodreads_id, {"tags_json": [*book["tags"], tag or ""]})
        return _tags_fragment(request, conn, goodreads_id)

    @app.post("/book/{goodreads_id}/tags/remove", response_class=HTMLResponse)
    def tag_remove(
        request: Request,
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
        tag: str = Form(...),
    ) -> HTMLResponse:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        # Normalise the incoming tag the same way stored tags are, so the match is
        # case/whitespace-insensitive.
        target = db.normalize_tags(tag)
        remaining = [t for t in book["tags"] if [t] != target]
        db.update_local_fields(conn, goodreads_id, {"tags_json": remaining})
        return _tags_fragment(request, conn, goodreads_id)

    @app.get("/covers/{goodreads_id}")
    def cover(
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Response:
        row = db.get_book_by_goodreads_id(conn, goodreads_id)
        if row is not None and row["cover_path"]:
            file_path = cover_root / row["cover_path"]
            if file_path.is_file():
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
        limit: int | None = Query(None, ge=1),
    ) -> dict:
        filters = _filters(status, format, tag, author, limit)
        books = _query_books(conn, q, filters)
        return {"count": len(books), "books": books}

    @app.get("/api/books/{goodreads_id}")
    def api_book(
        goodreads_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        book = get_book(conn, goodreads_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"No book for Goodreads ID {goodreads_id}")
        return book

    @app.get("/conflicts", response_class=HTMLResponse)
    def conflicts_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        groups = conflicts_service.list_open_conflicts(conn)
        decided = conflicts_service.list_decided_conflicts(conn)
        total = sum(len(group["conflicts"]) for group in groups)
        return templates.TemplateResponse(
            request,
            "conflicts.html",
            {
                "groups": groups,
                "decided": decided,
                "total": total,
                "pending_count": conflicts_service.pending_count(conn),
                "deferred_count": conflicts_service.deferred_count(conn),
                "duplicate_count": dedupe_service.pending_count(conn),
            },
        )

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
                    "pending_count": conflicts_service.pending_count(conn),
                },
            )
        return templates.TemplateResponse(
            request,
            "_conflict_field_resolved.html",
            {"swap": True, "pending_count": conflicts_service.pending_count(conn), **outcome},
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
            {"pending_count": conflicts_service.pending_count(conn), **outcome},
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
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": dedupe_service.pending_count(conn),
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
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": dedupe_service.pending_count(conn),
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
            except Exception as exc:  # noqa: BLE001 - surface any parse/IO error to the user
                context["error"] = f"Could not import that file: {exc}"
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        context["pending_count"] = conflicts_service.pending_count(conn)
        context["duplicate_count"] = dedupe_service.pending_count(conn)
        return templates.TemplateResponse(request, "import.html", context)

    @app.get("/duplicates", response_class=HTMLResponse)
    def duplicates_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        groups = dedupe_service.list_open_duplicates(conn)
        return templates.TemplateResponse(
            request,
            "duplicates.html",
            {
                "groups": groups,
                "total": len(groups),
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": len(groups),
            },
        )

    @app.post("/duplicates/scan", response_class=HTMLResponse)
    def scan_duplicates(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> HTMLResponse:
        dedupe_service.scan_duplicates(conn)
        groups = dedupe_service.list_open_duplicates(conn)
        return templates.TemplateResponse(
            request,
            "duplicates.html",
            {
                "groups": groups,
                "total": len(groups),
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": len(groups),
            },
        )

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
            {"duplicate_count": dedupe_service.pending_count(conn), **outcome},
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
            {"duplicate_count": dedupe_service.pending_count(conn), **outcome},
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
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": dedupe_service.pending_count(conn),
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
                "pending_count": conflicts_service.pending_count(conn),
                "duplicate_count": dedupe_service.pending_count(conn),
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
