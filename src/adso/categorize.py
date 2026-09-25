"""Categorisation: the taxonomy, the suggestion engine, and human review.

Adso organises books along a few fixed facets (form, genre, tradition, era,
audience, theme).
Genre is a hierarchy, and each book has at most one *primary* genre plus any
number of secondary categories. The vocabulary is seeded from
``taxonomy_seed`` and then belongs to the user.

The engine never decides on its own. It turns evidence the catalogue already
holds (custom Goodreads shelves, Open Library subjects, local tags) into
*mapping proposals* ("shelf cozy-fantasy -> Genre: Fantasy > Cozy Fantasy").
Accepting one creates a rule that applies to every matching book, now and on
every later sync, so one decision organises many books. Rejections are kept, so
the same proposal never returns. Rule-made assignments follow their evidence
(a shelf removed on Goodreads takes its category with it); anything the user
set or confirmed is never touched by a run.

Series membership is parsed from Goodreads titles ("Red Dragon (Hannibal, #1)").
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .errors import AdsoError
from .taxonomy_seed import ERA_YEARS, FACET_LABELS, FACETS, slugify

# Exclusive shelves describe reading state, not what a book is about.
STATUS_SHELVES = frozenset(
    {"read", "to-read", "currently-reading", "did-not-finish", "dnf"}
)

# Custom shelves people use for "did not finish". Like the status shelves they
# describe reading state, so they never raise a proposal; recommendations
# count them as DNF.
DNF_SHELF_TERMS = frozenset(
    {"did not finish", "dnf", "abandoned", "attempted", "gave up", "unfinished", "not finished"}
)

# Common Goodreads shelves that describe ownership or logistics rather than
# content. They never raise a proposal, but a rule the user adds for one
# (`adso taxonomy map --shelf favorites --to theme:favorites`) still applies.
NON_CATEGORY_SHELVES = frozenset(
    {
        "owned",
        "own",
        "owned books",
        "books i own",
        "my books",
        "kindle",
        "ebook",
        "ebooks",
        "e book",
        "audiobook",
        "audiobooks",
        "audible",
        "library",
        "library books",
        "borrowed",
        "wishlist",
        "wish list",
        "to buy",
        "default",
        "favorites",
        "favourites",
        "re read",
        "reread",
        "on hold",
        "paused",
        "abandoned",
        "recommendations",
        "recommended",
        "book club",
        "bookclub",
    }
)

# Date-stamped shelves ("read-in-2024", "2025-reads", "2023") track when, not what.
_DATED_SHELF_RE = re.compile(r"^(?:read )?(?:in )?(?:19|20)\d\d(?: reads?| books?)?$")


def _is_non_category_shelf(value: str) -> bool:
    return value in NON_CATEGORY_SHELVES or bool(_DATED_SHELF_RE.match(value))


# Open Library subjects that describe the edition, a list the book was on or a
# reading level rather than the book: never evidence for a category or a taste.
_NOISE_SUBJECT_RE = re.compile(
    r"^(?:new york times|nyt |long now manual|open syllabus|reading level|amerikanisches|"
    r"accessible book|protected daisy|in library|lending library|large type|popular print disabled|"
    r"internet archive|staff picks|general$)"
)


def is_noise_subject(term: str) -> bool:
    """True for a normalised subject that says nothing about the book."""
    return bool(_NOISE_SUBJECT_RE.match(term))


MATCH_KINDS = ("shelf", "subject", "tag")
MATCH_KIND_LABELS = {
    "shelf": "Goodreads shelf",
    "subject": "Open Library subject",
    "tag": "tag",
}

# How much each kind of evidence is trusted. The user's own shelves and tags
# are deliberate choices; Open Library subjects are crowd-sourced and noisy.
CONFIDENCE_EXACT = {"shelf": 0.9, "tag": 0.85, "subject": 0.7}
CONFIDENCE_PARTIAL_SHELF = 0.6
CONFIDENCE_NEW_THEME = 0.5
CONFIDENCE_PRIMARY = 0.6

# Facet preference when one term matches categories in several facets.
_FACET_ORDER = {
    facet: index for index, facet in enumerate(("genre", "form", "tradition", "audience", "theme", "era"))
}

_EXAMPLE_TITLES = 3

# Facets that shelves, subjects and tags never map to: era comes from the
# original publication year, not from evidence.
UNMAPPABLE_FACETS = frozenset({"era"})


class CategoryError(AdsoError):
    """A category, rule or suggestion reference that can't be used."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint=hint
            or "Run `adso taxonomy list` to see categories, or `adso taxonomy add` to create one.",
        )


def normalize_term(text: str) -> str:
    """Matching form of a label, alias, shelf, subject or tag.

    "Sci-Fi" / "sci_fi" -> "sci fi"; "Children's" -> "childrens";
    "Mystery & Crime" -> "mystery and crime".
    """
    text = str(text).lower().replace("'", "").replace("’", "").replace("&", " and ")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def _term_variants(term: str) -> list[str]:
    """Open Library phrasings of one idea, tightest first.

    OL subjects often wrap a genre in BISAC-style noise:
    "Fiction, science fiction, general" -> "science fiction".
    """
    variants = [term]
    stripped = term
    if stripped.startswith("fiction ") and stripped != "fiction general":
        stripped = stripped[len("fiction ") :]
    if stripped.endswith(" general"):
        stripped = stripped[: -len(" general")]
    if stripped and stripped not in variants:
        variants.append(stripped)
    return variants


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Category:
    id: int
    facet: str
    slug: str
    label: str
    parent_id: int | None
    position: int


class Taxonomy:
    """An in-memory snapshot of the category tree and its matching vocabulary."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id, facet, slug, label, parent_id, position FROM categories"
        ).fetchall()
        self.by_id: dict[int, Category] = {
            row["id"]: Category(
                row["id"], row["facet"], row["slug"], row["label"], row["parent_id"], row["position"]
            )
            for row in rows
        }
        self.children: dict[int | None, list[int]] = defaultdict(list)
        for category in self.by_id.values():
            parent = category.parent_id if category.parent_id in self.by_id else None
            self.children[parent].append(category.id)
        for ids in self.children.values():
            ids.sort(key=lambda cid: (self.by_id[cid].position, self.by_id[cid].label.lower()))

        # Labels and slugs are "names"; aliases rank below them when resolving.
        self._names: dict[str, set[int]] = defaultdict(set)
        self._aliases: dict[str, set[int]] = defaultdict(set)
        for category in self.by_id.values():
            self._names[normalize_term(category.label)].add(category.id)
            self._names[normalize_term(category.slug)].add(category.id)
        for row in conn.execute("SELECT category_id, alias FROM category_aliases"):
            if row["category_id"] in self.by_id:
                self._aliases[normalize_term(row["alias"])].add(row["category_id"])

    def get(self, category_id: int) -> Category:
        return self.by_id[category_id]

    def lineage(self, category_id: int) -> list[Category]:
        """Root-to-node chain for a category."""
        chain: list[Category] = []
        seen: set[int] = set()
        current: int | None = category_id
        while current is not None and current in self.by_id and current not in seen:
            seen.add(current)
            node = self.by_id[current]
            chain.append(node)
            current = node.parent_id
        return list(reversed(chain))

    def path(self, category_id: int) -> str:
        return " > ".join(node.label for node in self.lineage(category_id))

    def display(self, category_id: int) -> str:
        node = self.by_id[category_id]
        return f"{FACET_LABELS.get(node.facet, node.facet)}: {self.path(category_id)}"

    def depth(self, category_id: int) -> int:
        return len(self.lineage(category_id)) - 1

    def descendants(self, category_id: int) -> list[int]:
        """The category and everything beneath it."""
        out: list[int] = []
        stack = [category_id]
        while stack:
            current = stack.pop()
            if current in out:
                continue
            out.append(current)
            stack.extend(self.children.get(current, []))
        return out

    def is_ancestor(self, ancestor_id: int, category_id: int) -> bool:
        return ancestor_id != category_id and any(
            node.id == ancestor_id for node in self.lineage(category_id)
        )

    def match_term(self, term: str) -> list[int]:
        """Categories a normalised term names exactly, best candidate first."""
        found: set[int] = set()
        for variant in _term_variants(term):
            found = self._names.get(variant, set()) | self._aliases.get(variant, set())
            if found:
                break
        return self._rank(found)

    def _rank(self, ids: set[int]) -> list[int]:
        return sorted(
            ids,
            key=lambda cid: (
                _FACET_ORDER.get(self.by_id[cid].facet, 99),
                -self.depth(cid),
                self.by_id[cid].label.lower(),
            ),
        )

    def resolve(self, text: str, *, facet: str | None = None) -> Category:
        """Find one category from user input.

        Accepts a label, slug or alias ("sci-fi"), a path ("Fantasy > Cozy
        Fantasy", matched as a path suffix), and an optional facet prefix
        ("theme:cozy"). Ambiguity is an error that lists the candidates.
        """
        required = facet
        facet, segments = _parse_reference(text, facet)
        if required is not None and facet != required:
            raise CategoryError(
                f"{text!r} is not a {FACET_LABELS.get(required, required).lower()} category",
                hint=f"Only {required} categories fit here.",
            )
        if not segments:
            raise CategoryError(f"Empty category reference {text!r}")
        candidates = [
            category
            for category in self.by_id.values()
            if (facet is None or category.facet == facet)
            and self._path_matches(category.id, segments, names_only=True)
        ]
        if not candidates:
            candidates = [
                category
                for category in self.by_id.values()
                if (facet is None or category.facet == facet)
                and self._path_matches(category.id, segments, names_only=False)
            ]
        if not candidates:
            raise CategoryError(f"No category matches {text!r}")
        if len(candidates) > 1:
            # A subgenre carrying its parent's alias ("space opera" on Science
            # Fiction and on its Space Opera child): the more specific wins.
            deepest = max(candidates, key=lambda c: self.depth(c.id))
            if all(c.id == deepest.id or self.is_ancestor(c.id, deepest.id) for c in candidates):
                return deepest
        if len(candidates) > 1:
            options = "; ".join(sorted(self.display(c.id) for c in candidates))
            raise CategoryError(
                f"{text!r} matches more than one category: {options}",
                hint="Add a parent (\"Fantasy > Cozy\") or a facet prefix (\"theme:cozy\").",
            )
        return candidates[0]

    def _path_matches(self, category_id: int, segments: list[str], *, names_only: bool) -> bool:
        lineage = self.lineage(category_id)
        if len(segments) > len(lineage):
            return False
        for node, segment in zip(reversed(lineage), reversed(segments)):
            names = {normalize_term(node.label), normalize_term(node.slug)}
            if segment in names:
                continue
            if names_only or node.id not in self._aliases.get(segment, set()):
                return False
        return True


def _parse_reference(text: str, facet: str | None) -> tuple[str | None, list[str]]:
    raw = str(text).strip()
    if ":" in raw:
        prefix, rest = raw.split(":", 1)
        if prefix.strip().lower() in FACETS:
            facet = prefix.strip().lower()
            raw = rest
    segments = [normalize_term(part) for part in raw.split(">")]
    return facet, [segment for segment in segments if segment]


def taxonomy_tree(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every facet with its nodes in tree order and book counts.

    ``books`` counts direct assignments; ``total`` rolls up descendants
    (distinct books), which is what a filter on that category returns.
    """
    taxonomy = Taxonomy(conn)
    direct: dict[int, set[int]] = defaultdict(set)
    for row in conn.execute("SELECT book_id, category_id FROM book_categories"):
        direct[row["category_id"]].add(row["book_id"])

    facets: list[dict[str, Any]] = []
    for facet in FACETS:
        nodes: list[dict[str, Any]] = []

        def walk(parent: int | None, depth: int) -> None:
            for cid in taxonomy.children.get(parent, []):
                node = taxonomy.get(cid)
                if node.facet != facet:
                    continue
                rolled: set[int] = set()
                for descendant in taxonomy.descendants(cid):
                    rolled |= direct.get(descendant, set())
                nodes.append(
                    {
                        "id": cid,
                        "label": node.label,
                        "path": taxonomy.path(cid),
                        "depth": depth,
                        "books": len(direct.get(cid, set())),
                        "total": len(rolled),
                    }
                )
                walk(cid, depth + 1)

        walk(None, 0)
        facets.append({"facet": facet, "label": FACET_LABELS[facet], "categories": nodes})
    return facets


def add_category(conn: sqlite3.Connection, reference: str) -> Category:
    """Create a category from "facet:Label" or "Parent > Child" (facet from parent)."""
    taxonomy = Taxonomy(conn)
    facet, _ = _parse_reference(reference, None)
    raw = str(reference).split(":", 1)[1] if facet and ":" in reference else str(reference)
    parts = [part.strip() for part in raw.split(">") if part.strip()]
    if not parts:
        raise CategoryError(f"Empty category name {reference!r}")
    label = parts[-1]
    parent: Category | None = None
    if len(parts) > 1:
        parent = taxonomy.resolve(" > ".join(parts[:-1]), facet=facet)
        facet = parent.facet
    if facet is None:
        raise CategoryError(
            f"Say which facet {label!r} belongs to",
            hint=f"Prefix it with one of {', '.join(FACETS)}, e.g. \"theme:{label}\", "
            "or give a parent: \"Fantasy > " + label + "\".",
        )
    category_id = _create_category(conn, facet, label, parent.id if parent else None)
    conn.commit()
    return Taxonomy(conn).get(category_id)


def _create_category(conn: sqlite3.Connection, facet: str, label: str, parent_id: int | None) -> int:
    slug = slugify(label)
    if not slug:
        raise CategoryError(f"Category name {label!r} has no letters or digits")
    existing = conn.execute(
        "SELECT id FROM categories WHERE facet = ? AND COALESCE(parent_id, 0) = ? AND slug = ?",
        (facet, parent_id or 0, slug),
    ).fetchone()
    if existing:
        raise CategoryError(f"{Taxonomy(conn).display(existing['id'])} already exists")
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM categories WHERE facet = ? AND COALESCE(parent_id, 0) = ?",
        (facet, parent_id or 0),
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO categories (facet, slug, label, parent_id, position) VALUES (?, ?, ?, ?, ?)",
        (facet, slug, label.strip(), parent_id, position),
    )
    return int(cur.lastrowid)


