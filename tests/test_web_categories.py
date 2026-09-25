"""Web UI for categories: library facets, the book block, Review cards, Categories page.

Skipped unless the optional web test stack (``httpx`` for the FastAPI
``TestClient``) is installed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adso import categorize as cat
from adso import db

try:  # TestClient requires httpx; the web extra may not be installed.
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient

    _HAS_TESTCLIENT = True
except Exception:  # pragma: no cover - exercised only without the web extra
    _HAS_TESTCLIENT = False

BOOKS = [
    # goodreads_id, title, shelves
    ("1", "Leviathan Wakes (The Expanse, #1)", ["read", "sci-fi", "space-opera"]),
    ("2", "Caliban's War (The Expanse, #2)", ["to-read", "sci-fi", "space-opera"]),
    ("3", "Legends & Lattes", ["read", "cozy-fantasy"]),
    ("4", "Meditations", ["to-read", "stoicism"]),
    ("5", "It", ["read", "horror"]),
]


def _seed(db_path: str) -> None:
    conn = db.connect(db_path)
    db.initialize(conn)
    cat.add_category(conn, "Science Fiction > Space Opera")
    run = db.create_import_run(conn, source="goodreads", source_path="x.csv", mode="import", row_count=len(BOOKS))
    for gid, title, shelves in BOOKS:
        db.insert_book_from_goodreads(
            conn,
            {"goodreads_id": gid, "title": title, "author": "Author", "exclusive_shelf": shelves[0],
             "shelves_json": json.dumps(shelves), "date_added": "2024-01-01"},
            import_run_id=run,
        )
    conn.execute(
        "UPDATE books SET subjects_json = ? WHERE goodreads_id = '3'", (json.dumps(["Horror tales"]),)
    )
    conn.commit()
    cat.categorize(conn)
    conn.close()


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class WebCategoryTests(unittest.TestCase):
    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        _seed(self.db_path)
        self.client = TestClient(create_app(self.db_path))
        self.conn = db.connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def card(self, target: str) -> dict:
        return next(c for c in cat.list_suggestion_cards(self.conn) if c["target"] == target)

    def accept(self, target: str) -> None:
        response = self.client.post(f"/categories/suggestions/{self.card(target)['id']}/accept")
        self.assertEqual(response.status_code, 200)

    def book_id(self, gid: str) -> int:
        return int(self.conn.execute("SELECT id FROM books WHERE goodreads_id = ?", (gid,)).fetchone()[0])

    def category_id(self, label: str) -> int:
        return int(self.conn.execute("SELECT id FROM categories WHERE label = ?", (label,)).fetchone()[0])

    # --- Review --------------------------------------------------------------

    def test_review_lists_category_cards_and_counts_them_in_the_badge(self):
        body = self.client.get("/review").text
        self.assertIn('id="categories"', body)
        self.assertIn("Genre: Horror &amp; Gothic", body)
        self.assertIn("2 books", body)  # shelf horror (It) + subject "Horror tales" (Legends)
        # sci-fi, space opera, cozy fantasy, stoicism, horror (shelf + subject = one card)
        self.assertIn('class="badge-destructive">5<', body)

    def test_accepting_a_card_applies_its_rules_and_updates_the_badge(self):
        card = self.card("Genre: Horror & Gothic")
        response = self.client.post(f"/categories/suggestions/{card['id']}/accept")
        self.assertIn("now on 2 book(s)", response.text)
        self.assertIn('id="nav-more-badge" hx-swap-oob="true"', response.text)
        self.assertEqual(len(cat.list_rules(self.conn)), 2)

    def test_accept_as_another_category(self):
        card = self.card("Tag: #stoicism")
        self.client.post(f"/categories/suggestions/{card['id']}/accept", data={"as_category": "genre:Philosophy"})
        genres = cat.book_categories(self.conn, self.book_id("4"))["by_facet"]["genre"]
        self.assertEqual([g["path"] for g in genres], ["Philosophy"])

    def test_rejecting_one_source_rerenders_the_rest_of_the_card(self):
        card = self.card("Genre: Horror & Gothic")
        subject = next(m for m in card["members"] if m["match_kind"] == "subject")
        response = self.client.post(f"/categories/suggestions/{subject['id']}/reject", data={"only": "1"})
        self.assertIn('class="card catcard"', response.text)
        self.assertIn("shelf “horror”", response.text)
        self.assertNotIn("Horror tales", response.text)

    def test_rejecting_a_card(self):
        card = self.card("Tag: #stoicism")
        response = self.client.post(f"/categories/suggestions/{card['id']}/reject")
        self.assertIn("be suggested for these again", response.text)
        self.assertEqual(self.client.post(f"/categories/suggestions/{card['id']}/reject").status_code, 400)

    def test_primary_question_offers_its_genres(self):
        self.accept("Genre: Science Fiction")
        self.accept("Genre: Horror & Gothic")
        cat.add_book_category(self.conn, self.book_id("5"), "genre:History")
        body = self.client.get("/review").text
        self.assertIn("Which one leads?", body)
        question = next(c for c in cat.list_suggestion_cards(self.conn) if c["kind"] == "primary")
        self.client.post(f"/categories/suggestions/{question['id']}/accept", data={"as_category": "genre:History"})
        self.assertEqual(cat.book_categories(self.conn, self.book_id("5"))["primary"]["path"], "History")

    # --- Library --------------------------------------------------------------

    def test_sidebar_and_filters(self):
        self.accept("Genre: Science Fiction")
        body = self.client.get("/").text
        self.assertIn("Genres", body)
        # Goodreads shelves feed genres and tags; they aren't a sidebar taxonomy.
        self.assertNotIn("Goodreads shelves", body)
        speculative = self.category_id("Science Fiction")
        self.assertIn(f"/?category={speculative}", body)
        filtered = self.client.get(f"/?category={speculative}").text
        self.assertIn("Leviathan Wakes", filtered)
        self.assertNotIn("Meditations", filtered)
        self.conn.execute("UPDATE books SET original_publication_year = 1869 WHERE goodreads_id = '1'")
        self.conn.commit()
        cat.categorize(self.conn)
        self.assertIn("19th Century", self.client.get("/").text.split("Eras", 1)[1])
        review = self.client.get("/review").text
        self.assertIn('value="genre:Fantasy"', review)
        self.assertNotIn('value="era:19th Century"', review)  # nothing maps to an era
        self.assertIn("Era: 19th Century", self.client.get("/book/1").text)  # but a book can be given one
        shelf = self.client.get("/?gr_shelf=cozy-fantasy").text
        self.assertIn("Legends &amp; Lattes", shelf)
        self.assertNotIn("Leviathan Wakes", shelf)
        series = self.client.get("/?series=The+Expanse").text
        self.assertLess(series.index("Leviathan Wakes"), series.index("Caliban&#39;s War"))
        self.assertIn("Series: The Expanse", series)

    def test_unknown_category_param_is_ignored(self):
        self.assertEqual(self.client.get("/?category=99999").status_code, 200)
        self.assertEqual(self.client.get("/?category=abc").status_code, 200)

    # --- Book block -----------------------------------------------------------

    def test_book_page_shows_categories_and_series_strip(self):
        self.accept("Genre: Science Fiction")
        body = self.client.get("/book/1").text
        self.assertIn('id="detail-cats-1"', body)
        self.assertIn("Primary genre", body)
        self.assertIn("In the series", body)
        self.assertIn("Caliban", body.split("In the series", 1)[1])
        self.assertIn('id="insp-cats-1"', self.client.get("/book/1/inspect").text)

    def test_book_edits_add_primary_remove(self):
        response = self.client.post("/book/3/categories/add", data={"category": "Genre: Fantasy", "scope": "insp"})
        self.assertIn('id="insp-cats-3"', response.text)
        self.assertIn(">Fantasy<", response.text)
        self.client.post("/book/3/categories/add", data={"category": "Horror"})
        self.client.post("/book/3/categories/primary", data={"category": "genre:Horror & Gothic"})
        detail = cat.book_categories(self.conn, self.book_id("3"))
        self.assertEqual(detail["primary"]["path"], "Horror & Gothic")
        self.client.post("/book/3/categories/remove", data={"category": "genre:Horror & Gothic"})
        paths = [g["path"] for g in cat.book_categories(self.conn, self.book_id("3"))["by_facet"]["genre"]]
        self.assertEqual(paths, ["Fantasy"])

    def test_book_edit_error_is_shown_inline(self):
        response = self.client.post("/book/3/categories/add", data={"category": "Nonexistent"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="cat-err"', response.text)
        self.assertEqual(self.client.post("/book/nope/categories/add", data={"category": "x"}).status_code, 404)

    # --- Categories page -------------------------------------------------------

    def test_taxonomy_page_and_actions(self):
        self.accept("Genre: Science Fiction")
        page = self.client.get("/taxonomy")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Mapping rules", page.text)
        self.assertIn("Goodreads shelf “sci fi”", page.text)

        added = self.client.post(
            "/taxonomy/add", data={"facet": "genre", "parent": "genre:Fantasy", "name": "Grimdark"}
        )
        self.assertEqual(added.status_code, 200)  # followed the 303 back to the page
        self.assertIn("Added Genre: Fantasy &gt; Grimdark", added.text)

        grimdark = self.category_id("Grimdark")
        self.client.post(f"/taxonomy/{grimdark}/rename", data={"label": "Dark Fantasy"})
        self.client.post(f"/taxonomy/{grimdark}/alias", data={"alias": "grimdark"})
        self.assertEqual(cat.Taxonomy(self.conn).resolve("grimdark").label, "Dark Fantasy")
        self.client.post(f"/taxonomy/{grimdark}/move", data={"parent": ""})
        self.assertIsNone(cat.Taxonomy(self.conn).get(grimdark).parent_id)

        space_opera = self.category_id("Space Opera")
        merged = self.client.post(f"/taxonomy/{space_opera}/merge", data={"target": "genre:Science Fiction"})
        self.assertIn("Merged", merged.text)
        deleted = self.client.post(f"/taxonomy/{grimdark}/delete")
        self.assertIn("Deleted", deleted.text)

        mapped = self.client.post("/taxonomy/map", data={"kind": "shelf", "value": "cozy-fantasy", "to": "genre:Fantasy"})
        self.assertIn("(1 book(s))", mapped.text)
        rule = next(r for r in cat.list_rules(self.conn) if r["match_value"] == "cozy fantasy")
        self.client.post(f"/taxonomy/rules/{rule['id']}/delete")
        self.assertFalse(any(r["match_value"] == "cozy fantasy" for r in cat.list_rules(self.conn)))

    def test_taxonomy_errors_come_back_as_a_message(self):
        response = self.client.post(
            "/taxonomy/add", data={"facet": "genre", "parent": "", "name": "Fantasy"}
        )
        self.assertIn('class="alert-destructive', response.text)
        self.assertIn("already exists", response.text)
        self.assertEqual(self.client.post("/taxonomy/99999/delete").status_code, 404)

    def test_alias_can_be_removed_on_the_categories_page(self):
        horror = self.category_id("Horror & Gothic")
        page = self.client.get("/taxonomy").text
        self.assertIn(f'action="/taxonomy/{horror}/unalias"', page)
        response = self.client.post(f"/taxonomy/{horror}/unalias", data={"alias": "horror tales"})
        self.assertIn("no longer matches Horror &amp; Gothic", response.text)
        card = self.card("Genre: Horror & Gothic")
        self.assertEqual([m["match_value"] for m in card["members"]], ["horror"])

    def test_import_page_explains_shelves(self):
        self.assertIn("Your Goodreads shelves are how you organised books there", self.client.get("/import").text)

    def test_novel_question_card(self):
        conn = self.conn
        conn.execute("UPDATE books SET subjects_json = ? WHERE goodreads_id IN ('1', '2')",
                     (json.dumps(["Fiction", "History"]),))
        conn.commit()
        cat.categorize(conn)
        for target in ("Form: Fiction", "Genre: History"):
            self.accept(target)
        body = self.client.get("/review").text
        self.assertIn("Genre: History?", body)
        self.assertIn("Open Library files these novels under “history”", body)
        question = next(c for c in cat.list_suggestion_cards(conn) if c["kind"] == "assign")
        response = self.client.post(f"/categories/suggestions/{question['id']}/accept")
        self.assertIn("Added Genre: History", response.text)

    # --- API ------------------------------------------------------------------

    def test_api_filters_and_fields(self):
        self.accept("Genre: Science Fiction")
        data = self.client.get("/api/books?category=Science Fiction").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["books"][0]["primary_genre"], "Science Fiction")
        self.assertEqual(data["books"][0]["series"]["name"], "The Expanse")
        self.assertEqual(self.client.get("/api/books?category=Nope").status_code, 422)
        self.assertEqual(self.client.get("/api/books/1").json()["series"]["position"], 1.0)
        self.assertIn("facets", self.client.get("/api/taxonomy").json())


if __name__ == "__main__":
    unittest.main()
