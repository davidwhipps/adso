"""Tests for informational (non-tracked) sync fields like average_rating."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from adso import db
from adso.sync import import_goodreads_csv

HEADERS = [
    "Book Id", "Title", "Author", "Additional Authors", "ISBN", "ISBN13",
    "Exclusive Shelf", "Bookshelves", "My Rating", "Average Rating",
    "Number of Pages", "Original Publication Year", "Publisher",
]


def _base_row(**overrides) -> dict:
    row = {
        "Book Id": "1", "Title": "A Book", "Author": "An Author",
        "Additional Authors": "Helper One", "ISBN": "1234567890", "ISBN13": "9781234567890",
        "Exclusive Shelf": "to-read", "Bookshelves": "", "My Rating": "0",
        "Average Rating": "4.23", "Number of Pages": "100",
        "Original Publication Year": "2000", "Publisher": "Text Publishing",
    }
    row.update(overrides)
    return row


def _write(path: Path, row: dict) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        w.writerow(row)


class InformationalFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / "adso.sqlite")
        db.initialize(self.conn)
        first = self.root / "first.csv"
        _write(first, _base_row(**{"Average Rating": "4.23"}))
        import_goodreads_csv(self.conn, first, mode="import")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _avg(self) -> str:
        return self.conn.execute(
            "SELECT average_rating FROM books WHERE goodreads_id='1'"
        ).fetchone()[0]

    def test_average_rating_drift_is_not_an_update(self) -> None:
        changed = self.root / "changed.csv"
        _write(changed, _base_row(**{"Average Rating": "4.50"}))
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.conflicts, 0)
        # ...but the value is still refreshed for display.
        self.assertEqual(self._avg(), "4.50")

    def test_blank_informational_value_never_erases_real_data(self) -> None:
        # An empty incoming value must not blank stored data — this protects
        # enrichment backfills (e.g. OL ISBNs) and ordinary informational
        # fields alike from being silently erased.
        changed = self.root / "blank.csv"
        _write(changed, _base_row(**{"Average Rating": ""}))
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(self._avg(), "4.23")  # retained, not blanked

    def test_isbn_backfill_survives_resync_with_empty_isbn(self) -> None:
        # Simulate a Goodreads row with no ISBNs, then an OL backfill, then a
        # re-sync of the same empty row: the backfilled ISBN must be retained
        # and no conflict raised.
        first = self.root / "noisbn.csv"
        _write(first, _base_row(**{"Book Id": "2", "Title": "No ISBN", "ISBN": "", "ISBN13": ""}))
        import_goodreads_csv(self.conn, first, mode="sync")
        book_id = int(
            self.conn.execute("SELECT id FROM books WHERE goodreads_id='2'").fetchone()[0]
        )
        self.assertTrue(db.backfill_isbns(self.conn, book_id, isbn13="9780000000001"))

        again = self.root / "noisbn-again.csv"
        _write(again, _base_row(**{"Book Id": "2", "Title": "No ISBN", "ISBN": "", "ISBN13": ""}))
        summary = import_goodreads_csv(self.conn, again, mode="sync")

        isbn13 = self.conn.execute(
            "SELECT isbn13 FROM books WHERE goodreads_id='2'"
        ).fetchone()[0]
        self.assertEqual(isbn13, "9780000000001")
        self.assertEqual(summary.conflicts, 0)

    def test_nonempty_goodreads_isbn_still_wins_over_backfill(self) -> None:
        # Informational fields stay Goodreads-authoritative: a real incoming
        # ISBN replaces a backfilled one silently.
        first = self.root / "noisbn2.csv"
        _write(first, _base_row(**{"Book Id": "3", "Title": "Late ISBN", "ISBN": "", "ISBN13": ""}))
        import_goodreads_csv(self.conn, first, mode="sync")
        book_id = int(
            self.conn.execute("SELECT id FROM books WHERE goodreads_id='3'").fetchone()[0]
        )
        db.backfill_isbns(self.conn, book_id, isbn13="9780000000001")

        update = self.root / "lateisbn.csv"
        _write(update, _base_row(**{"Book Id": "3", "Title": "Late ISBN", "ISBN13": "9785555555555"}))
        summary = import_goodreads_csv(self.conn, update, mode="sync")

        isbn13 = self.conn.execute(
            "SELECT isbn13 FROM books WHERE goodreads_id='3'"
        ).fetchone()[0]
        self.assertEqual(isbn13, "9785555555555")
        self.assertEqual(summary.conflicts, 0)
        self.assertEqual(summary.updated, 0)  # informational: silent refresh

    def test_publisher_relabel_is_not_an_update(self) -> None:
        changed = self.root / "pub.csv"
        _write(changed, _base_row(**{"Publisher": "TEXT EBOOK"}))
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.conflicts, 0)
        publisher = self.conn.execute(
            "SELECT publisher FROM books WHERE goodreads_id='1'"
        ).fetchone()[0]
        self.assertEqual(publisher, "TEXT EBOOK")  # still refreshed for display

    def test_metadata_drift_is_not_an_update(self) -> None:
        # Page counts, ISBNs, edition year, extra authors all drift on Goodreads.
        changed = self.root / "meta.csv"
        _write(changed, _base_row(**{
            "Number of Pages": "222",
            "ISBN": "0987654321",
            "ISBN13": "9780987654321",
            "Original Publication Year": "1999",
            "Additional Authors": "Helper Two",
        }))
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.conflicts, 0)
        # ...but the values are still refreshed for display.
        pages = self.conn.execute(
            "SELECT number_of_pages FROM books WHERE goodreads_id='1'"
        ).fetchone()[0]
        self.assertEqual(pages, 222)

    def test_case_only_title_change_is_not_an_update(self) -> None:
        changed = self.root / "case.csv"
        _write(changed, _base_row(**{"Title": "a  BOOK"}))  # only case/whitespace differ
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.conflicts, 0)

    def test_edition_tag_change_is_not_an_update(self) -> None:
        changed = self.root / "edition.csv"
        _write(changed, _base_row(**{"Title": "A Book (Vintage International)"}))
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.unchanged, 1)
        self.assertEqual(summary.conflicts, 0)

    def test_subtitle_change_still_counts(self) -> None:
        changed = self.root / "subtitle.csv"
        _write(changed, _base_row(**{"Title": "A Book: A Health Resort Horror Story"}))
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 1)
        self.assertEqual(summary.conflicts, 0)

    def test_title_change_still_counts(self) -> None:
        # Identity fields stay tracked so a bad-data change surfaces.
        changed = self.root / "title.csv"
        _write(changed, _base_row(**{"Title": "A Completely Different Book"}))
        summary = import_goodreads_csv(self.conn, changed, mode="sync")
        self.assertEqual(summary.updated, 1)
        self.assertEqual(summary.conflicts, 0)
        title = self.conn.execute(
            "SELECT title FROM books WHERE goodreads_id='1'"
        ).fetchone()[0]
        self.assertEqual(title, "A Completely Different Book")


class FloatStringRatingTests(unittest.TestCase):
    """Goodreads started exporting "My Rating" as a float string ("5.0") in
    July 2026; strict int() parsing turned every rating into NULL and a sync
    then wiped them all. These pin the fix."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / "adso.sqlite")
        db.initialize(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _rating(self):
        return self.conn.execute(
            "SELECT rating FROM books WHERE goodreads_id='1'"
        ).fetchone()[0]

    def test_parse_int_accepts_integral_float_strings(self) -> None:
        from adso.goodreads import parse_int

        self.assertEqual(parse_int("5.0"), 5)
        self.assertEqual(parse_int("0.0"), 0)
        self.assertEqual(parse_int("5"), 5)
        self.assertIsNone(parse_int("4.5"))
        self.assertIsNone(parse_int("abc"))
        self.assertIsNone(parse_int(""))
        self.assertIsNone(parse_int(None))

    def test_float_rating_imports_as_integer(self) -> None:
        first = self.root / "first.csv"
        _write(first, _base_row(**{"My Rating": "5.0"}))
        import_goodreads_csv(self.conn, first, mode="import")
        self.assertEqual(self._rating(), 5)

    def test_sync_with_float_rating_does_not_wipe_existing_rating(self) -> None:
        first = self.root / "first.csv"
        _write(first, _base_row(**{"My Rating": "5"}))
        import_goodreads_csv(self.conn, first, mode="import")

        resync = self.root / "resync.csv"
        _write(resync, _base_row(**{"My Rating": "5.0"}))
        summary = import_goodreads_csv(self.conn, resync, mode="sync")

        self.assertEqual(self._rating(), 5)
        self.assertEqual(summary.updated, 0)
        self.assertEqual(summary.unchanged, 1)


if __name__ == "__main__":
    unittest.main()
