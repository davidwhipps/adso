"""Default category vocabulary for a new catalogue.

Pure data plus one seeding function. The seed is a starting point, not a
standard: once written, the taxonomy belongs to the user, who can rename, move,
merge or delete any of it (`adso taxonomy`). Seeding runs exactly once per
catalogue (tracked in ``adso_meta``), so deleting a seeded category never brings
it back.

Aliases double as the matching vocabulary for Goodreads shelf names and Open
Library subjects, so they lean towards the strings those sources actually use
("sci-fi", "detective and mystery stories", "fiction, fantasy, general").
"""

from __future__ import annotations

import sqlite3

# Facets are fixed dimensions; categories live inside exactly one. Only the
# genre facet is a hierarchy with a primary role (see db / categorize).
FACETS = ("form", "genre", "audience", "theme")

FACET_LABELS = {
    "form": "Form",
    "genre": "Genre",
    "audience": "Audience",
    "theme": "Theme",
}

# (label, aliases, children) trees per facet. Labels are matched too, so an
# alias that only restates the label is unnecessary.
_Node = tuple  # (label, tuple[str, ...], tuple[_Node, ...])

SEED: dict[str, tuple[_Node, ...]] = {
    "form": (
        ("Fiction", ("novel", "novels", "fiction general"), ()),
        ("Nonfiction", ("non fiction", "non-fiction"), ()),
        ("Poetry", ("poems", "verse"), ()),
        ("Graphic", ("graphic novels", "graphic novel", "comics", "manga"), ()),
        ("Drama", ("plays", "theatre", "theater"), ()),
        ("Short Stories", ("short story", "short fiction", "anthology", "anthologies"), ()),
        ("Essays", ("essay",), ()),
    ),
    "genre": (
        (
            "Speculative Fiction",
            ("speculative", "spec fic", "sff", "sf and f"),
            (
                (
                    "Science Fiction",
                    ("sci fi", "scifi", "sf", "science fiction general", "hard sf", "hard science fiction"),
                    (
                        ("Space Opera", (), ()),
                        ("Cyberpunk", (), ()),
                        ("Dystopian", ("dystopia", "dystopias", "dystopian fiction"), ()),
                        ("Time Travel", (), ()),
                    ),
                ),
                (
                    "Fantasy",
                    ("fantasy fiction", "fantasy general"),
                    (
                        ("Epic Fantasy", ("high fantasy",), ()),
                        ("Urban Fantasy", (), ()),
                        ("Cozy Fantasy", ("cosy fantasy",), ()),
                        ("Magical Realism", (), ()),
                    ),
                ),
                ("Horror", ("horror tales", "horror fiction", "horror general"), ()),
            ),
        ),
        (
            "Literary Fiction",
            ("literary", "literature", "lit fic", "litfic"),
            (
                ("Classics", ("classic", "classic literature"), ()),
                ("Contemporary Fiction", ("contemporary",), ()),
            ),
        ),
        (
            "Mystery & Crime",
            (
                "mystery",
                "mysteries",
                "crime",
                "crime fiction",
                "detective",
                "detective fiction",
                "detective and mystery stories",
                "mystery general",
                "whodunit",
                "whodunnit",
            ),
            (
                ("Cozy Mystery", ("cozy mysteries", "cosy mystery"), ()),
                (
                    "Thriller",
                    ("thrillers", "suspense", "thrillers general", "psychological thriller"),
                    (),
                ),
            ),
        ),
        ("Historical Fiction", ("historical", "historical novel", "historical general"), ()),
        ("Romance", ("love stories", "romance general", "romantic fiction"), ()),
        ("Humor", ("humour", "comedy", "humorous fiction", "humorous stories", "funny"), ()),
        ("Adventure", ("adventure stories", "action and adventure", "action adventure"), ()),
        (
            "History",
            ("world history",),
            (("Military History", ("ww2", "wwii", "world war ii", "ww1", "wwi"), ()),),
        ),
        (
            "Biography & Memoir",
            ("biography", "biographies", "memoir", "memoirs", "autobiography", "autobiographies"),
            (),
        ),
        ("Philosophy", (), ()),
        ("Religion & Spirituality", ("religion", "spirituality", "theology"), ()),
        (
            "Science",
            ("popular science", "pop science", "popsci", "pop sci"),
            (
                ("Physics", ("astronomy", "cosmology"), ()),
                ("Biology", ("evolution", "genetics"), ()),
                ("Mathematics", ("math", "maths"), ()),
                ("Nature", ("natural history", "ecology", "environment"), ()),
            ),
        ),
        ("Psychology", ("neuroscience",), ()),
        ("Politics & Society", ("politics", "political science", "sociology", "current affairs"), ()),
        ("Economics & Business", ("economics", "business", "finance", "management"), ()),
        ("Self-Help", ("self help", "personal development", "productivity"), ()),
        ("Technology", ("computers", "programming", "computer science", "software"), ()),
        ("Art & Design", ("art", "design", "architecture", "photography"), ()),
        ("Food & Cooking", ("cooking", "cookbooks", "food"), ()),
        ("Travel", ("travel writing",), ()),
    ),
    "audience": (
        ("Young Adult", ("ya", "young adult fiction", "teen"), ()),
        (
            "Children's",
            ("children", "childrens books", "middle grade", "kids"),
            (),
        ),
    ),
    # Themes are the open vocabulary: moods, subjects and personal groupings.
    # They start empty and grow from accepted shelf mappings and user edits.
    "theme": (),
}


