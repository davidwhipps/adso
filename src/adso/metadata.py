"""Open Library metadata enrichment for the local catalogue.

Descriptions, subjects, and place/time facets are fetched from Open Library and
*owned* locally: like covers, this is enrichment, not a Goodreads-sourced field,
so it deliberately stays out of the source_snapshots/sync_conflicts machinery.

Resolution chain per book (first *content-bearing* hit wins):
    1. Edition by ISBN-13 then ISBN-10 -> work record.
    2. Open Library Search -> work record, trying progressively looser queries:
       cleaned title (parentheticals and subtitle stripped) + author, cleaned
       title alone, then full-text q= search. Candidates whose authors visibly
       disagree with the book's are rejected. When the book has no ISBN at all,
       the matched edition is also fetched to backfill empty isbn13/isbn10
       columns (fill-only-if-empty, enforced in db.backfill_isbns; a guard in
       adso.sync keeps later Goodreads syncs from blanking them).

A matched work with no description and no subjects does not stop the chain:
the match is kept for provenance but resolution continues, because skeleton
work records are common (especially for recent books) while a sibling work
reachable via search often carries the content. The 2026-07 not-found audit
motivated this shape: half the misses were skeleton records, and most of the
rest were exact-title searches broken by Goodreads subtitle/series noise.

Description and subjects are always read from the WORK record so every book
gets the same canonical shape regardless of which path matched it. Open Library
is the only source: open, no API key, and already trusted by the cover fetcher.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from . import db
from .ol_http import RATE_LIMIT_DELAY
from .ol_http import request as _http_request

OPENLIBRARY_EDITION_ISBN = "https://openlibrary.org/isbn/{isbn}.json"
OPENLIBRARY_WORK = "https://openlibrary.org{work_key}.json"
OPENLIBRARY_EDITION = "https://openlibrary.org/books/{olid}.json"
OPENLIBRARY_SEARCH = "https://openlibrary.org/search.json"

# Display caps applied at store time: the stored form IS the display form, the
# same philosophy as db.normalize_tags. Popular works carry 40+ noisy subject
# tags; past these caps they stop being browsable.
SUBJECTS_CAP = 25
PLACES_CAP = 10
TIMES_CAP = 10

# Library-plumbing tags Open Library attaches that say nothing about the book.
_JUNK_SUBJECTS = {
    "accessible book",
    "protected daisy",
    "in library",
    "overdrive",
    "large type books",
    "lending library",
    "popular print disabled books",
    "internet archive wishlist",
    "staff picks",
    "open library staff picks",
}
_JUNK_SUBJECT_RE = re.compile(r"^(nyt:|award:|collection:)")

# Sentence-length classification strings aren't badges; drop them.
_MAX_SUBJECT_LENGTH = 60

# Open Library aggregates subjects across every translated edition of a work,
# so popular books carry French/Spanish/German/Catalan duplicates (Noblesse,
# Biografía, Kultur, "S. XIV"). Heuristic English-only filter: any non-ASCII
# letter, any word from this curated foreign-term set, or a Spanish/Catalan
# century code drops the tag. Imperfect by design — rare English subjects with
# accents are sacrificed for a clean badge list.
_FOREIGN_SUBJECT_WORDS = frozenset(
    {
        # history / biography / nobility
        "histoire", "geschichte", "geschiedenis", "historia", "storia",
        "biographie", "biografia", "biographien",
        "noblesse", "nobleza", "noblesa",
        # culture / literature / philosophy
        "kultur", "cultura", "culturele",
        "literatura", "letteratura", "literatur",
        "philosophie", "filosofia", "filosofie",
        "novela", "romanzo",
        # periods (words like "roman" or "media" are deliberately absent —
        # they'd false-positive on Roman Empire / mass media)
        "siglo", "segle", "jahrhundert", "secolo",
        "moyen", "edad", "mittelalter",
        # languages / nationalities as subject words
        "francais", "espagnol", "castellano", "deutsch",
    }
)
_SPANISH_CENTURY_RE = re.compile(r"^s\.?\s*[xvi]+\b", re.IGNORECASE)


def _looks_english(subject: str) -> bool:
    if any(ord(char) > 127 for char in subject):
        return False
    if _SPANISH_CENTURY_RE.match(subject):
        return False
    words = {word.strip(".,;:()") for word in subject.lower().split()}
    return not (words & _FOREIGN_SUBJECT_WORDS)


class MetadataError(RuntimeError):
    pass


def _request(method: str, url: str, **kwargs):
    """Shared polite HTTP client (see ol_http), raising MetadataError on failure.

    Kept as a module attribute so tests can patch ``adso.metadata._request``.
    """
    return _http_request(method, url, error_cls=MetadataError, **kwargs)


def _get_json(url: str, **kwargs) -> dict[str, Any] | None:
    response = _request("get", url, **kwargs)
    if response is None or response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_description(value: Any) -> str | None:
    """Open Library descriptions come as a plain string or {"type", "value"}."""
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clean_subjects(raw: Any, *, cap: int) -> list[str]:
    """Normalise an OL subject list: junk filter, dedupe, length cap."""
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        subject = " ".join(entry.split())
        key = subject.lower()
        if (
            not subject
            or len(subject) > _MAX_SUBJECT_LENGTH
            or key in _JUNK_SUBJECTS
            or _JUNK_SUBJECT_RE.match(key)
            or key in seen
            or not _looks_english(subject)
        ):
            continue
        seen.add(key)
        cleaned.append(subject)
        if len(cleaned) >= cap:
            break
    return cleaned


def _edition_by_isbn(isbn: str) -> dict[str, Any] | None:
    return _get_json(OPENLIBRARY_EDITION_ISBN.format(isbn=isbn))


def _edition_by_olid(olid: str) -> dict[str, Any] | None:
    return _get_json(OPENLIBRARY_EDITION.format(olid=olid))


def _fetch_work(work_key: str) -> dict[str, Any] | None:
    if not isinstance(work_key, str) or not work_key.startswith("/works/"):
        return None
    return _get_json(OPENLIBRARY_WORK.format(work_key=work_key))


def _work_key_from_edition(edition: dict[str, Any]) -> str | None:
    works = edition.get("works") or []
    if works and isinstance(works[0], dict):
        return works[0].get("key")
    return None


# How many docs each search query returns, and how many candidate works one
# book may fetch across all queries. Requests within a book are not
# individually rate-limited, so the fetch bound keeps a book with many
# implausible candidates from bursting through the politeness budget.
SEARCH_RESULT_LIMIT = 5
MAX_CANDIDATE_WORK_FETCHES = 4

_SEARCH_FIELDS = "key,cover_edition_key,edition_key,author_name"
_PARENTHETICAL_RE = re.compile(r"\([^)]*\)")


def _clean_title(title: str) -> str:
    """Search form of a Goodreads title: no parentheticals, no subtitle.

    Goodreads decorates titles with series/edition parentheticals and long
    subtitles ("Atomic Habits: An Easy and Proven Way…") that Open Library's
    title search treats as hard requirements.
    """
    cleaned = _PARENTHETICAL_RE.sub(" ", title).split(":")[0]
    cleaned = " ".join(cleaned.split())
    return cleaned or " ".join(title.split())


def _author_tokens(author: str) -> set[str]:
    return {token for token in re.split(r"[^\w]+", author.lower()) if len(token) > 2}


def _plausible_author(doc: dict[str, Any], tokens: set[str]) -> bool:
    """Reject a search doc only when its authors visibly disagree with ours."""
    if not tokens:
        return True
    names = " ".join(
        name for name in doc.get("author_name") or [] if isinstance(name, str)
    ).lower()
    if not names:
        return True  # doc carries no author info: give it the benefit of the doubt
    return any(token in names for token in tokens)


def _search_docs(params: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _get_json(
        OPENLIBRARY_SEARCH,
        params={**params, "limit": SEARCH_RESULT_LIMIT, "fields": _SEARCH_FIELDS},
    )
    if payload is None:
        return []
    return [doc for doc in payload.get("docs") or [] if isinstance(doc, dict)]


def _search_candidates(title: str, author: str):
    """Yield plausible search docs, tightest query first, deduped by work key.

    Three passes: title+author field search, title-only field search, then
    full-text q= search. The field search misses records whose canonical OL
    title differs slightly (leading article, alternate subtitle); q= does not.
    """
    cleaned = _clean_title(title)
    tokens = _author_tokens(author)
    queries: list[dict[str, Any]] = [
        {"title": cleaned, "author": author} if author else {"title": cleaned}
    ]
    if author:
        queries.append({"title": cleaned})
    queries.append({"q": f"{cleaned} {author}".strip()})
    seen: set[str] = set()
    for params in queries:
        for doc in _search_docs(params):
            key = doc.get("key")
            if not isinstance(key, str) or key in seen:
                continue
            seen.add(key)
            if _plausible_author(doc, tokens):
                yield doc


def _first_isbn(edition: dict[str, Any], field: str) -> str | None:
    values = edition.get(field) or []
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _work_content(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": _parse_description(work.get("description")),
        "subjects": _clean_subjects(work.get("subjects"), cap=SUBJECTS_CAP),
        "subject_places": _clean_subjects(work.get("subject_places"), cap=PLACES_CAP),
        "subject_times": _clean_subjects(work.get("subject_times"), cap=TIMES_CAP),
    }


def _has_content(resolved: dict[str, Any]) -> bool:
    return bool(
        resolved["description"]
        or resolved["subjects"]
        or resolved["subject_places"]
        or resolved["subject_times"]
    )


def resolve_metadata(book: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve work metadata (and possibly backfill ISBNs) for one book.

    Returns a dict with description/subjects/places/times, provenance, and any
    backfill ISBNs, or None when no Open Library work could be matched at all.
    A returned dict whose content fields are all empty means a work matched
    but carries no content yet; the caller records it as not_found while
    keeping the provenance.
    """
    empty_match: dict[str, Any] | None = None

    def _result(content: dict[str, Any], source: str, source_url: str) -> dict[str, Any]:
        return {
            **content,
            "source": source,
            "source_url": source_url,
            "backfill_isbn10": None,
            "backfill_isbn13": None,
        }

    # 1. Edition by ISBN -> work.
    for isbn in (book.get("isbn13"), book.get("isbn10")):
        if not isbn:
            continue
        edition = _edition_by_isbn(isbn)
        if edition is None:
            continue
        work_key = _work_key_from_edition(edition)
        work = _fetch_work(work_key) if work_key else None
        if work is None:
            continue
        result = _result(
            _work_content(work), "openlibrary:isbn", OPENLIBRARY_EDITION_ISBN.format(isbn=isbn)
        )
        if _has_content(result):
            return result
        # Skeleton record: remember the match, fall through to search — a
        # sibling work often carries the content this one lacks.
        empty_match = result
        break

    # 2. Search -> work (+ edition for ISBN backfill).
    title = (book.get("title") or "").strip()
    author = (book.get("author") or "").strip()
    if not title:
        return empty_match
    work_fetches = 0
    for doc in _search_candidates(title, author):
        if work_fetches >= MAX_CANDIDATE_WORK_FETCHES:
            break
        work_fetches += 1
        work = _fetch_work(doc.get("key"))
        if work is None:
            continue
        result = _result(
            _work_content(work), "openlibrary:search", f"https://openlibrary.org{doc.get('key')}"
        )
        if not _has_content(result):
            empty_match = empty_match or result
            continue

        # Backfill only books with no ISBN at all; Goodreads-supplied ISBNs are
        # never second-guessed.
        if not book.get("isbn13") and not book.get("isbn10"):
            olid = doc.get("cover_edition_key") or next(
                (k for k in (doc.get("edition_key") or []) if isinstance(k, str)), None
            )
            if olid:
                edition = _edition_by_olid(olid)
                if edition is not None:
                    result["backfill_isbn13"] = _first_isbn(edition, "isbn_13")
                    result["backfill_isbn10"] = _first_isbn(edition, "isbn_10")
        return result

    return empty_match


