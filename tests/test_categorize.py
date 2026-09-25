"""Tests for categorisation: taxonomy, suggestion engine, review, series, filters."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from adso import categorize as cat
from adso import cli, db
from adso.catalogue import BookFilters, list_books
from adso.sync import import_goodreads_csv
from adso.taxonomy_seed import slugify

HEADERS = [
    "Book Id",
    "Title",
    "Author",
    "Bookshelves",
    "Exclusive Shelf",
    "My Rating",
    "Date Added",
]


def book(book_id: str, title: str, shelves: str, *, author: str = "Author") -> dict[str, str]:
    return {
        "Book Id": book_id,
        "Title": title,
        "Author": author,
        "Bookshelves": shelves,
        "Exclusive Shelf": shelves.split(",")[0].strip(),
        "My Rating": "0",
        "Date Added": "2026/01/01",
    }


LIBRARY = [
    book("1", "Leviathan Wakes (The Expanse, #1)", "read, sci-fi, space-opera"),
    book("2", "Caliban's War (The Expanse, #2)", "to-read, sci-fi, space-opera"),
    book("3", "Legends & Lattes", "read, cozy-fantasy, favorites, owned"),
    book("4", "The Name of the Rose", "read, mystery, historical-fiction"),
    book("5", "Meditations", "to-read, philosophy, stoicism"),
]


class CategorizeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "adso.sqlite"
        self.conn = db.connect(self.db_path)
        db.initialize(self.conn)
        # The seed genres are flat; these tests need subgenres, as a user adds them.
        for child in ("Science Fiction > Space Opera", "Science Fiction > Cyberpunk", "Fantasy > Cozy Fantasy"):
            cat.add_category(self.conn, child)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def sync(self, rows: list[dict[str, str]], name: str = "export.csv") -> None:
        path = self.root / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(rows)
        import_goodreads_csv(self.conn, path, mode="sync")

    def book_id(self, goodreads_id: str) -> int:
        return int(
            self.conn.execute("SELECT id FROM books WHERE goodreads_id = ?", (goodreads_id,)).fetchone()[0]
        )

    def proposal(self, kind: str, value: str) -> dict:
        for item in cat.list_suggestions(self.conn):
            if item["kind"] == "map" and item["match_kind"] == kind and item["match_value"] == value:
                return item
        self.fail(f"no pending proposal for {kind} {value!r}")

    def tags(self, goodreads_id: str) -> list[str]:
        row = self.conn.execute("SELECT tags_json FROM books WHERE goodreads_id = ?", (goodreads_id,)).fetchone()
        return json.loads(row[0])

    def paths(self, goodreads_id: str, facet: str = "genre") -> list[str]:
        data = cat.book_categories(self.conn, self.book_id(goodreads_id))
        return sorted(entry["path"] for entry in data["by_facet"][facet])

    def primary(self, goodreads_id: str) -> str | None:
        data = cat.book_categories(self.conn, self.book_id(goodreads_id))
        return data["primary"]["path"] if data["primary"] else None


class SeedTests(CategorizeTestCase):
    def test_seed_runs_once_and_survives_reinitialise(self):
        count = self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        self.assertGreater(count, 30)
        db.initialize(self.conn)
        self.assertEqual(count, self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0])

    def test_deleted_seed_category_is_not_reseeded(self):
        cat.delete_category(self.conn, "Adventure")
        db.initialize(self.conn)
        with self.assertRaises(cat.CategoryError):
            cat.Taxonomy(self.conn).resolve("Adventure")

    def test_upgrades_a_catalogue_without_category_tables(self):
        legacy = sqlite3.connect(self.root / "legacy.sqlite")
        legacy.row_factory = sqlite3.Row
        legacy.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, goodreads_id TEXT, title TEXT NOT NULL)")
        legacy.commit()
        db.initialize(legacy)
        self.assertTrue(cat.Taxonomy(legacy).resolve("Fantasy"))
        legacy.close()


class TaxonomyTests(CategorizeTestCase):
    def test_resolve_by_label_alias_path_and_facet(self):
        taxonomy = cat.Taxonomy(self.conn)
        self.assertEqual(taxonomy.resolve("sci-fi").label, "Science Fiction")
        self.assertEqual(taxonomy.resolve("Fantasy > Cozy Fantasy").label, "Cozy Fantasy")
        self.assertEqual(taxonomy.resolve("genre:Horror").facet, "genre")
        self.assertEqual(taxonomy.path(taxonomy.resolve("space opera").id),
                         "Science Fiction > Space Opera")

    def test_ambiguous_reference_lists_candidates(self):
        cat.add_category(self.conn, "Crime & Mystery > Cozy")
        cat.add_category(self.conn, "Fantasy > Cozy")
        with self.assertRaises(cat.CategoryError) as ctx:
            cat.Taxonomy(self.conn).resolve("cozy")
        self.assertIn("more than one", str(ctx.exception))
        self.assertEqual(cat.Taxonomy(self.conn).resolve("Fantasy > Cozy").label, "Cozy")

    def test_add_needs_a_facet_or_parent(self):
        with self.assertRaises(cat.CategoryError):
            cat.add_category(self.conn, "Found family")
        added = cat.add_category(self.conn, "theme:Found family")
        self.assertEqual(added.facet, "theme")
        child = cat.add_category(self.conn, "Fantasy > Grimdark")
        self.assertEqual(child.facet, "genre")
        with self.assertRaises(cat.CategoryError):
            cat.add_category(self.conn, "Fantasy > Grimdark")

    def test_move_rejects_cycles(self):
        with self.assertRaises(cat.CategoryError):
            cat.move_category(self.conn, "Science Fiction", "Space Opera")
        moved = cat.move_category(self.conn, "Space Opera", None)
        self.assertIsNone(moved.parent_id)

    def test_rename_keeps_old_name_matching(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.rename_category(self.conn, "Space Opera", "Planetary Romance")
        self.assertEqual(cat.Taxonomy(self.conn).resolve("space opera").label, "Planetary Romance")


class EngineTests(CategorizeTestCase):
    def test_shelves_become_ranked_mapping_proposals(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        items = cat.list_suggestions(self.conn)
        values = [item["match_value"] for item in items]
        # Status and logistics shelves never become proposals.
        for skipped in ("read", "to read", "favorites", "owned"):
            self.assertNotIn(skipped, values)
        # Two-book shelves outrank one-book ones.
        self.assertEqual(set(values[:2]), {"sci fi", "space opera"})
        self.assertEqual(self.proposal("shelf", "sci fi")["book_count"], 2)
        self.assertEqual(
            self.proposal("shelf", "stoicism")["target"], "Tag: #stoicism"
        )

    def test_nothing_is_assigned_without_review(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM book_categories").fetchone()[0], 0)

    def test_accepted_mapping_applies_now_and_on_later_syncs(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        outcome = cat.accept_suggestion(self.conn, self.proposal("shelf", "sci fi")["id"])
        self.assertEqual(outcome["books"], 2)
        self.assertEqual(self.paths("1"), ["Science Fiction"])

        self.sync([*LIBRARY, book("6", "Dune", "to-read, sci-fi")], name="later.csv")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("6"), ["Science Fiction"])
        self.assertEqual(self.primary("6"), "Science Fiction")
        pending_values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        self.assertNotIn("sci fi", pending_values)

    def test_accept_as_redirects_and_unmatched_shelf_becomes_a_tag(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        outcome = cat.accept_suggestion(self.conn, self.proposal("shelf", "stoicism")["id"])
        self.assertEqual((outcome["category"], outcome["books"]), ("Tag: #stoicism", 1))
        self.assertEqual(self.tags("5"), ["stoicism"])
        self.assertEqual(self.paths("5", "theme"), [])
        cat.accept_suggestion(
            self.conn, self.proposal("shelf", "philosophy")["id"], as_category="Religion & Mythology"
        )
        self.assertEqual(self.paths("5"), ["Religion & Mythology"])

    def test_rejected_proposal_never_returns(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.reject_suggestion(self.conn, self.proposal("shelf", "stoicism")["id"])
        cat.categorize(self.conn)
        values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        self.assertNotIn("stoicism", values)
        rejected = cat.list_suggestions(self.conn, status="rejected")
        self.assertEqual([item["match_value"] for item in rejected], ["stoicism"])

    def test_rule_assignment_follows_its_shelf_but_user_edits_stay(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "sci fi")["id"])
        cat.add_book_category(self.conn, self.book_id("2"), "theme:" + cat.add_category(
            self.conn, "theme:Politics in space").label)
        changed = [dict(row) for row in LIBRARY]
        changed[1] = book("2", "Caliban's War (The Expanse, #2)", "to-read, space-opera")
        self.sync(changed, name="changed.csv")
        result = cat.categorize(self.conn)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.paths("2"), [])
        self.assertEqual(self.paths("2", "theme"), ["Politics in space"])

    def test_removed_category_is_not_readded_by_rules(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "sci fi")["id"])
        cat.remove_book_category(self.conn, self.book_id("2"), "Science Fiction")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("2"), [])
        self.assertEqual(self.paths("1"), ["Science Fiction"])

    def test_unmap_removes_rule_assignments(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "sci fi")["id"])
        rule = cat.list_rules(self.conn)[0]
        self.assertEqual(rule["books"], 2)
        cat.delete_rule(self.conn, rule["id"])
        self.assertEqual(self.paths("1"), [])
        self.assertIsNone(self.primary("1"))

    def test_subjects_propose_only_clean_matches(self):
        self.sync(LIBRARY)
        self.conn.execute(
            "UPDATE books SET subjects_json = ? WHERE goodreads_id = '1'",
            (json.dumps(["Fiction, science fiction, general", "Interplanetary voyages", "Fiction"]),),
        )
        self.conn.commit()
        cat.categorize(self.conn)
        self.assertEqual(
            self.proposal("subject", "fiction science fiction general")["target"],
            "Genre: Science Fiction",
        )
        self.assertEqual(self.proposal("subject", "fiction")["target"], "Form: Fiction")
        values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        self.assertNotIn("interplanetary voyages", values)

    def test_manual_map_answers_the_open_proposal(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        outcome = cat.add_rule(self.conn, "shelf", "cozy-fantasy", "theme:" + cat.add_category(
            self.conn, "theme:Comfort reads").label)
        self.assertEqual(outcome["books"], 1)
        values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        self.assertNotIn("cozy fantasy", values)

    def test_deleting_a_mapped_category_reopens_its_shelf(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "space opera")["id"])
        cat.delete_category(self.conn, "Space Opera")
        cat.categorize(self.conn)
        # The shelf is unmapped again, so it is asked about again (with a new guess).
        self.assertEqual(self.proposal("shelf", "space opera")["book_count"], 2)

    def test_dated_and_club_shelves_are_not_proposed(self):
        self.sync([
            book("8", "A", "read, read-in-2024, 2025-reads, book-club"),
            book("9", "B", "read, lit-fic, whodunnit, popsci, ww2"),
        ])
        cat.categorize(self.conn)
        items = {item["match_value"]: item["target"] for item in cat.list_suggestions(self.conn)}
        for skipped in ("read in 2024", "2025 reads", "book club"):
            self.assertNotIn(skipped, items)
        self.assertEqual(items["lit fic"], "Tag: #lit-fic")  # no catch-all genre to file it under
        self.assertEqual(items["whodunnit"], "Genre: Crime & Mystery")
        self.assertEqual(items["popsci"], "Genre: Science & Nature")
        self.assertEqual(items["ww2"], "Theme: War > World War II")

    def test_dry_run_writes_nothing(self):
        self.sync(LIBRARY)
        result = cat.categorize(self.conn, dry_run=True)
        self.assertGreater(result["new_proposals"], 0)
        self.assertEqual(result["series"], 2)
        for table in ("category_suggestions", "book_series", "series"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)


class PrimaryGenreTests(CategorizeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sync(LIBRARY)
        cat.categorize(self.conn)

    def accept(self, value: str) -> None:
        cat.accept_suggestion(self.conn, self.proposal("shelf", value)["id"])

    def primary_questions(self) -> list[dict]:
        return [item for item in cat.list_suggestions(self.conn) if item["kind"] == "primary"]

    def test_single_genre_becomes_primary_and_moves_to_more_specific(self):
        self.accept("sci fi")
        self.assertEqual(self.primary("1"), "Science Fiction")
        self.accept("space opera")
        self.assertEqual(self.primary("1"), "Science Fiction > Space Opera")
        self.assertEqual(self.primary_questions(), [])

    def test_competing_genres_are_picked_not_asked(self):
        self.accept("mystery")
        self.accept("historical fiction")
        # The pick already made stays put; nothing goes to review.
        self.assertEqual(self.primary("4"), "Crime & Mystery")
        self.assertEqual(self.primary_questions(), [])
        cat.categorize(self.conn)
        self.assertEqual(self.primary("4"), "Crime & Mystery")
        cat.set_primary_genre(self.conn, self.book_id("4"), "Historical Fiction")
        cat.categorize(self.conn)
        self.assertEqual(self.primary("4"), "Historical Fiction")

    def test_pick_prefers_the_books_side_then_the_rarer_genre(self):
        self.sync([
            *LIBRARY,
            book("6", "Wolf Hall", "read, fiction, history, historical-fiction"),
            book("7", "SPQR", "read, history"),
        ], name="more.csv")
        for shelf, target in (("fiction", "form:Fiction"), ("history", "History"),
                              ("historical-fiction", "Historical Fiction")):
            cat._insert_rule(self.conn, "shelf", cat.normalize_term(shelf), cat.Taxonomy(self.conn).resolve(target).id,
                             actor="cli")
        self.conn.commit()
        cat.categorize(self.conn)
        self.assertEqual(self.primary("6"), "Historical Fiction")  # a novel leads with its fiction genre
        self.assertEqual(self.primary("7"), "History")
        self.assertEqual(self.primary_questions(), [])

    def test_user_primary_is_final(self):
        self.accept("sci fi")
        cat.set_primary_genre(self.conn, self.book_id("1"), "Science Fiction")
        self.accept("space opera")
        self.assertEqual(self.primary("1"), "Science Fiction")

    def test_primary_must_be_a_genre_and_unique(self):
        with self.assertRaises(cat.CategoryError):
            cat.set_primary_genre(self.conn, self.book_id("1"), "form:Fiction")
        cat.set_primary_genre(self.conn, self.book_id("1"), "Horror")
        genre = cat.Taxonomy(self.conn).resolve("Fantasy").id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO book_categories (book_id, category_id, role, source) VALUES (?, ?, 'primary', 'user')",
                (self.book_id("1"), genre),
            )


class GroupedReviewTests(CategorizeTestCase):
    """One card per target: a shelf and the OL spellings of the same genre."""

    def setUp(self) -> None:
        super().setUp()
        self.sync([
            book("1", "It", "read, horror"),
            book("2", "Carrie", "read, horror"),
            book("3", "Dracula", "read"),
            book("4", "Frankenstein", "to-read, stoicism"),
        ])
        subjects = {
            "1": ["Horror tales", "Fiction, horror"],
            "3": ["Horror tales"],
            "4": ["Fiction, horror"],
        }
        for goodreads_id, values in subjects.items():
            self.conn.execute(
                "UPDATE books SET subjects_json = ? WHERE goodreads_id = ?", (json.dumps(values), goodreads_id)
            )
        self.conn.commit()
        cat.categorize(self.conn)

    def horror_card(self) -> dict:
        return next(c for c in cat.list_suggestion_cards(self.conn) if c["target"] == "Genre: Horror & Gothic")

    def test_sources_with_one_target_form_one_card_with_distinct_books(self):
        card = self.horror_card()
        values = sorted(m["match_value"] for m in card["members"])
        self.assertEqual(values, ["fiction horror", "horror", "horror tales"])
        self.assertEqual(card["book_count"], 4)  # books 1-4, counted once each
        themes = [c for c in cat.list_suggestion_cards(self.conn) if c["target"] == "Tag: #stoicism"]
        self.assertEqual(len(themes[0]["members"]), 1)

    def test_accepting_any_member_accepts_the_card(self):
        member = self.horror_card()["members"][-1]["id"]
        outcome = cat.accept_suggestion(self.conn, member)
        self.assertEqual(outcome["sources"], 3)
        self.assertEqual(outcome["books"], 4)
        self.assertEqual(len(cat.list_rules(self.conn)), 3)
        self.assertEqual(self.paths("3"), ["Horror & Gothic"])

    def test_only_narrows_to_one_source(self):
        card = self.horror_card()
        subject = next(m for m in card["members"] if m["match_value"] == "horror tales")
        cat.reject_suggestion(self.conn, subject["id"], only=True)
        outcome = cat.accept_suggestion(self.conn, card["id"])
        self.assertEqual(outcome["sources"], 2)
        self.assertEqual(self.paths("3"), [])  # only backed by the rejected subject
        cat.reopen_suggestion(self.conn, subject["id"], only=True)
        cat.accept_suggestion(self.conn, subject["id"], only=True)
        self.assertEqual(self.paths("3"), ["Horror & Gothic"])

    def test_rejecting_a_card_rejects_every_source(self):
        cat.reject_suggestion(self.conn, self.horror_card()["id"])
        cat.categorize(self.conn)
        targets = [c["target"] for c in cat.list_suggestion_cards(self.conn)]
        self.assertNotIn("Genre: Horror & Gothic", targets)
        self.assertEqual(len(cat.list_suggestions(self.conn, status="rejected")), 3)


class PrimaryDefaultTests(CategorizeTestCase):
    """Settle primaries without asking where the evidence allows."""

    def map_all(self) -> None:
        cat.categorize(self.conn)
        for card in cat.list_suggestion_cards(self.conn):
            if card["kind"] == "map" and not card["target"].startswith("new "):
                cat.accept_suggestion(self.conn, card["id"])

    def questions(self) -> list[dict]:
        return [c for c in cat.list_suggestion_cards(self.conn) if c["kind"] == "primary"]

    def set_subjects(self, goodreads_id: str, values: list[str]) -> None:
        self.conn.execute(
            "UPDATE books SET subjects_json = ? WHERE goodreads_id = ?", (json.dumps(values), goodreads_id)
        )
        self.conn.commit()

    def test_your_shelf_beats_an_open_library_subject(self):
        self.sync([book("1", "Piranesi", "read, fantasy")])
        self.set_subjects("1", ["Mystery general"])
        self.map_all()
        self.assertEqual(self.primary("1"), "Fantasy")
        self.assertEqual(self.questions(), [])

    def test_sibling_subgenres_default_to_their_parent(self):
        self.sync([book("1", "Neuromancer in Space", "read, cyberpunk, space-opera")])
        self.map_all()
        self.assertEqual(self.primary("1"), "Science Fiction")
        self.assertEqual(self.questions(), [])
        detail = cat.book_categories(self.conn, self.book_id("1"))
        derived = detail["primary"]
        self.assertEqual(derived["source"], "derived")
        self.assertIn("Cyberpunk", derived["evidence"])

    def test_derived_default_goes_when_evidence_settles(self):
        self.sync([book("1", "Neuromancer in Space", "read, cyberpunk, space-opera")])
        self.map_all()
        self.sync([book("1", "Neuromancer in Space", "read, cyberpunk")], name="later.csv")
        cat.categorize(self.conn)
        self.assertEqual(self.primary("1"), "Science Fiction > Cyberpunk")
        self.assertEqual(self.paths("1"), ["Science Fiction > Cyberpunk"])

    def test_derived_parent_becomes_a_rule_row_when_mapped_directly(self):
        self.sync([book("1", "Neuromancer in Space", "read, cyberpunk, space-opera")])
        self.map_all()
        self.sync([book("1", "Neuromancer in Space", "read, cyberpunk, space-opera, sci-fi")], name="later.csv")
        self.map_all()
        detail = cat.book_categories(self.conn, self.book_id("1"))
        self.assertEqual(detail["primary"]["path"], "Science Fiction")
        self.assertEqual(detail["primary"]["source"], "rule")

    def test_unrelated_genres_are_picked_without_asking(self):
        self.sync([book("1", "The Name of the Rose", "read, mystery, historical-fiction")])
        self.map_all()
        self.assertIn(self.primary("1"), ("Crime & Mystery", "Historical Fiction"))
        self.assertEqual(self.questions(), [])

    def test_user_primary_and_removals_are_respected(self):
        self.sync([book("1", "Neuromancer in Space", "read, cyberpunk, space-opera")])
        self.map_all()
        book_id = self.book_id("1")
        cat.remove_book_category(self.conn, book_id, "Science Fiction")
        # Removing the derived default: it is not derived back; one of the
        # subgenres is picked instead, without a question.
        cat.categorize(self.conn)
        self.assertIn(self.primary("1"), ("Science Fiction > Cyberpunk", "Science Fiction > Space Opera"))
        self.assertEqual(self.questions(), [])
        cat.set_primary_genre(self.conn, book_id, "Space Opera")
        cat.categorize(self.conn)
        self.assertEqual(self.primary("1"), "Science Fiction > Space Opera")
        self.assertEqual(self.questions(), [])


class ShelfTagTests(CategorizeTestCase):
    """Shelves that aren't genres become the user's tags."""

    def test_tag_rule_tags_once_and_follows_new_books(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "stoicism")["id"])
        self.assertEqual(self.tags("5"), ["stoicism"])
        # Removing the tag sticks: the rule already applied to this book.
        db.update_local_fields(self.conn, "5", {"tags_json": []})
        cat.categorize(self.conn)
        self.assertEqual(self.tags("5"), [])
        # A new book on the same shelf is tagged on the next sync.
        self.sync([*LIBRARY, book("9", "Letters from a Stoic", "to-read, stoicism")], name="later.csv")
        result = cat.categorize(self.conn)
        self.assertEqual(result["tagged"], 1)
        self.assertEqual(self.tags("9"), ["stoicism"])
        # Unmapping stops future tagging but leaves the tags the user has.
        rule = next(r for r in cat.list_rules(self.conn) if r["tag"] == "stoicism")
        self.assertEqual((rule["category"], rule["books"]), ("Tag: #stoicism", 2))
        cat.delete_rule(self.conn, rule["id"])
        self.assertEqual(self.tags("9"), ["stoicism"])

    def test_manual_tag_rule_and_accept_as_tag(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        outcome = cat.add_rule(self.conn, "shelf", "favorites", "tag:Keepers")
        self.assertEqual((outcome["category"], outcome["books"]), ("Tag: #keepers", 1))
        self.assertEqual(self.tags("3"), ["keepers"])
        cat.accept_suggestion(self.conn, self.proposal("shelf", "philosophy")["id"], as_category="tag:philosophy")
        self.assertEqual(self.tags("5"), ["philosophy"])
        self.assertEqual(self.paths("5"), [])


class FictionGuardTests(CategorizeTestCase):
    """Open Library subjects alone can't put a novel in a nonfiction genre."""

    def setUp(self) -> None:
        super().setUp()
        self.sync([
            book("1", "A Brief History of Seven Killings", "read"),
            book("2", "The Guns of August", "read"),
            book("3", "Wolf Hall", "read, history"),
            book("4", "The Blind Assassin", "read"),
        ])
        subjects = {
            "1": ["Fiction", "History"],
            "2": ["History", "World War, 1914-1918"],
            "3": ["Fiction", "History"],
            "4": ["Fiction", "History"],
        }
        for gid, values in subjects.items():
            self.conn.execute("UPDATE books SET subjects_json = ? WHERE goodreads_id = ?", (json.dumps(values), gid))
        self.conn.commit()
        cat.categorize(self.conn)
        for card in cat.list_suggestion_cards(self.conn):
            if card["target"] in ("Form: Fiction", "Genre: History"):
                cat.accept_suggestion(self.conn, card["id"])

    def test_novels_stay_out_without_a_question(self):
        self.assertEqual(self.paths("2"), ["History"])  # nonfiction: filed directly
        self.assertEqual(self.paths("3"), ["History"])  # your own shelf says so
        self.assertEqual(self.paths("1"), [])
        self.assertEqual(self.paths("4"), [])
        self.assertFalse([c for c in cat.list_suggestion_cards(self.conn) if c["kind"] == "assign"])

    def test_adding_it_by_hand_sticks(self):
        cat.add_book_category(self.conn, self.book_id("1"), "History")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("1"), ["History"])


