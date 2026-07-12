from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adso import db
from adso.metadata import (
    MAX_CANDIDATE_WORK_FETCHES,
    OPENLIBRARY_SEARCH,
    PLACES_CAP,
    SEARCH_RESULT_LIMIT,
    SUBJECTS_CAP,
    _clean_subjects,
    _clean_title,
    _parse_description,
    fetch_metadata,
)


class FakeResp:
    def __init__(self, *, status_code: int = 200, json_data=None) -> None:
        self.status_code = status_code
        self._json = json_data
        self.headers: dict[str, str] = {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def edition_payload(work_key="/works/OL1W", isbn_13=None, isbn_10=None) -> dict:
    payload: dict = {"works": [{"key": work_key}]}
    if isbn_13:
        payload["isbn_13"] = isbn_13
    if isbn_10:
        payload["isbn_10"] = isbn_10
    return payload


def work_payload(description=None, subjects=None, places=None, times=None) -> dict:
    payload: dict = {}
    if description is not None:
        payload["description"] = description
    if subjects is not None:
        payload["subjects"] = subjects
    if places is not None:
        payload["subject_places"] = places
    if times is not None:
        payload["subject_times"] = times
    return payload


def search_payload(work_key="/works/OL1W", cover_edition_key=None, edition_keys=None) -> dict:
    doc: dict = {"key": work_key}
    if cover_edition_key:
        doc["cover_edition_key"] = cover_edition_key
    if edition_keys:
        doc["edition_key"] = edition_keys
    return {"docs": [doc]}


def search_doc(work_key, author_names=None) -> dict:
    doc: dict = {"key": work_key}
    if author_names is not None:
        doc["author_name"] = author_names
    return doc


class MetadataHelperTests(unittest.TestCase):
    def test_parse_description_both_shapes(self) -> None:
        self.assertEqual(_parse_description("Plain text."), "Plain text.")
        self.assertEqual(
            _parse_description({"type": "/type/text", "value": " Wrapped. "}), "Wrapped."
        )
        self.assertIsNone(_parse_description(None))
        self.assertIsNone(_parse_description({"type": "/type/text"}))
        self.assertIsNone(_parse_description("   "))
        self.assertIsNone(_parse_description(42))

    def test_clean_subjects_filters_dedupes_and_caps(self) -> None:
        raw = [
            "Accessible book",
            "Protected DAISY",
            "nyt:hardcover-fiction=2020-01-01",
            "Mystery fiction",
            "mystery FICTION",
            "  Monastic   life  ",
            "x" * 80,
            42,
            "",
        ]
        self.assertEqual(_clean_subjects(raw, cap=25), ["Mystery fiction", "Monastic life"])
        many = [f"subject {i}" for i in range(40)]
        self.assertEqual(len(_clean_subjects(many, cap=SUBJECTS_CAP)), SUBJECTS_CAP)
        self.assertEqual(_clean_subjects("not-a-list", cap=PLACES_CAP), [])

    def test_clean_title_strips_noise(self) -> None:
        self.assertEqual(
            _clean_title("Atomic Habits: An Easy and Proven Way to Build Good Habits"),
            "Atomic Habits",
        )
        self.assertEqual(_clean_title("Red Dragon (Hannibal Lecter, #1)"), "Red Dragon")
        self.assertEqual(
            _clean_title("Blood Year: Terror and the Islamic State (Quarterly Essay, #58)"),
            "Blood Year",
        )
        self.assertEqual(_clean_title("Stoner"), "Stoner")
        # Degenerate titles fall back to the whitespace-normalised original.
        self.assertEqual(_clean_title("(untitled)"), "(untitled)")

    def test_clean_subjects_drops_non_english_tags(self) -> None:
        # OL aggregates subjects across translated editions; foreign duplicates
        # are dropped by the non-ASCII check, the foreign-word set, or the
        # Spanish century pattern.
        raw = [
            "Biografía",          # non-ASCII
            "França",             # non-ASCII
            "14e siècle",         # non-ASCII
            "Noblesse",           # foreign word
            "Kultur",             # foreign word
            "Histoire",           # foreign word
            "France, histoire",   # foreign word inside a phrase
            "S. XIV",             # Spanish century code
            "s.XV",               # Spanish century code, no space
            "Nobility",
            "Middle Ages",
            "Roman Empire",       # must survive: 'roman' is not in the word set
            "Mass media",         # must survive: 'media' is not in the word set
        ]
        self.assertEqual(
            _clean_subjects(raw, cap=25),
            ["Nobility", "Middle Ages", "Roman Empire", "Mass media"],
        )


class MetadataFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(self.root / "adso.sqlite")
        self.conn.row_factory = sqlite3.Row
        db.initialize(self.conn)
        self._sleep_patch = patch("adso.metadata.time.sleep", lambda *_a, **_k: None)
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _add_book(self, goodreads_id, title, *, isbn13=None, isbn10=None, author=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO books (goodreads_id, title, author, isbn13, isbn10) VALUES (?, ?, ?, ?, ?)",
            (goodreads_id, title, author, isbn13, isbn10),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _book(self, goodreads_id):
        return db.get_book_by_goodreads_id(self.conn, goodreads_id)

    def test_isbn_path_stores_description_and_subjects(self) -> None:
        self._add_book("1", "The Name of the Rose", isbn13="9780156001311")

        def fake_request(method, url, **kwargs):
            if url == "https://openlibrary.org/isbn/9780156001311.json":
                return FakeResp(json_data=edition_payload())
            if url == "https://openlibrary.org/works/OL1W.json":
                return FakeResp(
                    json_data=work_payload(
                        description="A mystery in a medieval abbey.",
                        subjects=["Mystery fiction", "Monasticism"],
                        places=["Italy"],
                        times=["Middle Ages"],
                    )
                )
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["isbn_backfilled"], 0)
        book = self._book("1")
        self.assertEqual(book["metadata_status"], "fetched")
        self.assertEqual(book["metadata_source"], "openlibrary:isbn")
        self.assertEqual(book["description"], "A mystery in a medieval abbey.")
        self.assertEqual(json.loads(book["subjects_json"]), ["Mystery fiction", "Monasticism"])
        self.assertEqual(json.loads(book["subject_places_json"]), ["Italy"])
        self.assertEqual(json.loads(book["subject_times_json"]), ["Middle Ages"])

    def test_dict_shaped_description_is_unwrapped(self) -> None:
        self._add_book("2", "Wrapped", isbn13="1111111111111")

        def fake_request(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload())
            return FakeResp(
                json_data=work_payload(description={"type": "/type/text", "value": "Unwrapped."})
            )

        with patch("adso.metadata._request", side_effect=fake_request):
            fetch_metadata(self.conn)

        self.assertEqual(self._book("2")["description"], "Unwrapped.")

    def test_subjects_without_description_still_counts_as_fetched(self) -> None:
        self._add_book("3", "No Blurb", isbn13="2222222222222")

        def fake_request(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload())
            return FakeResp(json_data=work_payload(subjects=["History"]))

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["fetched"], 1)
        book = self._book("3")
        self.assertEqual(book["metadata_status"], "fetched")
        self.assertIsNone(book["description"])

    def test_empty_work_is_not_found(self) -> None:
        self._add_book("4", "Empty Work", isbn13="3333333333333")

        def fake_request(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload())
            return FakeResp(json_data=work_payload())

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["not_found"], 1)
        self.assertEqual(self._book("4")["metadata_status"], "not_found")

    def test_search_path_backfills_missing_isbns(self) -> None:
        self._add_book("5", "No ISBN Here", author="A. Writer")

        def fake_request(method, url, **kwargs):
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data=search_payload(cover_edition_key="OL1M"))
            if url == "https://openlibrary.org/works/OL1W.json":
                return FakeResp(json_data=work_payload(description="Found via search."))
            if url == "https://openlibrary.org/books/OL1M.json":
                return FakeResp(
                    json_data=edition_payload(isbn_13=["9780000000001"], isbn_10=["0000000001"])
                )
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["isbn_backfilled"], 1)
        book = self._book("5")
        self.assertEqual(book["metadata_source"], "openlibrary:search")
        self.assertEqual(book["isbn13"], "9780000000001")
        self.assertEqual(book["isbn10"], "0000000001")

    def test_search_path_never_fetches_edition_when_isbn_present(self) -> None:
        self._add_book("6", "Has ISBN", isbn13="4444444444444")
        urls = []

        def fake_request(method, url, **kwargs):
            urls.append(url)
            if "isbn/4444444444444" in url:
                return FakeResp(status_code=404)
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data=search_payload())
            if "works" in url:
                return FakeResp(json_data=work_payload(description="Via search."))
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.metadata._request", side_effect=fake_request):
            fetch_metadata(self.conn)

        book = self._book("6")
        self.assertEqual(book["isbn13"], "4444444444444")  # untouched
        self.assertFalse(any("/books/" in url for url in urls))  # no edition fetch

    def test_backfill_isbns_never_overwrites(self) -> None:
        book_id = self._add_book("7", "Filled", isbn13="5555555555555")
        changed = db.backfill_isbns(self.conn, book_id, isbn13="9999999999999", isbn10="123456789X")
        book = self._book("7")
        self.assertEqual(book["isbn13"], "5555555555555")
        self.assertEqual(book["isbn10"], "123456789X")  # was empty -> filled
        self.assertTrue(changed)

    def test_idempotency_refresh_and_retry_missing(self) -> None:
        self._add_book("8", "Once", isbn13="6666666666666")

        def hit(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload())
            return FakeResp(json_data=work_payload(description="Hit."))

        def miss(method, url, **kwargs):
            return FakeResp(status_code=404)

        with patch("adso.metadata._request", side_effect=hit):
            first = fetch_metadata(self.conn)
        self.assertEqual(first["fetched"], 1)

        # Already fetched -> skipped without any HTTP.
        with patch("adso.metadata._request", side_effect=AssertionError("no requests expected")):
            second = fetch_metadata(self.conn)
        self.assertEqual(second["skipped"], 1)

        # --refresh reconsiders it.
        with patch("adso.metadata._request", side_effect=miss):
            third = fetch_metadata(self.conn, refresh=True)
        self.assertEqual(third["not_found"], 1)

        # not_found is skipped unless --retry-missing.
        with patch("adso.metadata._request", side_effect=AssertionError("no requests expected")):
            fourth = fetch_metadata(self.conn)
        self.assertEqual(fourth["skipped"], 1)
        with patch("adso.metadata._request", side_effect=hit):
            fifth = fetch_metadata(self.conn, retry_missing=True)
        self.assertEqual(fifth["fetched"], 1)

    def test_dry_run_writes_nothing_and_limit_caps_attempts(self) -> None:
        self._add_book("9", "Dry", isbn13="7777777777777")
        self._add_book("10", "Beyond Limit", isbn13="8888888888888")

        def fake_request(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload())
            return FakeResp(json_data=work_payload(description="Dry run."))

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn, dry_run=True, limit=1)

        self.assertEqual(result["fetched"], 1)
        self.assertIsNone(self._book("9")["metadata_status"])
        self.assertIsNone(self._book("10")["metadata_status"])

    def test_persistent_429_is_bounded_and_treated_as_miss(self) -> None:
        self._add_book("11", "Throttled", isbn13="9999999999990")

        class FakeRequests:
            def __init__(self):
                self.calls = 0

            def request(self, *a, **k):
                self.calls += 1
                return FakeResp(status_code=429)

        fake = FakeRequests()
        with patch("adso.ol_http.require_requests", return_value=fake), patch(
            "adso.ol_http.time.sleep", lambda *_a, **_k: None
        ):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["not_found"], 1)
        self.assertEqual(self._book("11")["metadata_status"], "not_found")
        self.assertLess(fake.calls, 20)

    def test_empty_isbn_work_falls_through_to_search(self) -> None:
        # A skeleton work behind the ISBN must not stop resolution: a sibling
        # work found via search carries the content.
        self._add_book("12", "Skeleton", isbn13="1212121212121", author="A. Writer")

        def fake_request(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload(work_key="/works/OLEMPTYW"))
            if url == "https://openlibrary.org/works/OLEMPTYW.json":
                return FakeResp(json_data=work_payload())
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data={"docs": [search_doc("/works/OLFULLW", ["A. Writer"])]})
            if url == "https://openlibrary.org/works/OLFULLW.json":
                return FakeResp(json_data=work_payload(description="From the sibling work."))
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["fetched"], 1)
        book = self._book("12")
        self.assertEqual(book["metadata_status"], "fetched")
        self.assertEqual(book["metadata_source"], "openlibrary:search")
        self.assertEqual(book["description"], "From the sibling work.")

    def test_search_uses_cleaned_title_and_filters_wrong_authors(self) -> None:
        self._add_book(
            "13",
            "Atomic Habits: An Easy and Proven Way to Build Good Habits (Bestseller)",
            author="James Clear",
        )
        search_params = []

        def fake_request(method, url, **kwargs):
            if url == OPENLIBRARY_SEARCH:
                search_params.append(kwargs.get("params") or {})
                return FakeResp(
                    json_data={
                        "docs": [
                            search_doc("/works/OLWRONGW", ["Somebody Else"]),
                            search_doc("/works/OLRIGHTW", ["James Clear"]),
                        ]
                    }
                )
            if url == "https://openlibrary.org/works/OLRIGHTW.json":
                return FakeResp(json_data=work_payload(description="Tiny changes."))
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(search_params[0]["title"], "Atomic Habits")
        self.assertEqual(search_params[0]["limit"], SEARCH_RESULT_LIMIT)
        # The wrong-author doc was never fetched; the right one won.
        self.assertEqual(self._book("13")["description"], "Tiny changes.")

    def test_q_fallback_when_title_search_returns_nothing(self) -> None:
        # OL titles the record "City Lost and Found"; the title= field search
        # misses it but the full-text q= search does not.
        self._add_book("14", "A City Lost and Found: Whelan the Wrecker", author="Robyn Annear")
        queries = []

        def fake_request(method, url, **kwargs):
            if url == OPENLIBRARY_SEARCH:
                params = kwargs.get("params") or {}
                queries.append(params)
                if "q" in params:
                    return FakeResp(json_data={"docs": [search_doc("/works/OLQW", ["Robyn Annear"])]})
                return FakeResp(json_data={"docs": []})
            if url == "https://openlibrary.org/works/OLQW.json":
                return FakeResp(json_data=work_payload(subjects=["Melbourne history"]))
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(queries[-1]["q"], "A City Lost and Found Robyn Annear")
        self.assertEqual(self._book("14")["metadata_source"], "openlibrary:search")

    def test_refresh_not_found_preserves_previous_content(self) -> None:
        self._add_book("15", "Keeper", isbn13="1515151515151")

        def hit(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload())
            return FakeResp(json_data=work_payload(description="Owned content."))

        def miss(method, url, **kwargs):
            return FakeResp(status_code=404)

        with patch("adso.metadata._request", side_effect=hit):
            fetch_metadata(self.conn)
        with patch("adso.metadata._request", side_effect=miss):
            result = fetch_metadata(self.conn, refresh=True)

        self.assertEqual(result["not_found"], 1)
        book = self._book("15")
        self.assertEqual(book["metadata_status"], "not_found")
        self.assertEqual(book["description"], "Owned content.")  # not cleared

    def test_matched_empty_records_provenance(self) -> None:
        self._add_book("16", "Skeleton Only", isbn13="1616161616161")

        def fake_request(method, url, **kwargs):
            if "isbn" in url:
                return FakeResp(json_data=edition_payload())
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data={"docs": []})
            return FakeResp(json_data=work_payload())  # empty work

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["not_found"], 1)
        self.assertEqual(result["actions"][0].get("matched_empty"), "yes")
        book = self._book("16")
        self.assertEqual(book["metadata_status"], "not_found")
        self.assertEqual(book["metadata_source"], "openlibrary:isbn")  # skeleton, not absent

    def test_candidate_work_fetches_are_bounded(self) -> None:
        self._add_book("17", "All Empty", author="A. Writer")
        work_urls = []

        def fake_request(method, url, **kwargs):
            if url == OPENLIBRARY_SEARCH:
                docs = [search_doc(f"/works/OL{i}W", ["A. Writer"]) for i in range(10)]
                return FakeResp(json_data={"docs": docs})
            if "/works/" in url:
                work_urls.append(url)
                return FakeResp(json_data=work_payload())  # every candidate is empty
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.metadata._request", side_effect=fake_request):
            result = fetch_metadata(self.conn)

        self.assertEqual(result["not_found"], 1)
        self.assertLessEqual(len(work_urls), MAX_CANDIDATE_WORK_FETCHES)


class MetadataMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = db.connect(self.root / "adso.sqlite")
        db.initialize(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_pre_metadata_catalogue_gains_columns_and_search_index(self) -> None:
        # Simulate a catalogue from before metadata: no metadata columns,
        # FTS built over the metadata-less column set.
        self.conn.executescript(
            """
            DROP TRIGGER IF EXISTS books_fts_ai;
            DROP TRIGGER IF EXISTS books_fts_ad;
            DROP TRIGGER IF EXISTS books_fts_au;
            DROP TABLE IF EXISTS books_fts;
            ALTER TABLE books DROP COLUMN description;
            ALTER TABLE books DROP COLUMN subjects_json;
            ALTER TABLE books DROP COLUMN subject_places_json;
            ALTER TABLE books DROP COLUMN subject_times_json;
            ALTER TABLE books DROP COLUMN metadata_source;
            ALTER TABLE books DROP COLUMN metadata_source_url;
            ALTER TABLE books DROP COLUMN metadata_status;
            ALTER TABLE books DROP COLUMN metadata_fetched_at;
            """
        )
        self.conn.execute(
            "INSERT INTO books (goodreads_id, title, loaned_to) VALUES ('1', 'Meditations', 'Sam')"
        )
        self.conn.commit()

        db.initialize(self.conn)

        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(books)")}
        self.assertIn("description", columns)
        self.assertIn("subjects_json", columns)
        self.assertIn("metadata_status", columns)

        from adso.catalogue import search_books

        book_id = int(self.conn.execute("SELECT id FROM books").fetchone()["id"])
        db.set_metadata(
            self.conn,
            book_id,
            description="Stoic reflections.",
            subjects=["Stoicism"],
            subject_places=["Rome"],
            subject_times=[],
            metadata_source="openlibrary:isbn",
            metadata_source_url="https://openlibrary.org/isbn/x.json",
            metadata_status="fetched",
        )
        # FTS was rebuilt over the new tuple: subjects and places are searchable,
        # and so are pre-existing indexed fields.
        self.assertEqual([b["goodreads_id"] for b in search_books(self.conn, "stoicism")], ["1"])
        self.assertEqual([b["goodreads_id"] for b in search_books(self.conn, "rome")], ["1"])
        self.assertEqual([b["goodreads_id"] for b in search_books(self.conn, "sam")], ["1"])


if __name__ == "__main__":
    unittest.main()
