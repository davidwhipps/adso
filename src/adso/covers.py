"""Cover-art enrichment for the local catalogue.

Covers are downloaded from public sources and *owned* locally: image files are
written to a ``covers/`` directory beside the SQLite database and the books row
records a relative path plus provenance. This is enrichment, not a
Goodreads-sourced field, so it deliberately stays out of the
source_snapshots/sync_conflicts machinery.

Source chain (first hit wins):
    1. Goodreads book page for the book's own Goodreads ID -> og:image.
    2. Goodreads autocomplete (ISBN, then title/author) -> exact Book ID match.
    3. Open Library cover by ISBN-13 then ISBN-10.
    4. Open Library Search by title + author -> cover id -> cover by id.
    5. iTunes / Apple Books Search by title + author -> artwork.

Goodreads comes first because every book is keyed by its Goodreads ID, so it is
the only source guaranteed to return the same edition's cover you see there.
The regular book page sits behind an AWS WAF JavaScript challenge, but the
``.xml`` variant of the same URL serves the full HTML page; that is an
undocumented quirk, so autocomplete (a public JSON endpoint) backs it up, and
only a result whose ``bookId`` equals ours is accepted. Both are plain, polite
GETs against public pages for books in your own library — no sign-in.

Open Library is the open, community fallback, needs no API key, and is lenient
about volume. iTunes is a no-key fallback that fills gaps Open Library lacks art for. Google
Books is deliberately not used — its keyless tier rate-limits (HTTP 429) almost
immediately and its throttled connections can stall.

A manually-set cover (``cover_status == 'manual'``) is never overwritten by an
automatic fetch.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db

# The polite HTTP client lives in ol_http (shared with the metadata fetcher);
# the names are re-exported here because tests and callers patch/read them as
# adso.covers attributes.
from .ol_http import (  # noqa: F401
    HTTP_TIMEOUT,
    RATE_LIMIT_DELAY,
    USER_AGENT,
)
from .ol_http import request as _http_request

# iTunes' unauthenticated Search API allows ~20 requests/minute, so space the
# iTunes calls (only hit as a fallback) to stay comfortably under that.
ITUNES_MIN_INTERVAL = 2.0

OPENLIBRARY_COVER_ISBN = "https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
OPENLIBRARY_COVER_ID = "https://covers.openlibrary.org/b/id/{cover_id}-L.jpg?default=false"
OPENLIBRARY_SEARCH = "https://openlibrary.org/search.json"
ITUNES_SEARCH = "https://itunes.apple.com/search"

# Goodreads URLs and markup live here so drift is a one-line fix.
GOODREADS_BOOK_PAGE = "https://www.goodreads.com/book/show/{goodreads_id}.xml"
GOODREADS_AUTOCOMPLETE = "https://www.goodreads.com/book/auto_complete"
_OG_IMAGE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"')
# Autocomplete thumbnails carry a size suffix (``123._SY75_.jpg``); dropping it
# yields the full-size image.
_GOODREADS_SIZE_SUFFIX = re.compile(r"\._S[XY]\d+_(?=\.\w+$)")

# Magic-byte signatures -> file extension. Only these are accepted as covers.
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class CoversError(RuntimeError):
    pass


def _request(method: str, url: str, **kwargs):
    """Shared polite HTTP client (see ol_http), raising CoversError on failure.

    Kept as a module attribute so tests can patch ``adso.covers._request``.
    """
    return _http_request(method, url, error_cls=CoversError, **kwargs)


def _detect_image_ext(data: bytes) -> str | None:
    """Return a file extension if ``data`` looks like a supported image, else None.

    WEBP (RIFF....WEBP) is detected separately because the marker is split.
    """
    for signature, ext in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _download_image(url: str) -> tuple[bytes, str] | None:
    """Fetch ``url`` and return (bytes, ext) only if it is a valid image."""
    response = _request("get", url)
    if response is None or response.status_code != 200 or not response.content:
        return None
    ext = _detect_image_ext(response.content)
    if ext is None:
        return None
    return response.content, ext


def _openlibrary_search_cover_id(title: str, author: str) -> int | None:
    """Look up a cover id for a title (+author) via the Open Library Search API."""
    params = {"title": title, "limit": 1, "fields": "cover_i"}
    if author:
        params["author"] = author
    response = _request("get", OPENLIBRARY_SEARCH, params=params)
    if response is None or response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    docs = payload.get("docs") or []
    if not docs:
        return None
    cover_id = docs[0].get("cover_i")
    return cover_id if isinstance(cover_id, int) and cover_id > 0 else None


def _goodreads_usable(url: str | None) -> bool:
    """False for missing URLs and Goodreads' "no photo" placeholder art."""
    return bool(url) and "nophoto" not in url  # type: ignore[operator]


