"""Tests for the MCP agent surface (adso.mcp_server).

These exercise the plain tool functions directly against a temporary catalogue
(never a real one). The load-bearing tests are the leak guard — private_notes
must never appear in any tool output — and the write-refusal guard — the write
tools reach only LOCAL_FIELDS, never local_notes or Goodreads/source columns.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adso import db, mcp_server

SECRET = "PRIVATE diary text that must never reach an agent"


def _seed(conn) -> None:
    run = db.create_import_run(
        conn, source="goodreads", source_path="x.csv", mode="import", row_count=3
    )
    db.insert_book_from_goodreads(
        conn,
        {
            "goodreads_id": "1",
            "title": "The Clockwork Herbarium",
            "author": "Mara Ellison",
            "reading_status": "Read",
            "exclusive_shelf": "read",
            "rating": 5,
            "private_notes": SECRET,
            "shelves_json": "[]",
        },
        import_run_id=run,
    )
    db.insert_book_from_goodreads(
        conn,
        {
            "goodreads_id": "2",
            "title": "Tidal Glass",
            "author": "Mara Ellison",
            "reading_status": "To Read",
            "exclusive_shelf": "to-read",
            "shelves_json": "[]",
        },
        import_run_id=run,
    )
    db.insert_book_from_goodreads(
        conn,
        {
            "goodreads_id": "3",
            "title": "Salt and Cedar",
            "author": "Ivo Marsh",
            "reading_status": "Read",
            "exclusive_shelf": "read",
            "rating": 3,
            "shelves_json": "[]",
        },
        import_run_id=run,
    )
    # Local fields for read-path coverage.
    conn.commit()
    # update_local_fields commits internally.
    db.update_local_fields(conn, "1", {"format": "physical", "tags_json": ["philosophy"]})


class MCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        self.conn = db.connect(self.db_path)
        db.initialize(self.conn)
        _seed(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    # --- Reads ---------------------------------------------------------------

    def test_search_all_returns_every_book(self) -> None:
        result = mcp_server.search_books(self.conn)
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["books"]), 3)

    def test_search_query_matches_title(self) -> None:
        result = mcp_server.search_books(self.conn, "Tidal")
        titles = [b["title"] for b in result["books"]]
        self.assertEqual(titles, ["Tidal Glass"])

    def test_search_filters_by_shelf_author_rating(self) -> None:
        self.assertEqual(mcp_server.search_books(self.conn, shelf="to-read")["count"], 1)
        self.assertEqual(mcp_server.search_books(self.conn, author="Marsh")["count"], 1)
        self.assertEqual(mcp_server.search_books(self.conn, rating=5)["count"], 1)
        self.assertEqual(mcp_server.search_books(self.conn, tag="philosophy")["count"], 1)

    def test_search_rating_out_of_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            mcp_server.search_books(self.conn, rating=9)

    def test_search_limit_is_clamped(self) -> None:
        result = mcp_server.search_books(self.conn, limit=1)
        self.assertEqual(result["count"], 1)
        # Over-large limits are capped, not rejected.
        self.assertEqual(
            mcp_server.search_books(self.conn, limit=10_000)["count"], 3
        )

    def test_get_book_returns_record(self) -> None:
        book = mcp_server.get_book(self.conn, "1")
        self.assertEqual(book["title"], "The Clockwork Herbarium")
        self.assertEqual(book["format"], "physical")
        self.assertEqual(book["tags"], ["philosophy"])

    def test_get_book_missing_raises(self) -> None:
        with self.assertRaises(ValueError):
            mcp_server.get_book(self.conn, "does-not-exist")

    def test_library_stats(self) -> None:
        stats = mcp_server.library_stats(self.conn)
        self.assertEqual(stats["total_books"], 3)
        self.assertEqual(stats["owned_books"], 1)
        self.assertEqual(stats["by_shelf"]["read"], 2)
        self.assertEqual(stats["by_shelf"]["to-read"], 1)
        self.assertEqual(stats["by_format"]["physical"], 1)
        # Book 2 is unrated (NULL) -> counted under rating 0.
        self.assertEqual(stats["by_rating"][0], 1)
        self.assertEqual(stats["by_rating"][5], 1)

    def test_list_facets(self) -> None:
        facets = mcp_server.list_facets(self.conn)
        self.assertEqual(sorted(facets["shelves"]), ["read", "to-read"])
        self.assertIn("philosophy", facets["tags"])
        self.assertEqual(facets["formats"], list(db.VALID_FORMATS))

    # --- Leak guard ----------------------------------------------------------

    def test_private_notes_never_in_search_output(self) -> None:
        result = mcp_server.search_books(self.conn)
        for book in result["books"]:
            self.assertNotIn("private_notes", book)
        self.assertNotIn(SECRET, repr(result))

    def test_private_notes_never_in_get_book_output(self) -> None:
        book = mcp_server.get_book(self.conn, "1")
        self.assertNotIn("private_notes", book)
        self.assertNotIn(SECRET, repr(book))

    def test_allowlist_excludes_forbidden_fields(self) -> None:
        self.assertNotIn("private_notes", mcp_server.AGENT_BOOK_FIELDS)
        # local_notes is visible (readable), private_notes is not.
        self.assertIn("local_notes", mcp_server.AGENT_BOOK_FIELDS)

    # --- Curated writes ------------------------------------------------------

    def test_add_tags_unions_and_normalizes(self) -> None:
        out = mcp_server.add_tags(self.conn, "1", ["Sci-Fi", "philosophy", "  Fiction "])
        # philosophy already present (not duplicated); new ones normalized/lowercased.
        self.assertEqual(out["tags"], ["philosophy", "sci-fi", "fiction"])
        self.assertEqual(mcp_server.get_book(self.conn, "1")["tags"], out["tags"])

    def test_remove_tags(self) -> None:
        mcp_server.add_tags(self.conn, "2", ["keep", "drop"])
        out = mcp_server.remove_tags(self.conn, "2", ["drop", "never-had-this"])
        self.assertEqual(out["tags"], ["keep"])

    def test_set_format_valid_clear_and_invalid(self) -> None:
        self.assertEqual(
            mcp_server.set_format(self.conn, "2", "ebook")["format"], "ebook"
        )
        # Empty / 'none' clears ownership.
        self.assertIsNone(mcp_server.set_format(self.conn, "2", "none")["format"])
        self.assertIsNone(mcp_server.set_format(self.conn, "2", "")["format"])
        with self.assertRaises(ValueError):
            mcp_server.set_format(self.conn, "2", "hardback")

    def test_set_loaned_and_clear(self) -> None:
        self.assertEqual(
            mcp_server.set_loaned(self.conn, "1", "Beatriz")["loaned_to"], "Beatriz"
        )
        self.assertIsNone(mcp_server.set_loaned(self.conn, "1", "")["loaned_to"])

    def test_write_on_missing_book_raises(self) -> None:
        for call in (
            lambda: mcp_server.add_tags(self.conn, "nope", ["x"]),
            lambda: mcp_server.set_format(self.conn, "nope", "ebook"),
            lambda: mcp_server.set_loaned(self.conn, "nope", "someone"),
        ):
            with self.assertRaises(ValueError):
                call()

    # --- Write-refusal guard -------------------------------------------------

    def test_no_write_tool_touches_local_notes(self) -> None:
        # local_notes stays read-only in v1: there is deliberately no tool for it.
        self.assertFalse(hasattr(mcp_server, "set_local_notes"))
        self.assertFalse(hasattr(mcp_server, "set_notes"))

    def test_local_field_writer_rejects_source_columns(self) -> None:
        # The underlying writer the tools use refuses any field outside
        # LOCAL_FIELDS, so no tool could smuggle a write to private_notes or a
        # Goodreads column. (local_notes IS a LOCAL_FIELD, so it is kept
        # read-only by exposing no tool for it — see the test above — rather than
        # by this writer.)
        for field in ("private_notes", "title", "rating"):
            with self.assertRaises(ValueError):
                db.update_local_fields(self.conn, "1", {field: "x"})

    def test_writes_leave_goodreads_and_local_notes_untouched(self) -> None:
        before = mcp_server.get_book(self.conn, "1")
        mcp_server.add_tags(self.conn, "1", ["extra"])
        mcp_server.set_loaned(self.conn, "1", "Reader")
        after = mcp_server.get_book(self.conn, "1")
        for field in ("title", "author", "rating", "reading_status", "local_notes"):
            self.assertEqual(before[field], after[field])


if __name__ == "__main__":
    unittest.main()