def rename_category(conn: sqlite3.Connection, reference: str, new_label: str) -> Category:
    """Rename a category; the old name stays as an alias so shelves still match."""
    category = Taxonomy(conn).resolve(reference)
    slug = slugify(new_label)
    if not slug:
        raise CategoryError(f"Category name {new_label!r} has no letters or digits")
    clash = conn.execute(
        "SELECT id FROM categories WHERE facet = ? AND COALESCE(parent_id, 0) = ? AND slug = ? AND id != ?",
        (category.facet, category.parent_id or 0, slug, category.id),
    ).fetchone()
    if clash:
        raise CategoryError(
            f"{Taxonomy(conn).display(clash['id'])} already exists",
            hint="Use `adso taxonomy merge` to fold one category into the other.",
        )
    conn.execute(
        "INSERT OR IGNORE INTO category_aliases (category_id, alias) VALUES (?, ?)",
        (category.id, category.label),
    )
    conn.execute(
        "UPDATE categories SET label = ?, slug = ? WHERE id = ?",
        (new_label.strip(), slug, category.id),
    )
    conn.commit()
    return Taxonomy(conn).get(category.id)


def move_category(conn: sqlite3.Connection, reference: str, under: str | None) -> Category:
    """Re-parent a category within its facet (``under=None`` makes it top-level)."""
    taxonomy = Taxonomy(conn)
    category = taxonomy.resolve(reference)
    parent_id: int | None = None
    if under:
        parent = taxonomy.resolve(under, facet=category.facet)
        if parent.id == category.id or taxonomy.is_ancestor(category.id, parent.id):
            raise CategoryError(f"Can't move {taxonomy.path(category.id)} under itself")
        parent_id = parent.id
    clash = conn.execute(
        "SELECT id FROM categories WHERE facet = ? AND COALESCE(parent_id, 0) = ? AND slug = ? AND id != ?",
        (category.facet, parent_id or 0, category.slug, category.id),
    ).fetchone()
    if clash:
        raise CategoryError(
            f"{taxonomy.display(clash['id'])} already exists there",
            hint="Use `adso taxonomy merge` to fold one category into the other.",
        )
    conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (parent_id, category.id))
    conn.commit()
    return Taxonomy(conn).get(category.id)


def add_alias(conn: sqlite3.Connection, reference: str, alias: str) -> Category:
    category = Taxonomy(conn).resolve(reference)
    if not normalize_term(alias):
        raise CategoryError(f"Alias {alias!r} has no letters or digits")
    conn.execute(
        "INSERT OR IGNORE INTO category_aliases (category_id, alias) VALUES (?, ?)",
        (category.id, alias.strip()),
    )
    conn.commit()
    categorize(conn)  # open suggestions pick up the new match straight away
    return category


def remove_alias(conn: sqlite3.Connection, reference: str, alias: str) -> Category:
    """Stop an alternative name from matching a category.

    Open suggestions that relied on it are re-checked straight away. Rules the
    user already accepted are explicit and stay; remove those from the rules
    list if they're wrong too.
    """
    category = Taxonomy(conn).resolve(reference)
    wanted = normalize_term(alias)
    rows = conn.execute("SELECT alias FROM category_aliases WHERE category_id = ?", (category.id,)).fetchall()
    matches = [row["alias"] for row in rows if normalize_term(row["alias"]) == wanted]
    if not matches:
        raise CategoryError(f"{category.label} has no alias {alias!r}")
    for name in matches:
        conn.execute("DELETE FROM category_aliases WHERE category_id = ? AND alias = ?", (category.id, name))
    conn.commit()
    categorize(conn)
    return category


def category_impact(conn: sqlite3.Connection, category_id: int) -> dict[str, int]:
    """What deleting or merging this category would touch."""
    return {
        "books": conn.execute(
            "SELECT COUNT(*) FROM book_categories WHERE category_id = ?", (category_id,)
        ).fetchone()[0],
        "rules": conn.execute(
            "SELECT COUNT(*) FROM category_rules WHERE category_id = ?", (category_id,)
        ).fetchone()[0],
        "children": conn.execute(
            "SELECT COUNT(*) FROM categories WHERE parent_id = ?", (category_id,)
        ).fetchone()[0],
    }


def delete_category(conn: sqlite3.Connection, reference: str) -> dict[str, Any]:
    """Delete a category. Children move up to its parent; its assignments,
    rules and aliases go with it."""
    taxonomy = Taxonomy(conn)
    category = taxonomy.resolve(reference)
    impact = category_impact(conn, category.id)
    conn.execute(
        "UPDATE categories SET parent_id = ? WHERE parent_id = ?",
        (category.parent_id, category.id),
    )
    _purge_category(conn, category.id)
    _settle_primaries(conn, Taxonomy(conn))
    conn.commit()
    return {"category": taxonomy.display(category.id), **impact}


def _purge_category(conn: sqlite3.Connection, category_id: int) -> None:
    """Delete a category and everything hanging off it.

    Done explicitly rather than through ON DELETE CASCADE, which only fires on
    connections that enabled foreign keys.
    """
    conn.execute(
        "DELETE FROM book_categories WHERE category_id = ? OR rule_id IN "
        "(SELECT id FROM category_rules WHERE category_id = ?)",
        (category_id, category_id),
    )
    conn.execute("DELETE FROM category_rules WHERE category_id = ?", (category_id,))
    conn.execute("DELETE FROM category_aliases WHERE category_id = ?", (category_id,))
    conn.execute("DELETE FROM category_exclusions WHERE category_id = ?", (category_id,))
    # Open questions and accepted mappings about this category go: the rule
    # behind an accepted mapping was just deleted, so its shelf/subject is
    # unmapped again and the next run re-asks with a fresh guess. Rejections
    # stay as history so "don't map this shelf" still holds.
    conn.execute(
        "DELETE FROM category_suggestions WHERE category_id = ? "
        "AND (kind = 'primary' OR status IN ('pending', 'superseded', 'accepted'))",
        (category_id,),
    )
    conn.execute(
        "UPDATE category_suggestions SET category_id = NULL WHERE category_id = ?", (category_id,)
    )
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))


