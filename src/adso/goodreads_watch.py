"""Sync a Goodreads export the moment it lands in your Downloads folder.

Goodreads has no API, and it blocks headless browsers (HTTP 403), so the one
step Adso can't do for you is clicking "Export Library" on
goodreads.com/review/import. Everything after that click is automated:

* `adso goodreads ingest` picks up `goodreads_library_export*.csv` from the
  watch folder, backs up the catalogue, syncs, and files the CSV away under
  `exports/goodreads/` (moving it keeps the next download's name clean and stops
  the watcher re-syncing the same file).
* A LaunchAgent with `WatchPaths` runs `ingest` whenever the folder changes
  (see `adso service install-sync`), and a weekly reminder runs
  `adso goodreads remind` to prompt the click.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from .errors import AdsoError

EXPORT_URL = "https://www.goodreads.com/review/import"
EXPORT_GLOB = "goodreads_library_export*.csv"
# Every Goodreads export starts with this header column (optionally after a BOM).
CSV_SIGNATURE = b"Book Id,"


def default_watch_dir() -> Path:
    return Path.home() / "Downloads"


def find_exports(watch_dir: str | Path) -> list[Path]:
    """Goodreads exports waiting in `watch_dir`, oldest first."""
    return sorted(Path(watch_dir).glob(EXPORT_GLOB), key=lambda p: p.stat().st_mtime)


def is_goodreads_export(path: str | Path) -> bool:
    with open(path, "rb") as fh:
        head = fh.read(64)
    return head.lstrip(b"\xef\xbb\xbf").startswith(CSV_SIGNATURE)


def downloaded_at(path: str | Path) -> datetime:
    """When the export landed on disk (its mtime, which browsers set on download)."""
    return datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc)


def last_sync_time(db_path: str | Path) -> datetime | None:
    """When the catalogue last synced from Goodreads, or None if it never has."""
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT max(imported_at) FROM import_runs WHERE source = 'goodreads'"
        ).fetchone()
    except sqlite3.OperationalError:  # no import_runs table yet
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    # SQLite's CURRENT_TIMESTAMP is UTC.
    return datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)


def is_stale(path: str | Path, last_sync: datetime | None) -> bool:
    """An export downloaded before the last sync is older than what's already in
    the catalogue; syncing it would offer outdated Goodreads values as updates."""
    return last_sync is not None and downloaded_at(path) < last_sync


def archive_path(dest_dir: str | Path, today: date | None = None) -> Path:
    """Return a dated, non-clobbering path such as `goodreads-2026-09-24.csv`."""
    dest = Path(dest_dir)
    stem = f"goodreads-{(today or date.today()).isoformat()}"
    candidate = dest / f"{stem}.csv"
    n = 2
    while candidate.exists():
        candidate = dest / f"{stem}-{n}.csv"
        n += 1
    return candidate


def archive(path: str | Path, dest_dir: str | Path) -> Path:
    """Move an export out of the watch folder, named for the day it was downloaded."""
    downloaded = date.fromtimestamp(Path(path).stat().st_mtime)
    target = archive_path(dest_dir, downloaded)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), target)
    return target


def backup_db(db_path: str | Path, now: datetime | None = None) -> Path | None:
    """Snapshot the catalogue beside itself (`<db>.bak-YYYYMMDD-HHMMSS`) before a sync.

    Uses SQLite's backup API so a WAL-mode database is copied consistently.
    Returns None when there is no database yet (first-ever sync).
    """
    source = Path(db_path)
    if not source.exists():
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = source.with_name(f"{source.name}.bak-{stamp}")
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(script: str) -> subprocess.CompletedProcess | None:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return None
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)


def notify(message: str, *, title: str = "Adso") -> None:
    """Best-effort macOS notification; silently does nothing elsewhere."""
    _osascript(
        f"display notification {_applescript_string(message)} with title {_applescript_string(title)}"
    )


def open_export_page() -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", EXPORT_URL], check=False)
    else:
        import webbrowser

        webbrowser.open(EXPORT_URL)


def remind() -> bool:
    """Ask (via a dialog) whether to open the export page now. Returns True if opened."""
    message = (
        "Time to back up Goodreads. Click “Export Library”, then download the file; "
        "Adso syncs it automatically once it lands in your Downloads folder."
    )
    result = _osascript(
        f"display dialog {_applescript_string(message)} with title \"Adso\" "
        'buttons {"Later", "Open Goodreads"} default button "Open Goodreads" '
        "giving up after 3600"
    )
    if result is None:
        print(f"Time to export your Goodreads library: {EXPORT_URL}")
        return False
    if "Open Goodreads" in (result.stdout or ""):
        open_export_page()
        return True
    return False


def check_readable(watch_dir: str | Path) -> None:
    """Fail clearly when macOS privacy settings hide the folder from this process."""
    try:
        list(Path(watch_dir).iterdir())
    except PermissionError as exc:
        raise AdsoError(
            f"macOS won't let Adso read {watch_dir}.",
            hint=(
                "Allow it in System Settings > Privacy & Security > Files and Folders "
                f"(or Full Disk Access) for {sys.executable}, or reinstall with "
                "`adso service install-sync --watch-dir <folder>` and save the export there."
            ),
        ) from exc
    except FileNotFoundError as exc:
        raise AdsoError(f"Watch folder {watch_dir} doesn't exist.") from exc
