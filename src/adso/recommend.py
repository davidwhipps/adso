"""What to read next: a taste profile, ranked picks with reasons, related books.

Everything is computed from the catalogue on demand (a library is thousands of
rows at most): no model, no network, no stored state. The inputs are the
user's own signals:

- **Taste** comes from books on the read and did-not-finish shelves (custom
  shelves such as "attempted" or "abandoned" count as did-not-finish). A rating
  is the strongest signal (5 stars +2 ... 1 star -2); a book read but unrated
  counts mildly for (it was finished), a DNF counts against.
- **Features** of a book are its categories (with their ancestors, so loving
  Space Opera also warms Science Fiction), tags, Open Library subjects and
  author. Rare features weigh more than common ones (idf), and every
  per-feature score is shrunk toward neutral until a few books back it, so one
  book can't make or break a genre.

``next_reads`` ranks the to-read pile against that profile, adds series order
(the next unread book in a series you're enjoying; never book 3 before book
2), a nudge for books the user owns and for strong community ratings, and a
variety pass so the list isn't one genre. Every pick carries plain-language
reasons. ``related_books`` finds neighbours of one book; ``insights``
summarises reading by genre.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import categorize as cat
from .catalogue import list_books

READ_SHELVES = frozenset({"read"})
PILE_SHELVES = frozenset({"to-read"})
# Shelves or tags that mean "read this soon": a small push up the list.
SHORTLIST_TERMS = frozenset({"shortlist", "short list", "up next", "next up", "read next", "priority"})
SHORTLIST_BONUS = 0.35
# Subjects too broad to say anything about taste.
GENERIC_SUBJECTS = frozenset({"fiction", "nonfiction", "non fiction", "literature", "fiction general"})

RATING_SIGNAL = {5: 2.0, 4: 1.0, 3: 0.0, 2: -1.0, 1: -2.0}
UNRATED_READ_SIGNAL = 0.3
DNF_SIGNAL = -1.5

# How much each kind of feature counts, before idf. Categories are the user's
# curated view; subjects are Open Library's noisier one.
FEATURE_WEIGHT = {"cat": 1.0, "tag": 0.8, "subject": 0.35}
# Within categories, broad facets say little about taste ("Fiction"), so they
# count for less and are never given as the reason for a pick.
FACET_WEIGHT = {"genre": 1.0, "theme": 1.0, "tradition": 0.8, "audience": 0.5, "era": 0.4, "form": 0.3}
REASON_FACETS = frozenset({"genre", "theme", "tradition", "era"})
# Facets whose labels read as adjectives ("Russian", "19th Century").
_ADJECTIVE_FACETS = frozenset({"tradition", "era"})
# Shrinkage: a feature's affinity is sum(signal) / (reads + PRIOR), so it needs
# a few books behind it before it counts fully.
PRIOR = 2.0
AUTHOR_PRIOR = 1.0

SERIES_NEXT_BONUS = 1.2
SERIES_SKIP_PENALTY = -2.5
OWNED_BONUS = 0.25
COMMUNITY_WEIGHT = 0.4
VARIETY_PENALTY = 0.35


@dataclass
class _Book:
    record: dict[str, Any]
    features: dict[tuple[str, Any], float]
    primary: int | None
    series: dict[str, Any] | None

    @property
    def shelf(self) -> str:
        return self.record.get("exclusive_shelf") or ""

    @property
    def is_dnf(self) -> bool:
        if cat.normalize_term(self.shelf) in cat.DNF_SHELF_TERMS:
            return True
        # A book shelved "read" and also "attempted" wasn't really finished.
        return self.shelf in READ_SHELVES and any(
            cat.normalize_term(s) in cat.DNF_SHELF_TERMS for s in self.record.get("shelves") or []
        )

    @property
    def shortlisted(self) -> bool:
        marks = list(self.record.get("shelves") or []) + list(self.record.get("tags") or [])
        return any(cat.normalize_term(m) in SHORTLIST_TERMS for m in marks)

    @property
    def signal(self) -> float | None:
        if self.is_dnf:
            return DNF_SIGNAL
        if self.shelf in READ_SHELVES:
            return RATING_SIGNAL.get(self.record.get("rating") or 0, UNRATED_READ_SIGNAL)
        return None


@dataclass
class _Stat:
    total: float = 0.0
    reads: int = 0
    ratings: list[int] = field(default_factory=list)

    def affinity(self, prior: float = PRIOR) -> float:
        return self.total / (self.reads + prior)

    @property
    def avg_rating(self) -> float | None:
        return sum(self.ratings) / len(self.ratings) if self.ratings else None


class Library:
    """The whole catalogue with features and a taste profile, built once per call."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.taxonomy = cat.Taxonomy(conn)
        assignments = cat.book_category_map(conn)
        series = cat.book_series_map(conn)
        self.books: dict[int, _Book] = {}
        for record in list_books(conn):
            features: dict[tuple[str, Any], float] = {}
            primary = None
            for category_id, role in assignments.get(record["id"], {}).items():
                if category_id not in self.taxonomy.by_id:
                    continue
                if role == "primary":
                    primary = category_id
                for node in self.taxonomy.lineage(category_id):
                    features[("cat", node.id)] = FEATURE_WEIGHT["cat"] * FACET_WEIGHT.get(node.facet, 1.0)
            for tag in record.get("tags") or []:
                features[("tag", tag)] = FEATURE_WEIGHT["tag"]
            for subject in record.get("subjects") or []:
                term = cat.normalize_term(subject)
                if term and term not in GENERIC_SUBJECTS and not cat.is_noise_subject(term):
                    features.setdefault(("subject", term), FEATURE_WEIGHT["subject"])
            self.books[record["id"]] = _Book(record, features, primary, series.get(record["id"]))

        # idf: rare features say more about a book than ubiquitous ones.
        counts: dict[tuple[str, Any], int] = defaultdict(int)
        for book in self.books.values():
            for feature in book.features:
                counts[feature] += 1
        total = max(len(self.books), 1)
        self.idf = {f: math.log(1 + total / n) for f, n in counts.items()}

        self.taste: dict[tuple[str, Any], _Stat] = defaultdict(_Stat)
        self.authors: dict[str, _Stat] = defaultdict(_Stat)
        for book in self.books.values():
            signal = book.signal
            if signal is None:
                continue
            rating = book.record.get("rating") or 0
            for feature in book.features:
                stat = self.taste[feature]
                stat.total += signal
                stat.reads += 1
                if rating and not book.is_dnf:
                    stat.ratings.append(rating)
            author = book.record.get("author")
            if author:
                stat = self.authors[author]
                stat.total += signal
                stat.reads += 1
                if rating and not book.is_dnf:
                    stat.ratings.append(rating)

        self.series_members: dict[str, list[_Book]] = defaultdict(list)
        for book in self.books.values():
            if book.series and book.series.get("name"):
                self.series_members[book.series["name"].casefold()].append(book)

    # -- labels ---------------------------------------------------------------

    def feature_label(self, feature: tuple[str, Any]) -> str:
        kind, value = feature
        if kind == "cat":
            return self.taxonomy.get(value).label
        if kind == "tag":
            return f"#{value}"
        return f"“{value}”"

    def _taste_phrase(self, feature: tuple[str, Any]) -> str:
        stat = self.taste[feature]
        avg = stat.avg_rating
        label = self.feature_label(feature)
        where = {"cat": "", "tag": "books tagged ", "subject": "books about "}[feature[0]]
        if avg is not None:
            if feature[0] == "cat" and self.taxonomy.get(feature[1]).facet in _ADJECTIVE_FACETS:
                return f"You rate {label} books {avg:.1f}★ ({stat.reads} read)"
            return f"You rate {where}{label} {avg:.1f}★ ({stat.reads} read)"
        return f"You've read {stat.reads} {where}{label} book{'s' if stat.reads != 1 else ''}"

    # -- scoring --------------------------------------------------------------

    def taste_match(self, book: _Book) -> tuple[float, list[tuple[float, tuple[str, Any]]]]:
        """Weighted average affinity over the book's features, plus the top contributors."""
        weights = {f: w * self.idf.get(f, 0.0) for f, w in book.features.items()}
        if book.primary is not None and ("cat", book.primary) in weights:
            weights[("cat", book.primary)] *= 2.0
        total_weight = sum(weights.values())
        if not total_weight:
            return 0.0, []
        contributions = []
        for feature, weight in weights.items():
            stat = self.taste.get(feature)
            if stat is None or not stat.reads:
                continue
            contributions.append((weight * stat.affinity() / total_weight, feature))
        contributions.sort(reverse=True)
        return sum(c for c, _ in contributions), contributions

    def series_position(self, book: _Book) -> tuple[float, str | None]:
        """Boost the next unread book in a series; penalise skipping ahead."""
        if not book.series or book.series.get("position") is None:
            return 0.0, None
        members = self.series_members.get(book.series["name"].casefold(), [])
        position = book.series["position"]
        earlier = [m for m in members if m.series.get("position") is not None and m.series["position"] < position]
        if not earlier:
            return 0.0, None
        unread = [m for m in earlier if m.shelf not in READ_SHELVES and float(m.series["position"]).is_integer()]
        name = book.series["name"]
        if unread:
            first = min(unread, key=lambda m: m.series["position"])
            return SERIES_SKIP_PENALTY, (
                f"Wait: {cat.format_position(first.series['position'])} in {name} comes first"
            )
        previous = max(earlier, key=lambda m: m.series["position"])
        signals = [m.signal for m in earlier if m.signal is not None]
        liked = sum(signals) / len(signals) if signals else 0.0
        if liked < 0:
            return -0.5, None
        rating = previous.record.get("rating")
        stars = f" (you rated {cat.format_position(previous.series['position'])} {rating}★)" if rating else ""
        return SERIES_NEXT_BONUS + max(0.0, liked) * 0.5, f"Next in {name}{stars}"

    def author_match(self, book: _Book) -> tuple[float, str | None]:
        author = book.record.get("author")
        stat = self.authors.get(author) if author else None
        if not stat or not stat.reads:
            return 0.0, None
        affinity = stat.affinity(AUTHOR_PRIOR)
        if affinity <= 0:
            return affinity * 0.5, None
        avg = stat.avg_rating
        rated = f", whom you rate {avg:.1f}★" if avg is not None else ""
        return affinity * 0.8, f"By {author}{rated} ({stat.reads} read)"

    def score(self, book: _Book) -> tuple[float, list[str]]:
        taste, contributions = self.taste_match(book)
        series, series_reason = self.series_position(book)
        author, author_reason = self.author_match(book)
        owned = OWNED_BONUS if book.record.get("format") else 0.0
        shortlist = SHORTLIST_BONUS if book.shortlisted else 0.0
        community = 0.0
        try:
            community = max(-0.4, min(0.4, (float(book.record.get("average_rating") or 0) - 3.9) * COMMUNITY_WEIGHT))
        except ValueError:
            pass
        if not book.record.get("average_rating"):
            community = 0.0

        reasons: list[str] = []
        if series_reason:
            reasons.append(series_reason)
        if shortlist:
            reasons.append("On your shortlist")
        # Say which of the user's tastes the book matches: the strongest
        # positive contributors, preferring categories and skipping ancestors
        # of an already-named category.
        named: list[tuple[str, Any]] = []
        for contribution, feature in contributions:
            if contribution <= 0.02 or len(named) >= 2:
                break
            if feature[0] == "cat" and (
                self.taxonomy.get(feature[1]).facet not in REASON_FACETS
                or any(n[0] == "cat" and self.taxonomy.is_ancestor(feature[1], n[1]) for n in named)
            ):
                continue
            if self.taste[feature].affinity() <= 0.1:
                continue
            named.append(feature)
        reasons.extend(self._taste_phrase(f) for f in named)
        if author_reason:
            reasons.append(author_reason)
        if owned:
            reasons.append(f"You own it ({book.record['format']})")
        if community >= 0.2:
            reasons.append(f"Goodreads readers rate it {book.record['average_rating']}")
        return taste + series + author + owned + shortlist + community, reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _card(lib: Library, book: _Book, score: float, reasons: list[str]) -> dict[str, Any]:
    record = book.record
    primary = lib.taxonomy.path(book.primary) if book.primary is not None else None
    return {
        "goodreads_id": record.get("goodreads_id"),
        "title": record.get("title"),
        "author": record.get("author"),
        "cover_url": record.get("cover_url"),
        "shelf": book.shelf,
        "rating": record.get("rating") or 0,
        "format": record.get("format"),
        "pages": record.get("number_of_pages"),
        "primary_genre": primary,
        "series": book.series,
        "score": round(score, 3),
        "reasons": reasons,
    }