def merge_categories(conn: sqlite3.Connection, source_ref: str, target_ref: str) -> dict[str, Any]:
    """Fold ``source`` into ``target`` (same facet) and delete ``source``.

    Books, rules, exclusions, pending suggestions and children move across; the
    source's name and aliases become aliases of the target, so shelves and
    subjects that matched it keep matching.
    """
    taxonomy = Taxonomy(conn)
    source = taxonomy.resolve(source_ref)
    target = taxonomy.resolve(target_ref, facet=source.facet)
    if source.id == target.id:
        raise CategoryError("Can't merge a category into itself")
    if taxonomy.is_ancestor(source.id, target.id):
        raise CategoryError(
            f"{taxonomy.path(target.id)} sits under {taxonomy.path(source.id)}",
            hint="Move it out first with `adso taxonomy move`, or merge the other way round.",
        )
    impact = category_impact(conn, source.id)
    labels = {"source": taxonomy.display(source.id), "target": taxonomy.display(target.id)}

    # Rules first, so moved assignments can point at the surviving rule.
    rule_map: dict[int, int] = {}
    for rule in conn.execute(
        "SELECT id, match_kind, match_value FROM category_rules WHERE category_id = ?",
        (source.id,),
    ).fetchall():
        twin = conn.execute(
            "SELECT id FROM category_rules WHERE match_kind = ? AND match_value = ? AND category_id = ?",
            (rule["match_kind"], rule["match_value"], target.id),
        ).fetchone()
        if twin:
            rule_map[rule["id"]] = twin["id"]
        else:
            conn.execute(
                "UPDATE category_rules SET category_id = ? WHERE id = ?", (target.id, rule["id"])
            )
            rule_map[rule["id"]] = rule["id"]

    moved = conn.execute(
        "SELECT * FROM book_categories WHERE category_id = ?", (source.id,)
    ).fetchall()
    conn.execute("DELETE FROM book_categories WHERE category_id = ?", (source.id,))
    for row in moved:
        existing = conn.execute(
            "SELECT role FROM book_categories WHERE book_id = ? AND category_id = ?",
            (row["book_id"], target.id),
        ).fetchone()
        if existing:
            if row["role"] == "primary":
                conn.execute(
                    "UPDATE book_categories SET role = 'primary' WHERE book_id = ? AND category_id = ?",
                    (row["book_id"], target.id),
                )
            continue
        conn.execute(
            """
            INSERT INTO book_categories
                (book_id, category_id, role, source, rule_id, evidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["book_id"],
                target.id,
                row["role"],
                row["source"],
                rule_map.get(row["rule_id"]) if row["rule_id"] else None,
                row["evidence"],
                row["created_at"],
            ),
        )
    for old_id, new_id in rule_map.items():
        if old_id != new_id:
            conn.execute("DELETE FROM book_categories WHERE rule_id = ?", (old_id,))
            conn.execute("DELETE FROM category_rules WHERE id = ?", (old_id,))

    conn.execute(
        """
        INSERT OR IGNORE INTO category_exclusions (book_id, category_id, created_at)
        SELECT book_id, ?, created_at FROM category_exclusions WHERE category_id = ?
        """,
        (target.id, source.id),
    )
    conn.execute(
        "UPDATE OR IGNORE category_suggestions SET category_id = ? WHERE category_id = ?",
        (target.id, source.id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO category_aliases (category_id, alias) "
        "SELECT ?, alias FROM category_aliases WHERE category_id = ?",
        (target.id, source.id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO category_aliases (category_id, alias) VALUES (?, ?)",
        (target.id, source.label),
    )
    for child_id in taxonomy.children.get(source.id, []):
        child = taxonomy.get(child_id)
        clash = conn.execute(
            "SELECT id FROM categories WHERE facet = ? AND parent_id = ? AND slug = ?",
            (child.facet, target.id, child.slug),
        ).fetchone()
        if clash:
            # Same-named child on both sides: fold it too.
            conn.commit()
            merge_categories(conn, f"{child.facet}:{taxonomy.path(child_id)}", f"{child.facet}:{taxonomy.path(clash['id'])}")
        else:
            conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (target.id, child_id))
    _purge_category(conn, source.id)
    _settle_primaries(conn, Taxonomy(conn))
    conn.commit()
    return {**labels, **impact}


# ---------------------------------------------------------------------------
# Book assignments
# ---------------------------------------------------------------------------


def book_categories(conn: sqlite3.Connection, book_id: int) -> dict[str, Any]:
    """A book's categories grouped by facet, with provenance, plus its series."""
    taxonomy = Taxonomy(conn)
    rows = conn.execute(
        """
        SELECT bc.category_id, bc.role, bc.source, bc.evidence
        FROM book_categories bc WHERE bc.book_id = ?
        """,
        (book_id,),
    ).fetchall()
    primary: dict[str, Any] | None = None
    by_facet: dict[str, list[dict[str, Any]]] = {facet: [] for facet in FACETS}
    for row in rows:
        if row["category_id"] not in taxonomy.by_id:
            continue
        node = taxonomy.get(row["category_id"])
        entry = {
            "id": node.id,
            "facet": node.facet,
            "label": node.label,
            "path": taxonomy.path(node.id),
            "role": row["role"],
            "source": row["source"],
            "evidence": row["evidence"],
        }
        if row["role"] == "primary":
            primary = entry
        by_facet.setdefault(node.facet, []).append(entry)
    for entries in by_facet.values():
        entries.sort(key=lambda e: (e["role"] != "primary", e["path"].lower()))
    return {"primary": primary, "by_facet": by_facet, "series": book_series(conn, book_id)}


def _require_book(conn: sqlite3.Connection, book_id: int) -> None:
    if conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone() is None:
        raise CategoryError(f"No book with id {book_id}", hint="Run `adso list` to find books.")


def add_book_category(conn: sqlite3.Connection, book_id: int, reference: str) -> Category:
    """Assign a category by hand. A user assignment is never removed by a run."""
    _require_book(conn, book_id)
    category = Taxonomy(conn).resolve(reference)
    _upsert_user_assignment(conn, book_id, category.id, role=None)
    _settle_primaries(conn, Taxonomy(conn), book_ids={book_id})
    conn.commit()
    return category


def set_primary_genre(conn: sqlite3.Connection, book_id: int, reference: str) -> Category:
    _require_book(conn, book_id)
    category = Taxonomy(conn).resolve(reference, facet="genre")
    _set_primary(conn, book_id, category.id)
    conn.commit()
    return category


def _set_primary(conn: sqlite3.Connection, book_id: int, category_id: int) -> None:
    conn.execute(
        "UPDATE book_categories SET role = 'secondary' WHERE book_id = ? AND role = 'primary'",
        (book_id,),
    )
    _upsert_user_assignment(conn, book_id, category_id, role="primary")
    # Any open primary-genre question for this book is now answered.
    conn.execute(
        """
        UPDATE category_suggestions
        SET status = 'superseded', decided_at = CURRENT_TIMESTAMP
        WHERE kind = 'primary' AND book_id = ? AND status = 'pending' AND category_id != ?
        """,
        (book_id, category_id),
    )


def _upsert_user_assignment(
    conn: sqlite3.Connection, book_id: int, category_id: int, *, role: str | None
) -> None:
    conn.execute(
        "DELETE FROM category_exclusions WHERE book_id = ? AND category_id = ?",
        (book_id, category_id),
    )
    existing = conn.execute(
        "SELECT role FROM book_categories WHERE book_id = ? AND category_id = ?",
        (book_id, category_id),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE book_categories SET source = 'user', rule_id = NULL, role = ?
            WHERE book_id = ? AND category_id = ?
            """,
            (role or existing["role"], book_id, category_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO book_categories (book_id, category_id, role, source)
            VALUES (?, ?, ?, 'user')
            """,
            (book_id, category_id, role or "secondary"),
        )


def remove_book_category(conn: sqlite3.Connection, book_id: int, reference: str) -> Category:
    """Take a category off a book and remember it, so no rule re-adds it."""
    _require_book(conn, book_id)
    category = Taxonomy(conn).resolve(reference)
    conn.execute(
        "DELETE FROM book_categories WHERE book_id = ? AND category_id = ?",
        (book_id, category.id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO category_exclusions (book_id, category_id) VALUES (?, ?)",
        (book_id, category.id),
    )
    _settle_primaries(conn, Taxonomy(conn), book_ids={book_id})
    conn.commit()
    return category


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

# Goodreads appends series as a trailing parenthetical: "(The Expanse, #1)",
# "(Discworld, #1; Rincewind #1)" (first series wins), "(Dune #1.5)".
_SERIES_PAREN_RE = re.compile(r"\(([^()]*#\s*\d[^()]*)\)\s*$")
_SERIES_ENTRY_RE = re.compile(r"^(.*?),?\s*#\s*(\d+(?:\.\d+)?)")


def parse_series(title: str | None) -> tuple[str, float] | None:
    """Series name and position from a Goodreads title, if it carries one."""
    if not title:
        return None
    match = _SERIES_PAREN_RE.search(title)
    if not match:
        return None
    first = match.group(1).split(";")[0].strip()
    entry = _SERIES_ENTRY_RE.match(first)
    if not entry:
        return None
    name = entry.group(1).strip().rstrip(",").strip()
    if not name:
        return None
    return name, float(entry.group(2))


def format_position(position: float | None) -> str:
    if position is None:
        return ""
    return f"#{int(position)}" if float(position).is_integer() else f"#{position:g}"


def book_series(conn: sqlite3.Connection, book_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT s.name, bs.position, bs.source FROM book_series bs
        JOIN series s ON s.id = bs.series_id WHERE bs.book_id = ?
        """,
        (book_id,),
    ).fetchone()
    if row is None:
        return None
    return {"name": row["name"], "position": row["position"], "source": row["source"]}


def set_book_series(
    conn: sqlite3.Connection, book_id: int, name: str | None, position: float | None = None
) -> None:
    """Set or clear a book's series by hand; title parsing never overrides it.

    Clearing stores a NULL series so a later run doesn't re-parse the title.
    """
    _require_book(conn, book_id)
    series_id = _series_id(conn, name) if name and name.strip() else None
    conn.execute(
        """
        INSERT INTO book_series (book_id, series_id, position, source) VALUES (?, ?, ?, 'user')
        ON CONFLICT(book_id) DO UPDATE SET
            series_id = excluded.series_id, position = excluded.position, source = 'user'
        """,
        (book_id, series_id, position if series_id else None),
    )
    _drop_empty_series(conn)
    conn.commit()


def _series_id(conn: sqlite3.Connection, name: str) -> int:
    name = " ".join(name.split())
    conn.execute("INSERT OR IGNORE INTO series (name) VALUES (?)", (name,))
    return int(conn.execute("SELECT id FROM series WHERE name = ?", (name,)).fetchone()[0])


def _drop_empty_series(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM series WHERE id NOT IN "
        "(SELECT series_id FROM book_series WHERE series_id IS NOT NULL)"
    )


def _apply_series(conn: sqlite3.Connection, books: list[sqlite3.Row]) -> int:
    """Parse series from titles for books the user hasn't set by hand."""
    current = {
        row["book_id"]: row
        for row in conn.execute("SELECT book_id, series_id, position, source FROM book_series")
    }
    changed = 0
    for book in books:
        existing = current.get(book["id"])
        if existing is not None and existing["source"] == "user":
            continue
        parsed = parse_series(book["title"])
        if parsed is None:
            if existing is not None:
                conn.execute("DELETE FROM book_series WHERE book_id = ?", (book["id"],))
                changed += 1
            continue
        name, position = parsed
        series_id = _series_id(conn, name)
        if existing is not None and existing["series_id"] == series_id and existing["position"] == position:
            continue
        conn.execute(
            """
            INSERT INTO book_series (book_id, series_id, position, source) VALUES (?, ?, ?, 'title')
            ON CONFLICT(book_id) DO UPDATE SET
                series_id = excluded.series_id, position = excluded.position, source = 'title'
            """,
            (book["id"], series_id, position),
        )
        changed += 1
    _drop_empty_series(conn)
    return changed


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _tag_target(reference: str) -> str | None:
    """The tag a rule reference names ("tag:comfort reads"), or None for a category."""
    raw = str(reference).strip()
    if raw.lower().startswith("tag:"):
        tag = " ".join(raw[4:].strip().lstrip("#").lower().split())
        if not tag:
            raise CategoryError("Empty tag name")
        return tag
    return None


def list_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    taxonomy = Taxonomy(conn)
    rows = conn.execute(
        """
        SELECT r.id, r.match_kind, r.match_value, r.category_id, r.tag, r.created_by,
               CASE WHEN r.tag IS NULL
                    THEN (SELECT COUNT(*) FROM book_categories bc WHERE bc.rule_id = r.id)
                    ELSE (SELECT COUNT(*) FROM tag_rule_applications ta WHERE ta.rule_id = r.id)
               END AS books
        FROM category_rules r ORDER BY r.match_kind, r.match_value, r.id
        """
    ).fetchall()
    return [
        {
            "id": row["id"],
            "match_kind": row["match_kind"],
            "match_label": MATCH_KIND_LABELS.get(row["match_kind"], row["match_kind"]),
            "match_value": row["match_value"],
            "category": f"Tag: #{row['tag']}" if row["tag"] else taxonomy.display(row["category_id"]),
            "tag": row["tag"],
            "books": row["books"],
            "created_by": row["created_by"],
        }
        for row in rows
        if row["tag"] or row["category_id"] in taxonomy.by_id
    ]


def add_rule(
    conn: sqlite3.Connection,
    match_kind: str,
    match_value: str,
    reference: str,
    *,
    actor: str = "cli",
) -> dict[str, Any]:
    """Map a shelf/subject/tag to a category (or to a tag, "tag:name") across the library."""
    if match_kind not in MATCH_KINDS:
        raise CategoryError(f"Unknown match kind {match_kind!r}; expected one of {', '.join(MATCH_KINDS)}")
    value = normalize_term(match_value)
    if not value:
        raise CategoryError(f"Nothing to match in {match_value!r}")
    tag = _tag_target(reference)
    category_id = None if tag else _mapping_target(Taxonomy(conn), reference).id
    rule_id = _insert_rule(conn, match_kind, value, category_id, tag=tag, actor=actor)
    # A manual mapping answers any open proposal for the same value.
    conn.execute(
        """
        UPDATE category_suggestions
        SET status = 'accepted', category_id = ?, decided_at = CURRENT_TIMESTAMP, decided_by = ?
        WHERE kind = 'map' AND match_kind = ? AND match_value = ? AND status = 'pending'
        """,
        (category_id, actor, match_kind, value),
    )
    conn.commit()
    result = categorize(conn)
    rule = next(r for r in list_rules(conn) if r["id"] == rule_id)
    return {"rule_id": rule_id, "category": rule["category"], "books": rule["books"], "run": result}


def _mapping_target(taxonomy: Taxonomy, reference: str) -> Category:
    """The category a shelf, subject or tag may be mapped to."""
    return _require_mappable(taxonomy, taxonomy.resolve(reference))


def _require_mappable(taxonomy: Taxonomy, category: Category) -> Category:
    if category.facet in UNMAPPABLE_FACETS:
        raise CategoryError(
            f"{taxonomy.display(category.id)} comes from each book's publication year, so nothing maps to it",
            hint="Map to a genre, tradition, theme, form or tag; set an era on one book with `adso edit`.",
        )
    return category


def _insert_rule(
    conn: sqlite3.Connection,
    match_kind: str,
    value: str,
    category_id: int | None,
    *,
    tag: str | None = None,
    actor: str,
) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO category_rules (match_kind, match_value, category_id, tag, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        (match_kind, value, category_id, tag, actor),
    )
    return int(
        conn.execute(
            """
            SELECT id FROM category_rules WHERE match_kind = ? AND match_value = ?
                AND COALESCE(category_id, 0) = ? AND COALESCE(tag, '') = ?
            """,
            (match_kind, value, category_id or 0, tag or ""),
        ).fetchone()[0]
    )


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any]:
    """Remove a rule and the category assignments it made.

    User-confirmed categories stay, and so do tags a tag rule added: once on a
    book, a tag is the user's.
    """
    rules = {rule["id"]: rule for rule in list_rules(conn)}
    if rule_id not in rules:
        raise CategoryError(f"No rule with id {rule_id}", hint="Run `adso taxonomy rules` to list them.")
    conn.execute("DELETE FROM book_categories WHERE rule_id = ?", (rule_id,))
    conn.execute("DELETE FROM tag_rule_applications WHERE rule_id = ?", (rule_id,))
    conn.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,))
    _settle_primaries(conn, Taxonomy(conn))
    conn.commit()
    return rules[rule_id]


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _book_signals(book: sqlite3.Row) -> dict[tuple[str, str], str]:
    """(match_kind, normalised value) -> original text, for one book."""
    signals: dict[tuple[str, str], str] = {}
    exclusive = normalize_term(book["exclusive_shelf"] or "")
    for shelf in json.loads(book["shelves_json"] or "[]"):
        value = normalize_term(shelf)
        if not value or shelf in STATUS_SHELVES or value == exclusive or value in DNF_SHELF_TERMS:
            continue
        signals.setdefault(("shelf", value), shelf)
    for subject in json.loads(book["subjects_json"] or "[]"):
        value = normalize_term(subject)
        if value and not is_noise_subject(value):
            signals.setdefault(("subject", value), subject)
    for tag in json.loads(book["tags_json"] or "[]"):
        value = normalize_term(tag)
        if value:
            signals.setdefault(("tag", value), tag)
    return signals