class AliasAndSeedTests(CategorizeTestCase):
    def test_removing_an_alias_withdraws_suggestions_that_needed_it(self):
        self.sync([book("1", "Slaughterhouse-Five", "read")])
        self.conn.execute(
            "UPDATE books SET subjects_json = ? WHERE goodreads_id = '1'", (json.dumps(["Time loops"]),)
        )
        self.conn.commit()
        cat.add_alias(self.conn, "Science Fiction", "time loops")
        self.assertEqual(self.proposal("subject", "time loops")["target"], "Genre: Science Fiction")
        cat.remove_alias(self.conn, "Science Fiction", "time loops")
        values = [i["match_value"] for i in cat.list_suggestions(self.conn)]
        self.assertNotIn("time loops", values)
        with self.assertRaises(cat.CategoryError):
            cat.remove_alias(self.conn, "Science Fiction", "time loops")

    def old_seed(self) -> dict[str, int]:
        """Replace the taxonomy with the shape revision 2 seeded (a subset of it)."""
        self.conn.execute("DELETE FROM category_aliases")
        self.conn.execute("DELETE FROM categories")
        ids: dict[str, int] = {}
        tree = [
            ("Speculative Fiction", None), ("Science Fiction", "Speculative Fiction"),
            ("Space Opera", "Science Fiction"), ("Cyberpunk", "Science Fiction"),
            ("Fantasy", "Speculative Fiction"), ("Horror", "Speculative Fiction"),
            ("Literary Fiction", None), ("Classics", "Literary Fiction"),
            ("Mystery & Crime", None), ("Thriller", "Mystery & Crime"),
            ("History", None), ("Military History", "History"), ("Romance", None),
        ]
        for label, parent in tree:
            cur = self.conn.execute(
                "INSERT INTO categories (facet, slug, label, parent_id, origin) VALUES ('genre', ?, ?, ?, 'seed')",
                (slugify(label), label, ids.get(parent)),
            )
            ids[label] = int(cur.lastrowid)
        for label, alias in (("Military History", "ww2"), ("Literary Fiction", "literature"), ("Horror", "horror tales")):
            self.conn.execute("INSERT INTO category_aliases (category_id, alias) VALUES (?, ?)", (ids[label], alias))
        self.conn.execute("UPDATE adso_meta SET value = '2' WHERE key = 'taxonomy_seed_rev'")
        self.conn.commit()
        return ids

    def test_revision_three_reshapes_an_older_taxonomy(self):
        self.sync([
            book("1", "Leviathan Wakes", "read, space-opera"),
            book("2", "Anna Karenina", "read"),
            book("3", "Carrie", "read"),
        ])
        ids = self.old_seed()
        cat.add_rule(self.conn, "shelf", "space-opera", "Space Opera")  # used: stays
        cat.add_book_category(self.conn, self.book_id("2"), "Classics")  # used: stays
        self.conn.execute(
            "UPDATE books SET subjects_json = ?, original_publication_year = 1878 WHERE goodreads_id = '2'",
            (json.dumps(["Russian literature", "New York Times reviewed"]),),
        )
        self.conn.commit()

        db.initialize(self.conn)
        taxonomy = cat.Taxonomy(self.conn)
        labels = {node.label for node in taxonomy.by_id.values()}
        # Unused old categories are gone; renamed ones keep their id and move up.
        for gone in ("Speculative Fiction", "Literary Fiction", "Military History", "Cyberpunk", "Romance"):
            self.assertNotIn(gone, labels)
        self.assertEqual(taxonomy.path(ids["Horror"]), "Horror & Gothic")
        self.assertEqual(taxonomy.path(ids["Thriller"]), "Thriller & Espionage")
        self.assertEqual(taxonomy.path(ids["Mystery & Crime"]), "Crime & Mystery")
        self.assertEqual(taxonomy.path(ids["Science Fiction"]), "Science Fiction")
        self.assertEqual(taxonomy.path(ids["Space Opera"]), "Science Fiction > Space Opera")
        self.assertEqual(taxonomy.path(ids["Classics"]), "Classics")
        self.assertEqual(taxonomy.get(taxonomy.match_term("horror tales")[0]).label, "Horror & Gothic")
        self.assertEqual(taxonomy.get(taxonomy.match_term("ww2")[0]).label, "World War II")
        for new in ("Novel of Ideas", "Love Stories", "Russian", "19th Century", "Diaries & Letters"):
            self.assertIn(new, labels)
        self.assertEqual(self.paths("1"), ["Science Fiction > Space Opera"])
        self.assertEqual(self.paths("2"), ["Classics"])
        # The engine re-ran against the new tree: era filled in, tradition proposed, noise ignored.
        self.assertEqual(self.paths("2", "era"), ["19th Century"])
        self.assertEqual(self.proposal("subject", "russian literature")["target"], "Tradition: Russian")
        values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        self.assertNotIn("new york times reviewed", values)
        # Once only.
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM adso_meta WHERE key = 'taxonomy_recategorize'").fetchone()
        )
        count = self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        db.initialize(self.conn)
        self.assertEqual(count, self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0])

    def test_phase_one_rules_table_is_migrated_without_losing_assignments(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "sci fi")["id"])
        rule_id = cat.list_rules(self.conn)[0]["id"]
        # Rebuild category_rules in its phase-1 shape (no tag, NOT NULL category).
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.executescript(
            """
            DROP INDEX IF EXISTS idx_category_rules_identity;
            CREATE TABLE old_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, match_kind TEXT NOT NULL, match_value TEXT NOT NULL,
                category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                created_by TEXT NOT NULL DEFAULT 'cli', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(match_kind, match_value, category_id));
            INSERT INTO old_rules SELECT id, match_kind, match_value, category_id, created_by, created_at
                FROM category_rules;
            DROP TABLE tag_rule_applications;
            DROP TABLE category_rules;
            ALTER TABLE old_rules RENAME TO category_rules;
            """
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        db.initialize(self.conn)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(category_rules)")}
        self.assertIn("tag", columns)
        self.assertEqual(cat.list_rules(self.conn)[0]["id"], rule_id)
        self.assertEqual(self.paths("1"), ["Science Fiction"])
        self.assertEqual(cat.list_rules(self.conn)[0]["books"], 2)

    def test_examples_are_the_most_recently_added(self):
        rows = [dict(book(str(i), f"Title {i}", "read, obscure-shelf"), **{"Date Added": f"2024/01/{i:02d}"})
                for i in range(1, 6)]
        self.sync(rows)
        cat.categorize(self.conn)
        self.assertEqual(self.proposal("shelf", "obscure shelf")["evidence"], "e.g. Title 5; Title 4; Title 3")


class EraAndNoiseTests(CategorizeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sync([
            book("1", "Anna Karenina", "read, attempted"),
            book("2", "Mrs Dalloway", "read"),
            book("3", "No Year", "to-read"),
        ])
        for gid, year in (("1", 1878), ("2", 1925)):
            self.conn.execute("UPDATE books SET original_publication_year = ? WHERE goodreads_id = ?", (year, gid))
        self.conn.execute(
            "UPDATE books SET subjects_json = ? WHERE goodreads_id = '1'",
            (json.dumps(["New York Times reviewed", "open_syllabus_project", "Russian literature", "General"]),),
        )
        self.conn.commit()
        cat.categorize(self.conn)

    def test_era_comes_from_the_original_publication_year(self):
        self.assertEqual(self.paths("1", "era"), ["19th Century"])
        self.assertEqual(self.paths("2", "era"), ["Modernist"])
        self.assertEqual(self.paths("3", "era"), [])
        entry = cat.book_categories(self.conn, self.book_id("1"))["by_facet"]["era"][0]
        self.assertEqual((entry["source"], entry["evidence"]), ("year", "First published 1878"))
        # It follows a corrected year.
        self.conn.execute("UPDATE books SET original_publication_year = 1995 WHERE goodreads_id = '2'")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("2", "era"), ["Contemporary"])

    def test_your_era_wins_and_removals_stick(self):
        cat.add_book_category(self.conn, self.book_id("1"), "era:Modernist")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("1", "era"), ["Modernist"])
        cat.remove_book_category(self.conn, self.book_id("2"), "era:Modernist")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("2", "era"), [])

    def test_nothing_maps_to_an_era(self):
        self.conn.execute(
            "UPDATE books SET subjects_json = ? WHERE goodreads_id = '2'", (json.dumps(["19th century"]),)
        )
        self.conn.commit()
        cat.categorize(self.conn)
        values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        self.assertNotIn("19th century", values)
        with self.assertRaises(cat.CategoryError):
            cat.add_rule(self.conn, "shelf", "attempted", "era:Modernist")
        card = self.proposal("subject", "russian literature")
        with self.assertRaises(cat.CategoryError):
            cat.accept_suggestion(self.conn, card["id"], as_category="era:Modernist")

    def test_era_rules_from_before_are_dropped(self):
        era = cat.Taxonomy(self.conn).resolve("era:Contemporary").id
        cat._insert_rule(self.conn, "subject", "russian literature", era, actor="cli")
        self.conn.commit()
        cat.categorize(self.conn)  # an era rule no longer applies; the year still does
        self.assertEqual(self.paths("1", "era"), ["19th Century"])
        db.initialize(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM category_rules WHERE category_id = ?", (era,)).fetchone()[0], 0
        )

    def test_noise_subjects_and_dnf_shelves_raise_nothing(self):
        values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        for noise in ("new york times reviewed", "open syllabus project", "general", "attempted"):
            self.assertNotIn(noise, values)
        self.assertEqual(self.proposal("subject", "russian literature")["target"], "Tradition: Russian")