# Changes to the seed after catalogues were created with it. Seeding happens
# once, so each revision is replayed on older catalogues by upgrade_seed:
# (facet, label) pairs whose aliases to add or remove. A category the user
# renamed or deleted is simply skipped.
SEED_REVISION = 2
_REVISIONS: dict[int, dict[str, tuple[tuple[str, str, str], ...]]] = {
    2: {
        # Over-broad Open Library subjects: "war" and "military" tag war
        # novels; "juvenile fiction" tags classics with children's editions.
        "remove": (
            ("genre", "Military History", "war"),
            ("genre", "Military History", "military"),
            ("audience", "Children's", "juvenile fiction"),
        ),
        "add": (
            ("genre", "Literary Fiction", "lit fic"),
            ("genre", "Literary Fiction", "litfic"),
            ("genre", "Mystery & Crime", "whodunnit"),
            ("genre", "Science", "popsci"),
            ("genre", "Science", "pop sci"),
            ("genre", "Military History", "ww2"),
            ("genre", "Military History", "wwii"),
            ("genre", "Military History", "world war ii"),
            ("genre", "Military History", "ww1"),
            ("genre", "Military History", "wwi"),
            ("genre", "Science Fiction", "hard sf"),
            ("genre", "Science Fiction", "hard science fiction"),
        ),
    },
}


def upgrade_seed(conn: sqlite3.Connection) -> list[str]:
    """Replay seed revisions newer than this catalogue's. Returns changed aliases."""
    row = conn.execute("SELECT value FROM adso_meta WHERE key = 'taxonomy_seed_rev'").fetchone()
    current = int(row[0]) if row else 1
    removed: list[str] = []
    for revision in sorted(r for r in _REVISIONS if r > current):
        changes = _REVISIONS[revision]
        for facet, label, alias in changes.get("remove", ()):
            cur = conn.execute(
                """
                DELETE FROM category_aliases WHERE alias = ? AND category_id IN
                    (SELECT id FROM categories WHERE facet = ? AND label = ?)
                """,
                (alias, facet, label),
            )
            if cur.rowcount:
                removed.append(alias)
        for facet, label, alias in changes.get("add", ()):
            conn.execute(
                """
                INSERT OR IGNORE INTO category_aliases (category_id, alias)
                SELECT id, ? FROM categories WHERE facet = ? AND label = ?
                """,
                (alias, facet, label),
            )
    if current < SEED_REVISION:
        conn.execute(
            "INSERT OR REPLACE INTO adso_meta (key, value) VALUES ('taxonomy_seed_rev', ?)",
            (str(SEED_REVISION),),
        )
    return removed


def slugify(label: str) -> str:
    """URL-safe identity for a category label ("Mystery & Crime" -> "mystery-and-crime")."""
    text = label.lower().replace("&", " and ").replace("'", "").replace("’", "")
    out = []
    for char in text:
        out.append(char if char.isalnum() else " ")
    return "-".join("".join(out).split())


def seed_taxonomy(conn: sqlite3.Connection) -> bool:
    """Write the default taxonomy once per catalogue. Returns True if it seeded."""
    if conn.execute("SELECT 1 FROM adso_meta WHERE key = 'taxonomy_seeded'").fetchone():
        upgrade_seed(conn)
        return False
    # A catalogue that somehow already has categories (e.g. hand-built) is
    # treated as seeded rather than having defaults mixed into it.
    if conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone() is None:
        for facet, nodes in SEED.items():
            for position, node in enumerate(nodes):
                _insert_node(conn, facet, node, parent_id=None, position=position)
    conn.execute(
        "INSERT OR REPLACE INTO adso_meta (key, value) VALUES ('taxonomy_seeded', '1')"
    )
    # The data above is already the latest revision.
    conn.execute(
        "INSERT OR REPLACE INTO adso_meta (key, value) VALUES ('taxonomy_seed_rev', ?)", (str(SEED_REVISION),)
    )
    return True


def _insert_node(
    conn: sqlite3.Connection,
    facet: str,
    node: _Node,
    *,
    parent_id: int | None,
    position: int,
) -> None:
    label, aliases, children = node
    cur = conn.execute(
        """
        INSERT INTO categories (facet, slug, label, parent_id, position, origin)
        VALUES (?, ?, ?, ?, ?, 'seed')
        """,
        (facet, slugify(label), label, parent_id, position),
    )
    category_id = int(cur.lastrowid)
    for alias in aliases:
        conn.execute(
            "INSERT OR IGNORE INTO category_aliases (category_id, alias) VALUES (?, ?)",
            (category_id, alias),
        )
    for child_position, child in enumerate(children):
        _insert_node(conn, facet, child, parent_id=category_id, position=child_position)