# Top-level seed genres that are nonfiction (older seed slugs included, for
# catalogues that kept them). An Open Library subject alone
# can't put a novel in one of these (OL tags novels "history", "war",
# "biography"...): that becomes a question instead. Keyed by slug so a
# renamed category keeps its meaning only if the user kept the slug's words.
NONFICTION_GENRE_ROOTS = frozenset(
    {
        "history",
        "biography-and-memoir",
        "philosophy",
        "religion-and-mythology",
        "religion-and-spirituality",
        "psychology",
        "politics-and-society",
        "economics-and-business",
        "science-and-nature",
        "science",
        "technology",
        "art-architecture-and-design",
        "art-and-design",
        "writing-and-literature",
        "travel-and-place",
        "travel",
        "self-help",
        "food-and-cooking",
    }
)


def _nonfiction_genres(taxonomy: Taxonomy) -> set[int]:
    return {
        cid
        for cid, node in taxonomy.by_id.items()
        if node.facet == "genre" and taxonomy.lineage(cid)[0].slug in NONFICTION_GENRE_ROOTS
    }


def _form_id(taxonomy: Taxonomy, slug: str) -> int | None:
    return next((c.id for c in taxonomy.by_id.values() if c.facet == "form" and c.slug == slug), None)


def _era_for_year(taxonomy: Taxonomy, year: int | None) -> int | None:
    if year is None:
        return None
    for category in taxonomy.by_id.values():
        if category.facet != "era" or category.slug not in ERA_YEARS:
            continue
        low, high = ERA_YEARS[category.slug]
        if (low is None or year >= low) and (high is None or year <= high):
            return category.id
    return None