class ExportTests(CategorizeTestCase):
    def test_exports_carry_categories_and_series(self):
        import csv as csv_module

        from adso.exports import catalogue_csv_string, catalogue_json_string

        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "sci fi")["id"])
        cat.add_book_category(self.conn, self.book_id("1"), "theme:" + cat.add_category(self.conn, "theme:Belters").label)
        rows = {r["goodreads_id"]: r for r in csv_module.DictReader(io.StringIO(catalogue_csv_string(self.conn)))}
        self.assertEqual(rows["1"]["primary_genre"], "Science Fiction")
        self.assertEqual(rows["1"]["categories"], "Genre: Science Fiction; Theme: Belters")
        self.assertEqual((rows["1"]["series"], rows["1"]["series_position"]), ("The Expanse", "1"))
        self.assertEqual(rows["4"]["categories"], "")
        data = {b["goodreads_id"]: b for b in json.loads(catalogue_json_string(self.conn))}
        self.assertEqual(data["2"]["series"], {"name": "The Expanse", "position": 2.0})
        self.assertEqual(data["2"]["categories"], {"genre": ["Science Fiction"]})


class MergeDeleteTests(CategorizeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "space opera")["id"])

    def test_merge_moves_books_rules_and_keeps_alias(self):
        result = cat.merge_categories(self.conn, "Space Opera", "Science Fiction")
        self.assertEqual(result["books"], 2)
        self.assertEqual(self.paths("1"), ["Science Fiction"])
        self.assertEqual(self.primary("1"), "Science Fiction")
        self.assertEqual(cat.list_rules(self.conn)[0]["category"], "Genre: Science Fiction")
        self.assertEqual(cat.Taxonomy(self.conn).resolve("space opera").label, "Science Fiction")
        # The rule still applies to new books after the merge.
        self.sync([*LIBRARY, book("7", "Revelation Space", "to-read, space-opera")], name="new.csv")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("7"), ["Science Fiction"])

    def test_merge_refuses_other_facets_and_descendants(self):
        with self.assertRaises(cat.CategoryError):
            cat.merge_categories(self.conn, "Science Fiction", "Space Opera")
        with self.assertRaises(cat.CategoryError):
            cat.merge_categories(self.conn, "Space Opera", "form:Fiction")

    def test_delete_removes_assignments_and_lifts_children(self):
        result = cat.delete_category(self.conn, "Science Fiction")
        self.assertEqual(result["children"], 2)
        taxonomy = cat.Taxonomy(self.conn)
        self.assertEqual(taxonomy.path(taxonomy.resolve("Space Opera").id), "Space Opera")
        cat.delete_category(self.conn, "Space Opera")
        self.assertEqual(self.paths("1"), [])
        self.assertEqual(cat.list_rules(self.conn), [])

    def test_merging_duplicate_books_keeps_categories(self):
        db.merge_books(self.conn, keep_id=self.book_id("3"), drop_id=self.book_id("1"))
        self.conn.commit()
        self.assertEqual(self.paths("3"), ["Science Fiction > Space Opera"])
        orphans = self.conn.execute(
            "SELECT COUNT(*) FROM book_categories WHERE book_id NOT IN (SELECT id FROM books)"
        ).fetchone()[0]
        self.assertEqual(orphans, 0)