def next_reads(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    category: str | None = None,
    owned_only: bool = False,
    max_pages: int | None = None,
    variety: bool = True,
) -> list[dict[str, Any]]:
    """The to-read pile, best first, each with the reasons it ranks there."""
    lib = Library(conn)
    wanted: set[int] | None = None
    if category:
        node = lib.taxonomy.resolve(category)
        wanted = set(lib.taxonomy.descendants(node.id))
    scored = []
    for book in lib.books.values():
        if book.shelf not in PILE_SHELVES:
            continue
        if owned_only and not book.record.get("format"):
            continue
        # A length filter only keeps books known to be short enough.
        if max_pages and not (0 < (book.record.get("number_of_pages") or 0) <= max_pages):
            continue
        if wanted is not None and not any(f[0] == "cat" and f[1] in wanted for f in book.features):
            continue
        score, reasons = lib.score(book)
        scored.append((score, book, reasons))
    scored.sort(key=lambda s: (-s[0], (s[1].record.get("title") or "").lower()))
    if not variety:
        return [_card(lib, b, s, r) for s, b, r in scored[:limit]]

    # Variety: each pick from an already-picked primary genre costs a little,
    # so a strong second genre can surface instead of a tenth Space Opera.
    picked: list[tuple[float, _Book, list[str]]] = []
    seen: dict[int | None, int] = defaultdict(int)
    remaining = scored[:]
    while remaining and len(picked) < limit:
        best = max(
            remaining,
            key=lambda s: s[0] - (VARIETY_PENALTY * seen[s[1].primary] if s[1].primary is not None else 0.0),
        )
        remaining.remove(best)
        seen[best[1].primary] += 1
        picked.append(best)
    return [_card(lib, b, s, r) for s, b, r in picked]


