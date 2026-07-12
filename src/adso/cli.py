"""Command line interface for Adso."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import __version__, branding, db
from . import config as config_module
from . import conflicts as conflicts_service
from . import dedupe as dedupe_service
from . import doctor as doctor_module
from .catalogue import BookFilters, get_book, list_books, search_books
from .config import DEFAULT_DB, ResolvedConfig
from .covers import CoversError, fetch_covers, set_manual_cover
from .doctor import doctor_report
from .errors import AdsoError
from .exports import export_csv, export_json
from .metadata import MetadataError, fetch_metadata
from .notion import NotionConfigError, export_to_notion
from .reports import (
    latest_conflicts_markdown,
    latest_sync_summary_markdown,
    write_latest_conflicts,
    write_latest_sync_summary,
)
from .sync import import_goodreads_csv


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return _welcome(args, parser)
    try:
        return _dispatch(args, parser)
    except AdsoError as exc:
        return _fail(exc, hint=exc.hint)
    except NotionConfigError as exc:
        return _fail(
            exc,
            hint="Set NOTION_API_KEY / NOTION_DB_ID, or pick a profile with `adso config use`.",
        )
    except (CoversError, MetadataError) as exc:
        return _fail(exc)
    except sqlite3.DatabaseError as exc:
        return _fail(f"catalogue database problem: {exc}", hint="Try `adso doctor`.")
    except (FileNotFoundError, PermissionError, IsADirectoryError) as exc:
        return _fail(exc)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


def _welcome(args, parser) -> int:
    """Greet a bare `adso` invocation: quickstart for new users, help otherwise."""
    cfg = config_module.load(db_arg=args.db, profile_arg=args.profile)
    db_state = doctor_module._inspect_database(Path(cfg.db_path))
    config_present = (
        config_module.project_config_path().exists()
        or config_module.user_config_path().exists()
    )

    first_run = not db_state["exists"] and not config_present
    if first_run:
        csv_files = doctor_module._find_goodreads_csvs(Path.cwd())
        csv_hint = csv_files[0] if csv_files else None
        print(branding.render_welcome(csv_hint=csv_hint))
    else:
        print(branding.render_help(parser.prog))
    return 0


def _fail(error: object, *, hint: str | None = None) -> int:
    print(f"Error: {error}", file=sys.stderr)
    if hint:
        print(f"Next: {hint}", file=sys.stderr)
    return 1


def _dispatch(args, parser) -> int:
    if args.command == "config":
        return _run_config(args, parser)

    cfg = config_module.load(db_arg=args.db, profile_arg=args.profile)

    if args.command == "doctor":
        print(doctor_report(cfg.db_path, config=cfg))
        return 0

    if args.command == "serve":
        return _run_server(
            cfg.db_path,
            config=cfg,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )

    conn = db.connect(cfg.db_path)
    try:
        if args.command == "init":
            db.initialize(conn)
            print(f"Initialized Adso catalogue at {cfg.db_path}")
            return 0

        db.initialize(conn)

        if args.command == "list":
            books = list_books(conn, _book_filters_from_args(args))
            print(_format_book_table(books))
            return 0

        if args.command == "search":
            books = search_books(conn, args.query, _book_filters_from_args(args))
            print(_format_book_table(books))
            return 0

        if args.command == "show":
            book = get_book(conn, args.goodreads_id)
            if book is None:
                parser.error(f"No book found for Goodreads ID {args.goodreads_id}")
            print(_format_book_detail(book))
            return 0

        # `import` and `sync` run the same safe, idempotent operation; the command
        # name only sets the run label (a friendly name for the first load vs. a
        # later refresh). Both must surface conflicts — earlier, `import` recorded
        # conflicts in the database but wrote no report, so they passed silently.
        if args.command in ("import", "sync") and args.source == "goodreads":
            summary = import_goodreads_csv(conn, args.csv, mode=args.command)
            print(latest_sync_summary_markdown(conn))
            if summary.conflicts:
                output = Path("reports") / f"conflicts-import-{summary.import_run_id}.md"
                write_latest_conflicts(conn, output)
                print(f"Conflict report: {output}")
            if not args.no_covers:
                _auto_fetch_covers(conn, cfg.db_path)
            if not args.no_metadata:
                _auto_fetch_metadata(conn)
            return 0

        if args.command == "fetch-covers":
            result = fetch_covers(
                conn,
                _data_dir(cfg.db_path),
                limit=args.limit,
                refresh=args.refresh,
                retry_missing=args.retry_missing,
                dry_run=args.dry_run,
            )
            print(_format_cover_result(result, dry_run=args.dry_run))
            return 0

        if args.command == "fetch-metadata":
            result = fetch_metadata(
                conn,
                limit=args.limit,
                refresh=args.refresh,
                retry_missing=args.retry_missing,
                dry_run=args.dry_run,
            )
            print(_format_metadata_result(result, dry_run=args.dry_run))
            return 0

        if args.command == "set-cover":
            outcome = set_manual_cover(
                conn, _data_dir(cfg.db_path), args.goodreads_id, url=args.url, file=args.file
            )
            print(f"Set manual cover for {outcome['title']} → {outcome['cover_path']}")
            return 0

        if args.command == "edit":
            updates = _local_updates_from_args(args)
            if not updates:
                parser.error("No local fields provided to update.")
            db.update_local_fields(conn, args.goodreads_id, updates)
            print(f"Updated local catalogue fields for Goodreads ID {args.goodreads_id}")
            return 0

        if args.command == "conflicts":
            groups = conflicts_service.list_open_conflicts(conn)
            output = _format_conflicts(groups)
            if args.all:
                decided = conflicts_service.list_decided_conflicts(conn)
                output += "\n\n" + _format_decided_conflicts(decided)
            print(output)
            return 0

        if args.command == "dedupe":
            dedupe_service.scan_duplicates(conn)
            groups = dedupe_service.list_open_duplicates(conn)
            print(_format_duplicates(groups))
            return 0

        if args.command == "resolve":
            if args.accept_incoming:
                choice, custom = "incoming", None
            elif args.ignore:
                choice, custom = "ignore", None
            elif args.review_later:
                choice, custom = "later", None
            elif args.reopen:
                choice, custom = "reopen", None
            elif args.set is not None:
                choice, custom = "custom", args.set
            else:
                choice, custom = "local", None
            try:
                outcome = conflicts_service.resolve_conflict(
                    conn, args.conflict_id, choice=choice, custom_value=custom, actor="cli"
                )
            except ValueError as exc:
                raise AdsoError(
                    str(exc), hint="Run `adso conflicts` to list open conflict IDs."
                ) from exc
            message = f"Conflict {args.conflict_id} ({outcome['field_label']}): {outcome['resolution_label']}"
            if outcome["value"]:
                message += f" → {outcome['value']}"
            print(message)
            return 0

        if args.command == "report" and args.report_type == "conflicts":
            if args.output:
                path = write_latest_conflicts(conn, args.output)
                print(f"Wrote conflict report to {path}")
            else:
                print(latest_conflicts_markdown(conn))
            return 0

        if args.command == "report" and args.report_type == "summary":
            if args.output:
                path = write_latest_sync_summary(conn, args.output)
                print(f"Wrote sync summary to {path}")
            else:
                print(latest_sync_summary_markdown(conn))
            return 0

        if args.command == "export" and args.target == "csv":
            path = export_csv(conn, args.output)
            print(f"Exported catalogue CSV to {path}")
            return 0

        if args.command == "export" and args.target == "json":
            path = export_json(conn, args.output)
            print(f"Exported catalogue JSON to {path}")
            return 0

        if args.command == "export" and args.target == "notion":
            print(_notion_target_banner(cfg))
            result = export_to_notion(
                conn,
                api_key=cfg.notion_api_key,
                database_id=cfg.notion_database_id,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            print(_format_notion_export_result(result, dry_run=args.dry_run))
            return 0

        parser.error("Unsupported command.")
        return 2
    finally:
        conn.close()


def _run_server(
    db_path: str,
    *,
    config: ResolvedConfig | None = None,
    host: str,
    port: int,
    open_browser: bool,
) -> int:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise AdsoError(
            "The web UI needs extra dependencies.",
            hint="Install them with: pip install -e '.[web]'",
        ) from exc

    from .web.app import create_app

    # Make sure the catalogue file exists and is initialized before serving.
    conn = db.connect(db_path)
    db.initialize(conn)
    conn.close()

    app = create_app(db_path, config=config)
    url = f"http://{host}:{port}"
    print(f"Adso web UI running at {url}  (Ctrl+C to stop)")

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


class _BrandedParser(argparse.ArgumentParser):
    """Top-level parser whose `--help` renders the branded Adso help screen.

    Subparsers stay vanilla, so `adso <command> --help` keeps argparse's detail.
    """

    def format_help(self) -> str:
        return branding.render_help(self.prog) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = _BrandedParser(prog="adso", description="Adso local-first book catalogue")
    parser.add_argument(
        "--db",
        default=None,
        help=f"SQLite database path (overrides the active profile; default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Configuration profile to use (see `adso config`)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"adso {__version__}",
        help="Show the Adso version and exit",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=False, parser_class=argparse.ArgumentParser
    )

    subparsers.add_parser("init", help="Initialize the local catalogue database")
    subparsers.add_parser("doctor", help="Check local Adso setup and suggest next commands")

    _add_config_parser(subparsers)

    serve_parser = subparsers.add_parser("serve", help="Run the local web UI")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    serve_parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")

    list_parser = subparsers.add_parser("list", help="List books in the local catalogue")
    list_parser.add_argument("--status", help="Filter by reading status, e.g. 'Read' or 'To Read'")
    list_parser.add_argument(
        "--format", choices=["physical", "ebook", "audiobook"], help="Filter by owned format"
    )
    list_parser.add_argument("--tag", help="Filter by a local tag, e.g. 'philosophy'")
    list_parser.add_argument("--author", help="Filter by author")
    list_parser.add_argument("--shelf", help="Filter by exclusive shelf, e.g. 'read' or 'to-read'")
    list_parser.add_argument(
        "--rating",
        type=int,
        choices=range(0, 6),
        help="Filter by your star rating (0 = unrated)",
    )
    list_parser.add_argument("--limit", type=int, help="Maximum number of books to show")

    search_parser = subparsers.add_parser("search", help="Search books in the local catalogue")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--status", help="Filter by reading status, e.g. 'Read' or 'To Read'")
    search_parser.add_argument(
        "--format", choices=["physical", "ebook", "audiobook"], help="Filter by owned format"
    )
    search_parser.add_argument("--tag", help="Filter by a local tag, e.g. 'philosophy'")
    search_parser.add_argument("--author", help="Filter by author")
    search_parser.add_argument("--shelf", help="Filter by exclusive shelf, e.g. 'read' or 'to-read'")
    search_parser.add_argument(
        "--rating",
        type=int,
        choices=range(0, 6),
        help="Filter by your star rating (0 = unrated)",
    )
    search_parser.add_argument("--limit", type=int, help="Maximum number of books to show")

    show_parser = subparsers.add_parser("show", help="Show detailed information for one book")
    show_parser.add_argument("goodreads_id", help="Goodreads Book ID")

    import_parser = subparsers.add_parser(
        "import", help="Load source data (alias of `sync`; conventional name for the first load)"
    )
    import_sub = import_parser.add_subparsers(dest="source", required=True)
    goodreads_import = import_sub.add_parser("goodreads", help="Import a Goodreads CSV export")
    goodreads_import.add_argument("csv", help="Path to Goodreads CSV export")
    goodreads_import.add_argument(
        "--no-covers", action="store_true", help="Skip the automatic cover-art fetch after import"
    )
    goodreads_import.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip the automatic Open Library metadata fetch after import",
    )

    sync_parser = subparsers.add_parser(
        "sync", help="Refresh the catalogue from source data (same safe operation as `import`)"
    )
    sync_sub = sync_parser.add_subparsers(dest="source", required=True)
    goodreads_sync = sync_sub.add_parser("goodreads", help="Sync a Goodreads CSV export")
    goodreads_sync.add_argument("csv", help="Path to Goodreads CSV export")
    goodreads_sync.add_argument(
        "--no-covers", action="store_true", help="Skip the automatic cover-art fetch after sync"
    )
    goodreads_sync.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip the automatic Open Library metadata fetch after sync",
    )

    covers_parser = subparsers.add_parser("fetch-covers", help="Download missing cover art")
    covers_parser.add_argument("--limit", type=int, help="Maximum number of books to fetch covers for")
    covers_parser.add_argument(
        "--refresh", action="store_true", help="Re-fetch even books already tried (skips manual covers)"
    )
    covers_parser.add_argument(
        "--retry-missing",
        action="store_true",
        help="Re-attempt books previously marked not found (keeps already-fetched covers)",
    )
    covers_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be fetched without writing files"
    )

    metadata_parser = subparsers.add_parser(
        "fetch-metadata",
        help="Download descriptions and subjects from Open Library",
        description=(
            "Fetch work descriptions, subjects, and place/time facets from Open "
            "Library; books with no ISBN also get empty ISBN columns backfilled "
            "from the matched edition. A full first run over a large catalogue "
            "takes ~2 requests per book at a polite delay (about 35 minutes per "
            "1000 books) — use --limit to enrich in batches."
        ),
    )
    metadata_parser.add_argument("--limit", type=int, help="Maximum number of books to fetch metadata for")
    metadata_parser.add_argument(
        "--refresh", action="store_true", help="Re-fetch even books already enriched"
    )
    metadata_parser.add_argument(
        "--retry-missing",
        action="store_true",
        help="Re-attempt books previously marked not found (keeps already-fetched metadata)",
    )
    metadata_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be fetched without writing"
    )

    set_cover_parser = subparsers.add_parser("set-cover", help="Set a cover from a URL or local file")
    set_cover_parser.add_argument("goodreads_id", help="Goodreads Book ID")
    set_cover_group = set_cover_parser.add_mutually_exclusive_group(required=True)
    set_cover_group.add_argument("--url", help="Image URL to download")
    set_cover_group.add_argument("--file", help="Path to a local image file")

    edit_parser = subparsers.add_parser("edit", help="Edit local library fields")
    edit_parser.add_argument("goodreads_id", help="Goodreads Book ID")
    edit_parser.add_argument(
        "--format",
        choices=["physical", "ebook", "audiobook", "none"],
        help="Owned format ('none' clears it — not owned)",
    )
    edit_parser.add_argument(
        "--tags", help="Comma-separated local tags (replaces the set; '' clears)"
    )
    edit_parser.add_argument("--loaned-to", help="Who currently has the book")
    edit_parser.add_argument("--local-notes", help="Local catalogue notes")

    conflicts_parser = subparsers.add_parser(
        "conflicts", help="List open sync conflicts with their IDs"
    )
    conflicts_parser.add_argument(
        "--all",
        action="store_true",
        help="Also show already-decided conflicts and how they were decided",
    )

    subparsers.add_parser(
        "dedupe", help="Scan the catalogue for duplicate books (merge them in the web UI)"
    )

    resolve_parser = subparsers.add_parser("resolve", help="Decide a sync conflict by ID")
    resolve_parser.add_argument("conflict_id", type=int, help="Conflict ID (see `adso conflicts`)")
    resolve_group = resolve_parser.add_mutually_exclusive_group()
    resolve_group.add_argument(
        "--keep-local", action="store_true", help="Keep the local value (default)"
    )
    resolve_group.add_argument(
        "--accept-incoming", action="store_true", help="Accept the incoming Goodreads value"
    )
    resolve_group.add_argument("--set", dest="set", metavar="VALUE", help="Set a custom value")
    resolve_group.add_argument(
        "--ignore", action="store_true", help="Dismiss the conflict, leaving the local value unchanged"
    )
    resolve_group.add_argument(
        "--review-later",
        "--later",
        dest="review_later",
        action="store_true",
        help="Defer the decision; the conflict stays open but is flagged",
    )
    resolve_group.add_argument(
        "--reopen", action="store_true", help="Return a decided conflict to pending"
    )

    report_parser = subparsers.add_parser("report", help="Generate reports")
    report_sub = report_parser.add_subparsers(dest="report_type", required=True)
    conflicts = report_sub.add_parser("conflicts", help="Show latest conflict report")
    conflicts.add_argument("--output", help="Write report to this path")
    summary = report_sub.add_parser("summary", help="Show latest sync summary")
    summary.add_argument("--output", help="Write summary to this path")

    export_parser = subparsers.add_parser("export", help="Export catalogue data")
    export_sub = export_parser.add_subparsers(dest="target", required=True)
    csv_export = export_sub.add_parser("csv", help="Export catalogue to CSV")
    csv_export.add_argument("--output", default="exports/catalogue.csv")
    json_export = export_sub.add_parser("json", help="Export catalogue to JSON")
    json_export.add_argument("--output", default="exports/catalogue.json")
    notion_export = export_sub.add_parser("notion", help="Export catalogue to Notion")
    notion_export.add_argument("--dry-run", action="store_true", help="Preview create/update actions without writing")
    notion_export.add_argument("--limit", type=int, help="Maximum number of books to export")

    return parser


def _add_config_parser(subparsers) -> None:
    config_parser = subparsers.add_parser(
        "config", help="Manage configuration profiles (database path, Notion target)"
    )
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)

    config_sub.add_parser("path", help="Show which config files are in effect")
    config_sub.add_parser("list", help="List profiles and the active one")

    show_parser = config_sub.add_parser("show", help="Show resolved settings for a profile")
    show_parser.add_argument("profile", nargs="?", help="Profile name (default: active profile)")

    set_parser = config_sub.add_parser("set", help="Set a profile setting")
    set_parser.add_argument("profile", help="Profile name")
    set_parser.add_argument(
        "key",
        help="Setting to change: " + ", ".join(sorted(config_module.PROFILE_KEYS)),
    )
    set_parser.add_argument("value", help="New value")
    set_parser.add_argument(
        "--local", action="store_true", help="Write to ./adso.ini instead of the user config"
    )

    use_parser = config_sub.add_parser("use", help="Set the default profile")
    use_parser.add_argument("profile", help="Profile name to make default")
    use_parser.add_argument(
        "--local", action="store_true", help="Write to ./adso.ini instead of the user config"
    )

    init_parser = config_sub.add_parser("init", help="Write a starter config file")
    init_parser.add_argument(
        "--local", action="store_true", help="Write ./adso.ini instead of the user config"
    )


def _run_config(args, parser) -> int:
    command = args.config_command

    if command == "path":
        user = config_module.user_config_path()
        project = config_module.project_config_path()
        lines = ["Config files (project-local overrides user-level):"]
        for label, path in (("project", project), ("user", user)):
            mark = "exists" if path.exists() else "not present"
            lines.append(f"- {label}: {path} ({mark})")
        print("\n".join(lines))
        return 0

    if command == "list":
        profiles = config_module.list_profiles()
        active = config_module.default_profile()
        if not profiles:
            print("No profiles defined yet. Create one with `adso config init`.")
            return 0
        lines = ["Profiles:"]
        for name in profiles:
            marker = " (default)" if name == active else ""
            lines.append(f"- {name}{marker}")
        print("\n".join(lines))
        return 0

    if command == "show":
        profile = args.profile or config_module.default_profile()
        if not profile:
            parser.error("No profile given and no default profile set.")
        settings = config_module.profile_settings(profile)
        if not settings:
            parser.error(f"No profile named '{profile}'. See `adso config list`.")
        lines = [f"Profile '{profile}':"]
        for key in ("db", "notion_database_id", "notion_target", "notion_api_key"):
            if key not in settings:
                continue
            value = settings[key]
            if key in config_module.SECRET_KEYS:
                value = config_module.mask_secret(value)
            lines.append(f"  {key} = {value}")
        print("\n".join(lines))
        return 0

    if command == "set":
        try:
            path = config_module.set_value(
                args.profile, args.key, args.value, local=args.local
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Set {args.key} for profile '{args.profile}' in {path}")
        return 0

    if command == "use":
        path = config_module.set_default_profile(args.profile, local=args.local)
        print(f"Default profile set to '{args.profile}' in {path}")
        return 0

    if command == "init":
        path, created = config_module.init_config(local=args.local)
        if created:
            print(f"Wrote starter config to {path}")
        else:
            print(f"Config already exists at {path} (left unchanged)")
        return 0

    parser.error("Unsupported config command.")
    return 2


def _notion_target_banner(cfg: ResolvedConfig) -> str:
    profile = cfg.profile or "(none)"
    target = cfg.notion_target or "(unnamed)"
    db_id = cfg.notion_database_id or "(unset)"
    return f"Notion target → profile: {profile}, target: {target}, database: {db_id}"


def _data_dir(db_path: str) -> Path:
    """Cover files live beside the SQLite database so the library stays portable."""
    return Path(db_path).resolve().parent


def _auto_fetch_covers(conn, db_path: str) -> None:
    """Best-effort cover fetch after an import; network errors must not fail import."""
    try:
        result = fetch_covers(conn, _data_dir(db_path))
    except CoversError as exc:
        print(f"\nSkipped cover fetch: {exc}")
        return
    if result["fetched"] or result["not_found"] or result["errors"]:
        print(
            f"\nCovers: {result['fetched']} fetched, "
            f"{result['not_found']} not found, {result['errors']} errors."
        )


def _auto_fetch_metadata(conn) -> None:
    """Best-effort metadata fetch after an import; network errors must not fail import."""
    try:
        result = fetch_metadata(conn)
    except MetadataError as exc:
        print(f"\nSkipped metadata fetch: {exc}")
        return
    if result["fetched"] or result["not_found"] or result["errors"]:
        line = (
            f"\nMetadata: {result['fetched']} fetched, "
            f"{result['not_found']} not found, {result['errors']} errors."
        )
        if result["isbn_backfilled"]:
            line += f" ISBNs backfilled: {result['isbn_backfilled']}."
        print(line)


def _format_metadata_result(result: dict[str, object], *, dry_run: bool) -> str:
    heading = "Metadata dry-run complete" if dry_run else "Metadata fetch complete"
    lines = [
        f"{heading}: "
        f"{result['fetched']} fetched, {result['not_found']} not found, "
        f"{result['errors']} errors, {result['skipped']} skipped, "
        f"{result['isbn_backfilled']} ISBNs backfilled"
    ]
    if dry_run:
        actions = result.get("actions", [])
        if actions:
            lines.append("")
            for action in actions:  # type: ignore[union-attr]
                if not isinstance(action, dict):
                    continue
                title = action.get("title") or "Untitled"
                outcome = action.get("result")
                if outcome == "fetched":
                    detail = f"would fetch from {action.get('source')}"
                    if action.get("isbn_backfilled"):
                        detail += ", would backfill ISBN"
                elif outcome == "not_found":
                    detail = (
                        "matched an Open Library record with no content yet"
                        if action.get("matched_empty")
                        else "no metadata found"
                    )
                else:
                    detail = "error"
                lines.append(f"- {title} (Goodreads ID {action.get('goodreads_id')}): {detail}")
    return "\n".join(lines)


def _format_cover_result(result: dict[str, object], *, dry_run: bool) -> str:
    heading = "Cover dry-run complete" if dry_run else "Cover fetch complete"
    lines = [
        f"{heading}: "
        f"{result['fetched']} fetched, {result['not_found']} not found, "
        f"{result['errors']} errors, {result['skipped']} skipped"
    ]
    if dry_run:
        actions = result.get("actions", [])
        if actions:
            lines.append("")
            for action in actions:  # type: ignore[union-attr]
                if not isinstance(action, dict):
                    continue
                title = action.get("title") or "Untitled"
                outcome = action.get("result")
                if outcome == "fetched":
                    detail = f"would fetch from {action.get('source')}"
                elif outcome == "not_found":
                    detail = "no cover found"
                else:
                    detail = "error"
                lines.append(f"- {title} (Goodreads ID {action.get('goodreads_id')}): {detail}")
    return "\n".join(lines)


def _local_updates_from_args(args) -> dict[str, object]:
    updates: dict[str, object] = {}
    if args.format is not None:
        # argparse choices can't express "empty", so 'none' is the explicit
        # clear-it value (sets the column to NULL: not owned).
        updates["format"] = None if args.format == "none" else args.format
    if args.tags is not None:
        updates["tags_json"] = db.normalize_tags(args.tags)
    for arg_name, field_name in (
        ("loaned_to", "loaned_to"),
        ("local_notes", "local_notes"),
    ):
        value = getattr(args, arg_name)
        if value is not None:
            updates[field_name] = value
    return updates


def _book_filters_from_args(args) -> BookFilters:
    return BookFilters(
        status=getattr(args, "status", None),
        format=getattr(args, "format", None),
        tag=getattr(args, "tag", None),
        author=getattr(args, "author", None),
        shelf=getattr(args, "shelf", None),
        rating=getattr(args, "rating", None),
        limit=getattr(args, "limit", None),
    )


def _format_notion_export_result(result: dict[str, object], *, dry_run: bool) -> str:
    created_label = "would be created" if dry_run else "created"
    updated_label = "would be updated" if dry_run else "updated"
    heading = "Notion dry-run complete" if dry_run else "Notion export complete"
    lines = [
        f"{heading}: "
        f"{result['created']} {created_label}, {result['updated']} {updated_label}, {result['errors']} errors"
    ]
    if dry_run:
        actions = result.get("actions", [])
        if actions:
            lines.append("")
            lines.append("Planned Notion actions:")
            for action in actions:
                if not isinstance(action, dict):
                    continue
                verb = "Would update" if action.get("action") == "update" else "Would create"
                title = action.get("title") or "Untitled"
                goodreads_id = action.get("goodreads_id") or "unknown"
                lines.append(f"- {verb}: {title} (Goodreads ID {goodreads_id})")
        else:
            lines.append("")
            lines.append("No Notion actions planned.")
    return "\n".join(lines)


def _format_conflicts(groups: list[dict[str, object]]) -> str:
    if not groups:
        return "No open conflicts."
    lines: list[str] = []
    total = 0
    for group in groups:
        if lines:
            lines.append("")
        author = group.get("author") or "Unknown author"
        lines.append(f"{group['title']} — {author} (Goodreads ID {group.get('goodreads_id') or '?'})")
        for conflict in group["conflicts"]:  # type: ignore[index]
            total += 1
            deferred = " [deferred]" if conflict.get("deferred") else ""
            lines.append(
                f"  [{conflict['id']}] {conflict['field_label']}{deferred}: "
                f"local={_display_value(conflict['local'])!r}  "
                f"incoming={_display_value(conflict['incoming'])!r}"
            )
    lines.append("")
    lines.append(
        f"{total} open conflict(s). Decide with "
        "`adso resolve ID [--accept-incoming|--set VALUE|--ignore|--review-later]`."
    )
    return "\n".join(lines)


def _format_decided_conflicts(groups: list[dict[str, object]]) -> str:
    if not groups:
        return "No decided conflicts yet."
    lines: list[str] = ["Decided conflicts", "-----------------"]
    for group in groups:
        lines.append("")
        author = group.get("author") or "Unknown author"
        lines.append(f"{group['title']} — {author} (Goodreads ID {group.get('goodreads_id') or '?'})")
        for conflict in group["conflicts"]:  # type: ignore[index]
            actor = conflict.get("actor")
            provenance = f" via {actor}" if actor else ""
            value = conflict.get("value")
            value_suffix = f" → {value}" if value else ""
            lines.append(
                f"  [{conflict['id']}] {conflict['field_label']}: "
                f"{conflict['decision_label']}{provenance}{value_suffix}"
            )
    lines.append("")
    lines.append("Reopen any of these with `adso resolve ID --reopen`.")
    return "\n".join(lines)


def _format_duplicates(groups: list[dict[str, object]]) -> str:
    if not groups:
        return "No suspected duplicates."
    lines: list[str] = []
    for group in groups:
        if lines:
            lines.append("")
        author = group.get("author") or "Unknown author"
        lines.append(f"{group['title']} — {author} ({group['count']} records)")
        for book in group["books"]:  # type: ignore[index]
            keeper = " (keep — newest)" if book["id"] == group["suggested_keeper_id"] else ""
            lines.append(
                f"  Goodreads ID {book['goodreads_id'] or '?'}: "
                f"{book['reading_status'] or '—'}{keeper}"
            )
    lines.append("")
    lines.append(
        f"{len(groups)} duplicate group(s). Review and merge them in the web UI under Duplicates."
    )
    return "\n".join(lines)


def _format_book_table(books: list[dict[str, object]]) -> str:
    if not books:
        return "No books found."

    rows = [
        {
            "Goodreads ID": str(book.get("goodreads_id") or ""),
            "Title": str(book.get("title") or ""),
            "Author": str(book.get("author") or ""),
            "Status": str(book.get("reading_status") or ""),
            "Format": str(book.get("format") or ""),
        }
        for book in books
    ]
    headers = ["Goodreads ID", "Title", "Author", "Status", "Format"]
    widths = {
        header: min(
            max(len(header), *(len(_truncate(row[header], 48)) for row in rows)),
            48,
        )
        for header in headers
    }
    lines = [
        "  ".join(header.ljust(widths[header]) for header in headers),
        "  ".join("-" * widths[header] for header in headers),
    ]
    for row in rows:
        lines.append(
            "  ".join(_truncate(row[header], widths[header]).ljust(widths[header]) for header in headers)
        )
    return "\n".join(lines)


def _format_book_detail(book: dict[str, object]) -> str:
    shelves = book.get("shelves") or []
    if isinstance(shelves, list):
        shelves_text = ", ".join(str(shelf) for shelf in shelves)
    else:
        shelves_text = str(shelves)

    sections = [
        (
            "Goodreads Fields",
            [
                ("Goodreads ID", book.get("goodreads_id")),
                ("Title", book.get("title")),
                ("Author", book.get("author")),
                ("Additional Authors", book.get("additional_authors")),
                ("ISBN-10", book.get("isbn10")),
                ("ISBN-13", book.get("isbn13")),
                ("Publisher", book.get("publisher")),
                ("Binding", book.get("binding")),
                ("Number of Pages", book.get("number_of_pages")),
                ("Year Published", book.get("year_published")),
                ("Original Publication Year", book.get("original_publication_year")),
                ("Reading Status", book.get("reading_status")),
                ("Exclusive Shelf", book.get("exclusive_shelf")),
                ("Shelves", shelves_text),
                ("Rating", book.get("rating")),
                ("Average Rating", book.get("average_rating")),
                ("Date Read", book.get("date_read")),
                ("Date Added", book.get("date_added")),
                ("Read Count", book.get("read_count")),
                ("Owned Copies", book.get("owned_copies")),
                ("Review", book.get("my_review")),
                ("Private Notes", book.get("private_notes")),
            ],
        ),
        (
            "Local Catalogue Fields",
            [
                ("Format", book.get("format")),
                ("Tags", ", ".join(book.get("tags") or [])),
                ("Loaned To", book.get("loaned_to")),
                ("Local Notes", book.get("local_notes")),
            ],
        ),
    ]
    lines: list[str] = []
    for section, fields in sections:
        if lines:
            lines.append("")
        lines.append(section)
        lines.append("-" * len(section))
        for label, value in fields:
            lines.append(f"{label}: {_display_value(value)}")
    return "\n".join(lines)


def _display_value(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