def _goodreads_page_image_url(goodreads_id: str) -> str | None:
    """The og:image on the book's own Goodreads page (exact edition)."""
    response = _request("get", GOODREADS_BOOK_PAGE.format(goodreads_id=goodreads_id))
    if response is None or response.status_code != 200:
        return None  # 202 = WAF challenge; treat as a miss and fall through
    match = _OG_IMAGE.search(response.text or "")
    url = match.group(1) if match else None
    return url if _goodreads_usable(url) else None


def _bare_title(title: str) -> str:
    """Drop series/subtitle noise: "Red Dragon (Hannibal, #1)" -> "Red Dragon"."""
    return re.split(r"[(:]", title, maxsplit=1)[0].strip()


def _goodreads_search_image_url(goodreads_id: str, book: dict[str, Any]) -> str | None:
    """Search Goodreads autocomplete, accepting only an exact Book ID match."""
    title = _bare_title(str(book.get("title") or ""))
    author = " ".join(str(book.get("author") or "").split())
    queries = [q for q in (book.get("isbn13"), book.get("isbn10")) if q]
    if title:
        queries += [f"{title} {author}".strip(), title]
    for query in dict.fromkeys(queries):  # de-duplicate, keep order
        response = _request(
            "get", GOODREADS_AUTOCOMPLETE, params={"format": "json", "q": query}
        )
        if response is None or response.status_code != 200:
            continue
        try:
            results = response.json()
        except ValueError:
            continue
        for result in results if isinstance(results, list) else []:
            if str(result.get("bookId")) != str(goodreads_id):
                continue
            url = result.get("imageUrl")
            if _goodreads_usable(url):
                return _GOODREADS_SIZE_SUFFIX.sub("", url)
            return None  # our edition has no Goodreads art; searching on won't help
    return None


def _goodreads_cover(book: dict[str, Any]) -> tuple[bytes, str, str, str] | None:
    """Resolve the cover Goodreads shows for this exact edition, if any."""
    goodreads_id = str(book.get("goodreads_id") or "").strip()
    if not goodreads_id:
        return None
    for source, find_url in (
        ("goodreads:page", lambda: _goodreads_page_image_url(goodreads_id)),
        ("goodreads:search", lambda: _goodreads_search_image_url(goodreads_id, book)),
    ):
        url = find_url()
        if url:
            result = _download_image(url)
            if result is not None:
                data, ext = result
                return data, source, url, ext
    return None


def _itunes_artwork_url(title: str, author: str) -> str | None:
    """Look up cover artwork for a title (+author) via the iTunes Search API.

    The API returns a 100x100 ``artworkUrl100``; the dimension segment can be
    swapped for a larger size to get a usable-resolution image.
    """
    term = f"{title} {author}".strip()
    response = _request(
        "get", ITUNES_SEARCH, params={"term": term, "entity": "ebook", "limit": 1}
    )
    # iTunes is gentle about volume; space calls out to respect its rate limit.
    time.sleep(ITUNES_MIN_INTERVAL)
    if response is None or response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    results = payload.get("results") or []
    if not results:
        return None
    artwork = results[0].get("artworkUrl100")
    if not artwork:
        return None
    return artwork.replace("100x100bb", "600x600bb")


def resolve_cover(book: dict[str, Any]) -> tuple[bytes, str, str, str] | None:
    """Resolve a cover for one book.

    Returns ``(image_bytes, source, source_url, ext)`` for the first source that
    yields a valid image, or ``None`` if no source has one.
    """
    # 1-2. Goodreads, for the exact edition.
    resolved = _goodreads_cover(book)
    if resolved is not None:
        return resolved

    isbns = [isbn for isbn in (book.get("isbn13"), book.get("isbn10")) if isbn]

    # 3. Open Library cover by ISBN.
    for isbn in isbns:
        url = OPENLIBRARY_COVER_ISBN.format(isbn=isbn)
        result = _download_image(url)
        if result is not None:
            data, ext = result
            return data, "openlibrary:isbn", url, ext

    # 4. Open Library Search by title + author -> cover id -> cover image.
    title = (book.get("title") or "").strip()
    author = (book.get("author") or "").strip()
    if title:
        cover_id = _openlibrary_search_cover_id(title, author)
        if cover_id:
            url = OPENLIBRARY_COVER_ID.format(cover_id=cover_id)
            result = _download_image(url)
            if result is not None:
                data, ext = result
                return data, "openlibrary:search", url, ext

    # 5. iTunes / Apple Books Search by title + author.
    if title:
        artwork_url = _itunes_artwork_url(title, author)
        if artwork_url:
            result = _download_image(artwork_url)
            if result is not None:
                data, ext = result
                return data, "itunes:search", artwork_url, ext

    return None


