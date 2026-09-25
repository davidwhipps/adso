"""Command line interface for Adso."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import __version__, branding, db
from . import categorize as categorize_service
from . import config as config_module
from . import conflicts as conflicts_service
from . import dedupe as dedupe_service
from . import doctor as doctor_module
from . import recommend as recommend_service
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

    if args.command == "mcp":
        return _run_mcp(cfg.db_path)

    if args.command == "service":
        return _run_service(args, cfg)

    if args.command == "goodreads":
        return _run_goodreads(args, cfg)

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
            print("")
            print(_format_book_categories(categorize_service.book_categories(conn, int(book["id"]))))
            return 0

        # `import` and `sync` run the same safe, idempotent operation; the command
        # name only sets the run label (a friendly name for the first load vs. a
        # later refresh). Both must surface conflicts — earlier, `import` recorded
        # conflicts in the database but wrote no report, so they passed silently.
        if args.command in ("import", "sync") and args.source == "goodreads":
            _sync_goodreads(conn, cfg, args.csv, mode=args.command, args=args)
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
            category_edits = _has_category_edits(args)
            if not updates and not category_edits:
                parser.error("No local fields provided to update.")
            book = get_book(conn, args.goodreads_id)
            if book is None:
                raise AdsoError(
                    f"No book found for Goodreads ID {args.goodreads_id}",
                    hint="Run `adso search <title>` or `adso list` to find the ID.",
                )
            if updates:
                try:
                    db.update_local_fields(conn, args.goodreads_id, updates)
                except ValueError as exc:
                    raise AdsoError(str(exc)) from exc
                print(f"Updated local catalogue fields for Goodreads ID {args.goodreads_id}")
            if category_edits:
                for line in _apply_category_edits(conn, int(book["id"]), args):
                    print(line)
            return 0

        if args.command == "categorize":
            result = categorize_service.categorize(conn, dry_run=args.dry_run)
            print(_format_categorize_result(result, dry_run=args.dry_run))
            return 0

        if args.command == "review":
            return _run_review(conn, args)

        if args.command == "taxonomy":
            return _run_taxonomy(conn, args)

        if args.command == "next":
            if args.explore:
                print(_format_paths(recommend_service.explore_paths(conn, limit=args.limit or 4)))
                return 0
            picks = recommend_service.next_reads(
                conn,
                limit=args.limit or 10,
                category=args.category,
                owned_only=args.owned,
                max_pages=args.max_pages,
            )
            print(_format_picks(picks, empty="Nothing on your to-read shelf matches."))
            return 0

        if args.command == "related":
            try:
                related = recommend_service.related_books(conn, args.goodreads_id, limit=args.limit or 10)
            except ValueError as exc:
                raise AdsoError(str(exc), hint="Run `adso search <title>` to find the ID.") from exc
            print(_format_picks(related, empty="No related books found.", show_shelf=True))
            return 0

        if args.command == "insights":
            print(_format_insights(recommend_service.insights(conn)))
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


def _sync_goodreads(conn, cfg: ResolvedConfig, csv_path, *, mode: str, args):
    summary = import_goodreads_csv(conn, csv_path, mode=mode)
    print(latest_sync_summary_markdown(conn))
    if summary.conflicts:
        output = Path("reports") / f"conflicts-import-{summary.import_run_id}.md"
        write_latest_conflicts(conn, output)
        print(f"Conflict report: {output}")
    if not args.no_covers:
        _auto_fetch_covers(conn, cfg.db_path)
    if not args.no_metadata:
        _auto_fetch_metadata(conn)
    _auto_categorize(conn)
    return summary


def _run_goodreads(args, cfg: ResolvedConfig) -> int:
    from . import goodreads_watch

    if args.action == "open":
        goodreads_watch.open_export_page()
        print(f"Opened {goodreads_watch.EXPORT_URL}; click Export Library, then download the file.")
        return 0
    if args.action == "remind":
        goodreads_watch.remind()
        return 0

    # ingest
    if hasattr(sys.stdout, "reconfigure"):
        # Under launchd stdout is a log file; flush per line so the log is live
        # while the post-sync cover/metadata fetch runs.
        sys.stdout.reconfigure(line_buffering=True)
    watch_dir = Path(args.watch_dir) if args.watch_dir else goodreads_watch.default_watch_dir()
    try:
        goodreads_watch.check_readable(watch_dir)
        exports = []
        for path in goodreads_watch.find_exports(watch_dir):
            if path.stat().st_size == 0:
                continue  # still downloading (Firefox creates the file first)
            if not goodreads_watch.is_goodreads_export(path):
                print(f"Skipping {path}: not a Goodreads library export.")
                continue
            exports.append(path)
        if not exports:
            print(f"No Goodreads export waiting in {watch_dir}.")
            return 0

        # Move every waiting export out of the watch folder *before* syncing, so
        # a failed sync (or a skipped stale file) can't re-trigger on each later
        # change to the folder.
        last_sync = goodreads_watch.last_sync_time(cfg.db_path)
        fresh = [p for p in exports if not goodreads_watch.is_stale(p, last_sync)]
        stale = [p for p in exports if goodreads_watch.is_stale(p, last_sync)]
        archive_dir = _data_dir(cfg.db_path) / "exports" / "goodreads"
        for path in stale:
            filed = goodreads_watch.archive(path, archive_dir)
            print(
                f"Skipped {path.name}: downloaded before the last sync, so it's older than "
                f"the catalogue. Filed as {filed}; sync it by hand with "
                f"`adso sync goodreads {filed}` if you really mean to."
            )
        # A re-download of the export we last synced from changes nothing; trash
        # it (recoverable) rather than filing an identical copy.
        previous = goodreads_watch.last_synced_export(cfg.db_path)
        duplicates = [p for p in fresh if goodreads_watch.is_duplicate(p, previous)]
        fresh = [p for p in fresh if p not in duplicates]
        for path in duplicates:
            if goodreads_watch.trash(path):
                print(f"Skipped {path.name}: identical to {previous.name}; moved it to the Trash.")
            else:
                filed = goodreads_watch.archive(path, archive_dir)
                print(f"Skipped {path.name}: identical to {previous.name}; filed as {filed}.")
        if not fresh:
            if args.notify:
                if duplicates:
                    message = "Goodreads export unchanged since your last sync. Nothing to do."
                else:
                    message = (
                        f"Skipped {len(stale)} old Goodreads export(s), downloaded before "
                        "your last sync. Nothing changed."
                    )
                goodreads_watch.notify(message)
            return 0
        archived = [goodreads_watch.archive(path, archive_dir) for path in fresh]
        csv_path = archived[-1]  # newest
        print(f"Filed Goodreads export as {csv_path}")

        backup = goodreads_watch.backup_db(cfg.db_path)
        if backup:
            print(f"Backed up catalogue to {backup}")
        conn = db.connect(cfg.db_path)
        try:
            db.initialize(conn)
            summary = _sync_goodreads(conn, cfg, csv_path, mode="sync", args=args)
        finally:
            conn.close()
    except (AdsoError, sqlite3.DatabaseError, OSError) as exc:
        if args.notify:
            goodreads_watch.notify(f"Goodreads sync failed: {exc}")
        raise

    if args.notify:
        message = f"Goodreads sync: {summary.created} new, {summary.updated} updated"
        if summary.conflicts:
            message += f", {summary.conflicts} conflicts to review"
        goodreads_watch.notify(message)
    return 0


def _run_service(args, cfg: ResolvedConfig) -> int:
    from . import service

    if args.action == "install-sync":
        from . import goodreads_watch

        if args.watch_dir:
            watch_dir = Path(args.watch_dir).expanduser()
        else:
            watch_dir = goodreads_watch.default_watch_dir()
        service.install_sync(
            db_path=cfg.db_path,
            working_dir=Path.cwd(),
            watch_dir=watch_dir,
            day=args.day,
            hour=args.hour,
            reminder=not args.no_reminder,
        )
        info = service.sync_status()
        print(f"Adso now syncs any Goodreads export saved to {info['watch_dir']}.")
        if info["reminder"]:
            print(f"Reminder to export: {info['reminder']}.")
        print(f"Catalogue: {Path(cfg.db_path).resolve()}")
        print(f"Log: {info['log']}")
        print("Try it: `adso goodreads open`, click Export Library, and download the file.")
        return 0
    if args.action == "uninstall-sync":
        if service.uninstall_sync():
            print("Goodreads auto-sync removed.")
        else:
            print("Goodreads auto-sync was not installed.")
        return 0

    if args.action == "install":
        url = service.install(db_path=cfg.db_path, port=args.port, working_dir=Path.cwd())
        print(f"Adso is now always running at {url} (starts at login, restarts if it stops).")
        print(f"Catalogue: {Path(cfg.db_path).resolve()}")
        print("Tip: open it in Safari, then File > Add to Dock for a standalone Adso app.")
        return 0
    if args.action == "uninstall":
        if service.uninstall():
            print("Adso service stopped and removed.")
        else:
            print("Adso service was not installed.")
        return 0
    if args.action == "restart":
        service.restart()
        print("Adso service restarted.")
        return 0

    info = service.status()
    if not info["installed"]:
        print("Adso service: not installed (run `adso service install`).")
        _print_sync_status(service)
        return 0
    state = f"running (pid {info['pid']})" if info.get("pid") else (
        "loaded, not running" if info["loaded"] else "installed, not loaded"
    )
    print(f"Adso service: {state}")
    for key in ("url", "db", "python", "log", "plist"):
        if info.get(key):
            print(f"  {key + ':':8} {info[key]}")
    _print_sync_status(service)
    return 0


def _print_sync_status(service) -> None:
    sync = service.sync_status()
    if not sync["installed"]:
        print("Goodreads auto-sync: off (run `adso service install-sync`).")
        return
    state = "" if sync["loaded"] else " (not loaded)"
    print(f"Goodreads auto-sync: watching {sync['watch_dir']}{state}")
    for key in ("reminder", "log"):
        if sync.get(key):
            print(f"  {key + ':':9} {sync[key]}")


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


def _run_mcp(db_path: str) -> int:
    from .mcp_server import run_stdio

    # Make sure the catalogue file exists and is initialized before serving.
    conn = db.connect(db_path)
    db.initialize(conn)
    conn.close()

    try:
        return run_stdio(db_path)
    except ModuleNotFoundError as exc:
        raise AdsoError(
            "The MCP server needs extra dependencies.",
            hint="Install them with: pip install -e '.[mcp]'",
        ) from exc


class _BrandedParser(argparse.ArgumentParser):
    """Top-level parser whose `--help` renders the branded Adso help screen.

    Subparsers stay vanilla, so `adso <command> --help` keeps argparse's detail.
    """

    def format_help(self) -> str:
        return branding.render_help(self.prog) + "\n"


def _add_category_filter_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--category",
        help="Filter by category, including everything beneath it, e.g. 'Fantasy' or 'theme:cozy'",
    )
    sub.add_argument(
        "--gr-shelf", help="Filter by any Goodreads shelf (not just the exclusive one), e.g. 'cozy-fantasy'"
    )
    sub.add_argument("--series", help="Filter by series name; lists books in reading order")


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

    service_parser = subparsers.add_parser(
        "service", help="Keep the web UI always running in the background (macOS)"
    )
    service_parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=[
            "install",
            "uninstall",
            "restart",
            "status",
            "install-sync",
            "uninstall-sync",
        ],
        help="What to do (default: status). install-sync / uninstall-sync manage "
        "Goodreads auto-sync: exports saved to Downloads are synced automatically.",
    )
    service_parser.add_argument(
        "--port", type=int, default=8420, help="Port for the always-on server (default: 8420)"
    )
    service_parser.add_argument(
        "--watch-dir", help="Folder to watch for Goodreads exports (default: ~/Downloads)"
    )
    service_parser.add_argument(
        "--day",
        default="sun",
        choices=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        help="Day for the weekly export reminder (default: sun)",
    )
    service_parser.add_argument(
        "--hour",
        type=int,
        default=9,
        choices=range(0, 24),
        metavar="0-23",
        help="Hour for the weekly export reminder (default: 9)",
    )
    service_parser.add_argument(
        "--no-reminder", action="store_true", help="Install auto-sync without the weekly reminder"
    )

    goodreads_parser = subparsers.add_parser(
        "goodreads", help="Pick up Goodreads exports from your Downloads folder"
    )
    goodreads_sub = goodreads_parser.add_subparsers(dest="action", required=True)
    goodreads_sub.add_parser("open", help="Open the Goodreads export page in your browser")
    goodreads_sub.add_parser(
        "remind", help="Show the 'time to export' prompt (used by the weekly reminder)"
    )
    ingest_parser = goodreads_sub.add_parser(
        "ingest",
        help="Back up, sync and file away any Goodreads export waiting in the watch folder",
    )
    ingest_parser.add_argument(
        "--watch-dir", help="Folder to look in (default: ~/Downloads)"
    )
    ingest_parser.add_argument(
        "--notify", action="store_true", help="Post a macOS notification with the result"
    )
    ingest_parser.add_argument(
        "--no-covers", action="store_true", help="Skip the automatic cover-art fetch after sync"
    )
    ingest_parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip the automatic Open Library metadata fetch after sync",
    )

    subparsers.add_parser(
        "mcp",
        help="Run the MCP server over stdio so LLM agents can query the catalogue",
    )

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
    _add_category_filter_args(list_parser)

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
    _add_category_filter_args(search_parser)

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
    edit_parser.add_argument(
        "--genre", metavar="CATEGORY", help="Set the primary genre, e.g. 'Fantasy > Cozy Fantasy'"
    )
    edit_parser.add_argument(
        "--add-category",
        action="append",
        metavar="CATEGORY",
        help="Add a category (repeatable), e.g. 'Literary Fiction' or 'theme:grief'",
    )
    edit_parser.add_argument(
        "--remove-category",
        action="append",
        metavar="CATEGORY",
        help="Remove a category (repeatable); rules won't add it back to this book",
    )
    edit_parser.add_argument(
        "--series", help="Set the series by hand ('none' marks the book as not in a series)"
    )
    edit_parser.add_argument(
        "--series-position", type=float, metavar="N", help="Position in the series, e.g. 2 or 1.5"
    )

    categorize_parser = subparsers.add_parser(
        "categorize",
        help="Apply your category rules and raise new suggestions for review",
        description=(
            "Apply accepted category rules across the library, read series from "
            "Goodreads titles, and turn unmapped Goodreads shelves, Open Library "
            "subjects and tags into suggestions for `adso review`. Runs "
            "automatically after every sync; never uses the network."
        ),
    )
    categorize_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing"
    )

    review_parser = subparsers.add_parser(
        "review",
        help="Review categorisation suggestions",
        description=(
            "With no ID, list open suggestions (highest-leverage first). With an "
            "ID, decide it. Accepting a shelf/subject/tag mapping creates a rule "
            "that also applies to future books."
        ),
    )
    review_parser.add_argument("suggestion_id", type=int, nargs="?", help="Suggestion ID to decide")
    review_action = review_parser.add_mutually_exclusive_group()
    review_action.add_argument("--accept", action="store_true", help="Accept the suggestion")
    review_action.add_argument("--reject", action="store_true", help="Reject it; it won't be suggested again")
    review_action.add_argument("--reopen", action="store_true", help="Return a rejected suggestion to the queue")
    review_parser.add_argument(
        "--as", dest="as_category", metavar="CATEGORY", help="Accept, but map to this category instead"
    )
    review_parser.add_argument(
        "--only",
        action="store_true",
        help="Act on this one source instead of its whole card (e.g. reject one bad subject)",
    )
    review_parser.add_argument(
        "--all", action="store_true", help="Also list decided suggestions (accepted and rejected)"
    )

    next_parser = subparsers.add_parser(
        "next",
        help="What to read next: your to-read pile ranked by your own ratings",
        description=(
            "Rank your to-read shelf by how well each book matches what you rate "
            "highly (genres, themes, tags, subjects, authors), the next unread book "
            "in series you're enjoying, and books you own, with the reasons for "
            "each. --explore suggests genres next to ones you love that you've "
            "barely read."
        ),
    )
    next_parser.add_argument("--limit", type=int, help="How many picks (default 10)")
    next_parser.add_argument("--category", help="Only this category, e.g. 'Fantasy' (includes subgenres)")
    next_parser.add_argument("--owned", action="store_true", help="Only books you own")
    next_parser.add_argument("--max-pages", type=int, metavar="N", help="Only books up to N pages")
    next_parser.add_argument("--explore", action="store_true", help="Suggest new directions instead")

    related_parser = subparsers.add_parser("related", help="Books most like one book")
    related_parser.add_argument("goodreads_id", help="Goodreads Book ID")
    related_parser.add_argument("--limit", type=int, help="How many (default 10)")

    subparsers.add_parser("insights", help="Your reading by genre, and where your to-read pile leans")

    taxonomy_parser = subparsers.add_parser(
        "taxonomy", help="Manage categories, aliases and shelf/subject mapping rules"
    )
    taxonomy_sub = taxonomy_parser.add_subparsers(dest="taxonomy_command", required=True)
    tax_list = taxonomy_sub.add_parser("list", help="Show the category tree with book counts")
    tax_list.add_argument("--facet", help="Only this facet (form, genre, audience, theme)")
    tax_list.add_argument("--used", action="store_true", help="Hide categories with no books")
    tax_add = taxonomy_sub.add_parser(
        "add", help="Add a category: 'theme:Found family' or 'Fantasy > Grimdark'"
    )
    tax_add.add_argument("category")
    tax_rename = taxonomy_sub.add_parser("rename", help="Rename a category (old name kept as an alias)")
    tax_rename.add_argument("category")
    tax_rename.add_argument("new_label")
    tax_move = taxonomy_sub.add_parser("move", help="Move a category under another parent")
    tax_move.add_argument("category")
    tax_move_where = tax_move.add_mutually_exclusive_group(required=True)
    tax_move_where.add_argument("--under", metavar="PARENT", help="New parent category")
    tax_move_where.add_argument("--top", action="store_true", help="Make it top-level")
    tax_merge = taxonomy_sub.add_parser("merge", help="Fold one category into another")
    tax_merge.add_argument("source")
    tax_merge.add_argument("target")
    tax_merge.add_argument("--yes", action="store_true", help="Confirm (otherwise just show the impact)")
    tax_delete = taxonomy_sub.add_parser("delete", help="Delete a category (children move up)")
    tax_delete.add_argument("category")
    tax_delete.add_argument("--yes", action="store_true", help="Confirm (otherwise just show the impact)")
    tax_alias = taxonomy_sub.add_parser("alias", help="Add an alternative name used for matching")
    tax_alias.add_argument("category")
    tax_alias.add_argument("alias")
    tax_unalias = taxonomy_sub.add_parser("unalias", help="Stop an alternative name matching a category")
    tax_unalias.add_argument("category")
    tax_unalias.add_argument("alias")
    taxonomy_sub.add_parser("rules", help="List shelf/subject/tag mapping rules")
    tax_map = taxonomy_sub.add_parser("map", help="Map a shelf, subject or tag to a category")
    tax_map_what = tax_map.add_mutually_exclusive_group(required=True)
    tax_map_what.add_argument("--shelf", help="Goodreads shelf name")
    tax_map_what.add_argument("--subject", help="Open Library subject")
    tax_map_what.add_argument("--tag", help="Local tag")
    tax_map.add_argument(
        "--to", dest="to", required=True, metavar="CATEGORY", help="Target category, or tag:NAME to add a tag"
    )
    tax_unmap = taxonomy_sub.add_parser(
        "unmap", help="Delete a rule and the category assignments it made"
    )
    tax_unmap.add_argument("rule_id", type=int)

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


def _auto_categorize(conn) -> None:
    """Apply accepted category rules to new/changed books; purely local, no network."""
    result = categorize_service.categorize(conn)
    if result["assigned"] or result["removed"] or result["series"] or result["primaries_set"] or result["tagged"]:
        print(
            f"\nCategories: {result['assigned']} assigned and {result['removed']} removed by your rules, "
            f"{result['tagged']} tag(s) added, {result['primaries_set']} primary genres set, "
            f"{result['series']} series updated."
        )
    if result["pending"]:
        print(f"{result['pending']} categorisation suggestion(s) waiting — run `adso review`.")


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
        + (f", {result['kept']} kept existing cover" if result.get("kept") else "")
        + (f", {result['locked']} skipped (catalogue locked)" if result.get("locked") else "")
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
                elif outcome == "kept":
                    detail = "no new cover found; would keep the existing one"
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
        category=getattr(args, "category", None),
        gr_shelf=getattr(args, "gr_shelf", None),
        series=getattr(args, "series", None),
        limit=getattr(args, "limit", None),
    )


def _has_category_edits(args) -> bool:
    return bool(
        args.genre
        or args.add_category
        or args.remove_category
        or args.series is not None
        or args.series_position is not None
    )


def _apply_category_edits(conn, book_id: int, args) -> list[str]:
    lines: list[str] = []
    for reference in args.remove_category or []:
        category = categorize_service.remove_book_category(conn, book_id, reference)
        lines.append(f"Removed {reference!r} ({category.label})")
    for reference in args.add_category or []:
        category = categorize_service.add_book_category(conn, book_id, reference)
        lines.append(f"Added {category.label}")
    if args.genre:
        category = categorize_service.set_primary_genre(conn, book_id, args.genre)
        lines.append(f"Primary genre: {category.label}")
    if args.series is not None or args.series_position is not None:
        if args.series is None:
            current = categorize_service.book_series(conn, book_id)
            if current is None:
                raise AdsoError("This book has no series yet", hint="Pass --series NAME as well.")
            name = current["name"]
        else:
            name = None if args.series.strip().lower() in ("", "none") else args.series
        categorize_service.set_book_series(conn, book_id, name, args.series_position)
        if name:
            position = categorize_service.format_position(args.series_position)
            lines.append(f"Series: {name} {position}".rstrip())
        else:
            lines.append("Series: none")
    return lines


def _run_review(conn, args) -> int:
    if args.suggestion_id is None:
        if args.accept or args.reject or args.reopen or args.as_category:
            raise AdsoError("Give a suggestion ID to decide", hint="Run `adso review` to list them.")
        print(_format_suggestions(categorize_service.list_suggestion_cards(conn)))
        if args.all:
            for status in ("accepted", "rejected"):
                decided = categorize_service.list_suggestion_cards(conn, status=status)
                if decided:
                    print("")
                    print(_format_suggestions(decided, heading=f"{status.capitalize()} suggestions"))
        return 0
    if args.reject:
        item = categorize_service.reject_suggestion(conn, args.suggestion_id, only=args.only)
        print(f"Rejected {_sources_phrase(item)} → {item['target']}")
        return 0
    if args.reopen:
        item = categorize_service.reopen_suggestion(conn, args.suggestion_id, only=args.only)
        print(f"Reopened {_sources_phrase(item)} → {item['target']}")
        return 0
    if not (args.accept or args.as_category):
        raise AdsoError(
            "Say what to do with the suggestion",
            hint=f"`adso review {args.suggestion_id} --accept`, `--as CATEGORY` or `--reject`.",
        )
    outcome = categorize_service.accept_suggestion(
        conn, args.suggestion_id, as_category=args.as_category, only=args.only
    )
    if outcome["kind"] == "primary":
        print(f"Primary genre set: {outcome['category']}")
    else:
        sources = f" ({outcome['sources']} sources)" if outcome["sources"] > 1 else ""
        print(f"Mapped to {outcome['category']}{sources}: now applied to {outcome['books']} book(s).")
        _print_run_followups(outcome["run"])
    return 0


def _sources_phrase(item: dict[str, object]) -> str:
    if item["kind"] == "primary":
        return f"[{item['id']}] {item['subject']}"
    sources = int(item.get("sources") or 1)
    return f"[{item['id']}] {item['subject']}" + (f" and {sources - 1} more source(s)" if sources > 1 else "")


def _run_taxonomy(conn, args) -> int:
    command = args.taxonomy_command
    if command == "list":
        print(_format_taxonomy(categorize_service.taxonomy_tree(conn), facet=args.facet, used=args.used))
        return 0
    if command == "add":
        category = categorize_service.add_category(conn, args.category)
        print(f"Added {categorize_service.Taxonomy(conn).display(category.id)}")
        return 0
    if command == "rename":
        category = categorize_service.rename_category(conn, args.category, args.new_label)
        print(f"Renamed to {categorize_service.Taxonomy(conn).display(category.id)}")
        return 0
    if command == "move":
        category = categorize_service.move_category(conn, args.category, None if args.top else args.under)
        print(f"Moved to {categorize_service.Taxonomy(conn).display(category.id)}")
        return 0
    if command == "alias":
        category = categorize_service.add_alias(conn, args.category, args.alias)
        print(f"{args.alias!r} now also matches {category.label}")
        return 0
    if command == "unalias":
        category = categorize_service.remove_alias(conn, args.category, args.alias)
        print(f"{args.alias!r} no longer matches {category.label}; open suggestions were re-checked.")
        return 0
    if command in ("merge", "delete"):
        taxonomy = categorize_service.Taxonomy(conn)
        source = taxonomy.resolve(args.source if command == "merge" else args.category)
        impact = categorize_service.category_impact(conn, source.id)
        if command == "merge":
            target = taxonomy.resolve(args.target, facet=source.facet)
            what = f"Merge {taxonomy.display(source.id)} into {taxonomy.display(target.id)}"
        else:
            what = f"Delete {taxonomy.display(source.id)}"
        summary = (
            f"{what}: affects {impact['books']} book assignment(s), "
            f"{impact['rules']} rule(s), {impact['children']} subcategor(y/ies)."
        )
        if not args.yes:
            print(summary)
            print("Nothing changed. Re-run with --yes to confirm.")
            return 0
        if command == "merge":
            categorize_service.merge_categories(conn, args.source, args.target)
            print(f"{summary}\nDone; the old name is kept as an alias.")
        else:
            categorize_service.delete_category(conn, args.category)
            print(f"{summary}\nDone.")
        return 0
    if command == "rules":
        print(_format_rules(categorize_service.list_rules(conn)))
        return 0
    if command == "map":
        kind, value = next(
            (kind, getattr(args, kind)) for kind in ("shelf", "subject", "tag") if getattr(args, kind)
        )
        outcome = categorize_service.add_rule(conn, kind, value, args.to)
        print(
            f"Rule [{outcome['rule_id']}]: {categorize_service.MATCH_KIND_LABELS[kind]} {value!r} → "
            f"{outcome['category']} ({outcome['books']} book(s))."
        )
        _print_run_followups(outcome["run"])
        return 0
    if command == "unmap":
        rule = categorize_service.delete_rule(conn, args.rule_id)
        print(
            f"Deleted rule [{rule['id']}] {rule['match_label']} {rule['match_value']!r} → {rule['category']}; "
            f"removed it from {rule['books']} book(s)."
        )
        return 0
    raise AdsoError(f"Unknown taxonomy command {command!r}")


def _print_run_followups(run: dict[str, int]) -> None:
    if run.get("primaries_set"):
        print(f"{run['primaries_set']} book(s) got a primary genre.")
    if run.get("pending"):
        print(f"{run['pending']} suggestion(s) still open — `adso review`.")


_SHELF_SHORT = {"read": "read", "to-read": "to read", "currently-reading": "reading", "did-not-finish": "DNF"}


def _format_picks(picks: list[dict[str, object]], *, empty: str, show_shelf: bool = False) -> str:
    if not picks:
        return empty
    lines: list[str] = []
    for index, pick in enumerate(picks, 1):
        author = f" — {pick['author']}" if pick.get("author") else ""
        extra = []
        if show_shelf and pick.get("shelf"):
            extra.append(_SHELF_SHORT.get(str(pick["shelf"]), str(pick["shelf"])))
        if pick.get("rating"):
            extra.append(f"{pick['rating']}★")
        suffix = f"  [{', '.join(extra)}]" if extra else ""
        lines.append(f"{index:>2}. {pick['title']}{author}  (Goodreads ID {pick['goodreads_id']}){suffix}")
        for reason in pick.get("reasons") or []:  # type: ignore[union-attr]
            lines.append(f"      · {reason}")
    return "\n".join(lines)


def _format_paths(paths: list[dict[str, object]]) -> str:
    if not paths:
        return (
            "No new directions yet. They appear once you've rated a few books in a genre and "
            "have books in a neighbouring genre on your to-read shelf."
        )
    lines: list[str] = []
    for path in paths:
        if lines:
            lines.append("")
        lines.append(f"Try {path['genre']}")
        lines.append(f"  {path['because']}")
        for book in path["books"]:  # type: ignore[union-attr]
            author = f" — {book['author']}" if book.get("author") else ""
            lines.append(f"  · {book['title']}{author}  (Goodreads ID {book['goodreads_id']})")
    return "\n".join(lines)


def _format_insights(data: dict[str, object]) -> str:
    avg = data.get("average_rating")
    lines = [
        f"Read {data['read']} · did not finish {data['dnf']} · to read {data['to_read']}"
        + (f" · average rating {avg}★" if avg else ""),
    ]
    if data.get("unrated_read"):
        lines.append(f"{data['unrated_read']} read books are unrated; rating them sharpens `adso next`.")
    if data.get("uncategorised"):
        lines.append(f"{data['uncategorised']} books have no category yet; `adso review` helps.")
    genres = data.get("genres") or []
    if genres:
        lines += ["", f"{'Genre':<26}{'read':>6}{'avg':>7}{'DNF':>7}{'to read':>9}"]
        for row in genres:  # type: ignore[union-attr]
            avg_text = f"{row['avg_rating']:.1f}★" if row["avg_rating"] is not None else "—"
            dnf_text = f"{int(row['dnf_rate'] * 100)}%" if row["dnf_rate"] is not None else "—"
            lines.append(f"{row['genre'][:25]:<26}{row['read']:>6}{avg_text:>7}{dnf_text:>7}{row['to_read']:>9}")
    notes = data.get("notes") or []
    if notes:
        lines.append("")
        lines += [f"· {note}" for note in notes]  # type: ignore[union-attr]
    years = data.get("read_by_year") or {}
    if years:
        lines.append("")
        lines.append("Read per year: " + ", ".join(f"{y} {n}" for y, n in years.items()))  # type: ignore[union-attr]
    return "\n".join(lines)


def _format_categorize_result(result: dict[str, int], *, dry_run: bool) -> str:
    prefix = "Dry run — would have: " if dry_run else ""
    lines = [
        f"{prefix}Checked {result['books']} book(s): "
        f"{result['assigned']} categories assigned and {result['removed']} removed by rules, "
        f"{result['tagged']} tag(s) added, {result['primaries_set']} primary genres set, "
        f"{result['series']} series updated.",
        f"{result['new_proposals']} new mapping proposal(s) and {result['primaries_suggested']} "
        f"question(s) about single books; {result['pending']} suggestion(s) open in total.",
    ]
    if result["pending"] and not dry_run:
        lines.append("Next: `adso review`.")
    return "\n".join(lines)


_SHORT_KIND = {"shelf": "shelf", "subject": "subject", "tag": "tag"}


def _format_suggestions(cards: list[dict[str, object]], *, heading: str | None = None) -> str:
    if not cards:
        return "No open suggestions. Run `adso categorize` after a sync or metadata fetch."
    maps = [card for card in cards if card["kind"] == "map"]
    primaries = [card for card in cards if card["kind"] == "primary"]
    lines: list[str] = []
    if heading:
        lines += [heading, "-" * len(heading)]
    if maps:
        lines.append("Mappings — decide once, applies to every matching book now and after each sync")
        for card in maps:
            lines.append(
                f"  [{card['id']}] {card['target']} — {card['book_count']} book(s)"
                f"  {int(float(card['confidence']) * 100)}%"
            )
            members = card["members"]  # type: ignore[index]
            if len(members) == 1:
                member = members[0]
                lines.append(f'       from {_SHORT_KIND.get(member["match_kind"], member["match_kind"])} "{member["match_value"]}"')
            else:
                sources = ", ".join(
                    f'{_SHORT_KIND.get(m["match_kind"], m["match_kind"])} "{m["match_value"]}" ({m["book_count"]}) [{m["id"]}]'
                    for m in members
                )
                lines.append(f"       from {sources}")
            if card.get("evidence"):
                lines.append(f"       {card['evidence']}")
    assigns = [card for card in cards if card["kind"] == "assign"]
    if assigns:
        if lines:
            lines.append("")
        lines.append("Check these — genres for particular books")
        for card in assigns:
            members = card["members"]  # type: ignore[index]
            lines.append(f"  [{card['id']}] {card['target']} — {len(members)} book(s)")
            if card.get("evidence"):
                source = f" (suggested by {card['proposed_by']})" if card.get("proposed_by") not in (None, "adso") else ""
                lines.append(f"       {card['evidence']}{source}")
            shown = ", ".join(f"{m['subject'].split(' — ')[0]} [{m['id']}]" for m in members[:8])
            more = f", and {len(members) - 8} more" if len(members) > 8 else ""
            lines.append(f"       {shown}{more}")
    if primaries:
        if lines:
            lines.append("")
        lines.append("Primary genre — the book's genres don't share a parent; which one leads?")
        for card in primaries:
            lines.append(f"  [{card['id']}] {card['subject']} → {card['target']}")
            if card.get("evidence"):
                lines.append(f"       {card['evidence']}")
    if not heading:
        lines.append("")
        lines.append(
            "Decide a card with `adso review ID --accept`, `--as \"Genre > Path\"` or `--reject`; "
            "add `--only` to act on just that one source."
        )
    return "\n".join(lines)


def _format_taxonomy(facets: list[dict[str, object]], *, facet: str | None, used: bool) -> str:
    lines: list[str] = []
    for group in facets:
        if facet and group["facet"] != facet.lower():
            continue
        nodes = [n for n in group["categories"] if not used or n["total"]]  # type: ignore[index]
        if lines:
            lines.append("")
        lines.append(f"{group['label']} ({group['facet']})")
        if not nodes:
            lines.append("  (none yet)")
        for node in nodes:
            count = f" ({node['total']})" if node["total"] else ""
            lines.append(f"{'  ' * (node['depth'] + 1)}{node['label']}{count}")
    if not lines:
        return f"No facet named {facet!r}. Facets: form, genre, audience, theme."
    return "\n".join(lines)


def _format_rules(rules: list[dict[str, object]]) -> str:
    if not rules:
        return "No mapping rules yet. Accept suggestions with `adso review`, or add one with `adso taxonomy map`."
    lines = [
        f"  [{rule['id']}] {rule['match_label']} {rule['match_value']!r} → {rule['category']} "
        f"({rule['books']} book(s))"
        for rule in rules
    ]
    lines.append("")
    lines.append("Remove one with `adso taxonomy unmap RULE_ID`.")
    return "\n".join(lines)


_SOURCE_LABELS = {"user": "you", "rule": "rule", "derived": "default"}


def _format_book_categories(data: dict[str, object]) -> str:
    lines = ["Categories", "----------"]
    primary = data.get("primary")
    lines.append(f"Primary Genre: {primary['path'] if primary else '-'}")  # type: ignore[index]
    by_facet = data.get("by_facet") or {}
    for facet, label in categorize_service.FACET_LABELS.items():
        entries = by_facet.get(facet) or []  # type: ignore[union-attr]
        if not entries:
            continue
        rendered = []
        for entry in entries:
            source = _SOURCE_LABELS.get(entry["source"], entry["source"])
            why = f"{source}: {entry['evidence']}" if entry.get("evidence") else source
            rendered.append(f"{entry['path']} [{why}]")
        lines.append(f"{label}: " + "; ".join(rendered))
    series = data.get("series")
    if series:
        position = categorize_service.format_position(series["position"])  # type: ignore[index]
        lines.append(f"Series: {series['name']} {position}".rstrip())  # type: ignore[index]
    return "\n".join(lines)


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