def explore_paths(conn: sqlite3.Connection, *, limit: int = 4, books_per_path: int = 3) -> list[dict[str, Any]]:
    """Genres next to ones the user loves that they've barely read, with books to start.

    Neighbours are a loved genre's children, parent and siblings in the tree,
    then the genres that most often share books with it (top-level genres are
    flat, so for them co-occurrence is what "nearby" means). A path needs at
    least one to-read book and at most one book already read.
    """
    lib = Library(conn)
    genre_ids = {cid for cid, node in lib.taxonomy.by_id.items() if node.facet == "genre"}
    loved = sorted(
        (
            (stat.affinity(), cid)
            for (kind, cid), stat in lib.taste.items()
            if kind == "cat" and cid in genre_ids and stat.reads >= 2 and stat.affinity() > 0.3
        ),
        reverse=True,
    )
    pile: dict[int, list[_Book]] = defaultdict(list)
    for book in lib.books.values():
        if book.shelf in PILE_SHELVES:
            for kind, cid in book.features:
                if kind == "cat" and cid in genre_ids:
                    pile[cid].append(book)
    together: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for book in lib.books.values():
        genres = [cid for kind, cid in book.features if kind == "cat" and cid in genre_ids]
        for a in genres:
            for b in genres:
                if a != b:
                    together[a][b] += 1
    paths: list[dict[str, Any]] = []
    used: set[int] = set()
    for _, source in loved:
        node = lib.taxonomy.get(source)
        neighbours = list(lib.taxonomy.children.get(source, []))
        if node.parent_id is not None:
            neighbours += [node.parent_id, *lib.taxonomy.children.get(node.parent_id, [])]
        neighbours += sorted(together[source], key=lambda cid: -together[source][cid])
        for cid in neighbours:
            if cid == source or cid in used or cid not in genre_ids:
                continue
            stat = lib.taste.get(("cat", cid))
            reads = stat.reads if stat else 0
            if reads > 1 or not pile.get(cid):
                continue
            ranked = sorted(pile[cid], key=lambda b: -lib.score(b)[0])[:books_per_path]
            used.add(cid)
            paths.append(
                {
                    "genre": lib.taxonomy.path(cid),
                    "because": f"{lib._taste_phrase(('cat', source))}; you've read {reads} {lib.taxonomy.get(cid).label}.",
                    "books": [_card(lib, b, *lib.score(b)) for b in ranked],
                }
            )
            if len(paths) >= limit:
                return paths
    return paths