class SeriesTests(CategorizeTestCase):
    def test_parse_series(self):
        cases = {
            "Leviathan Wakes (The Expanse, #1)": ("The Expanse", 1.0),
            "The Colour of Magic (Discworld, #1; Rincewind #1)": ("Discworld", 1.0),
            "Dune Messiah (Dune #2)": ("Dune", 2.0),
            "Edge of Empire (Star Wars, #0.5)": ("Star Wars", 0.5),
            "The Hobbit (Vintage International)": None,
            "Plain Title": None,
            "Omnibus (Hannibal Lecter, #1-3)": ("Hannibal Lecter", 1.0),
        }
        for title, expected in cases.items():
            self.assertEqual(cat.parse_series(title), expected, title)

    def test_series_from_titles_and_user_override_sticks(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        self.assertEqual(cat.book_series(self.conn, self.book_id("2"))["name"], "The Expanse")
        titles = [b["title"] for b in list_books(self.conn, BookFilters(series="the expanse"))]
        self.assertEqual(titles, ["Leviathan Wakes (The Expanse, #1)", "Caliban's War (The Expanse, #2)"])

        cat.set_book_series(self.conn, self.book_id("2"), None)
        cat.set_book_series(self.conn, self.book_id("3"), "Legends & Lattes", 1)
        cat.categorize(self.conn)
        self.assertIsNone(cat.book_series(self.conn, self.book_id("2")))
        self.assertEqual(cat.book_series(self.conn, self.book_id("3"))["position"], 1.0)


class FilterTests(CategorizeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        for value in ("sci fi", "space opera", "cozy fantasy"):
            cat.accept_suggestion(self.conn, self.proposal("shelf", value)["id"])

    def titles(self, **kwargs) -> list[str]:
        return sorted(b["title"] for b in list_books(self.conn, BookFilters(**kwargs)))

    def test_category_filter_rolls_up_children(self):
        self.assertEqual(self.titles(category="Fantasy"), ["Legends & Lattes"])  # via Cozy Fantasy
        self.assertEqual(len(self.titles(category="Science Fiction")), 2)
        self.assertEqual(len(self.titles(category="Space Opera")), 2)

    def test_unknown_category_is_an_error(self):
        with self.assertRaises(cat.CategoryError):
            list_books(self.conn, BookFilters(category="Nonexistent"))

    def test_goodreads_shelf_filter(self):
        self.assertEqual(self.titles(gr_shelf="favorites"), ["Legends & Lattes"])

    def test_taxonomy_tree_counts(self):
        tree = {facet["facet"]: facet for facet in cat.taxonomy_tree(self.conn)}
        nodes = {node["label"]: node for node in tree["genre"]["categories"]}
        self.assertEqual(nodes["Fantasy"]["total"], 1)
        self.assertEqual(nodes["Fantasy"]["books"], 0)
        self.assertEqual(nodes["Science Fiction"]["total"], 2)


class CliTests(CategorizeTestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["--db", str(self.db_path), *argv])
        return code, out.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self.sync(LIBRARY)

    def test_categorize_review_and_show(self):
        code, out = self.run_cli("categorize")
        self.assertEqual(code, 0)
        self.assertIn("mapping proposal", out)
        _, listing = self.run_cli("review")
        self.assertIn("Genre: Science Fiction — 2 book(s)", listing)
        self.assertIn('from shelf "sci fi"', listing)
        sid = self.proposal("shelf", "sci fi")["id"]
        code, out = self.run_cli("review", str(sid), "--accept")
        self.assertEqual(code, 0)
        self.assertIn("now applied to 2 book(s)", out)
        _, detail = self.run_cli("show", "1")
        self.assertIn("Primary Genre: Science Fiction", detail)
        self.assertIn("Series: The Expanse #1", detail)
        _, listed = self.run_cli("list", "--category", "Science Fiction")
        self.assertIn("Leviathan Wakes", listed)
        self.assertNotIn("Meditations", listed)

    def test_edit_categories_and_series(self):
        code, out = self.run_cli(
            "edit", "5", "--genre", "Philosophy", "--add-category", "form:Nonfiction",
            "--series", "none",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.primary("5"), "Philosophy")
        self.assertEqual(self.paths("5", "form"), ["Nonfiction"])

    def test_review_only_flag(self):
        self.run_cli("categorize")
        sid = self.proposal("shelf", "stoicism")["id"]
        code, out = self.run_cli("review", str(sid), "--only", "--reject")
        self.assertEqual(code, 0, out)
        self.assertIn(f'Rejected [{sid}] Goodreads shelf "stoicism"', out)

    def test_delete_needs_confirmation(self):
        _, out = self.run_cli("taxonomy", "delete", "Horror")
        self.assertIn("Nothing changed", out)
        self.assertTrue(cat.Taxonomy(self.conn).resolve("Horror"))
        self.run_cli("taxonomy", "delete", "Horror", "--yes")
        with self.assertRaises(cat.CategoryError):
            cat.Taxonomy(self.conn).resolve("Horror")

    def test_map_rules_and_unknown_category_error(self):
        code, out = self.run_cli("taxonomy", "map", "--shelf", "favorites", "--to", "Cozy Fantasy")
        self.assertEqual(code, 0, out)
        self.assertIn("(1 book(s))", out)
        _, rules = self.run_cli("taxonomy", "rules")
        self.assertIn("favorites", rules)
        code, _ = self.run_cli("edit", "1", "--genre", "Nope")
        self.assertEqual(code, 1)

    def test_taxonomy_list(self):
        _, out = self.run_cli("taxonomy", "list", "--facet", "genre")
        self.assertIn("Psychological Fiction", out)
        self.assertNotIn("Form (form)", out)


if __name__ == "__main__":
    unittest.main()
