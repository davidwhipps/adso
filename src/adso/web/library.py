"""Library browsing for the web UI: shelves, smart views, tags, sort and counts.

The catalogue page is one faceted view over the whole library. Facets are
mutually independent (shelf, smart view, tag, plus the older status/format/
rating/author filters that remain accepted in the URL), and each sidebar count
is computed with every *other* active facet applied, so a count always says
how many books clicking that entry would show.

The library is small (thousands of rows at most), so this module loads it once
per request via :func:`adso.catalogue.list_books` and filters in Python. Full-
text search still goes through :func:`adso.catalogue.search_books` (FTS5 when
available) and only contributes the set of matching ids.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .. import categorize as cat
from .. import db
from ..catalogue import list_books, search_books
from ..covers import image_size

# Goodreads' raw exclusive-shelf values, in sidebar order. "Library" (no shelf
# chosen) is the whole collection, to-read included.
TO_READ_SHELF = "to-read"
SHELVES: tuple[tuple[str, str], ...] = (
    ("currently-reading", "Currently reading"),
    ("to-read", "To read"),
    ("read", "Read"),
    ("did-not-finish", "Did not finish"),
)
SHELF_LABELS = dict(SHELVES)

RECENT_DAYS = 90


def _recent(book: dict[str, Any]) -> bool:
    added = (book.get("date_added") or "")[:10].replace("/", "-")
    try:
        return date.fromisoformat(added) >= date.today() - timedelta(days=RECENT_DAYS)
    except ValueError:
        return False


SMART_VIEWS: tuple[tuple[str, str, Callable[[dict[str, Any]], bool]], ...] = (
    ("loaned", "Loaned out", lambda b: bool(b.get("loaned_to"))),
    ("unrated", "Read, unrated", lambda b: b.get("exclusive_shelf") == "read" and not b.get("rating")),
    ("recent", "Recently added", _recent),
    ("loved", "Five stars", lambda b: b.get("rating") == 5),
    ("untagged", "Untagged", lambda b: not b.get("tags")),
)
SMART_LABELS = {key: label for key, label, _ in SMART_VIEWS}
_SMART_TESTS = {key: test for key, _, test in SMART_VIEWS}

SORTS: tuple[tuple[str, str], ...] = (
    ("title", "Title"),
    ("author", "Author"),
    ("added", "Recently added"),
    ("rating", "Rating"),
    ("year", "Year published"),
    ("read", "Date read"),
)
SORT_LABELS = dict(SORTS)
VIEWS = ("grid", "table", "wall")
# Parameters that make up a library URL, in a stable order for link building.
URL_PARAMS = (
    "q", "shelf", "smart", "tag", "category", "gr_shelf", "series",
    "status", "format", "rating", "author", "sort", "view", "book",
)

_ARTICLES = ("the ", "a ", "an ")


def _bare(title: str) -> str:
    low = title.lower()
    for article in _ARTICLES:
        if low.startswith(article):
            return low[len(article):]
    return low


def _last_name(author: str | None) -> str:
    parts = (author or "").split()
    return parts[-1].lower() if parts else ""


def _year(book: dict[str, Any]) -> int:
    return book.get("original_publication_year") or book.get("year_published") or 0


_SORT_KEYS: dict[str, tuple[Callable[[dict[str, Any]], Any], bool]] = {
    # key function, reverse
    "title": (lambda b: _bare(b["title"]), False),
    "author": (lambda b: (_last_name(b["author"]), _bare(b["title"])), False),
    "added": (lambda b: b.get("date_added") or "", True),
    "rating": (lambda b: b.get("rating") or 0, True),
    "year": (_year, True),
    "read": (lambda b: b.get("date_read") or "", True),
}


def sort_books(books: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Sort by ``sort`` with title as the stable tie-break."""
    by_title = sorted(books, key=lambda b: _bare(b["title"]))
    key, reverse = _SORT_KEYS.get(sort, _SORT_KEYS["title"])
    return sorted(by_title, key=key, reverse=reverse)