def _covers_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "covers"


def _remove_existing(data_dir: str | Path, cover_path: str | None) -> None:
    if not cover_path:
        return
    existing = Path(data_dir) / cover_path
    try:
        existing.unlink()
    except FileNotFoundError:
        pass


# Another writer (e.g. the Downloads-watcher Goodreads sync) can hold the
# catalogue's write lock for longer than db.connect's busy_timeout. A cover run
# takes an hour or more on a large library, so it waits and retries instead of
# crashing; if the lock persists across several books it stops cleanly.
LOCK_RETRIES = 4
LOCK_RETRY_DELAY = 5.0
MAX_CONSECUTIVE_LOCKED = 3


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _record_cover(conn, book_id: int, **fields: Any) -> bool:
    """``db.set_cover`` that rides out a busy catalogue. False if it stayed locked."""
    for attempt in range(1, LOCK_RETRIES + 1):
        try:
            db.set_cover(conn, book_id, **fields)
            return True
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc):
                raise
            conn.rollback()
            if attempt < LOCK_RETRIES:
                time.sleep(LOCK_RETRY_DELAY)
    return False


def _should_skip(status: str | None, refresh: bool, retry_missing: bool) -> bool:
    if status == "manual":
        return True  # never clobber a manual cover
    if refresh:
        return False  # reconsider everything (except manual)
    if status == "fetched":
        return True
    if status == "not_found":
        return not retry_missing  # retry_missing re-attempts past misses
    return False  # None / error -> always process


