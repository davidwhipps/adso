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
# genre facet is a hierarchy with a primary role (see db / categorize). Era is
# filled in from the original publication year rather than from evidence.
FACETS = ("form", "genre", "tradition", "era", "audience", "theme")

FACET_LABELS = {
    "form": "Form",
    "genre": "Genre",
    "tradition": "Tradition",
    "era": "Era",
    "audience": "Audience",
    "theme": "Theme",
}

# (label, aliases, children) trees per facet. Labels are matched too, so an
# alias that only restates the label is unnecessary.
_Node = tuple  # (label, tuple[str, ...], tuple[_Node, ...])

# Genres describe what kind of book it is. There's deliberately no catch-all
# ("Literary Fiction", "Classics"): a novel that fits none of these has no
# primary genre, and its form, tradition and era still describe it. Subgenres
# are left for the user to add once enough books need them.
SEED: dict[str, tuple[_Node, ...]] = {
    "form": (
        ("Fiction", ("novel", "novels", "fiction general"), ()),
        ("Nonfiction", ("non fiction", "non-fiction"), ()),
        ("Short Stories", ("short story", "short fiction", "anthology", "anthologies"), ()),
        ("Essays", ("essay",), ()),
        ("Poetry", ("poems", "verse"), ()),
        ("Drama", ("plays", "theatre", "theater"), ()),
        ("Graphic", ("graphic novels", "graphic novel", "comics", "manga"), ()),
        (
            "Diaries & Letters",
            ("diaries", "diary", "letters", "correspondence", "authors correspondence", "journals"),
            (),
        ),
    ),
    "genre": (
        # Fiction
        (
            "Psychological Fiction",
            ("psychological", "psychological fiction", "psychological novel", "fiction psychological"),
            (),
        ),
        ("Novel of Ideas", ("philosophical fiction", "philosophical novel", "novel of ideas"), ()),
        ("Family Saga", ("family saga", "family sagas", "sagas", "domestic fiction"), ()),
        (
            "Coming of Age",
            ("bildungsroman", "bildungsromans", "coming of age fiction", "coming of age stories"),
            (),
        ),
        ("Social Novel", ("social novel", "social realism", "novel of manners", "social themes"), ()),
        (
            "Satire & Comic Fiction",
            ("satire", "satirical fiction", "comic fiction", "humorous", "humorous fiction",
             "humorous stories", "humor", "humour", "comedy"),
            (),
        ),
        ("Autofiction", ("autobiographical fiction", "autobiographical"), ()),
        (
            "Experimental & Metafiction",
            ("experimental", "experimental fiction", "experimental literature", "metafiction"),
            (),
        ),
        ("Historical Fiction", ("historical", "historical novel", "historical fiction"), ()),
        (
            "War Fiction",
            ("war stories", "war and military", "world war 1939 1945 fiction", "world war 1914 1918 fiction"),
            (),
        ),
        (
            "Political & Dystopian",
            ("political", "political fiction", "dystopia", "dystopias", "dystopian", "dystopian fiction"),
            (),
        ),
        ("Magical Realism & Fabulism", ("magical realism", "magic realism", "fabulism"), ()),
        ("Myth & Retellings", ("retellings", "myth retellings", "mythological fiction"), ()),
        ("Love Stories", ("love stories", "love story", "romance", "romantic fiction"), ()),
        (
            "Crime & Mystery",
            ("mystery", "mysteries", "crime", "crime fiction", "detective", "detective fiction",
             "detective and mystery stories", "mystery and detective", "mystery and detective stories",
             "whodunit", "whodunnit", "noir"),
            (),
        ),
        (
            "Thriller & Espionage",
            ("thriller", "thrillers", "suspense", "suspense fiction", "psychological thriller", "espionage",
             "spy stories", "spy fiction", "thrillers espionage"),
            (),
        ),
        (
            "Science Fiction",
            ("sci fi", "scifi", "sf", "hard sf", "hard science fiction", "space opera", "cyberpunk",
             "time travel"),
            (),
        ),
        (
            "Fantasy",
            ("fantasy fiction", "epic fantasy", "high fantasy", "urban fantasy", "cozy fantasy", "cosy fantasy"),
            (),
        ),
        (
            "Horror & Gothic",
            ("horror", "horror tales", "horror fiction", "gothic", "gothic fiction", "ghost stories"),
            (),
        ),
        ("Adventure", ("adventure stories", "action and adventure", "action adventure", "sea stories"), ()),
        # Nonfiction
        ("History", ("world history",), (("Economic History", ("economic history",), ()),)),
        (
            "Biography & Memoir",
            ("biography", "biographies", "memoir", "memoirs", "autobiography", "autobiographies",
             "biography and autobiography"),
            (),
        ),
        ("Philosophy", (), ()),
        (
            "Religion & Mythology",
            ("religion", "spirituality", "theology", "mythology", "classical mythology", "greek mythology"),
            (),
        ),
        ("Psychology", ("neuroscience",), ()),
        ("Politics & Society", ("politics", "political science", "sociology", "current affairs"), ()),
        ("Economics & Business", ("economics", "business", "finance", "management"), ()),
        (
            "Science & Nature",
            ("science", "popular science", "pop science", "popsci", "pop sci", "natural history", "nature",
             "ecology", "environment", "physics", "astronomy", "cosmology", "biology", "evolution",
             "mathematics", "math", "maths"),
            (),
        ),
        ("Technology", ("computers", "programming", "computer science", "software"), ()),
        (
            "Art, Architecture & Design",
            ("art", "design", "architecture", "photography", "art history"),
            (),
        ),
        (
            "Writing & Literature",
            ("literary criticism", "books and reading", "authorship", "creative writing"),
            (),
        ),
        ("Travel & Place", ("travel", "travel writing", "description and travel"), ()),
    ),
    # Literary traditions, matched from Open Library's "X literature/fiction"
    # subjects. A book can belong to several (a Russian novel in translation).
    "tradition": (
        ("American", ("american literature", "american fiction"), ()),
        (
            "British & Irish",
            ("english literature", "english fiction", "british literature", "british fiction",
             "irish literature", "irish fiction", "scottish literature", "scottish fiction"),
            (),
        ),
        (
            "Continental European",
            ("continental european fiction", "continental european literature", "european literature",
             "european fiction", "romance literature", "french literature", "french fiction",
             "german literature", "german fiction", "italian literature", "italian fiction",
             "spanish literature", "spanish fiction", "scandinavian literature", "scandinavian fiction",
             "polish literature", "czech literature", "portuguese literature"),
            (),
        ),
        ("Russian", ("russian literature", "russian fiction", "soviet literature"), ()),
        (
            "Latin American",
            ("latin american literature", "latin american fiction", "spanish american literature",
             "spanish american fiction", "argentine literature", "mexican literature",
             "colombian literature", "chilean literature", "brazilian literature"),
            (),
        ),
        (
            "Japanese & East Asian",
            ("japanese literature", "japanese fiction", "chinese literature", "chinese fiction",
             "korean literature", "korean fiction"),
            (),
        ),
        ("Translated", ("translations into english", "translated fiction", "in translation", "translations"), ()),
    ),
    # Filled in from each book's original publication year (see ERA_YEARS).
    "era": (
        ("Ancient & Medieval", ("ancient", "medieval"), ()),
        ("Early Modern", (), ()),
        ("19th Century", ("nineteenth century",), ()),
        ("Modernist", ("modernism",), ()),
        ("Postwar", (), ()),
        ("Contemporary", (), ()),
    ),
    "audience": (
        ("Young Adult", ("ya", "young adult fiction", "teen"), ()),
        (
            "Children's",
            ("children", "childrens books", "middle grade", "kids"),
            (),
        ),
    ),
    # What books are about, across fiction and nonfiction alike.
    "theme": (
        ("Family", ("family life", "families", "family relationships"), ()),
        ("Love & Marriage", ("marriage", "married people", "man woman relationships", "love"), ()),
        ("Friendship", ("friends",), ()),
        ("Grief & Loss", ("grief", "bereavement", "loss psychology"), ()),
        (
            "War",
            ("war", "military"),
            (
                ("World War I", ("world war 1914 1918", "ww1", "wwi", "first world war"), ()),
                ("World War II", ("world war 1939 1945", "ww2", "wwii", "second world war"), ()),
            ),
        ),
        (
            "Writers & Artists",
            ("authors", "writers", "novelists", "poets", "artists", "painters"),
            (),
        ),
    ),
}