@dataclass
class LibraryParams:
    q: str = ""
    shelf: str = ""
    smart: str = ""
    tag: str = ""
    category: str = ""  # category id; matches the category and everything beneath it
    gr_shelf: str = ""  # any Goodreads shelf, not just the exclusive one
    series: str = ""  # series name; lists it in reading order
    status: str = ""
    format: str = ""
    rating: int | None = None
    author: str = ""
    sort: str = "title"
    view: str = "grid"
    book: str = ""

    @classmethod
    def clean(cls, **raw: Any) -> LibraryParams:
        """Clamp raw query values to known options (unknowns become 'unset')."""
        p = cls(**{k: v for k, v in raw.items() if v is not None})
        p.q = p.q.strip()
        p.shelf = p.shelf if p.shelf in SHELF_LABELS else ""
        p.smart = p.smart if p.smart in SMART_LABELS else ""
        p.tag = p.tag.strip().lower()
        p.category = p.category.strip() if p.category.strip().isdigit() else ""
        p.gr_shelf = p.gr_shelf.strip().lower()
        p.series = " ".join(p.series.split())
        p.format = p.format if p.format in db.VALID_FORMATS else ""
        p.sort = p.sort if p.sort in SORT_LABELS else "title"
        p.view = p.view if p.view in VIEWS else "grid"
        return p

    def query(self, **changes: Any) -> str:
        """Encode these params (with ``changes``) as a ``/?...`` URL, dropping defaults."""
        values = replace(self, **changes)
        pairs = []
        for name in URL_PARAMS:
            value = getattr(values, name)
            if value in ("", None) or (name == "sort" and value == "title") or (name == "view" and value == "grid"):
                continue
            pairs.append((name, value))
        return "/?" + urlencode(pairs) if pairs else "/"


@dataclass
class Facet:
    key: str
    label: str
    count: int
    active: bool
    url: str
    depth: int = 0


@dataclass
class Library:
    params: LibraryParams
    books: list[dict[str, Any]]
    wall: list[dict[str, Any]]
    match_ids: set[str]
    total: int
    library_count: int
    shelves: list[Facet]
    smart: list[Facet]
    tags: list[Facet]
    genres: list[Facet] = field(default_factory=list)
    traditions: list[Facet] = field(default_factory=list)
    eras: list[Facet] = field(default_factory=list)
    themes: list[Facet] = field(default_factory=list)
    category_label: str = ""
    chips: list[tuple[str, str]] = field(default_factory=list)  # (label, remove-url)

    @property
    def heading(self) -> str:
        p = self.params
        parts = [
            SMART_LABELS.get(p.smart, ""), SHELF_LABELS.get(p.shelf, ""), self.category_label,
            p.series, f"#{p.tag}" if p.tag else "",
        ]
        parts.append(f"“{p.q}”" if p.q else "")
        parts = [x for x in parts if x]
        return " · ".join(parts) if parts else "The Library"

    @property
    def subline(self) -> str:
        n = len(self.books)
        if not n:
            return "Nothing on these shelves."
        bits = [f"{n:,} {'volume' if n == 1 else 'volumes'}"]
        read = sum(1 for b in self.books if b.get("exclusive_shelf") == "read")
        current = sum(1 for b in self.books if b.get("exclusive_shelf") == "currently-reading")
        if read and self.params.shelf != "read":
            bits.append(f"{read} read")
        if current and self.params.shelf != "currently-reading":
            bits.append(f"{current} on the go")
        return ", ".join(bits) + "."