def fetch_covers(
    conn,
    data_dir: str | Path,
    *,
    limit: int | None = None,
    refresh: bool = False,
    retry_missing: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch covers for books that need them.

    ``limit`` caps the number of books *attempted* (not merely scanned), which
    makes ``--limit 5`` useful for trial runs. ``retry_missing`` re-attempts
    books previously marked ``not_found`` (e.g. after adding a new source) while
    leaving already-fetched and manual covers untouched. ``refresh`` only ever
    upgrades: if a book that already has a cover misses or errors this time, its
    existing cover is kept (counted as ``kept``). If the catalogue is locked by
    another writer, the write is retried; a book whose write never gets through
    is left exactly as it was (counted as ``locked``). Returns summary stats.
    """
    if limit is not None and limit < 1:
        raise CoversError("limit must be at least 1.")

    covers_dir = _covers_dir(data_dir)
    fetched = not_found = errors = skipped = kept = locked = 0
    consecutive_locked = 0
    actions: list[dict[str, str]] = []
    attempted = 0

    def note_write(ok: bool, goodreads_id, title: str) -> None:
        nonlocal locked, consecutive_locked
        if ok:
            consecutive_locked = 0
            return
        locked += 1
        consecutive_locked += 1
        actions.append({"goodreads_id": str(goodreads_id), "title": title, "result": "locked"})
        if consecutive_locked >= MAX_CONSECUTIVE_LOCKED:
            raise CoversError(
                "The catalogue stayed locked by another process (is a sync running?). "
                "Covers fetched so far are saved; run fetch-covers again to continue."
            )

    for row in db.iter_books(conn):
        book = dict(row)
        goodreads_id = book.get("goodreads_id")
        if not goodreads_id:
            # Without a stable id we can't name a file or serve it in the web UI.
            skipped += 1
            continue
        if _should_skip(book.get("cover_status"), refresh, retry_missing):
            skipped += 1
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1

        title = str(book.get("title") or "")
        # Only reachable under refresh: a transient miss must not throw away a
        # cover we already have.
        has_cover = book.get("cover_status") == "fetched" and bool(book.get("cover_path"))
        try:
            resolved = resolve_cover(book)
        except CoversError:
            if has_cover:
                kept += 1
                actions.append({"goodreads_id": str(goodreads_id), "title": title, "result": "kept"})
                time.sleep(RATE_LIMIT_DELAY)
                continue
            errors += 1
            actions.append({"goodreads_id": str(goodreads_id), "title": title, "result": "error"})
            if not dry_run:
                ok = _record_cover(
                    conn,
                    int(book["id"]),
                    cover_path=book.get("cover_path"),
                    cover_source=book.get("cover_source"),
                    cover_source_url=book.get("cover_source_url"),
                    cover_status="error",
                )
                note_write(ok, goodreads_id, title)
            time.sleep(RATE_LIMIT_DELAY)
            continue

        if resolved is None and has_cover:
            kept += 1
            actions.append({"goodreads_id": str(goodreads_id), "title": title, "result": "kept"})
            time.sleep(RATE_LIMIT_DELAY)
            continue

        if resolved is None:
            not_found += 1
            actions.append({"goodreads_id": str(goodreads_id), "title": title, "result": "not_found"})
            if not dry_run:
                ok = _record_cover(
                    conn,
                    int(book["id"]),
                    cover_path=None,
                    cover_source=None,
                    cover_source_url=None,
                    cover_status="not_found",
                )
                note_write(ok, goodreads_id, title)
            time.sleep(RATE_LIMIT_DELAY)
            continue

        data, source, source_url, ext = resolved
        rel_path = f"covers/{goodreads_id}.{ext}"
        if not dry_run:
            # Stage the image, record it, then swap it in: if the catalogue
            # stays locked, the book's existing file and row are untouched.
            covers_dir.mkdir(parents=True, exist_ok=True)
            staged = Path(data_dir) / f"{rel_path}.part"
            staged.write_bytes(data)
            ok = _record_cover(
                conn,
                int(book["id"]),
                cover_path=rel_path,
                cover_source=source,
                cover_source_url=source_url,
                cover_status="fetched",
            )
            if not ok:
                staged.unlink()
                note_write(ok, goodreads_id, title)
                time.sleep(RATE_LIMIT_DELAY)
                continue
            note_write(ok, goodreads_id, title)
            staged.replace(Path(data_dir) / rel_path)
            if book.get("cover_path") != rel_path:
                _remove_existing(data_dir, book.get("cover_path"))
        actions.append(
            {"goodreads_id": str(goodreads_id), "title": title, "result": "fetched", "source": source}
        )
        fetched += 1
        time.sleep(RATE_LIMIT_DELAY)

    return {
        "fetched": fetched,
        "not_found": not_found,
        "errors": errors,
        "skipped": skipped,
        "kept": kept,
        "locked": locked,
        "actions": actions,
    }


def set_manual_cover(
    conn,
    data_dir: str | Path,
    goodreads_id: str,
    *,
    url: str | None = None,
    file: str | Path | None = None,
) -> dict[str, Any]:
    """Set a cover from a user-supplied URL or local file.

    The resulting cover is tagged ``manual`` so automatic fetches never replace it.
    """
    if bool(url) == bool(file):
        raise CoversError("Provide exactly one of url or file.")

    row = db.get_book_by_goodreads_id(conn, goodreads_id)
    if row is None:
        raise CoversError(f"No book found for Goodreads ID {goodreads_id}")
    book = dict(row)

    if url:
        result = _download_image(url)
        if result is None:
            raise CoversError(f"{url} did not return a usable image.")
        data, ext = result
        source_url = url
    else:
        path = Path(file)  # type: ignore[arg-type]
        if not path.exists():
            raise CoversError(f"Cover file not found: {path}")
        data = path.read_bytes()
        ext = _detect_image_ext(data)
        if ext is None:
            raise CoversError(f"{path} is not a supported image (JPEG/PNG/GIF/WEBP).")
        source_url = str(path)

    rel_path = f"covers/{goodreads_id}.{ext}"
    _remove_existing(data_dir, book.get("cover_path"))
    _covers_dir(data_dir).mkdir(parents=True, exist_ok=True)
    (Path(data_dir) / rel_path).write_bytes(data)
    db.set_cover(
        conn,
        int(book["id"]),
        cover_path=rel_path,
        cover_source="manual",
        cover_source_url=source_url,
        cover_status="manual",
    )
    return {"goodreads_id": goodreads_id, "title": book.get("title"), "cover_path": rel_path}