# Era buckets by original publication year, inclusive; keyed by seed slug.
# A renamed or deleted era simply stops being filled in.
ERA_YEARS: dict[str, tuple[int | None, int | None]] = {
    "ancient-and-medieval": (None, 1499),
    "early-modern": (1500, 1799),
    "19th-century": (1800, 1899),
    "modernist": (1900, 1945),
    "postwar": (1946, 1989),
    "contemporary": (1990, None),
}


# Changes to the seed after catalogues were created with it. Seeding happens
# once, so each revision is replayed on older catalogues by upgrade_seed:
# (facet, label) pairs whose aliases to add or remove. A category the user
# renamed or deleted is simply skipped.
SEED_REVISION = 3
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

# Revision 3 replaced the bookshop-style tree (Speculative Fiction > ...,
# Literary Fiction > Classics) with descriptive fiction genres, and added the
# tradition and era facets. Seed categories are renamed or moved into the new
# shape; ones the new tree drops are removed only if the user never used them
# (no rules, nothing they assigned), otherwise they stay where they are.
_V3_RENAMES = (
    ("Horror", "Horror & Gothic"),
    ("Mystery & Crime", "Crime & Mystery"),
    ("Thriller", "Thriller & Espionage"),
    ("Magical Realism", "Magical Realism & Fabulism"),
    ("Dystopian", "Political & Dystopian"),
    ("Humor", "Satire & Comic Fiction"),
    ("Romance", "Love Stories"),
    ("Religion & Spirituality", "Religion & Mythology"),
    ("Science", "Science & Nature"),
    ("Art & Design", "Art, Architecture & Design"),
    ("Travel", "Travel & Place"),
)
_V3_RETIRED = (
    "Space Opera", "Cyberpunk", "Time Travel", "Epic Fantasy", "Urban Fantasy", "Cozy Fantasy",
    "Speculative Fiction", "Classics", "Contemporary Fiction", "Literary Fiction", "Cozy Mystery",
    "Military History", "Physics", "Biology", "Mathematics", "Nature", "Self-Help", "Food & Cooking",
)


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
    if current < 3:
        _restructure_v3(conn)
        # The new tree changes what evidence matches: re-run the engine once
        # (db.initialize does, after every migration has run).
        conn.execute("INSERT OR REPLACE INTO adso_meta (key, value) VALUES ('taxonomy_recategorize', '1')")
    if current < SEED_REVISION:
        conn.execute(
            "INSERT OR REPLACE INTO adso_meta (key, value) VALUES ('taxonomy_seed_rev', ?)",
            (str(SEED_REVISION),),
        )
    return removed


