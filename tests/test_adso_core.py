from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from builtins import __import__ as real_import
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from adso import db
from adso.catalogue import BookFilters, get_book, list_books, search_books
from adso.cli import main as cli_main
from adso.doctor import doctor_report
from adso.exports import export_csv, export_json
from adso.notion import NotionConfigError, _load_existing_pages, export_to_notion
from adso.reports import latest_conflicts_markdown, latest_sync_summary_markdown
from adso.sync import import_goodreads_csv

HEADERS = [
    "Book Id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Average Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "My Review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
]


def write_goodreads_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in HEADERS})


def row(**overrides: str) -> dict[str, str]:
    data = {
        "Book Id": "1",
        "Title": "The Name of the Rose",
        "Author": "Umberto Eco",
        "ISBN": '="0156001314"',
        "ISBN13": '="9780156001311"',
        "My Rating": "0",
        "Average Rating": "4.14",
        "Publisher": "Harvest Books",
        "Binding": "Paperback",
        "Number of Pages": "536",
        "Year Published": "1994",
        "Original Publication Year": "1980",
        "Date Added": "2026/04/28",
        "Bookshelves": "to-read",
        "Bookshelves with positions": "to-read (#1)",
        "Exclusive Shelf": "to-read",
        "Read Count": "0",
        "Owned Copies": "0",
    }
    data.update(overrides)
    return data


class FakeNotionResponse:
    def __init__(
        self,
        data: dict[str, object] | None = None,
        *,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._data = data or {}
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text or f"HTTP {self.status_code}")
        return None


class FakeRequestsModule:
    @staticmethod
    def request(*args, **kwargs):
        raise RuntimeError("DNS lookup failed")


class AdsoCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(self.root / "adso.sqlite")
        self.conn.row_factory = sqlite3.Row
        db.initialize(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_import_preserves_raw_rows_and_is_idempotent(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])

        first = import_goodreads_csv(self.conn, csv_path, mode="import")
        second = import_goodreads_csv(self.conn, csv_path, mode="sync")

        self.assertEqual(first.created, 1)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_import_rows").fetchone()[0], 2)

    def test_synthetic_goodreads_sample_imports_successfully(self) -> None:
        sample_path = Path(__file__).resolve().parent.parent / "examples" / "goodreads_sample.csv"

        summary = import_goodreads_csv(self.conn, sample_path, mode="import")
        rows = self.conn.execute(
            """
            SELECT goodreads_id, title, reading_status, rating, date_read, isbn10, isbn13,
                shelves_json, my_review, private_notes, owned_copies
            FROM books
            ORDER BY goodreads_id
            """
        ).fetchall()

        self.assertEqual(summary.row_count, 3)
        self.assertEqual(summary.created, 3)
        self.assertEqual(summary.skipped, 0)
        self.assertEqual(
            [row["reading_status"] for row in rows],
            ["Read", "Currently Reading", "To Read"],
        )
        self.assertEqual(rows[0]["rating"], 5)
        self.assertEqual(rows[0]["date_read"], "2026-01-14")
        self.assertEqual(rows[0]["isbn10"], "1935555012")
        self.assertEqual(rows[0]["isbn13"], "9781935555010")
        self.assertIn("botanical-mystery", rows[0]["shelves_json"])
        self.assertIn("Invented sample row", rows[0]["my_review"])
        self.assertIn("Synthetic demo note", rows[0]["private_notes"])
        self.assertEqual(rows[0]["owned_copies"], 1)

    def test_doctor_reports_brand_new_folder_without_creating_database(self) -> None:
        db_path = self.root / "brand-new.sqlite"
        csv_path = self.root / "goodreads_export.csv"
        write_goodreads_csv(csv_path, [row()])

        output = doctor_report(db_path, root=self.root, env={})

        self.assertFalse(db_path.exists())
        self.assertIn("Database file: no", output)
        self.assertIn("Initialized: no", output)
        self.assertIn("- goodreads_export.csv", output)
        self.assertIn("Credentials configured: no", output)
        self.assertIn("adso init", output)

    def test_doctor_reports_empty_initialized_database(self) -> None:
        output = doctor_report(self.root / "adso.sqlite", root=self.root, env={})

        self.assertIn("Database file: yes", output)
        self.assertIn("Initialized: yes", output)
        self.assertIn("Books: 0", output)
        self.assertIn("Latest import: none yet", output)
        self.assertIn("Pending conflicts: 0", output)
        self.assertIn("adso import goodreads examples/goodreads_sample.csv", output)

    def test_doctor_reports_imported_catalogue_and_notion_config(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        output = doctor_report(
            self.root / "adso.sqlite",
            root=self.root,
            env={"NOTION_API_KEY": "secret-value", "NOTION_DB_ID": "database-id"},
        )

        self.assertIn("Books: 1", output)
        self.assertIn("Latest import: import from goodreads", output)
        self.assertIn("Pending conflicts: 0", output)
        self.assertIn("Credentials configured: yes", output)
        self.assertNotIn("secret-value", output)
        self.assertIn("adso export notion", output)

    def test_doctor_prioritizes_pending_conflicts(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")
        self.conn.execute("UPDATE books SET reading_status = ? WHERE goodreads_id = ?", ("Local Status", "1"))
        self.conn.commit()
        changed_path = self.root / "goodreads-changed.csv"
        write_goodreads_csv(changed_path, [row(**{"Exclusive Shelf": "read", "Bookshelves": "read"})])
        import_goodreads_csv(self.conn, changed_path, mode="sync")

        output = doctor_report(self.root / "adso.sqlite", root=self.root, env={})

        self.assertIn("Pending conflicts: 1", output)
        self.assertIn("adso report conflicts", output)

    def test_safe_goodreads_activity_update_when_local_value_unchanged(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        changed_path = self.root / "goodreads-changed.csv"
        write_goodreads_csv(
            changed_path,
            [row(**{"Exclusive Shelf": "read", "Bookshelves": "read", "Date Read": "2026/04/28"})],
        )
        summary = import_goodreads_csv(self.conn, changed_path, mode="sync")
        book = self.conn.execute("SELECT * FROM books WHERE goodreads_id = '1'").fetchone()

        self.assertEqual(summary.updated, 1)
        self.assertEqual(summary.conflicts, 0)
        self.assertEqual(book["reading_status"], "Read")
        self.assertEqual(book["date_read"], "2026-04-28")
        sync_report = latest_sync_summary_markdown(self.conn)
        self.assertIn("What happened", sync_report)
        self.assertIn("safely updated 1 existing books", sync_report)
        self.assertIn("No manual review is required", sync_report)

    def test_local_physical_fields_survive_goodreads_sync(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")
        db.update_local_fields(
            self.conn,
            "1",
            {
                "format": "physical",
                "tags_json": ["philosophy", "medieval"],
                "loaned_to": "Sam",
                "local_notes": "Signed",
            },
        )

        changed_path = self.root / "goodreads-changed.csv"
        write_goodreads_csv(changed_path, [row(**{"My Rating": "5"})])
        import_goodreads_csv(self.conn, changed_path, mode="sync")
        book = self.conn.execute("SELECT * FROM books WHERE goodreads_id = '1'").fetchone()

        self.assertEqual(book["format"], "physical")
        self.assertEqual(json.loads(book["tags_json"]), ["philosophy", "medieval"])
        self.assertEqual(book["loaned_to"], "Sam")
        self.assertEqual(book["local_notes"], "Signed")
        self.assertEqual(book["rating"], 5)

    def test_update_local_fields_validates_format(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        with self.assertRaises(ValueError):
            db.update_local_fields(self.conn, "1", {"format": "hardcover"})

        for value in (*db.VALID_FORMATS, None):
            db.update_local_fields(self.conn, "1", {"format": value})
            book = self.conn.execute("SELECT format FROM books WHERE goodreads_id = '1'").fetchone()
            self.assertEqual(book["format"], value)

    def test_tags_normalize_and_validate(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        # Messy comma-separated input is lowercased, trimmed, and deduped.
        self.assertEqual(
            db.normalize_tags(" Philosophy, medieval,philosophy , ,Stoicism"),
            ["philosophy", "medieval", "stoicism"],
        )
        db.update_local_fields(self.conn, "1", {"tags_json": ["Philosophy", "Medieval"]})
        self.assertEqual(get_book(self.conn, "1")["tags"], ["philosophy", "medieval"])

        # Clearing with an empty list works; junk types are rejected.
        db.update_local_fields(self.conn, "1", {"tags_json": []})
        self.assertEqual(get_book(self.conn, "1")["tags"], [])
        with self.assertRaises(ValueError):
            db.update_local_fields(self.conn, "1", {"tags_json": "not-json"})

    def test_conflict_report_when_local_activity_field_changed(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")
        self.conn.execute("UPDATE books SET reading_status = ? WHERE goodreads_id = ?", ("Local Status", "1"))
        self.conn.commit()

        changed_path = self.root / "goodreads-changed.csv"
        write_goodreads_csv(changed_path, [row(**{"Exclusive Shelf": "read", "Bookshelves": "read"})])
        summary = import_goodreads_csv(self.conn, changed_path, mode="sync")
        report = latest_conflicts_markdown(self.conn)
        book = self.conn.execute("SELECT * FROM books WHERE goodreads_id = '1'").fetchone()

        self.assertEqual(summary.conflicts, 1)
        self.assertEqual(book["reading_status"], "Local Status")
        # exclusive_shelf is derived from the same Goodreads column as
        # reading_status, so it must be held alongside the conflict rather than
        # silently advancing to "read" (DAV-143).
        self.assertEqual(book["exclusive_shelf"], "to-read")
        self.assertIn("The Name of the Rose", report)
        self.assertIn("reading_status", report)
        self.assertIn("current reading state", report)
        sync_report = latest_sync_summary_markdown(self.conn)
        self.assertIn("Local catalogue values were preserved", sync_report)
        self.assertIn("adso report conflicts", sync_report)

    def test_coupled_shelf_fields_are_held_together_on_conflict(self) -> None:
        # reading_status and exclusive_shelf both derive from Goodreads'
        # "Exclusive Shelf" column. When reading_status has diverged locally and
        # Goodreads moves the shelf, neither field may be auto-updated, or the
        # two would drift apart (DAV-143).
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")
        self.conn.execute(
            "UPDATE books SET reading_status = ? WHERE goodreads_id = ?", ("Local Status", "1")
        )
        self.conn.commit()

        changed_path = self.root / "goodreads-changed.csv"
        write_goodreads_csv(changed_path, [row(**{"Exclusive Shelf": "read", "Bookshelves": "read"})])
        summary = import_goodreads_csv(self.conn, changed_path, mode="sync")
        book = self.conn.execute("SELECT * FROM books WHERE goodreads_id = '1'").fetchone()

        # Only the genuinely diverged field is recorded as a conflict; the
        # coupled field is held (preserved), not flagged.
        self.assertEqual(summary.conflicts, 1)
        self.assertEqual(book["reading_status"], "Local Status")
        self.assertEqual(book["exclusive_shelf"], "to-read")
        conflict_fields = [
            r["field_name"]
            for r in self.conn.execute("SELECT field_name FROM sync_conflicts")
        ]
        self.assertEqual(conflict_fields, ["reading_status"])

    def test_coupled_shelf_fields_update_together_when_safe(self) -> None:
        # With no local divergence, a shelf change advances both coupled fields.
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        changed_path = self.root / "goodreads-changed.csv"
        write_goodreads_csv(changed_path, [row(**{"Exclusive Shelf": "read", "Bookshelves": "read"})])
        summary = import_goodreads_csv(self.conn, changed_path, mode="sync")
        book = self.conn.execute("SELECT * FROM books WHERE goodreads_id = '1'").fetchone()

        self.assertEqual(summary.conflicts, 0)
        self.assertEqual(summary.updated, 1)
        self.assertEqual(book["reading_status"], "Read")
        self.assertEqual(book["exclusive_shelf"], "read")

    def test_portable_exports(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        json_path = export_json(self.conn, self.root / "catalogue.json")
        csv_export_path = export_csv(self.conn, self.root / "catalogue.csv")

        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["title"], "The Name of the Rose")
        self.assertEqual(data[0]["publisher"], "Harvest Books")
        csv_text = csv_export_path.read_text(encoding="utf-8")
        self.assertIn("goodreads_id,title,author", csv_text)
        # Bibliographic fields are normalized + synced, so they must be exportable too.
        self.assertIn("publisher", csv_text)
        self.assertIn("Harvest Books", csv_text)
        self.assertIn("original_publication_year", csv_text)
        header = csv_text.splitlines()[0].split(",")
        self.assertIn("format", header)
        self.assertIn("tags", header)
        for dropped in ("owned", "copy_count", "location", "shelf_box"):
            self.assertNotIn(dropped, header)

    def test_catalogue_string_serializers_back_file_exports(self) -> None:
        from adso.exports import catalogue_csv_string, catalogue_json_string

        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        csv_str = catalogue_csv_string(self.conn)
        json_str = catalogue_json_string(self.conn)
        self.assertTrue(csv_str.startswith("goodreads_id,title,author"))
        self.assertIn("The Name of the Rose", csv_str)
        self.assertEqual(json.loads(json_str)[0]["title"], "The Name of the Rose")
        # The file exports must be exactly what the string serializers produce.
        self.assertEqual(export_csv(self.conn, self.root / "c.csv").read_bytes(), csv_str.encode("utf-8"))
        self.assertEqual(
            export_json(self.conn, self.root / "c.json").read_text(encoding="utf-8"), json_str
        )

    def test_notion_dry_run_reports_create_update_without_writes(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                    }
                ),
            ],
        )
        import_goodreads_csv(self.conn, csv_path, mode="import")

        with patch.dict(sys.modules, {"requests": object()}), patch(
            "adso.notion._load_existing_pages", return_value={"2": "page-id"}
        ), patch("adso.notion._request") as request:
            result = export_to_notion(
                self.conn,
                api_key="secret",
                database_id="database",
                dry_run=True,
            )

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(
            [(action["action"], action["goodreads_id"]) for action in result["actions"]],
            [("update", "2"), ("create", "1")],
        )
        request.assert_not_called()

    def test_notion_limit_applies_before_writes(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                    }
                ),
            ],
        )
        import_goodreads_csv(self.conn, csv_path, mode="import")

        with patch.dict(sys.modules, {"requests": object()}), patch(
            "adso.notion._load_existing_pages", return_value={"2": "page-id"}
        ), patch("adso.notion._request", return_value=FakeNotionResponse()) as request, patch(
            "adso.notion.time.sleep"
        ):
            result = export_to_notion(
                self.conn,
                api_key="secret",
                database_id="database",
                limit=1,
            )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(
            result["actions"],
            [{"action": "update", "goodreads_id": "2", "title": "The Left Hand of Darkness"}],
        )
        request.assert_called_once()

    def test_notion_existing_page_lookup_uses_goodreads_id(self) -> None:
        response = FakeNotionResponse(
            {
                "results": [
                    {
                        "id": "page-one",
                        "properties": {
                            "Goodreads ID": {"rich_text": [{"plain_text": "1"}]},
                        },
                    },
                    {
                        "id": "page-without-goodreads-id",
                        "properties": {
                            "Goodreads ID": {"rich_text": []},
                        },
                    },
                ],
                "has_more": False,
            }
        )

        with patch("adso.notion._request", return_value=response) as request, patch("adso.notion.time.sleep"):
            existing = _load_existing_pages("secret", "database")

        self.assertEqual(existing, {"1": "page-one"})
        request.assert_called_once_with(
            "secret",
            "post",
            "https://api.notion.com/v1/databases/database/query",
            json={"page_size": 100},
        )

    def test_notion_create_and_update_request_shapes(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                    }
                ),
            ],
        )
        import_goodreads_csv(self.conn, csv_path, mode="import")

        with patch.dict(sys.modules, {"requests": object()}), patch(
            "adso.notion._load_existing_pages", return_value={"2": "page-id"}
        ), patch("adso.notion._request", return_value=FakeNotionResponse()) as request, patch(
            "adso.notion.time.sleep"
        ):
            result = export_to_notion(self.conn, api_key="secret", database_id="database")

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(request.call_count, 2)
        update_call, create_call = request.call_args_list
        self.assertEqual(update_call.args[:3], ("secret", "patch", "https://api.notion.com/v1/pages/page-id"))
        self.assertEqual(create_call.args[:3], ("secret", "post", "https://api.notion.com/v1/pages"))
        self.assertEqual(create_call.kwargs["json"]["parent"], {"database_id": "database"})

    def test_notion_reports_missing_credentials_and_optional_dependency(self) -> None:
        def missing_requests_import(name, *args, **kwargs):
            if name == "requests":
                raise ModuleNotFoundError("No module named 'requests'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing_requests_import):
            with self.assertRaisesRegex(NotionConfigError, "Install the requests dependency"):
                export_to_notion(self.conn, api_key="secret", database_id="database")

        with patch.dict(sys.modules, {"requests": object()}), patch.dict("adso.notion.os.environ", {}, clear=True):
            with self.assertRaisesRegex(NotionConfigError, "NOTION_API_KEY and NOTION_DB_ID"):
                export_to_notion(self.conn)

    def test_notion_network_failure_reports_clean_error(self) -> None:
        with patch.dict(sys.modules, {"requests": FakeRequestsModule}):
            with self.assertRaisesRegex(NotionConfigError, "Could not reach the Notion API: DNS lookup failed"):
                export_to_notion(self.conn, api_key="secret", database_id="database", dry_run=True)

    def test_notion_lookup_http_error_reports_clean_error(self) -> None:
        response = FakeNotionResponse(status_code=403, text='{"message":"restricted"}')

        with patch("adso.notion._request", return_value=response):
            with self.assertRaisesRegex(NotionConfigError, "Notion database lookup failed: 403"):
                _load_existing_pages("secret", "database")

    def test_notion_write_http_error_counts_error_without_traceback(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        import_goodreads_csv(self.conn, csv_path, mode="import")

        with patch.dict(sys.modules, {"requests": object()}), patch(
            "adso.notion._load_existing_pages", return_value={}
        ), patch(
            "adso.notion._request",
            return_value=FakeNotionResponse(status_code=400, text='{"message":"missing property"}'),
        ), patch(
            "adso.notion.time.sleep"
        ):
            result = export_to_notion(self.conn, api_key="secret", database_id="database")

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["errors"], 1)

    def test_catalogue_query_service_lists_filters_and_limits_books(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                        "Exclusive Shelf": "read",
                        "Bookshelves": "read, fiction",
                        "My Rating": "5",
                    }
                ),
                row(
                    **{
                        "Book Id": "3",
                        "Title": "A Room of One's Own",
                        "Author": "Virginia Woolf",
                        "Exclusive Shelf": "currently-reading",
                        "Bookshelves": "currently-reading, essays",
                    }
                ),
                row(
                    **{
                        "Book Id": "4",
                        "Title": "Zen in the Art of Archery",
                        "Author": "Eugen Herrigel",
                        # An empty "My Rating" cell parses to NULL, not 0; the
                        # unrated filter must treat both the same.
                        "My Rating": "",
                    }
                ),
            ],
        )
        import_goodreads_csv(self.conn, csv_path, mode="import")
        db.update_local_fields(self.conn, "2", {"format": "physical", "tags_json": ["philosophy"]})

        all_books = list_books(self.conn)
        read_books = list_books(self.conn, BookFilters(status="Read"))
        physical_books = list_books(self.conn, BookFilters(format="physical"))
        tagged_books = list_books(self.conn, BookFilters(tag="Philosophy"))
        no_tag_books = list_books(self.conn, BookFilters(tag="philo"))
        shelf_books = list_books(self.conn, BookFilters(shelf="Read"))
        five_star_books = list_books(self.conn, BookFilters(rating=5))
        unrated_books = list_books(self.conn, BookFilters(rating=0))
        limited_books = list_books(self.conn, BookFilters(limit=2))

        self.assertEqual([book["title"] for book in all_books], [
            "A Room of One's Own",
            "The Left Hand of Darkness",
            "The Name of the Rose",
            "Zen in the Art of Archery",
        ])
        self.assertEqual([book["goodreads_id"] for book in read_books], ["2"])
        self.assertEqual([book["goodreads_id"] for book in physical_books], ["2"])
        # Tag filtering is exact-match (case-insensitive), not substring.
        self.assertEqual([book["goodreads_id"] for book in tagged_books], ["2"])
        self.assertEqual(no_tag_books, [])
        # Shelf filtering is exact-match on the exclusive shelf, case-insensitive.
        self.assertEqual([book["goodreads_id"] for book in shelf_books], ["2"])
        self.assertEqual([book["goodreads_id"] for book in five_star_books], ["2"])
        # Goodreads exports store "unrated" as 0 (or NULL when the cell is
        # empty); rating=0 finds both.
        self.assertEqual([book["goodreads_id"] for book in unrated_books], ["3", "1", "4"])
        self.assertEqual(len(limited_books), 2)
        self.assertIsNone(limited_books[0]["format"])

    def test_catalogue_query_service_searches_and_gets_books(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                        "ISBN": '="0441478123"',
                        "ISBN13": '="9780441478125"',
                        "Bookshelves": "fiction, science-fiction",
                        "My Review": "A remarkable novel about winter and society.",
                    }
                ),
            ],
        )
        import_goodreads_csv(self.conn, csv_path, mode="import")
        db.update_local_fields(
            self.conn,
            "2",
            {
                "format": "physical",
                "tags_json": ["philosophy"],
                "loaned_to": "Sam",
                "local_notes": "Lending copy",
            },
        )

        search_results = search_books(self.conn, "winter")
        filtered_results = search_books(self.conn, "novel", BookFilters(format="physical"))
        field_searches = {
            "title": search_books(self.conn, "darkness"),
            "author": search_books(self.conn, "guin"),
            "isbn": search_books(self.conn, "9780156001311"),
            "shelves": search_books(self.conn, "science"),
            "local_notes": search_books(self.conn, "lending"),
            "loaned_to": search_books(self.conn, "sam"),
            "tags": search_books(self.conn, "philosophy"),
            # format is deliberately not indexed: searching a format name
            # shouldn't match every owned book — that's what the filter is for.
            "format": search_books(self.conn, "physical"),
        }
        book = get_book(self.conn, "2")
        missing = get_book(self.conn, "missing")

        self.assertEqual([result["goodreads_id"] for result in search_results], ["2"])
        self.assertEqual([result["goodreads_id"] for result in filtered_results], ["2"])
        self.assertEqual([result["goodreads_id"] for result in field_searches["title"]], ["2"])
        self.assertEqual([result["goodreads_id"] for result in field_searches["author"]], ["2"])
        self.assertEqual([result["goodreads_id"] for result in field_searches["isbn"]], ["1"])
        self.assertEqual([result["goodreads_id"] for result in field_searches["shelves"]], ["2"])
        self.assertEqual([result["goodreads_id"] for result in field_searches["local_notes"]], ["2"])
        self.assertEqual([result["goodreads_id"] for result in field_searches["loaned_to"]], ["2"])
        self.assertEqual([result["goodreads_id"] for result in field_searches["tags"]], ["2"])
        self.assertEqual(field_searches["format"], [])
        self.assertEqual(book["title"], "The Left Hand of Darkness")
        self.assertEqual(book["shelves"], ["fiction", "science-fiction"])
        self.assertEqual(book["format"], "physical")
        self.assertEqual(book["loaned_to"], "Sam")
        self.assertIsNone(missing)

    def test_search_uses_fts5_when_available(self) -> None:
        with patch("adso.catalogue._fts_index_available", return_value=True), patch(
            "adso.catalogue._search_books_fts", return_value=[{"goodreads_id": "fts"}]
        ) as fts_search:
            results = search_books(self.conn, "winter", BookFilters(format="physical"))

        self.assertEqual(results, [{"goodreads_id": "fts"}])
        fts_search.assert_called_once()

    def test_search_finds_review_text_with_fts_query(self) -> None:
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                        "My Review": "A remarkable novel about winter and society.",
                    }
                ),
            ],
        )
        import_goodreads_csv(self.conn, csv_path, mode="import")

        self.assertEqual(
            [result["goodreads_id"] for result in search_books(self.conn, "winter society")],
            ["2"],
        )

    def test_search_index_is_persistent_and_trigger_maintained(self) -> None:
        # DAV-146: a persistent FTS5 index kept current by triggers replaces the
        # per-query temp rebuild. Prove it exists and tracks insert/update/delete.
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(**{"Book Id": "2", "Title": "The Left Hand of Darkness",
                       "Author": "Ursula K. Le Guin"}),
            ],
        )
        import_goodreads_csv(self.conn, csv_path, mode="import")

        # Persistent (not a temp table): present in sqlite_master as a real table.
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books_fts'"
            ).fetchone()
        )

        # INSERT trigger: imported rows are searchable.
        self.assertEqual([b["goodreads_id"] for b in search_books(self.conn, "Le Guin")], ["2"])

        # UPDATE trigger: a local-field edit is reflected without re-importing.
        db.update_local_fields(self.conn, "1", {"local_notes": "Signed first edition"})
        self.assertEqual([b["goodreads_id"] for b in search_books(self.conn, "Signed")], ["1"])

        # DELETE trigger: merging a book out drops it from the index.
        keep_id = db.get_book_by_goodreads_id(self.conn, "1")["id"]
        drop_id = db.get_book_by_goodreads_id(self.conn, "2")["id"]
        db.merge_books(self.conn, keep_id=keep_id, drop_id=drop_id)
        self.assertEqual(search_books(self.conn, "Le Guin"), [])

    def test_cli_list_outputs_readable_rows_and_filters(self) -> None:
        csv_path = self.root / "goodreads.csv"
        db_path = self.root / "cli.sqlite"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                        "Exclusive Shelf": "read",
                        "Bookshelves": "read, fiction",
                    }
                ),
            ],
        )
        _run_cli(["--db", str(db_path), "import", "goodreads", str(csv_path)])
        conn = db.connect(db_path)
        db.update_local_fields(conn, "2", {"format": "physical"})
        conn.close()

        output = _run_cli(["--db", str(db_path), "list"])
        filtered = _run_cli(["--db", str(db_path), "list", "--format", "physical"])
        no_match = _run_cli(["--db", str(db_path), "list", "--status", "Currently Reading"])

        self.assertIn("Goodreads ID", output)
        self.assertIn("The Left Hand of Darkness", output)
        self.assertIn("The Name of the Rose", output)
        self.assertIn("physical", filtered)
        self.assertNotIn("The Name of the Rose", filtered)
        self.assertEqual(no_match.strip(), "No books found.")

    def test_cli_search_outputs_readable_rows_and_filters(self) -> None:
        csv_path = self.root / "goodreads.csv"
        db_path = self.root / "cli-search.sqlite"
        write_goodreads_csv(
            csv_path,
            [
                row(),
                row(
                    **{
                        "Book Id": "2",
                        "Title": "The Left Hand of Darkness",
                        "Author": "Ursula K. Le Guin",
                        "Bookshelves": "fiction, science-fiction",
                        "My Review": "A remarkable novel about winter and society.",
                    }
                ),
            ],
        )
        _run_cli(["--db", str(db_path), "import", "goodreads", str(csv_path)])
        conn = db.connect(db_path)
        db.update_local_fields(conn, "2", {"format": "ebook", "local_notes": "Lending copy"})
        conn.close()

        output = _run_cli(["--db", str(db_path), "search", "winter", "--format", "ebook", "--limit", "1"])
        no_match = _run_cli(["--db", str(db_path), "search", "winter", "--author", "Eco"])

        self.assertIn("Goodreads ID", output)
        self.assertIn("The Left Hand of Darkness", output)
        self.assertIn("ebook", output)
        self.assertNotIn("The Name of the Rose", output)
        self.assertEqual(no_match.strip(), "No books found.")

    def test_cli_show_outputs_grouped_detail_and_missing_book_error(self) -> None:
        csv_path = self.root / "goodreads.csv"
        db_path = self.root / "cli-show.sqlite"
        write_goodreads_csv(csv_path, [row(**{"My Rating": "5", "Date Read": "2026/04/28"})])
        _run_cli(["--db", str(db_path), "import", "goodreads", str(csv_path)])
        conn = db.connect(db_path)
        db.update_local_fields(
            conn,
            "1",
            {"format": "physical", "loaned_to": "Sam", "local_notes": "Keeper"},
        )
        conn.close()

        output = _run_cli(["--db", str(db_path), "show", "1"])

        self.assertIn("Goodreads Fields", output)
        self.assertIn("Local Catalogue Fields", output)
        self.assertIn("Title: The Name of the Rose", output)
        self.assertIn("Rating: 5", output)
        self.assertIn("Publisher: Harvest Books", output)
        self.assertIn("Binding: Paperback", output)
        self.assertIn("Number of Pages: 536", output)
        self.assertIn("Original Publication Year: 1980", output)
        self.assertIn("Format: physical", output)
        self.assertIn("Loaned To: Sam", output)
        with self.assertRaises(SystemExit) as raised:
            _run_cli(["--db", str(db_path), "show", "missing"])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_conflict_decisions_and_report_status(self) -> None:
        # Force a reading_status conflict (base To Read, local Currently Reading,
        # incoming Read), then drive it through the new decision flags.
        db_path = self.root / "cli-decide.sqlite"
        first = self.root / "first.csv"
        write_goodreads_csv(first, [row(**{"Exclusive Shelf": "to-read", "Bookshelves": "to-read"})])
        _run_cli(["--db", str(db_path), "import", "goodreads", str(first), "--no-covers"])
        conn = db.connect(db_path)
        conn.execute("UPDATE books SET reading_status = 'Currently Reading' WHERE goodreads_id = '1'")
        conn.commit()
        conn.close()
        second = self.root / "second.csv"
        write_goodreads_csv(second, [row(**{"Exclusive Shelf": "read", "Bookshelves": "read"})])
        # `import` now writes a conflict report relative to the working directory,
        # so run it from the temp root to avoid leaving a stray reports/ file.
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            _run_cli(["--db", str(db_path), "import", "goodreads", str(second), "--no-covers"])
        finally:
            os.chdir(cwd)

        conn = db.connect(db_path)
        cid = conn.execute(
            "SELECT id FROM sync_conflicts WHERE field_name = 'reading_status'"
        ).fetchone()[0]
        conn.close()

        self.assertIn(
            "marked for later review",
            _run_cli(["--db", str(db_path), "resolve", str(cid), "--review-later"]),
        )
        self.assertIn("[deferred]", _run_cli(["--db", str(db_path), "conflicts"]))
        _run_cli(["--db", str(db_path), "resolve", str(cid), "--reopen"])
        self.assertIn(
            "accepted Goodreads value",
            _run_cli(["--db", str(db_path), "resolve", str(cid), "--accept-incoming"]),
        )

        all_listed = _run_cli(["--db", str(db_path), "conflicts", "--all"])
        self.assertIn("Decided conflicts", all_listed)
        self.assertIn("via cli", all_listed)

        report = _run_cli(["--db", str(db_path), "report", "conflicts"])
        self.assertIn("Status: Resolved", report)
        self.assertIn("Decision:", report)

    def test_cli_import_surfaces_conflicts_like_sync(self) -> None:
        # `import` and `sync` are the same operation; both must write a conflict
        # report when conflicts arise. Previously `import` recorded conflicts in
        # the database but wrote no report, so they passed silently (DAV-144).
        db_path = self.root / "cli-import.sqlite"
        csv_path = self.root / "goodreads.csv"
        write_goodreads_csv(csv_path, [row()])
        _run_cli(["--db", str(db_path), "import", "goodreads", str(csv_path), "--no-covers"])

        # Diverge a tracked field locally, then re-run `import` with a moved shelf.
        conn = db.connect(db_path)
        conn.execute("UPDATE books SET reading_status = ? WHERE goodreads_id = ?", ("Local Status", "1"))
        conn.commit()
        conn.close()

        changed = self.root / "goodreads-changed.csv"
        write_goodreads_csv(changed, [row(**{"Exclusive Shelf": "read", "Bookshelves": "read"})])

        # The conflict report path is resolved relative to the working directory.
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            output = _run_cli(["--db", str(db_path), "import", "goodreads", str(changed), "--no-covers"])
        finally:
            os.chdir(cwd)

        self.assertIn("Conflict report:", output)
        report = next((self.root / "reports").glob("conflicts-import-*.md"))
        self.assertIn("reading_status", report.read_text(encoding="utf-8"))

    def test_cli_doctor_reports_without_initializing_database(self) -> None:
        db_path = self.root / "cli-doctor.sqlite"

        output = _run_cli(["--db", str(db_path), "doctor"])

        self.assertFalse(db_path.exists())
        self.assertIn("Adso Doctor", output)
        self.assertIn("Database file: no", output)

    def test_cli_notion_dry_run_and_limit_output(self) -> None:
        db_path = self.root / "cli-notion.sqlite"

        with patch(
            "adso.cli.export_to_notion",
            return_value={
                "created": 1,
                "updated": 1,
                "errors": 0,
                "actions": [
                    {"action": "create", "goodreads_id": "1", "title": "The Name of the Rose"},
                    {"action": "update", "goodreads_id": "2", "title": "The Left Hand of Darkness"},
                ],
            },
        ) as export:
            output = _run_cli(["--db", str(db_path), "export", "notion", "--dry-run", "--limit", "2"])

        export.assert_called_once()
        self.assertTrue(export.call_args.kwargs["dry_run"])
        self.assertEqual(export.call_args.kwargs["limit"], 2)
        self.assertIn("Notion dry-run complete: 1 would be created, 1 would be updated, 0 errors", output)
        self.assertIn("Would create: The Name of the Rose (Goodreads ID 1)", output)
        self.assertIn("Would update: The Left Hand of Darkness (Goodreads ID 2)", output)