def _apply_era(
    conn: sqlite3.Connection,
    taxonomy: Taxonomy,
    book: sqlite3.Row,
    existing: dict[int, sqlite3.Row],
    exclusions: set[tuple[int, int]],
) -> tuple[int, int]:
    """Keep a book's era in line with its original publication year.

    Year-made rows follow the year; an era the user chose wins, and one they
    removed isn't put back. Returns (added, removed).
    """
    eras = {cid: row for cid, row in existing.items() if taxonomy.by_id[cid].facet == "era"}
    wanted = _era_for_year(taxonomy, book["original_publication_year"])
    if (wanted is not None and (book["id"], wanted) in exclusions) or any(
        row["source"] != "year" for row in eras.values()
    ):
        wanted = None
    eras = {cid: row for cid, row in eras.items() if row["source"] == "year"}
    removed = 0
    for category_id in eras:
        if category_id != wanted:
            conn.execute(
                "DELETE FROM book_categories WHERE book_id = ? AND category_id = ?", (book["id"], category_id)
            )
            removed += 1
    if wanted is None or wanted in eras:
        return 0, removed
    conn.execute(
        """
        INSERT INTO book_categories (book_id, category_id, role, source, evidence)
        VALUES (?, ?, 'secondary', 'year', ?)
        """,
        (book["id"], wanted, f"First published {book['original_publication_year']}"),
    )
    return 1, removed