def _should_skip(status: str | None, refresh: bool, retry_missing: bool) -> bool:
    if status == "manual":
        return True  # future-proofing: never clobber hand-set metadata
    if refresh:
        return False  # reconsider everything (except manual)
    if status == "fetched":
        return True
    if status == "not_found":
        return not retry_missing  # retry_missing re-attempts past misses
    return False  # None / error -> always process


def fetch_metadata(
    conn,
    *,
    limit: int | None = None,
    refresh: bool = False,
    retry_missing: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch Open Library metadata for books that need it.

    Mirrors covers.fetch_covers: ``limit`` caps books *attempted*, statuses are
    fetched / not_found / error, and ``--retry-missing`` re-attempts past
    misses. A matched work with neither description nor any subjects counts as
    not_found so retry semantics stay meaningful, but its provenance is kept
    (metadata_source set, status not_found = "matched a skeleton record") and
    any previously fetched content is preserved rather than cleared. Returns
    summary stats including how many empty ISBNs were backfilled.
    """
    if limit is not None and limit < 1:
        raise MetadataError("limit must be at least 1.")

    fetched = not_found = errors = skipped = isbn_backfilled = 0
    actions: list[dict[str, str]] = []
    attempted = 0

    for row in db.iter_books(conn):
        book = dict(row)
        goodreads_id = book.get("goodreads_id")
        if not goodreads_id:
            skipped += 1
            continue
        if _should_skip(book.get("metadata_status"), refresh, retry_missing):
            skipped += 1
            continue
        if limit is not None and attempted >= limit:
            break
        attempted += 1

        title = str(book.get("title") or "")
        try:
            resolved = resolve_metadata(book)
        except MetadataError:
            errors += 1
            actions.append({"goodreads_id": str(goodreads_id), "title": title, "result": "error"})
            if not dry_run:
                db.set_metadata(
                    conn,
                    int(book["id"]),
                    description=book.get("description"),
                    subjects=_loads_list(book.get("subjects_json")),
                    subject_places=_loads_list(book.get("subject_places_json")),
                    subject_times=_loads_list(book.get("subject_times_json")),
                    metadata_source=book.get("metadata_source"),
                    metadata_source_url=book.get("metadata_source_url"),
                    metadata_status="error",
                )
            time.sleep(RATE_LIMIT_DELAY)
            continue

        if resolved is None or not _has_content(resolved):
            not_found += 1
            action = {"goodreads_id": str(goodreads_id), "title": title, "result": "not_found"}
            if resolved is not None:
                action["matched_empty"] = "yes"
            actions.append(action)
            if not dry_run:
                # Mirror the error path: an empty or vanished OL record must
                # never erase content we already own (matters on --refresh).
                # When a work matched but was empty, record its provenance so
                # skeleton records are distinguishable from true absences.
                db.set_metadata(
                    conn,
                    int(book["id"]),
                    description=book.get("description"),
                    subjects=_loads_list(book.get("subjects_json")),
                    subject_places=_loads_list(book.get("subject_places_json")),
                    subject_times=_loads_list(book.get("subject_times_json")),
                    metadata_source=(
                        resolved["source"] if resolved else book.get("metadata_source")
                    ),
                    metadata_source_url=(
                        resolved["source_url"] if resolved else book.get("metadata_source_url")
                    ),
                    metadata_status="not_found",
                )
            time.sleep(RATE_LIMIT_DELAY)
            continue

        action = {
            "goodreads_id": str(goodreads_id),
            "title": title,
            "result": "fetched",
            "source": str(resolved["source"]),
        }
        if not dry_run:
            db.set_metadata(
                conn,
                int(book["id"]),
                description=resolved["description"],
                subjects=resolved["subjects"],
                subject_places=resolved["subject_places"],
                subject_times=resolved["subject_times"],
                metadata_source=resolved["source"],
                metadata_source_url=resolved["source_url"],
                metadata_status="fetched",
            )
            if resolved["backfill_isbn13"] or resolved["backfill_isbn10"]:
                if db.backfill_isbns(
                    conn,
                    int(book["id"]),
                    isbn10=resolved["backfill_isbn10"],
                    isbn13=resolved["backfill_isbn13"],
                ):
                    isbn_backfilled += 1
                    action["isbn_backfilled"] = "yes"
        elif resolved["backfill_isbn13"] or resolved["backfill_isbn10"]:
            isbn_backfilled += 1
            action["isbn_backfilled"] = "yes"
        fetched += 1
        actions.append(action)
        time.sleep(RATE_LIMIT_DELAY)

    return {
        "fetched": fetched,
        "not_found": not_found,
        "errors": errors,
        "skipped": skipped,
        "isbn_backfilled": isbn_backfilled,
        "actions": actions,
    }


def _loads_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
