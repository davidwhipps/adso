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
        cat.delete_category(self.conn, "Cyberpunk")
        db.initialize(self.conn)
        with self.assertRaises(cat.CategoryError):
            cat.Taxonomy(self.conn).resolve("Cyberpunk")

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
                         "Speculative Fiction > Science Fiction > Space Opera")

    def test_ambiguous_reference_lists_candidates(self):
        cat.add_category(self.conn, "Mystery & Crime > Cozy")
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
            cat.move_category(self.conn, "Speculative Fiction", "Space Opera")
        moved = cat.move_category(self.conn, "Horror", None)
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
            self.proposal("shelf", "stoicism")["target"], "new Theme: Stoicism"
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
        self.assertEqual(self.paths("1"), ["Speculative Fiction > Science Fiction"])

        self.sync([*LIBRARY, book("6", "Dune", "to-read, sci-fi")], name="later.csv")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("6"), ["Speculative Fiction > Science Fiction"])
        self.assertEqual(self.primary("6"), "Speculative Fiction > Science Fiction")
        pending_values = [item["match_value"] for item in cat.list_suggestions(self.conn)]
        self.assertNotIn("sci fi", pending_values)

    def test_accept_as_redirects_and_new_theme_is_created(self):
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "stoicism")["id"])
        self.assertEqual(self.paths("5", "theme"), ["Stoicism"])
        cat.accept_suggestion(
            self.conn, self.proposal("shelf", "philosophy")["id"], as_category="Religion & Spirituality"
        )
        self.assertEqual(self.paths("5"), ["Religion & Spirituality"])

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
        self.assertEqual(self.paths("1"), ["Speculative Fiction > Science Fiction"])

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
            "Genre: Speculative Fiction > Science Fiction",
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
        self.assertEqual(items["lit fic"], "Genre: Literary Fiction")
        self.assertEqual(items["whodunnit"], "Genre: Mystery & Crime")
        self.assertEqual(items["popsci"], "Genre: Science")
        self.assertEqual(items["ww2"], "Genre: History > Military History")

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
        self.assertEqual(self.primary("1"), "Speculative Fiction > Science Fiction")
        self.accept("space opera")
        self.assertEqual(self.primary("1"), "Speculative Fiction > Science Fiction > Space Opera")
        self.assertEqual(self.primary_questions(), [])

    def test_competing_genres_raise_one_question(self):
        self.accept("mystery")
        self.accept("historical fiction")
        questions = self.primary_questions()
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["target"], "Genre: Mystery & Crime")
        self.assertIn("Historical Fiction", questions[0]["evidence"])
        cat.categorize(self.conn)
        self.assertEqual(len(self.primary_questions()), 1)

    def test_rejecting_the_pick_asks_about_the_next_candidate(self):
        self.accept("mystery")
        self.accept("historical fiction")
        cat.reject_suggestion(self.conn, self.primary_questions()[0]["id"])
        self.assertIsNone(self.primary("4"))
        self.assertEqual(self.primary_questions()[0]["target"], "Genre: Historical Fiction")
        cat.accept_suggestion(self.conn, self.primary_questions()[0]["id"])
        self.assertEqual(self.primary("4"), "Historical Fiction")
        self.assertEqual(self.primary_questions(), [])

    def test_user_primary_is_final(self):
        self.accept("sci fi")
        cat.set_primary_genre(self.conn, self.book_id("1"), "Science Fiction")
        self.accept("space opera")
        self.assertEqual(self.primary("1"), "Speculative Fiction > Science Fiction")

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


class MergeDeleteTests(CategorizeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sync(LIBRARY)
        cat.categorize(self.conn)
        cat.accept_suggestion(self.conn, self.proposal("shelf", "space opera")["id"])

    def test_merge_moves_books_rules_and_keeps_alias(self):
        result = cat.merge_categories(self.conn, "Space Opera", "Science Fiction")
        self.assertEqual(result["books"], 2)
        self.assertEqual(self.paths("1"), ["Speculative Fiction > Science Fiction"])
        self.assertEqual(self.primary("1"), "Speculative Fiction > Science Fiction")
        self.assertEqual(cat.list_rules(self.conn)[0]["category"], "Genre: Speculative Fiction > Science Fiction")
        self.assertEqual(cat.Taxonomy(self.conn).resolve("space opera").label, "Science Fiction")
        # The rule still applies to new books after the merge.
        self.sync([*LIBRARY, book("7", "Revelation Space", "to-read, space-opera")], name="new.csv")
        cat.categorize(self.conn)
        self.assertEqual(self.paths("7"), ["Speculative Fiction > Science Fiction"])

    def test_merge_refuses_other_facets_and_descendants(self):
        with self.assertRaises(cat.CategoryError):
            cat.merge_categories(self.conn, "Science Fiction", "Space Opera")
        with self.assertRaises(cat.CategoryError):
            cat.merge_categories(self.conn, "Space Opera", "form:Fiction")

    def test_delete_removes_assignments_and_lifts_children(self):
        result = cat.delete_category(self.conn, "Science Fiction")
        self.assertEqual(result["children"], 4)
        taxonomy = cat.Taxonomy(self.conn)
        self.assertEqual(taxonomy.path(taxonomy.resolve("Space Opera").id), "Speculative Fiction > Space Opera")
        cat.delete_category(self.conn, "Space Opera")
        self.assertEqual(self.paths("1"), [])
        self.assertEqual(cat.list_rules(self.conn), [])

    def test_merging_duplicate_books_keeps_categories(self):
        db.merge_books(self.conn, keep_id=self.book_id("3"), drop_id=self.book_id("1"))
        self.conn.commit()
        self.assertEqual(self.paths("3"), ["Speculative Fiction > Science Fiction > Space Opera"])
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
        self.assertEqual(len(self.titles(category="Speculative Fiction")), 3)
        self.assertEqual(self.titles(category="Fantasy"), ["Legends & Lattes"])
        self.assertEqual(len(self.titles(category="Space Opera")), 2)

    def test_unknown_category_is_an_error(self):
        with self.assertRaises(cat.CategoryError):
            list_books(self.conn, BookFilters(category="Nonexistent"))

    def test_goodreads_shelf_filter(self):
        self.assertEqual(self.titles(gr_shelf="favorites"), ["Legends & Lattes"])

    def test_taxonomy_tree_counts(self):
        tree = {facet["facet"]: facet for facet in cat.taxonomy_tree(self.conn)}
        nodes = {node["label"]: node for node in tree["genre"]["categories"]}
        self.assertEqual(nodes["Speculative Fiction"]["total"], 3)
        self.assertEqual(nodes["Speculative Fiction"]["books"], 0)
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
        self.assertIn('Goodreads shelf "sci fi" (2 book(s))', listing)
        sid = self.proposal("shelf", "sci fi")["id"]
        code, out = self.run_cli("review", str(sid), "--accept")
        self.assertEqual(code, 0)
        self.assertIn("now applied to 2 book(s)", out)
        _, detail = self.run_cli("show", "1")
        self.assertIn("Primary Genre: Speculative Fiction > Science Fiction", detail)
        self.assertIn("Series: The Expanse #1", detail)
        _, listed = self.run_cli("list", "--category", "Speculative Fiction")
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
        self.assertIn("Speculative Fiction", out)
        self.assertNotIn("Form (form)", out)


if __name__ == "__main__":
    unittest.main()