def categorize(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict[str, int]:
    """Apply accepted rules, parse series, settle primaries, raise new proposals.

    Safe to run any time: it only ever writes rule-sourced assignments, tags
    from tag rules (once per book), title series and pending suggestions.
    With ``dry_run`` everything is computed and counted, then rolled back.
    """
    conn.commit()  # never roll back someone else's pending work on dry_run
    taxonomy = Taxonomy(conn)
    books = conn.execute(
        "SELECT id, title, date_added, exclusive_shelf, shelves_json, subjects_json, tags_json, "
        "original_publication_year FROM books"
    ).fetchall()

    category_rules: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    tag_rules: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for rule in conn.execute("SELECT id, match_kind, match_value, category_id, tag FROM category_rules"):
        key = (rule["match_kind"], rule["match_value"])
        if rule["tag"]:
            tag_rules[key].append((rule["id"], rule["tag"]))
        elif (
            rule["category_id"] in taxonomy.by_id
            and taxonomy.get(rule["category_id"]).facet not in UNMAPPABLE_FACETS
        ):
            category_rules[key].append((rule["id"], rule["category_id"]))
    applied = {(row[0], row[1]) for row in conn.execute("SELECT rule_id, book_id FROM tag_rule_applications")}
    exclusions = {
        (row["book_id"], row["category_id"])
        for row in conn.execute("SELECT book_id, category_id FROM category_exclusions")
    }
    current: dict[int, dict[int, sqlite3.Row]] = defaultdict(dict)
    for row in conn.execute("SELECT book_id, category_id, source, rule_id FROM book_categories"):
        current[row["book_id"]][row["category_id"]] = row
    nonfiction = _nonfiction_genres(taxonomy)
    fiction_id = _form_id(taxonomy, "fiction")
    nonfiction_form_id = _form_id(taxonomy, "nonfiction")

    series_changed = _apply_series(conn, books)

    assigned = removed = tagged = 0
    unmatched: dict[tuple[str, str], dict[str, Any]] = {}
    questions: dict[tuple[int, int], str] = {}
    for book in books:
        desired: dict[int, tuple[int, str]] = {}
        kinds: dict[int, set[str]] = defaultdict(set)
        new_tags: list[str] = []
        for (kind, value), original in _book_signals(book).items():
            matched = category_rules.get((kind, value), [])
            tagging = tag_rules.get((kind, value), [])
            if not matched and not tagging:
                slot = unmatched.setdefault((kind, value), {"original": original, "books": []})
                slot["books"].append((book["date_added"] or "", book["title"]))
                continue
            for rule_id, category_id in matched:
                if (book["id"], category_id) in exclusions:
                    continue
                desired.setdefault(category_id, (rule_id, f"{MATCH_KIND_LABELS[kind]}: {original}"))
                kinds[category_id].add(kind)
            for rule_id, tag in tagging:
                if (rule_id, book["id"]) not in applied:
                    new_tags.append(tag)
                    conn.execute(
                        "INSERT OR IGNORE INTO tag_rule_applications (rule_id, book_id) VALUES (?, ?)",
                        (rule_id, book["id"]),
                    )

        existing = {cid: row for cid, row in current.get(book["id"], {}).items() if cid in taxonomy.by_id}
        era_added, era_removed = _apply_era(conn, taxonomy, book, existing, exclusions)
        assigned += era_added
        removed += era_removed
        # A novel isn't History just because Open Library says "history": a
        # nonfiction genre that only subjects support becomes a question.
        chosen = set(desired) | {cid for cid, row in existing.items() if row["source"] != "rule"}
        if fiction_id in chosen and nonfiction_form_id not in chosen:
            for category_id in [c for c in desired if c in nonfiction and kinds[c] <= {"subject"}]:
                if category_id not in existing or existing[category_id]["source"] == "rule":
                    questions[(book["id"], category_id)] = desired[category_id][1]
                    del desired[category_id]

        if new_tags:
            tags = json.loads(book["tags_json"] or "[]")
            merged = tags + [t for t in dict.fromkeys(new_tags) if t not in tags]
            if merged != tags:
                conn.execute(
                    "UPDATE books SET tags_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (json.dumps(merged), book["id"]),
                )
                tagged += len(merged) - len(tags)

        for category_id, row in existing.items():
            if row["source"] == "rule" and category_id not in desired:
                conn.execute(
                    "DELETE FROM book_categories WHERE book_id = ? AND category_id = ?",
                    (book["id"], category_id),
                )
                removed += 1
        for category_id, (rule_id, evidence) in desired.items():
            row = existing.get(category_id)
            if row is None:
                conn.execute(
                    """
                    INSERT INTO book_categories (book_id, category_id, role, source, rule_id, evidence)
                    VALUES (?, ?, 'secondary', 'rule', ?, ?)
                    """,
                    (book["id"], category_id, rule_id, evidence),
                )
                assigned += 1
            elif row["source"] == "derived" or (row["source"] == "rule" and row["rule_id"] != rule_id):
                # A derived default that now has evidence of its own becomes a
                # rule assignment (and keeps its role).
                conn.execute(
                    "UPDATE book_categories SET source = 'rule', rule_id = ?, evidence = ? "
                    "WHERE book_id = ? AND category_id = ?",
                    (rule_id, evidence, book["id"], category_id),
                )

    fiction_questions = _raise_fiction_questions(conn, questions)
    primaries_set, primaries_suggested = _settle_primaries(conn, taxonomy)
    proposals = _propose_mappings(conn, taxonomy, unmatched)

    pending = conn.execute(
        "SELECT COUNT(*) FROM category_suggestions WHERE status = 'pending'"
    ).fetchone()[0]
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return {
        "books": len(books),
        "assigned": assigned,
        "removed": removed,
        "tagged": tagged,
        "series": series_changed,
        "primaries_set": primaries_set,
        "primaries_suggested": primaries_suggested + fiction_questions,
        "new_proposals": proposals,
        "pending": pending,
    }


def _raise_fiction_questions(conn: sqlite3.Connection, questions: dict[tuple[int, int], str]) -> int:
    """Ask about nonfiction genres that only Open Library puts on a novel.

    One 'assign' suggestion per book, grouped per genre on the review card.
    Decided ones stay decided; ones whose situation changed are withdrawn.
    """
    existing = {
        (row["book_id"], row["category_id"]): row
        for row in conn.execute(
            "SELECT id, book_id, category_id, status FROM category_suggestions "
            "WHERE kind = 'assign' AND proposed_by = 'adso'"
        )
    }
    raised = 0
    for (book_id, category_id), evidence in questions.items():
        row = existing.get((book_id, category_id))
        if row is not None:
            if row["status"] == "superseded":
                conn.execute(
                    "UPDATE category_suggestions SET status = 'pending', decided_at = NULL WHERE id = ?",
                    (row["id"],),
                )
            continue
        subject = evidence.split(": ", 1)[-1]
        conn.execute(
            """
            INSERT OR IGNORE INTO category_suggestions
                (kind, book_id, category_id, match_kind, match_value, confidence, book_count, evidence, proposed_by)
            VALUES ('assign', ?, ?, 'subject', ?, 0.4, 1, ?, 'adso')
            """,
            (book_id, category_id, normalize_term(subject),
             f"Open Library files this novel under “{subject}”", ),
        )
        raised += 1
    for key, row in existing.items():
        if row["status"] == "pending" and key not in questions:
            conn.execute(
                "UPDATE category_suggestions SET status = 'superseded', decided_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
    return raised


def _settle_primaries(
    conn: sqlite3.Connection, taxonomy: Taxonomy, *, book_ids: set[int] | None = None
) -> tuple[int, int]:
    """Keep each book's primary genre in line with its genres.

    A primary the user set or confirmed is final. One picked automatically
    (source 'rule' or 'derived') is provisional and re-decided on every run:

    1. One most-specific genre -> it is primary (so a provisional "Science
       Fiction" moves down to a newly mapped "Space Opera").
    2. Several -> the user's own evidence wins: if exactly one competing genre
       comes from their shelves, tags or hand edits (rather than Open Library
       subjects), it is primary.
    3. Otherwise, if the competitors share a genre ancestor, the deepest one is
       primary ("Cyberpunk" + "Space Opera" -> "Science Fiction"). When the book
       doesn't carry that ancestor, a 'derived' row is added for it, owned by
       this function and removed as soon as it stops being the answer.
    4. Otherwise a 'primary' question goes to review.

    Rejected candidates are never picked or proposed again for that book, and
    categories the user removed from a book are never derived back onto it.
    """
    genres: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT bc.book_id, bc.category_id, bc.role, bc.source, r.match_kind
        FROM book_categories bc
        JOIN categories c ON c.id = bc.category_id
        LEFT JOIN category_rules r ON r.id = bc.rule_id
        WHERE c.facet = 'genre'
        """
    ):
        if book_ids is None or row["book_id"] in book_ids:
            genres[row["book_id"]].append(row)

    decided: dict[int, dict[int, str]] = defaultdict(dict)
    for row in conn.execute(
        "SELECT book_id, category_id, status FROM category_suggestions WHERE kind = 'primary'"
    ):
        decided[row["book_id"]][row["category_id"]] = row["status"]
    exclusions = {
        (row["book_id"], row["category_id"])
        for row in conn.execute("SELECT book_id, category_id FROM category_exclusions")
    }
    popularity: dict[int, int] = defaultdict(int)
    for row in conn.execute("SELECT category_id, COUNT(*) AS n FROM book_categories GROUP BY category_id"):
        popularity[row["category_id"]] = row["n"]

    auto = suggested = 0
    for book_id, rows in genres.items():
        primary = next((row for row in rows if row["role"] == "primary"), None)
        derived = {row["category_id"] for row in rows if row["source"] == "derived"}
        if primary is not None and primary["source"] not in ("rule", "derived"):
            _drop_derived(conn, book_id, derived)
            continue
        real = [row for row in rows if row["source"] != "derived"]
        ids = {row["category_id"] for row in real}
        states = decided.get(book_id, {})
        current = primary["category_id"] if primary is not None else None
        if current is not None and states.get(current) == "rejected":
            conn.execute(
                "UPDATE book_categories SET role = 'secondary' WHERE book_id = ? AND category_id = ?",
                (book_id, current),
            )
            current = None
        leaves = [cid for cid in ids if not any(taxonomy.is_ancestor(cid, other) for other in ids)]
        candidates = [cid for cid in leaves if states.get(cid) != "rejected"]
        if not candidates:
            _drop_derived(conn, book_id, derived - {current})
            if current is not None:
                _close_primary_questions(conn, book_id)
            continue

        mine = [
            cid
            for cid in candidates
            if any(
                row["category_id"] == cid and (row["source"] == "user" or row["match_kind"] in ("shelf", "tag"))
                for row in real
            )
        ]
        pick: int | None = None
        basis: list[int] = []
        if len(candidates) == 1:
            pick = candidates[0]
        elif len(mine) == 1:
            pick = mine[0]
        else:
            basis = mine if len(mine) >= 2 else candidates
            shared = _deepest_shared_ancestor(taxonomy, basis)
            if shared is not None and states.get(shared) != "rejected" and (book_id, shared) not in exclusions:
                pick = shared

        if pick is not None:
            if pick not in ids:
                evidence = "shared parent of " + ", ".join(sorted(taxonomy.get(c).label for c in basis))
                conn.execute(
                    "UPDATE book_categories SET role = 'secondary' WHERE book_id = ? AND role = 'primary'",
                    (book_id,),
                )
                conn.execute(
                    """
                    INSERT INTO book_categories (book_id, category_id, role, source, evidence)
                    VALUES (?, ?, 'primary', 'derived', ?)
                    ON CONFLICT(book_id, category_id) DO UPDATE SET role = 'primary', evidence = excluded.evidence
                    """,
                    (book_id, pick, evidence),
                )
            elif current != pick:
                _promote(conn, book_id, pick)
            _drop_derived(conn, book_id, derived - {pick})
            if current != pick:
                auto += 1
            _close_primary_questions(conn, book_id)
            continue

        # Genuinely different genres: make sure exactly one live question is open.
        _drop_derived(conn, book_id, derived)
        pending = [cid for cid, status in states.items() if status == "pending"]
        for cid in pending:
            if cid not in candidates:
                conn.execute(
                    """
                    UPDATE category_suggestions SET status = 'superseded', decided_at = CURRENT_TIMESTAMP
                    WHERE kind = 'primary' AND book_id = ? AND category_id = ?
                    """,
                    (book_id, cid),
                )
        if any(cid in candidates for cid in pending):
            continue
        if current in candidates:
            best = current
        else:
            best = sorted(
                candidates,
                key=lambda cid: (
                    cid not in mine,
                    -taxonomy.depth(cid),
                    -popularity.get(cid, 0),
                    taxonomy.get(cid).label.lower(),
                ),
            )[0]
        others = [taxonomy.path(cid) for cid in sorted(candidates, key=taxonomy.path) if cid != best]
        evidence = "Also: " + "; ".join(others) if others else None
        conn.execute(
            """
            INSERT INTO category_suggestions (kind, book_id, category_id, confidence, book_count, evidence)
            VALUES ('primary', ?, ?, ?, 1, ?)
            ON CONFLICT(book_id, category_id) WHERE kind = 'primary' DO UPDATE SET
                status = 'pending', evidence = excluded.evidence, decided_at = NULL, decided_by = NULL
            """,
            (book_id, best, CONFIDENCE_PRIMARY, evidence),
        )
        suggested += 1
    return auto, suggested


def _deepest_shared_ancestor(taxonomy: Taxonomy, category_ids: list[int]) -> int | None:
    """The deepest genre all of these sit under (never one of them), if any."""
    lineages = [[node.id for node in taxonomy.lineage(cid)] for cid in category_ids]
    shared: int | None = None
    for level in zip(*lineages):
        if len(set(level)) != 1:
            break
        shared = level[0]
    if shared is None or shared in category_ids:
        return None
    return shared


def _drop_derived(conn: sqlite3.Connection, book_id: int, category_ids: set[int]) -> None:
    for category_id in category_ids:
        conn.execute(
            "DELETE FROM book_categories WHERE book_id = ? AND category_id = ? AND source = 'derived'",
            (book_id, category_id),
        )


def _promote(conn: sqlite3.Connection, book_id: int, category_id: int) -> None:
    """Make an existing assignment the (provisional) primary."""
    conn.execute(
        "UPDATE book_categories SET role = 'secondary' WHERE book_id = ? AND role = 'primary'",
        (book_id,),
    )
    conn.execute(
        "UPDATE book_categories SET role = 'primary' WHERE book_id = ? AND category_id = ?",
        (book_id, category_id),
    )


def _close_primary_questions(conn: sqlite3.Connection, book_id: int) -> None:
    conn.execute(
        """
        UPDATE category_suggestions SET status = 'superseded', decided_at = CURRENT_TIMESTAMP
        WHERE kind = 'primary' AND book_id = ? AND status = 'pending'
        """,
        (book_id,),
    )


def _guess_category(
    taxonomy: Taxonomy, kind: str, value: str
) -> tuple[int | None, float, str | None] | None:
    """Best category for an unmatched value: (category_id, confidence, new_label).

    ``None`` means "don't propose anything". A ``None`` category with a label
    means "propose tagging the books with the user's own shelf name".
    """
    exact = [cid for cid in taxonomy.match_term(value) if taxonomy.get(cid).facet not in UNMAPPABLE_FACETS]
    if exact:
        return exact[0], CONFIDENCE_EXACT[kind], None
    if kind != "shelf":
        return None  # subjects and tags only raise proposals on a clean match
    if _is_non_category_shelf(value):
        return None
    tokens = value.split()
    partial: set[int] = set()
    for size in (2, 1):
        for start in range(len(tokens) - size + 1):
            partial.update(
                cid
                for cid in taxonomy.match_term(" ".join(tokens[start : start + size]))
                if taxonomy.get(cid).facet not in UNMAPPABLE_FACETS
            )
        if partial:
            break
    ranked = taxonomy._rank(partial)
    if len(ranked) == 1:
        return ranked[0], CONFIDENCE_PARTIAL_SHELF, None
    # Not a genre: the user's own shelf becomes one of their tags.
    return None, CONFIDENCE_NEW_THEME, value


def _propose_mappings(
    conn: sqlite3.Connection, taxonomy: Taxonomy, unmatched: dict[tuple[str, str], dict[str, Any]]
) -> int:
    """Turn unmatched evidence into (at most one) mapping proposal per value.

    Open proposals are re-checked against the current taxonomy each run, so
    renaming, aliasing or removing an alias updates or withdraws them.
    """
    existing = {
        (row["match_kind"], row["match_value"]): row
        for row in conn.execute(
            "SELECT id, match_kind, match_value, status, category_id, proposed_facet, proposed_label "
            "FROM category_suggestions WHERE kind = 'map'"
        )
    }
    created = 0
    for (kind, value), info in unmatched.items():
        # Most recently added first: the books the user is likeliest to recognise.
        recent = sorted(info["books"], key=lambda b: (b[0], b[1]), reverse=True)
        examples = "e.g. " + "; ".join(title for _, title in recent[:_EXAMPLE_TITLES])
        guess = _guess_category(taxonomy, kind, value)
        target: tuple[Any, ...] = (None, None, None, 0.0)
        if guess is not None:
            category_id, confidence, new_label = guess
            if new_label is not None:
                # The user's own shelf name, as Goodreads spells it, becomes a tag.
                target = (None, "tag", " ".join(str(info["original"]).lower().split()), confidence)
            else:
                target = (category_id, None, None, confidence)
        row = existing.get((kind, value))
        if row is not None:
            if row["status"] not in ("pending", "superseded"):
                continue
            if guess is None:
                if row["status"] == "pending":
                    conn.execute(
                        "UPDATE category_suggestions SET status = 'superseded', decided_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (row["id"],),
                    )
                continue
            # Superseded means "the evidence or match went away"; it's back, so ask again.
            conn.execute(
                """
                UPDATE category_suggestions
                SET book_count = ?, evidence = ?, status = 'pending', decided_at = NULL,
                    category_id = ?, proposed_facet = ?, proposed_label = ?, confidence = ?
                WHERE id = ?
                """,
                (len(info["books"]), examples, *target, row["id"]),
            )
            continue
        if guess is None:
            continue
        conn.execute(
            """
            INSERT INTO category_suggestions
                (kind, match_kind, match_value, category_id, proposed_facet, proposed_label,
                 confidence, book_count, evidence, proposed_by)
            VALUES ('map', ?, ?, ?, ?, ?, ?, ?, ?, 'adso')
            """,
            (kind, value, *target, len(info["books"]), examples),
        )
        created += 1
    # A pending mapping whose evidence has vanished from the library (shelf
    # deleted on Goodreads, or now covered by a manual rule) is no longer a question.
    live = set(unmatched)
    for key, row in existing.items():
        if row["status"] == "pending" and key not in live:
            conn.execute(
                "UPDATE category_suggestions SET status = 'superseded', decided_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (row["id"],),
            )
    return created


# ---------------------------------------------------------------------------
# Batch lookups (listing pages, API, MCP, exports)
# ---------------------------------------------------------------------------


def book_category_map(conn: sqlite3.Connection) -> dict[int, dict[int, str]]:
    """book id -> {category id: role} for every assignment."""
    out: dict[int, dict[int, str]] = defaultdict(dict)
    for row in conn.execute("SELECT book_id, category_id, role FROM book_categories"):
        out[row["book_id"]][row["category_id"]] = row["role"]
    return out


def book_series_map(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """book id -> {"name", "position"} for books in a series."""
    return {
        row["book_id"]: {"name": row["name"], "position": row["position"]}
        for row in conn.execute(
            "SELECT bs.book_id, s.name, bs.position FROM book_series bs JOIN series s ON s.id = bs.series_id"
        )
    }


def attach_categories(conn: sqlite3.Connection, books: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``primary_genre``, ``categories`` and ``series`` to catalogue records.

    ``categories`` maps each facet to the category paths the book carries
    (primary genre first); ``series`` is ``{"name", "position"}`` or None.
    Records need their internal ``id``. Mutates and returns ``books``.
    """
    taxonomy = Taxonomy(conn)
    assignments = book_category_map(conn)
    series = book_series_map(conn)
    for book in books:
        by_facet: dict[str, list[str]] = {}
        primary = None
        roles = assignments.get(book.get("id"), {})
        for category_id, role in sorted(
            roles.items(), key=lambda kv: (kv[1] != "primary", taxonomy.path(kv[0]).lower())
        ):
            if category_id not in taxonomy.by_id:
                continue
            node = taxonomy.get(category_id)
            path = taxonomy.path(category_id)
            by_facet.setdefault(node.facet, []).append(path)
            if role == "primary":
                primary = path
        book["primary_genre"] = primary
        book["categories"] = by_facet
        book["series"] = series.get(book.get("id"))
    return books


def category_choices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every category as a pick-list entry, in tree order per facet."""
    return [
        {"id": node["id"], "facet": facet["facet"], "path": node["path"], "ref": f"{facet['facet']}:{node['path']}",
         "display": f"{facet['label']}: {node['path']}", "depth": node["depth"], "total": node["total"]}
        for facet in taxonomy_tree(conn)
        for node in facet["categories"]
    ]


def custom_shelves(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Goodreads shelves other than the status ones, with book counts."""
    counts: dict[str, int] = defaultdict(int)
    for row in conn.execute("SELECT exclusive_shelf, shelves_json FROM books"):
        for shelf in json.loads(row["shelves_json"] or "[]"):
            if shelf not in STATUS_SHELVES and shelf != (row["exclusive_shelf"] or ""):
                counts[shelf] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def series_books(conn: sqlite3.Connection, name: str) -> list[int]:
    """Book ids in a series, in reading order."""
    return [
        row["book_id"]
        for row in conn.execute(
            """
            SELECT bs.book_id FROM book_series bs JOIN series s ON s.id = bs.series_id
            WHERE s.name = ? COLLATE NOCASE ORDER BY bs.position IS NULL, bs.position
            """,
            (name,),
        )
    ]


# ---------------------------------------------------------------------------
# Agent proposals
# ---------------------------------------------------------------------------


def suggest_assignment(
    conn: sqlite3.Connection,
    book_id: int,
    reference: str,
    *,
    reason: str | None = None,
    proposed_by: str = "agent",
) -> dict[str, Any]:
    """Queue "this book belongs in this category" for the user to review.

    Never assigns anything itself. ``reference`` must name an existing category,
    or carry a facet prefix to propose a new one ("theme:Found family"), which
    is only created if the user accepts. A proposal the user already rejected
    is not raised again, and one the book already carries is a no-op.
    """
    _require_book(conn, book_id)
    taxonomy = Taxonomy(conn)
    category_id: int | None = None
    new_facet = new_label = None
    try:
        category_id = taxonomy.resolve(reference).id
    except CategoryError as exc:
        facet, _ = _parse_reference(reference, None)
        if facet is None or "more than one" in str(exc):
            raise
        new_facet = facet
        new_label = str(reference).split(":", 1)[1].split(">")[-1].strip()
        if not slugify(new_label):
            raise
    if category_id is not None and conn.execute(
        "SELECT 1 FROM book_categories WHERE book_id = ? AND category_id = ?", (book_id, category_id)
    ).fetchone():
        return {"status": "already assigned", "category": taxonomy.display(category_id)}
    existing = conn.execute(
        """
        SELECT id, status FROM category_suggestions
        WHERE kind = 'assign' AND book_id = ? AND COALESCE(category_id, 0) = ?
              AND COALESCE(proposed_label, '') = ?
        """,
        (book_id, category_id or 0, new_label or ""),
    ).fetchone()
    target = taxonomy.display(category_id) if category_id is not None else f"new {FACET_LABELS[new_facet]}: {new_label}"
    if existing is not None:
        if existing["status"] == "rejected":
            return {"status": "previously rejected", "id": existing["id"], "category": target}
        if existing["status"] == "pending":
            return {"status": "already pending", "id": existing["id"], "category": target}
    cur = conn.execute(
        """
        INSERT INTO category_suggestions
            (kind, book_id, category_id, proposed_facet, proposed_label, confidence, book_count,
             evidence, proposed_by)
        VALUES ('assign', ?, ?, ?, ?, 0.5, 1, ?, ?)
        ON CONFLICT(book_id, COALESCE(category_id, 0), COALESCE(proposed_label, '')) WHERE kind = 'assign'
        DO UPDATE SET status = 'pending', evidence = excluded.evidence, proposed_by = excluded.proposed_by,
                      decided_at = NULL, decided_by = NULL
        """,
        (book_id, category_id, new_facet, new_label, (reason or "").strip() or None, proposed_by),
    )
    conn.commit()
    suggestion_id = cur.lastrowid or conn.execute(
        """
        SELECT id FROM category_suggestions WHERE kind = 'assign' AND book_id = ?
              AND COALESCE(category_id, 0) = ? AND COALESCE(proposed_label, '') = ?
        """,
        (book_id, category_id or 0, new_label or ""),
    ).fetchone()[0]
    return {"status": "pending review", "id": int(suggestion_id), "category": target}


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


# Review order: library-wide mappings first (most leverage), then per-book
# category proposals, then primary-genre questions.
_KIND_ORDER = {"map": 0, "assign": 1, "primary": 2}


def list_suggestions(conn: sqlite3.Connection, *, status: str = "pending") -> list[dict[str, Any]]:
    """Suggestions in review order: highest-leverage mappings first, then books."""
    taxonomy = Taxonomy(conn)
    rows = conn.execute(
        """
        SELECT s.*, b.title AS book_title, b.author AS book_author, b.goodreads_id
        FROM category_suggestions s LEFT JOIN books b ON b.id = s.book_id
        WHERE s.status = ?
        """,
        (status,),
    ).fetchall()
    items = [_describe_suggestion(taxonomy, row) for row in rows]
    items.sort(
        key=lambda item: (
            _KIND_ORDER.get(item["kind"], 9),
            -(item["book_count"] * item["confidence"]),
            item["subject"].lower(),
        )
    )
    return items


def _describe_suggestion(taxonomy: Taxonomy, row: sqlite3.Row) -> dict[str, Any]:
    if row["category_id"] is not None and row["category_id"] in taxonomy.by_id:
        target = taxonomy.display(row["category_id"])
    elif row["proposed_label"] and row["proposed_facet"] == "tag":
        target = f"Tag: #{row['proposed_label']}"
    elif row["proposed_label"]:
        facet = FACET_LABELS.get(row["proposed_facet"] or "theme", "Theme")
        target = f"new {facet}: {row['proposed_label']}"
    else:
        target = "(category deleted)"
    if row["kind"] == "map":
        label = MATCH_KIND_LABELS.get(row["match_kind"], row["match_kind"])
        subject = f'{label} "{row["match_value"]}"'
    else:  # 'primary' and 'assign' are about one book
        author = f" — {row['book_author']}" if row["book_author"] else ""
        subject = f"{row['book_title'] or 'Unknown book'}{author}"
    return {
        "id": row["id"],
        "kind": row["kind"],
        "subject": subject,
        "target": target,
        "match_kind": row["match_kind"],
        "match_value": row["match_value"],
        "book_id": row["book_id"],
        "goodreads_id": row["goodreads_id"],
        "category_id": row["category_id"],
        "proposed_facet": row["proposed_facet"],
        "proposed_label": row["proposed_label"],
        "confidence": row["confidence"],
        "book_count": row["book_count"],
        "evidence": row["evidence"],
        "status": row["status"],
        "proposed_by": row["proposed_by"] if "proposed_by" in row.keys() else None,
    }


def _get_suggestion(conn: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM category_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None:
        raise CategoryError(
            f"No suggestion with id {suggestion_id}", hint="Run `adso review` to list open suggestions."
        )
    return row


def _group_key(row: sqlite3.Row | dict[str, Any]) -> tuple:
    """Suggestions that share a key form one review card.

    Mappings group by target (a shelf and every Open Library spelling of the
    same genre). Adso's "is this novel really History?" questions group by
    genre and subject. Everything else is a card of its own.
    """
    if row["kind"] == "assign":
        if row["match_kind"]:
            return ("assign", row["category_id"], row["match_kind"], row["match_value"])
        return ("single", row["id"])
    if row["kind"] != "map":
        return ("single", row["id"])
    if row["category_id"] is not None:
        return ("category", row["category_id"])
    return ("new", row["proposed_facet"] or "theme", normalize_term(row["proposed_label"] or ""))


def pending_card_count(conn: sqlite3.Connection) -> int:
    """How many review cards are open (cheap: no book scan)."""
    rows = conn.execute(
        "SELECT id, kind, category_id, proposed_facet, proposed_label, match_kind, match_value "
        "FROM category_suggestions WHERE status = 'pending'"
    ).fetchall()
    return len({_group_key(row) for row in rows})


def suggestion_group(conn: sqlite3.Connection, suggestion_id: int) -> list[int]:
    """Ids of the suggestions on the same card as this one (itself included).

    A card is every mapping in the same state that points at the same target,
    e.g. shelf "horror" + subject "Horror tales" -> Genre: Horror. A
    primary-genre question is always a card of its own.
    """
    row = _get_suggestion(conn, suggestion_id)
    key = _group_key(row)
    if key[0] == "single":
        return [suggestion_id]
    siblings = conn.execute(
        "SELECT id, kind, category_id, proposed_facet, proposed_label, match_kind, match_value "
        "FROM category_suggestions WHERE kind = ? AND status = ?",
        (row["kind"], row["status"]),
    ).fetchall()
    return sorted(sibling["id"] for sibling in siblings if _group_key(sibling) == key)


def _signal_books(conn: sqlite3.Connection) -> dict[tuple[str, str], set[int]]:
    """(match_kind, value) -> ids of the books carrying that evidence now."""
    books: dict[tuple[str, str], set[int]] = defaultdict(set)
    for book in conn.execute(
        "SELECT id, exclusive_shelf, shelves_json, subjects_json, tags_json FROM books"
    ):
        for key in _book_signals(book):
            books[key].add(book["id"])
    return books


def list_suggestion_cards(conn: sqlite3.Connection, *, status: str = "pending") -> list[dict[str, Any]]:
    """Suggestions as review cards, highest-leverage first.

    Mapping suggestions that share a target are one card whose ``book_count``
    is the number of distinct books across its sources, so one decision covers
    a shelf and every Open Library spelling of the same genre. Card ``id`` is
    its highest-leverage member; any member's id addresses the whole card.
    """
    items = list_suggestions(conn, status=status)
    signal_books = _signal_books(conn)
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    cards: list[dict[str, Any]] = []
    for item in items:
        key = _group_key(item)
        if key[0] == "single":
            cards.append({**item, "members": [item]})
        else:
            groups[key].append(item)
    for key, members in groups.items():
        if key[0] == "assign":
            # One question per book, asked together: "History for these novels?"
            members.sort(key=lambda m: m["subject"].lower())
            cards.append({**members[0], "book_count": len(members), "members": members})
            continue
        members.sort(key=lambda m: -(m["book_count"] * m["confidence"]))
        books: set[int] = set()
        for member in members:
            books |= signal_books.get((member["match_kind"], member["match_value"]), set())
        lead = members[0]
        cards.append(
            {
                **lead,
                "book_count": len(books) or sum(m["book_count"] for m in members),
                "confidence": max(m["confidence"] for m in members),
                "members": members,
            }
        )
    cards.sort(
        key=lambda card: (
            _KIND_ORDER.get(card["kind"], 9),
            -(card["book_count"] * card["confidence"]),
            card["target"].lower() if card["kind"] == "map" else card["subject"].lower(),
        )
    )
    return cards


def accept_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: int,
    *,
    as_category: str | None = None,
    only: bool = False,
    actor: str = "cli",
) -> dict[str, Any]:
    """Accept a suggestion's card, optionally redirecting it to another category.

    Every mapping on the card becomes a rule applied across the library
    straight away (``only`` limits it to this one source). A primary-genre
    suggestion sets that book's primary genre.
    """
    row = _get_suggestion(conn, suggestion_id)
    if row["status"] != "pending":
        raise CategoryError(
            f"Suggestion {suggestion_id} is already {row['status']}",
            hint=f"Reopen it first with `adso review {suggestion_id} --reopen`.",
        )
    taxonomy = Taxonomy(conn)
    tag = _tag_target(as_category) if as_category and row["kind"] == "map" else None
    if tag is None and not as_category and row["kind"] == "map" and row["proposed_facet"] == "tag":
        tag = row["proposed_label"]
    if tag is not None:
        members = [suggestion_id] if only else suggestion_group(conn, suggestion_id)
        _mark(conn, members, "accepted", actor)
        rule_ids = [
            _insert_rule(conn, member["match_kind"], member["match_value"], None, tag=tag, actor=actor)
            for member in (_get_suggestion(conn, member_id) for member_id in members)
        ]
        conn.commit()
        run = categorize(conn)
        placeholders = ", ".join("?" for _ in rule_ids)
        books = conn.execute(
            f"SELECT COUNT(DISTINCT book_id) FROM tag_rule_applications WHERE rule_id IN ({placeholders})",
            rule_ids,
        ).fetchone()[0]
        return {"kind": "map", "category": f"Tag: #{tag}", "books": books, "sources": len(members), "run": run}
    if as_category and row["kind"] == "map":
        category_id = _mapping_target(taxonomy, as_category).id
    elif as_category:
        facet = "genre" if row["kind"] == "primary" else None
        category_id = taxonomy.resolve(as_category, facet=facet).id
    elif row["category_id"] is not None:
        category_id = row["category_id"]
        if row["kind"] == "map" and category_id in taxonomy.by_id:
            _require_mappable(taxonomy, taxonomy.get(category_id))
    elif row["proposed_label"]:
        facet = row["proposed_facet"] or "theme"
        found = conn.execute(
            "SELECT id FROM categories WHERE facet = ? AND parent_id IS NULL AND slug = ?",
            (facet, slugify(row["proposed_label"])),
        ).fetchone()
        category_id = found["id"] if found else _create_category(conn, facet, row["proposed_label"], None)
    else:
        raise CategoryError(
            f"Suggestion {suggestion_id} has no category any more",
            hint=f'Accept it with `--as "<category>"`, or `adso review {suggestion_id} --reject`.',
        )

    if row["kind"] == "primary":
        _mark(conn, [suggestion_id], "accepted", actor, category_id=category_id)
        _set_primary(conn, row["book_id"], category_id)
        conn.commit()
        return {"kind": "primary", "category": Taxonomy(conn).display(category_id), "books": 1, "sources": 1}
    if row["kind"] == "assign":
        members = [suggestion_id] if only else suggestion_group(conn, suggestion_id)
        _mark(conn, members, "accepted", actor, category_id=category_id)
        book_ids = {_get_suggestion(conn, member_id)["book_id"] for member_id in members}
        # Accepting is the user's decision, so it lands as a user assignment.
        for book_id in book_ids:
            _upsert_user_assignment(conn, book_id, category_id, role=None)
        _settle_primaries(conn, Taxonomy(conn), book_ids=book_ids)
        conn.commit()
        return {
            "kind": "assign", "category": Taxonomy(conn).display(category_id), "books": len(book_ids),
            "sources": len(members),
        }

    members = [suggestion_id] if only else suggestion_group(conn, suggestion_id)
    _mark(conn, members, "accepted", actor, category_id=category_id)
    rule_ids = [
        _insert_rule(conn, member["match_kind"], member["match_value"], category_id, actor=actor)
        for member in (_get_suggestion(conn, member_id) for member_id in members)
    ]
    conn.commit()
    run = categorize(conn)
    placeholders = ", ".join("?" for _ in rule_ids)
    books = conn.execute(
        f"SELECT COUNT(DISTINCT book_id) FROM book_categories WHERE rule_id IN ({placeholders})",
        rule_ids,
    ).fetchone()[0]
    return {
        "kind": "map",
        "category": Taxonomy(conn).display(category_id),
        "books": books,
        "sources": len(members),
        "run": run,
    }


def _mark(
    conn: sqlite3.Connection,
    suggestion_ids: list[int],
    status: str,
    actor: str | None,
    *,
    category_id: int | None = None,
) -> None:
    for suggestion_id in suggestion_ids:
        if status == "pending":
            conn.execute(
                "UPDATE category_suggestions SET status = 'pending', decided_at = NULL, decided_by = NULL "
                "WHERE id = ?",
                (suggestion_id,),
            )
            continue
        conn.execute(
            """
            UPDATE category_suggestions
            SET status = ?, category_id = COALESCE(?, category_id),
                decided_at = CURRENT_TIMESTAMP, decided_by = ?
            WHERE id = ?
            """,
            (status, category_id, actor, suggestion_id),
        )


def reject_suggestion(
    conn: sqlite3.Connection, suggestion_id: int, *, only: bool = False, actor: str = "cli"
) -> dict[str, Any]:
    """Reject a suggestion's card (or with ``only``, just this source)."""
    row = _get_suggestion(conn, suggestion_id)
    if row["status"] != "pending":
        raise CategoryError(f"Suggestion {suggestion_id} is already {row['status']}")
    members = [suggestion_id] if only else suggestion_group(conn, suggestion_id)
    _mark(conn, members, "rejected", actor)
    conn.commit()
    if row["kind"] == "primary":
        # The next-best candidate (if any) becomes the new pick or question.
        _settle_primaries(conn, Taxonomy(conn), book_ids={row["book_id"]})
        conn.commit()
    item = _describe_suggestion(Taxonomy(conn), _get_suggestion_with_book(conn, suggestion_id))
    return {**item, "sources": len(members)}


def reopen_suggestion(conn: sqlite3.Connection, suggestion_id: int, *, only: bool = False) -> dict[str, Any]:
    """Return a rejected or superseded suggestion's card to the queue.

    Accepted mappings are undone with `adso taxonomy unmap` instead, which also
    removes the assignments the rule made.
    """
    row = _get_suggestion(conn, suggestion_id)
    if row["status"] == "accepted":
        raise CategoryError(
            f"Suggestion {suggestion_id} was accepted",
            hint="Undo a mapping with `adso taxonomy rules` and `adso taxonomy unmap RULE_ID`.",
        )
    members = [suggestion_id] if only else suggestion_group(conn, suggestion_id)
    _mark(conn, members, "pending", None)
    conn.commit()
    item = _describe_suggestion(Taxonomy(conn), _get_suggestion_with_book(conn, suggestion_id))
    return {**item, "sources": len(members)}


def _get_suggestion_with_book(conn: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT s.*, b.title AS book_title, b.author AS book_author, b.goodreads_id
        FROM category_suggestions s LEFT JOIN books b ON b.id = s.book_id WHERE s.id = ?
        """,
        (suggestion_id,),
    ).fetchone()