def related_books(conn: sqlite3.Connection, goodreads_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """Books most like this one: same series, same author, shared categories/tags/subjects."""
    lib = Library(conn)
    anchor = next((b for b in lib.books.values() if b.record.get("goodreads_id") == goodreads_id), None)
    if anchor is None:
        raise ValueError(f"No book found for Goodreads ID {goodreads_id}")
    anchor_norm = math.sqrt(sum((w * lib.idf.get(f, 0.0)) ** 2 for f, w in anchor.features.items())) or 1.0
    anchor_series = (anchor.series or {}).get("name", "").casefold()
    results = []
    for book in lib.books.values():
        if book is anchor:
            continue
        reasons: list[str] = []
        score = 0.0
        if anchor_series and (book.series or {}).get("name", "").casefold() == anchor_series:
            # Nearest in the reading order first (the next book leads).
            gap = abs((book.series.get("position") or 0) - ((anchor.series or {}).get("position") or 0))
            ahead = (book.series.get("position") or 0) > ((anchor.series or {}).get("position") or 0)
            score += 3.0 + (0.6 if ahead else 0.3) / (1 + gap)
            reasons.append(f"{cat.format_position(book.series.get('position'))} in {book.series['name']}".strip())
        if anchor.record.get("author") and book.record.get("author") == anchor.record.get("author"):
            score += 1.5
            reasons.append(f"Also by {book.record['author']}")
        shared = set(anchor.features) & set(book.features)
        if shared:
            dot = sum(anchor.features[f] * book.features[f] * lib.idf.get(f, 0.0) ** 2 for f in shared)
            norm = math.sqrt(sum((w * lib.idf.get(f, 0.0)) ** 2 for f, w in book.features.items())) or 1.0
            score += 2.0 * dot / (anchor_norm * norm)
            best = sorted(shared, key=lambda f: -(book.features[f] * lib.idf.get(f, 0.0)))
            specific = [
                f for f in best
                if not (f[0] == "cat" and (
                    lib.taxonomy.get(f[1]).facet not in REASON_FACETS
                    or any(g[0] == "cat" and lib.taxonomy.is_ancestor(f[1], g[1]) for g in shared)
                ))
            ]
            if specific:
                reasons.append("Both " + ", ".join(lib.feature_label(f) for f in specific[:2]))
        if score > 0.15:
            results.append((score, book, reasons))
    results.sort(key=lambda r: (-r[0], (r[1].record.get("title") or "").lower()))
    return [_card(lib, b, s, r) for s, b, r in results[:limit]]


def insights(conn: sqlite3.Connection) -> dict[str, Any]:
    """Reading by genre, and where the to-read pile leans away from what you enjoy."""
    lib = Library(conn)
    read = [b for b in lib.books.values() if b.shelf in READ_SHELVES and not b.is_dnf]
    dnf = [b for b in lib.books.values() if b.is_dnf]
    pile = [b for b in lib.books.values() if b.shelf in PILE_SHELVES]
    rated = [b.record["rating"] for b in read if b.record.get("rating")]
    overall = sum(rated) / len(rated) if rated else None

    tops = [cid for cid in lib.taxonomy.children.get(None, []) if lib.taxonomy.get(cid).facet == "genre"]
    rows = []
    for cid in tops:
        members = set(lib.taxonomy.descendants(cid))

        def has(book: _Book) -> bool:
            return any(f[0] == "cat" and f[1] in members for f in book.features)

        n_read = sum(1 for b in read if has(b))
        n_dnf = sum(1 for b in dnf if has(b))
        n_pile = sum(1 for b in pile if has(b))
        if not (n_read or n_dnf or n_pile):
            continue
        ratings = [b.record["rating"] for b in read if has(b) and b.record.get("rating")]
        rows.append(
            {
                "genre": lib.taxonomy.get(cid).label,
                "id": cid,
                "read": n_read,
                "dnf": n_dnf,
                "to_read": n_pile,
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "dnf_rate": round(n_dnf / (n_read + n_dnf), 2) if (n_read + n_dnf) else None,
                "read_share": round(n_read / len(read), 3) if read else 0.0,
                "pile_share": round(n_pile / len(pile), 3) if pile else 0.0,
            }
        )
    rows.sort(key=lambda r: -(r["read"] + r["to_read"]))

    notes = []
    for row in rows:
        lean = row["pile_share"] - row["read_share"]
        if lean >= 0.08 and row["to_read"] >= 3:
            low = row["avg_rating"] is not None and overall is not None and row["avg_rating"] < overall - 0.2
            finish = row["dnf_rate"] is not None and row["dnf_rate"] >= 0.2
            if low or finish:
                why = []
                if low:
                    why.append(f"you rate it {row['avg_rating']:.1f}★ against {overall:.1f}★ overall")
                if finish:
                    why.append(f"you abandon {int(row['dnf_rate'] * 100)}% of it")
                notes.append(
                    f"{row['genre']} is {int(row['pile_share'] * 100)}% of your to-read pile but "
                    f"{int(row['read_share'] * 100)}% of what you've read, and " + " and ".join(why) + "."
                )
        elif -lean >= 0.08 and row["avg_rating"] is not None and overall is not None and row["avg_rating"] > overall:
            notes.append(
                f"You rate {row['genre']} {row['avg_rating']:.1f}★, above your {overall:.1f}★ average, "
                f"but it's only {int(row['pile_share'] * 100)}% of your to-read pile."
            )

    by_year: dict[str, int] = defaultdict(int)
    for book in read:
        year = (book.record.get("date_read") or "")[:4]
        if year.isdigit():
            by_year[year] += 1
    uncategorised = sum(1 for b in lib.books.values() if not any(f[0] == "cat" for f in b.features))
    return {
        "read": len(read),
        "dnf": len(dnf),
        "to_read": len(pile),
        "unrated_read": sum(1 for b in read if not b.record.get("rating")),
        "average_rating": round(overall, 2) if overall is not None else None,
        "uncategorised": uncategorised,
        "genres": rows,
        "notes": notes,
        "read_by_year": dict(sorted(by_year.items())[-6:]),
    }
