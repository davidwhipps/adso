"""Web (FastAPI) tests for the export / report / sync-status surfaces (DAV-103).

Skipped unless the optional web test stack — including ``httpx``, which the
FastAPI ``TestClient`` needs — is installed. Notion is always patched so these
tests never touch the network.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adso import db
from adso.config import ResolvedConfig
from adso.notion import NotionConfigError

try:  # TestClient requires httpx; the web extra may not be installed.
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient

    _HAS_TESTCLIENT = True
except Exception:  # pragma: no cover - exercised only without the web extra
    _HAS_TESTCLIENT = False

HEADERS = ["Book Id", "Title", "Author", "Exclusive Shelf", "Bookshelves", "My Rating"]


def _seed(conn) -> None:
    run = db.create_import_run(conn, source="goodreads", source_path="x.csv", mode="import", row_count=1)
    db.insert_book_from_goodreads(
        conn,
        {"goodreads_id": "1", "title": "The Clockwork Herbarium", "author": "Mara Ellison",
         "reading_status": "Read", "shelves_json": "[]"},
        import_run_id=run,
    )
    conn.commit()


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class WebSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        conn = db.connect(self.db_path)
        db.initialize(conn)
        _seed(conn)
        conn.close()
        self.config = ResolvedConfig(
            db_path=self.db_path,
            profile="personal",
            notion_api_key="secret",
            notion_database_id="db-1234",
            notion_target="production",
        )
        self.client = TestClient(create_app(self.db_path, config=self.config))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_export_page_shows_download_and_notion_target(self) -> None:
        body = self.client.get("/export").text
        self.assertIn("Download CSV", body)
        self.assertIn("Preview (dry run)", body)
        self.assertIn("production", body)  # the configured Notion target

    def test_csv_download_streams_attachment(self) -> None:
        response = self.client.get("/export/catalogue.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("goodreads_id,title,author", response.text)
        self.assertIn("The Clockwork Herbarium", response.text)

    def test_json_download_streams_attachment(self) -> None:
        response = self.client.get("/export/catalogue.json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers["content-type"])
        self.assertEqual(response.json()[0]["title"], "The Clockwork Herbarium")

    def test_report_pages_render(self) -> None:
        self.assertIn("Sync summary", self.client.get("/reports/summary").text)
        self.assertIn("Conflict report", self.client.get("/reports/conflicts").text)

    def test_notion_preview_renders_planned_actions(self) -> None:
        fake = {"created": 1, "updated": 0, "errors": 0,
                "actions": [{"action": "create", "title": "The Clockwork Herbarium", "goodreads_id": "1"}]}
        with patch("adso.web.app.export_to_notion", return_value=fake) as mock:
            body = self.client.post("/export/notion", data={"dry_run": "true"}).text
        mock.assert_called_once()
        self.assertEqual(mock.call_args.kwargs["dry_run"], True)
        self.assertIn("Dry run", body)
        self.assertIn("Would create", body)

    def test_notion_not_configured_shows_friendly_error(self) -> None:
        with patch("adso.web.app.export_to_notion", side_effect=NotionConfigError("creds required")):
            body = self.client.post("/export/notion", data={"dry_run": "false"}).text
        self.assertIn("alert-destructive", body)
        self.assertIn("creds required", body)


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class BookLocalEditWebTests(unittest.TestCase):
    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        conn = db.connect(self.db_path)
        db.initialize(conn)
        run = db.create_import_run(conn, source="goodreads", source_path="x", mode="import", row_count=1)
        db.insert_book_from_goodreads(
            conn,
            {"goodreads_id": "1", "title": "T", "reading_status": "Read", "shelves_json": "[]"},
            import_run_id=run,
        )
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _format(self) -> str | None:
        conn = db.connect(self.db_path)
        value = db.get_book_by_goodreads_id(conn, "1")["format"]
        conn.close()
        return value

    def test_detail_shows_always_live_local_card(self) -> None:
        # No edit/save toggle: the detail card posts each field to its own
        # autosave endpoint, and tags autocomplete from a datalist.
        body = self.client.get("/book/1").text
        self.assertIn("Local catalogue", body)
        self.assertIn("never synced to Goodreads", body)
        self.assertIn('hx-post="/book/1/format"', body)
        self.assertIn('hx-post="/book/1/loaned"', body)
        self.assertIn('hx-post="/book/1/notes"', body)
        # Tags autocomplete from existing tags, seeded into the input component.
        self.assertIn('hx-post="/book/1/tags/add"', body)
        self.assertIn("data-tags=", body)
        # The old explicit edit form is gone.
        self.assertNotIn("/book/1/local/edit", body)

    def _tags(self) -> str:
        conn = db.connect(self.db_path)
        value = db.get_book_by_goodreads_id(conn, "1")["tags_json"]
        conn.close()
        return value

    def test_detail_renders_all_local_controls(self) -> None:
        # Tags are edited inline as chips (name="tag"), not a form field
        # named "tags".
        body = self.client.get("/book/1").text
        for name in ("format", "loaned_to", "local_notes"):
            self.assertIn(f'name="{name}"', body)
        self.assertNotIn('name="tags"', body)
        for value in ("physical", "ebook", "audiobook"):
            self.assertIn(f'value="{value}"', body)

    def test_each_field_autosaves_and_confirms(self) -> None:
        fmt = self.client.post("/book/1/format", data={"format": "ebook"}).text
        self.assertIn("Saved", fmt)
        self.assertIn("Ebook", fmt)
        loaned = self.client.post("/book/1/loaned", data={"loaned_to": "Sam"}).text
        self.assertIn("Saved", loaned)
        self.assertIn("Sam", loaned)
        notes = self.client.post("/book/1/notes", data={"local_notes": "Signed"}).text
        self.assertIn("Saved", notes)
        self.assertIn("Signed", notes)
        conn = db.connect(self.db_path)
        book = db.get_book_by_goodreads_id(conn, "1")
        self.assertEqual(
            (book["format"], book["loaned_to"], book["local_notes"]),
            ("ebook", "Sam", "Signed"),
        )
        conn.close()

    def test_format_save_emits_oob_table_cell(self) -> None:
        # The Format endpoint also re-renders the list table's Format badge out
        # of band so an edit from a list popover stays in sync.
        body = self.client.post("/book/1/format", data={"format": "ebook"}).text
        self.assertIn('id="row-format-1"', body)
        self.assertIn('hx-swap-oob="true"', body)

    def test_field_save_leaves_other_fields_untouched(self) -> None:
        # Each endpoint writes only its own field, so tags survive a format edit.
        self.client.post("/book/1/tags/add", data={"tag": "Philosophy"})
        self.client.post("/book/1/format", data={"format": "ebook"})
        self.client.post("/book/1/notes", data={"local_notes": "x"})
        self.assertEqual(self._tags(), '["philosophy"]')

    def test_quick_edit_panel_renders_scoped_controls(self) -> None:
        body = self.client.get("/book/1/local/panel", params={"scope": "shelf"}).text
        self.assertIn('id="shelf-format-1"', body)
        self.assertIn('name="local_notes"', body)
        self.assertIn('id="shelf-tags-1"', body)

    def test_unknown_scope_is_clamped(self) -> None:
        # `scope` lands in element ids/attributes, so an unknown value falls back
        # to "detail" rather than being reflected verbatim.
        body = self.client.post(
            "/book/1/format", data={"format": "ebook", "scope": "../evil"}
        ).text
        self.assertIn('id="detail-format-1"', body)
        self.assertNotIn("evil", body)

    def test_catalogue_offers_quick_edit_trigger(self) -> None:
        body = self.client.get("/").text
        self.assertIn("Quick edit T", body)
        self.assertIn("/book/1/local/panel?scope=shelf", body)

    def test_tag_add_appends_and_normalises(self) -> None:
        body = self.client.post("/book/1/tags/add", data={"tag": "Philosophy"}).text
        self.assertIn("philosophy", body)
        self.assertEqual(self._tags(), '["philosophy"]')

    def test_tag_add_dedupes_case_insensitively(self) -> None:
        self.client.post("/book/1/tags/add", data={"tag": "medieval"})
        self.client.post("/book/1/tags/add", data={"tag": "Medieval"})
        self.assertEqual(self._tags(), '["medieval"]')

    def test_tag_add_blank_is_noop(self) -> None:
        self.client.post("/book/1/tags/add", data={"tag": "   "})
        self.assertEqual(self._tags(), "[]")

    def test_tag_remove_drops_matching_tag(self) -> None:
        self.client.post("/book/1/tags/add", data={"tag": "philosophy"})
        self.client.post("/book/1/tags/add", data={"tag": "medieval"})
        body = self.client.post("/book/1/tags/remove", data={"tag": "Philosophy"}).text
        self.assertNotIn(">philosophy<", body)
        self.assertEqual(self._tags(), '["medieval"]')

    def test_empty_format_clears_to_not_owned(self) -> None:
        self.client.post("/book/1/format", data={"format": "physical"})
        self.assertEqual(self._format(), "physical")
        # The blank "— Not owned" option posts an empty string.
        self.client.post("/book/1/format", data={"format": ""})
        self.assertIsNone(self._format())

    def test_invalid_format_shows_error_and_keeps_value(self) -> None:
        self.client.post("/book/1/format", data={"format": "physical"})
        body = self.client.post("/book/1/format", data={"format": "hardcover"}).text
        self.assertIn("text-destructive", body)
        self.assertIn("Unsupported format", body)
        self.assertEqual(self._format(), "physical")

    def test_edit_missing_book_is_404(self) -> None:
        self.assertEqual(
            self.client.post("/book/nope/format", data={"format": "ebook"}).status_code, 404
        )
        self.assertEqual(self.client.get("/book/nope/local/panel").status_code, 404)


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class BookMetadataWebTests(unittest.TestCase):
    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        conn = db.connect(self.db_path)
        db.initialize(conn)
        _seed(conn)
        book_id = int(conn.execute("SELECT id FROM books WHERE goodreads_id='1'").fetchone()[0])
        db.set_metadata(
            conn,
            book_id,
            description="A clockmaker catalogues impossible plants.",
            subjects=["Botanical fiction"],
            subject_places=["Prague"],
            subject_times=["19th century"],
            metadata_source="openlibrary:isbn",
            metadata_source_url="https://openlibrary.org/isbn/x.json",
            metadata_status="fetched",
        )
        conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_detail_page_renders_description_and_subject_badges(self) -> None:
        body = self.client.get("/book/1").text
        self.assertIn("A clockmaker catalogues impossible plants.", body)
        self.assertIn("Botanical fiction", body)
        self.assertIn("Prague", body)
        self.assertIn("19th century", body)
        # Subject badges link into catalogue search.
        self.assertIn('href="/?q=Botanical%20fiction"', body)

    def test_api_book_includes_metadata_fields(self) -> None:
        payload = self.client.get("/api/books/1").json()
        self.assertEqual(payload["description"], "A clockmaker catalogues impossible plants.")
        self.assertEqual(payload["subjects"], ["Botanical fiction"])
        self.assertEqual(payload["subject_places"], ["Prague"])
        self.assertEqual(payload["subject_times"], ["19th century"])


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class ApiPrivateNotesLeakGuardTests(unittest.TestCase):
    """The JSON API must never serialize Goodreads' Private Notes field. It is a
    known must-not-leak column (the export path strips it at handoff), while the
    HTML book page and CLI legitimately keep using it. Seed a non-empty
    private_notes and assert it is absent from both endpoints."""

    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        conn = db.connect(self.db_path)
        db.initialize(conn)
        run = db.create_import_run(
            conn, source="goodreads", source_path="x.csv", mode="import", row_count=1
        )
        db.insert_book_from_goodreads(
            conn,
            {"goodreads_id": "1", "title": "The Clockwork Herbarium", "author": "Mara Ellison",
             "reading_status": "Read", "shelves_json": "[]",
             "private_notes": "SECRET: do not export or serve over HTTP"},
            import_run_id=run,
        )
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_private_notes_absent_from_list_endpoint(self) -> None:
        response = self.client.get("/api/books")
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        book = payload["books"][0]
        self.assertNotIn("private_notes", book)
        # Belt and braces: the value never appears anywhere in the raw body.
        self.assertNotIn("SECRET", response.text)

    def test_private_notes_absent_from_detail_endpoint(self) -> None:
        response = self.client.get("/api/books/1")
        book = response.json()
        # Sanity: we reached the seeded book, not a 404.
        self.assertEqual(book["goodreads_id"], "1")
        self.assertNotIn("private_notes", book)
        self.assertNotIn("SECRET", response.text)


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class CatalogueRatingFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        conn = db.connect(self.db_path)
        db.initialize(conn)
        run = db.create_import_run(
            conn, source="goodreads", source_path="x.csv", mode="import", row_count=2
        )
        db.insert_book_from_goodreads(
            conn,
            {"goodreads_id": "1", "title": "Unrated Book", "author": "Mara Ellison",
             "rating": 0, "shelves_json": "[]"},
            import_run_id=run,
        )
        db.insert_book_from_goodreads(
            conn,
            {"goodreads_id": "2", "title": "Five Star Book", "author": "Mara Ellison",
             "rating": 5, "shelves_json": "[]"},
            import_run_id=run,
        )
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rating_filter_narrows_catalogue_page(self) -> None:
        body = self.client.get("/", params={"rating": 5}).text
        self.assertIn("Five Star Book", body)
        self.assertNotIn("Unrated Book", body)

    def test_rating_zero_finds_unrated_books(self) -> None:
        payload = self.client.get("/api/books", params={"rating": 0}).json()
        self.assertEqual([b["goodreads_id"] for b in payload["books"]], ["1"])

    def test_rating_out_of_range_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/books", params={"rating": 6}).status_code, 422)
        self.assertEqual(self.client.get("/api/books", params={"rating": "five"}).status_code, 422)

    def test_empty_rating_param_means_no_filter(self) -> None:
        # The catalogue form submits rating="" when "Any rating" is selected —
        # searching must not 422 (regression: int query param rejected "").
        response = self.client.get("/", params={"q": "Book", "status": "", "format": "", "tag": "", "rating": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Five Star Book", response.text)
        self.assertIn("Unrated Book", response.text)

        payload = self.client.get("/api/books", params={"rating": ""}).json()
        self.assertEqual(payload["count"], 2)


class ShortAuDateTests(unittest.TestCase):
    def test_iso_dates_render_as_short_australian(self) -> None:
        from adso.web.app import _short_au_date

        self.assertEqual(_short_au_date("2013-09-14"), "14/09/13")
        # Goodreads occasionally stores slash-separated dates.
        self.assertEqual(_short_au_date("2021/02/05"), "05/02/21")

    def test_empty_is_em_dash_and_unparseable_passes_through(self) -> None:
        from adso.web.app import _short_au_date

        self.assertEqual(_short_au_date(""), "—")
        self.assertEqual(_short_au_date(None), "—")
        # A partial/odd value is shown as-is rather than dropped.
        self.assertEqual(_short_au_date("2019"), "2019")


if __name__ == "__main__":
    unittest.main()