def _seed_category(conn: sqlite3.Connection, facet: str, label: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, parent_id FROM categories WHERE facet = ? AND label = ? AND origin = 'seed' "
        "ORDER BY id LIMIT 1",
        (facet, label),
    ).fetchone()


def _slug_free(conn: sqlite3.Connection, facet: str, parent_id: int | None, slug: str, own_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM categories WHERE facet = ? AND COALESCE(parent_id, 0) = ? AND slug = ? AND id != ?",
        (facet, parent_id or 0, slug, own_id),
    ).fetchone() is None


def _restructure_v3(conn: sqlite3.Connection) -> None:
    from .categorize import _purge_category  # categorize imports this module

    for old, new in _V3_RENAMES:
        row = _seed_category(conn, "genre", old)
        if row is not None and _slug_free(conn, "genre", row["parent_id"], slugify(new), row["id"]):
            conn.execute("UPDATE categories SET label = ?, slug = ? WHERE id = ?", (new, slugify(new), row["id"]))

    # Retire unused old categories, deepest first; their children move up.
    for label in _V3_RETIRED:
        row = _seed_category(conn, "genre", label)
        if row is None:
            continue
        used = conn.execute(
            "SELECT 1 FROM category_rules WHERE category_id = ? UNION ALL "
            "SELECT 1 FROM book_categories WHERE category_id = ? AND source = 'user' LIMIT 1",
            (row["id"], row["id"]),
        ).fetchone()
        if used:
            # Kept, but no longer the match for words the new tree places elsewhere.
            for alias in _all_aliases():
                conn.execute(
                    "DELETE FROM category_aliases WHERE category_id = ? AND alias = ?", (row["id"], alias)
                )
            continue
        conn.execute("UPDATE categories SET parent_id = ? WHERE parent_id = ?", (row["parent_id"], row["id"]))
        _purge_category(conn, row["id"])

    # Put every seed node in its new place, add the new ones, refresh aliases.
    for facet, nodes in SEED.items():
        _reconcile(conn, facet, nodes, parent_id=None)


def _all_aliases() -> set[str]:
    out: set[str] = set()

    def walk(nodes: tuple[_Node, ...]) -> None:
        for _label, aliases, children in nodes:
            out.update(aliases)
            walk(children)

    for nodes in SEED.values():
        walk(nodes)
    return out


def _reconcile(conn: sqlite3.Connection, facet: str, nodes: tuple[_Node, ...], *, parent_id: int | None) -> None:
    for position, node in enumerate(nodes):
        label, aliases, children = node
        row = _seed_category(conn, facet, label)
        if row is None:
            if conn.execute("SELECT 1 FROM categories WHERE facet = ? AND label = ?", (facet, label)).fetchone():
                continue  # the user made their own; leave theirs alone
            if not _slug_free(conn, facet, parent_id, slugify(label), 0):
                continue
            _insert_node(conn, facet, node, parent_id=parent_id, position=position)
            continue
        category_id = int(row["id"])
        if row["parent_id"] != parent_id and _slug_free(conn, facet, parent_id, slugify(label), category_id):
            conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (parent_id, category_id))
        conn.execute("UPDATE categories SET position = ? WHERE id = ?", (position, category_id))
        for alias in aliases:
            conn.execute(
                "INSERT OR IGNORE INTO category_aliases (category_id, alias) VALUES (?, ?)", (category_id, alias)
            )
        _reconcile(conn, facet, children, parent_id=category_id)


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