class LocalFieldsFormatMigrationTests(unittest.TestCase):
    """Upgrading a catalogue created before local fields were simplified."""

    LEGACY_LOCAL_COLUMNS = ("owned", "copy_count", "location", "shelf_box")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / "adso.sqlite")
        db.initialize(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _book_columns(self) -> set[str]:
        return {r["name"] for r in self.conn.execute("PRAGMA table_info(books)")}

    def _make_legacy_schema(self) -> None:
        """Rebuild the pre-format shape: old local columns + FTS triggers over them."""
        self.conn.executescript(
            """
            DROP TRIGGER IF EXISTS books_fts_ai;
            DROP TRIGGER IF EXISTS books_fts_ad;
            DROP TRIGGER IF EXISTS books_fts_au;
            DROP TABLE IF EXISTS books_fts;
            ALTER TABLE books DROP COLUMN format;
            ALTER TABLE books ADD COLUMN owned INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE books ADD COLUMN copy_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE books ADD COLUMN location TEXT;
            ALTER TABLE books ADD COLUMN shelf_box TEXT;
            """
        )
        legacy_fields = (*db.SEARCH_FIELDS, "location", "shelf_box")
        cols = ", ".join(legacy_fields)
        new_values = ", ".join(f"new.{f}" for f in legacy_fields)
        old_values = ", ".join(f"old.{f}" for f in legacy_fields)
        self.conn.executescript(
            f"""
            CREATE VIRTUAL TABLE books_fts USING fts5(
                {cols}, content='books', content_rowid='id'
            );
            CREATE TRIGGER books_fts_ai AFTER INSERT ON books BEGIN
                INSERT INTO books_fts (rowid, {cols}) VALUES (new.id, {new_values});
            END;
            CREATE TRIGGER books_fts_ad AFTER DELETE ON books BEGIN
                INSERT INTO books_fts (books_fts, rowid, {cols})
                VALUES ('delete', old.id, {old_values});
            END;
            CREATE TRIGGER books_fts_au AFTER UPDATE ON books BEGIN
                INSERT INTO books_fts (books_fts, rowid, {cols})
                VALUES ('delete', old.id, {old_values});
                INSERT INTO books_fts (rowid, {cols}) VALUES (new.id, {new_values});
            END;
            """
        )
        self.conn.execute("INSERT INTO books_fts (books_fts) VALUES ('rebuild')")
        self.conn.commit()

    def test_legacy_catalogue_migrates_to_format(self) -> None:
        self._make_legacy_schema()
        self.conn.execute(
            """
            INSERT INTO books (goodreads_id, title, author, owned, copy_count,
                               location, shelf_box, loaned_to)
            VALUES ('1', 'The Dispossessed', 'Ursula K. Le Guin', 1, 2,
                    'Office', 'A1', 'Sam')
            """
        )
        self.conn.commit()

        db.initialize(self.conn)

        columns = self._book_columns()
        self.assertIn("format", columns)
        for column in self.LEGACY_LOCAL_COLUMNS:
            self.assertNotIn(column, columns)
        book = self.conn.execute("SELECT * FROM books WHERE goodreads_id = '1'").fetchone()
        self.assertIsNone(book["format"])
        self.assertEqual(book["loaned_to"], "Sam")

        # The FTS index was rebuilt over the new SEARCH_FIELDS: searching still
        # works, and updates don't trip triggers referencing dropped columns.
        self.assertEqual(
            [b["goodreads_id"] for b in search_books(self.conn, "sam")], ["1"]
        )
        db.update_local_fields(self.conn, "1", {"format": "physical"})
        self.assertEqual(
            self.conn.execute("SELECT format FROM books WHERE goodreads_id = '1'").fetchone()[0],
            "physical",
        )

    def test_pre_tags_catalogue_gains_tags_and_search_index(self) -> None:
        # Simulate a catalogue from before tags existed: no tags_json column,
        # FTS built over the tag-less column set.
        self.conn.executescript(
            """
            DROP TRIGGER IF EXISTS books_fts_ai;
            DROP TRIGGER IF EXISTS books_fts_ad;
            DROP TRIGGER IF EXISTS books_fts_au;
            DROP TABLE IF EXISTS books_fts;
            ALTER TABLE books DROP COLUMN tags_json;
            """
        )
        self.conn.execute(
            "INSERT INTO books (goodreads_id, title, loaned_to) VALUES ('1', 'Meditations', 'Sam')"
        )
        self.conn.commit()

        db.initialize(self.conn)

        self.assertIn("tags_json", self._book_columns())
        db.update_local_fields(self.conn, "1", {"tags_json": ["philosophy"]})
        self.assertEqual(
            [b["goodreads_id"] for b in search_books(self.conn, "philosophy")], ["1"]
        )
        self.assertEqual(
            [b["goodreads_id"] for b in list_books(self.conn, BookFilters(tag="philosophy"))],
            ["1"],
        )

    def test_initialize_is_idempotent_on_current_schema(self) -> None:
        before = self._book_columns()
        db.initialize(self.conn)
        db.initialize(self.conn)
        self.assertEqual(self._book_columns(), before)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='books_fts'"
            ).fetchone()
        )


def _run_cli(argv: list[str]) -> str:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli_main(argv)
    if exit_code != 0:
        raise AssertionError(f"CLI returned {exit_code}")
    return stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