class _CoverShapes:
    """Cache of cover aspect ratios (height / width), keyed by path + mtime."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, float], float] = {}

    def ratio(self, root: Path, cover_path: str | None) -> float:
        if not cover_path:
            return 1.5
        path = root / cover_path
        try:
            key = (str(path), path.stat().st_mtime)
        except OSError:
            return 1.5
        if key not in self._cache:
            size = image_size(path)
            # Clamp odd shapes so one extreme image can't wreck the grid.
            self._cache[key] = min(max(size[1] / size[0], 0.9), 2.0) if size and size[0] else 1.5
        return self._cache[key]


COVER_SHAPES = _CoverShapes()


def _passes(book: dict[str, Any], p: LibraryParams, match_ids: set[str] | None, skip: str = "") -> bool:
    shelf = book.get("exclusive_shelf") or ""
    if skip != "shelf" and p.shelf and shelf != p.shelf:
        return False
    if skip != "smart" and p.smart and not _SMART_TESTS[p.smart](book):
        return False
    if skip != "tag" and p.tag and p.tag not in book.get("tags", []):
        return False
    if skip != "category" and p.category and int(p.category) not in book.get("_cats", ()):
        return False
    if skip != "gr_shelf" and p.gr_shelf and p.gr_shelf not in book.get("shelves", []):
        return False
    if p.series and ((book.get("series") or {}).get("name") or "").casefold() != p.series.casefold():
        return False
    if p.status and book.get("reading_status") != p.status:
        return False
    if p.format and book.get("format") != p.format:
        return False
    if p.rating is not None and (book.get("rating") or 0) != p.rating:
        return False
    if p.author:
        needle = p.author.lower()
        if needle not in (book.get("author") or "").lower() and needle not in (book.get("additional_authors") or "").lower():
            return False
    if match_ids is not None and book.get("goodreads_id") not in match_ids:
        return False
    return True


def build_library(conn: sqlite3.Connection, p: LibraryParams, cover_root: Path) -> Library:
    """Everything the catalogue page renders for ``p``."""
    everything = list_books(conn)
    taxonomy = cat.Taxonomy(conn)
    assignments = cat.book_category_map(conn)
    series = cat.book_series_map(conn)
    for book in everything:
        book["ar"] = COVER_SHAPES.ratio(cover_root, book.get("cover_path"))
        # Each book's categories plus all their ancestors, so a filter on a
        # parent ("Speculative Fiction") includes its subcategories.
        rolled: set[int] = set()
        for category_id in assignments.get(book["id"], {}):
            rolled.update(node.id for node in taxonomy.lineage(category_id))
        book["_cats"] = rolled
        book["series"] = series.get(book["id"])
    match_ids = {b["goodreads_id"] for b in search_books(conn, p.q)} if p.q else None
    if p.category and int(p.category) not in taxonomy.by_id:
        p = replace(p, category="")

    books = [b for b in everything if _passes(b, p, match_ids)]
    if p.series:
        # A series reads in order; books without a position go last.
        books = sorted(sort_books(books, "title"), key=lambda b: (b["series"] or {}).get("position") or 1e9)
    else:
        books = sort_books(books, p.sort)

    def count(skip: str, test: Callable[[dict[str, Any]], bool]) -> int:
        return sum(1 for b in everything if test(b) and _passes(b, p, match_ids, skip))

    shelves = [
        Facet(key, label, count("shelf", lambda b, k=key: b.get("exclusive_shelf") == k), p.shelf == key,
              p.query(shelf="" if p.shelf == key else key, book=""))
        for key, label in SHELVES
    ]
    smart = [
        Facet(key, label, count("smart", test), p.smart == key, p.query(smart="" if p.smart == key else key, book=""))
        for key, label, test in SMART_VIEWS
    ]
    tag_totals = Counter(t for b in everything for t in b.get("tags", []))
    tags = [
        Facet(t, t, count("tag", lambda b, t=t: t in b.get("tags", [])), p.tag == t,
              p.query(tag="" if p.tag == t else t, book=""))
        for t, _ in sorted(tag_totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    def category_facets(facet: str) -> list[Facet]:
        out = []
        for node in next(f for f in cat.taxonomy_tree(conn) if f["facet"] == facet)["categories"]:
            n = count("category", lambda b, i=node["id"]: i in b["_cats"])
            active = p.category == str(node["id"])
            if n or active:
                out.append(Facet(str(node["id"]), node["label"], n, active,
                                 p.query(category="" if active else str(node["id"]), book=""), node["depth"]))
        return out

    chips: list[tuple[str, str]] = []
    category_label = taxonomy.path(int(p.category)) if p.category else ""
    if p.series:
        chips.append((f"Series: {p.series}", p.query(series="")))
    if p.gr_shelf:
        chips.append((f"Goodreads shelf: {p.gr_shelf}", p.query(gr_shelf="")))
    if p.status:
        chips.append((f"Status: {p.status}", p.query(status="")))
    if p.format:
        chips.append((f"Format: {p.format}", p.query(format="")))
    if p.rating is not None:
        chips.append((f"Rating: {p.rating or 'unrated'}", p.query(rating=None)))
    if p.author:
        chips.append((f"Author: {p.author}", p.query(author="")))

    wall = sort_books(everything, p.sort) if p.view == "wall" else []
    return Library(
        params=p,
        books=books,
        wall=wall,
        match_ids={b["goodreads_id"] for b in books},
        total=len(everything),
        # What "Library" (no shelf: the whole collection) would show.
        library_count=sum(1 for b in everything if _passes(b, replace(p, shelf=""), match_ids)),
        shelves=shelves,
        smart=smart,
        tags=tags,
        genres=category_facets("genre"),
        traditions=category_facets("tradition"),
        eras=category_facets("era"),
        themes=category_facets("theme"),
        category_label=category_label.split(" > ")[-1] if category_label else "",
        chips=chips,
    )
