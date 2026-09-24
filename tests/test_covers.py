from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adso import db
from adso.covers import (
    GOODREADS_AUTOCOMPLETE,
    ITUNES_SEARCH,
    OPENLIBRARY_SEARCH,
    CoversError,
    fetch_covers,
    set_manual_cover,
)

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
NOT_AN_IMAGE = b"<html>404 not found</html>"


class FakeResp:
    def __init__(
        self, *, status_code: int = 200, content: bytes = b"", json_data=None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.text = text
        self._json = json_data
        self.headers: dict[str, str] = {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def ol_search_payload(cover_id) -> dict:
    return {"docs": ([{"cover_i": cover_id}] if cover_id else [])}


class CoversTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(self.root / "adso.sqlite")
        self.conn.row_factory = sqlite3.Row
        db.initialize(self.conn)
        # No real network sleeps in tests.
        self._sleep_patch = patch("adso.covers.time.sleep", lambda *_a, **_k: None)
        self._sleep_patch.start()
        # These tests exercise the Open Library / iTunes fallbacks; Goodreads is
        # covered separately in GoodreadsCoversTests.
        self._goodreads_patch = patch("adso.covers._goodreads_cover", return_value=None)
        self._goodreads_patch.start()

    def tearDown(self) -> None:
        self._goodreads_patch.stop()
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

    def test_openlibrary_hit_by_isbn(self) -> None:
        self._add_book("1", "The Name of the Rose", isbn13="9780156001311")

        def fake_request(method, url, **kwargs):
            if url.startswith("https://covers.openlibrary.org"):
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=fake_request):
            result = fetch_covers(self.conn, self.root)

        self.assertEqual(result["fetched"], 1)
        book = self._book("1")
        self.assertEqual(book["cover_status"], "fetched")
        self.assertEqual(book["cover_source"], "openlibrary:isbn")
        self.assertEqual(book["cover_path"], "covers/1.jpg")
        self.assertTrue((self.root / "covers" / "1.jpg").is_file())

    def test_isbn_cover_404_falls_back_to_search(self) -> None:
        self._add_book("2", "Some Book", isbn13="9999999999999", author="A. Writer")
        cover_image = "https://covers.openlibrary.org/b/id/555-L.jpg?default=false"

        def fake_request(method, url, **kwargs):
            if url.startswith("https://covers.openlibrary.org/b/isbn/"):
                return FakeResp(status_code=404)
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data=ol_search_payload(555))
            if url == cover_image:
                return FakeResp(content=PNG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=fake_request):
            result = fetch_covers(self.conn, self.root)

        self.assertEqual(result["fetched"], 1)
        book = self._book("2")
        self.assertEqual(book["cover_source"], "openlibrary:search")
        self.assertEqual(book["cover_path"], "covers/2.png")
        self.assertTrue((self.root / "covers" / "2.png").is_file())

    def test_search_by_title_when_no_isbn(self) -> None:
        self._add_book("3", "Coverless", author="Anon")
        cover_image = "https://covers.openlibrary.org/b/id/777-L.jpg?default=false"
        seen_params = []

        def fake_request(method, url, **kwargs):
            if url == OPENLIBRARY_SEARCH:
                seen_params.append(kwargs.get("params", {}))
                return FakeResp(json_data=ol_search_payload(777))
            if url == cover_image:
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=fake_request):
            result = fetch_covers(self.conn, self.root)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(self._book("3")["cover_source"], "openlibrary:search")
        self.assertEqual(seen_params[-1].get("title"), "Coverless")
        self.assertEqual(seen_params[-1].get("author"), "Anon")

    def test_non_image_body_is_rejected_as_not_found(self) -> None:
        self._add_book("4", "Trick", isbn13="123")

        def fake_request(method, url, **kwargs):
            if url.startswith("https://covers.openlibrary.org"):
                return FakeResp(content=NOT_AN_IMAGE)  # 200 but not an image
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data=ol_search_payload(None))
            if url == ITUNES_SEARCH:
                return FakeResp(json_data={"results": []})
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=fake_request):
            result = fetch_covers(self.conn, self.root)

        self.assertEqual(result["not_found"], 1)
        book = self._book("4")
        self.assertEqual(book["cover_status"], "not_found")
        self.assertIsNone(book["cover_path"])
        self.assertFalse((self.root / "covers").exists())

    def test_falls_back_to_itunes_when_open_library_misses(self) -> None:
        self._add_book("9", "Indie Title", isbn13="9999999999999", author="Small Press")
        artwork = "https://is1-ssl.mzstatic.com/image/thumb/abc/600x600bb.jpg"

        def fake_request(method, url, **kwargs):
            if url.startswith("https://covers.openlibrary.org"):
                return FakeResp(status_code=404)
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data=ol_search_payload(None))
            if url == ITUNES_SEARCH:
                return FakeResp(json_data={"results": [{"artworkUrl100":
                    "https://is1-ssl.mzstatic.com/image/thumb/abc/100x100bb.jpg"}]})
            if url == artwork:
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=fake_request):
            result = fetch_covers(self.conn, self.root)

        self.assertEqual(result["fetched"], 1)
        book = self._book("9")
        self.assertEqual(book["cover_source"], "itunes:search")
        self.assertEqual(book["cover_source_url"], artwork)  # 100x100 upscaled to 600x600
        self.assertTrue((self.root / "covers" / "9.jpg").is_file())

    def test_retry_missing_reattempts_not_found_only(self) -> None:
        self._add_book("10", "Was Missing", isbn13="9780156001311")
        # First pass: everything misses -> not_found.
        def all_miss(method, url, **kwargs):
            if url.startswith("https://covers.openlibrary.org"):
                return FakeResp(status_code=404)
            if url == OPENLIBRARY_SEARCH:
                return FakeResp(json_data=ol_search_payload(None))
            if url == ITUNES_SEARCH:
                return FakeResp(json_data={"results": []})
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=all_miss):
            fetch_covers(self.conn, self.root)
        self.assertEqual(self._book("10")["cover_status"], "not_found")

        # A normal run skips not_found; --retry-missing re-attempts it.
        def ol_hit(m, u, **k):
            if u.startswith("https://covers.openlibrary.org"):
                return FakeResp(content=JPEG_BYTES)
            return FakeResp(status_code=404)

        with patch("adso.covers._request", side_effect=ol_hit):
            plain = fetch_covers(self.conn, self.root)
            retry = fetch_covers(self.conn, self.root, retry_missing=True)

        self.assertEqual(plain["skipped"], 1)   # not_found skipped on a normal run
        self.assertEqual(plain["fetched"], 0)
        self.assertEqual(retry["fetched"], 1)   # retry_missing re-attempts it
        self.assertEqual(self._book("10")["cover_status"], "fetched")

    def test_idempotency_and_refresh(self) -> None:
        self._add_book("5", "Repeat", isbn13="9780156001311")

        def fake_request(method, url, **kwargs):
            if url.startswith("https://covers.openlibrary.org"):
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=fake_request) as mock:
            first = fetch_covers(self.conn, self.root)
            calls_after_first = mock.call_count
            second = fetch_covers(self.conn, self.root)
            calls_after_second = mock.call_count
            third = fetch_covers(self.conn, self.root, refresh=True)

        self.assertEqual(first["fetched"], 1)
        self.assertEqual(second["fetched"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(calls_after_second, calls_after_first)  # second run made no requests
        self.assertEqual(third["fetched"], 1)  # refresh re-fetches

    def test_manual_cover_is_never_overwritten(self) -> None:
        self._add_book("6", "Manual", isbn13="9780156001311")
        source = self.root / "my_cover.png"
        source.write_bytes(PNG_BYTES)

        set_manual_cover(self.conn, self.root, "6", file=str(source))
        book = self._book("6")
        self.assertEqual(book["cover_status"], "manual")
        self.assertEqual(book["cover_path"], "covers/6.png")

        # A refresh fetch must skip manual covers entirely (no network calls).
        with patch("adso.covers._request", side_effect=AssertionError("should not fetch")):
            result = fetch_covers(self.conn, self.root, refresh=True)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self._book("6")["cover_status"], "manual")

    def test_dry_run_writes_nothing(self) -> None:
        self._add_book("7", "Preview", isbn13="9780156001311")

        def fake_request(method, url, **kwargs):
            if url.startswith("https://covers.openlibrary.org"):
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        with patch("adso.covers._request", side_effect=fake_request):
            result = fetch_covers(self.conn, self.root, dry_run=True)

        self.assertEqual(result["fetched"], 1)
        self.assertFalse((self.root / "covers").exists())
        self.assertIsNone(self._book("7")["cover_status"])

    def test_limit_caps_attempts(self) -> None:
        for i in range(3):
            self._add_book(str(100 + i), f"Book {i}", isbn13="9780156001311")

        def fake_request(method, url, **kwargs):
            return FakeResp(content=JPEG_BYTES)

        with patch("adso.covers._request", side_effect=fake_request):
            result = fetch_covers(self.conn, self.root, limit=2)

        self.assertEqual(result["fetched"], 2)

    def test_persistent_429_is_bounded_and_treated_as_miss(self) -> None:
        # Regression: a throttling host (HTTP 429) must not loop forever — the
        # real _request caps retries, so a book just ends up not_found.
        self._add_book("8", "Throttled", isbn13="9780156001311")

        class FakeRequests:
            def __init__(self):
                self.calls = 0

            def request(self, *a, **k):
                self.calls += 1
                return FakeResp(status_code=429)

        fake = FakeRequests()
        # The request loop (and its 429 cap) lives in the shared ol_http client.
        with patch("adso.ol_http.require_requests", return_value=fake):
            result = fetch_covers(self.conn, self.root)

        self.assertEqual(result["not_found"], 1)
        self.assertEqual(self._book("8")["cover_status"], "not_found")
        # Bounded: 3 attempts per request, a finite number of requests per book.
        self.assertLess(fake.calls, 20)

    def _ol_isbn_hit(self, method, url, **kwargs):
        if url.startswith("https://covers.openlibrary.org"):
            return FakeResp(content=JPEG_BYTES)
        raise AssertionError(f"unexpected request to {url}")

    def test_locked_catalogue_write_is_retried(self) -> None:
        self._add_book("20", "Busy", isbn13="9780156001311")
        real_set_cover = db.set_cover
        attempts = []

        def flaky_set_cover(*args, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
            return real_set_cover(*args, **kwargs)

        with patch("adso.covers._request", side_effect=self._ol_isbn_hit), patch(
            "adso.covers.db.set_cover", side_effect=flaky_set_cover
        ):
            result = fetch_covers(self.conn, self.root)

        self.assertEqual((result["fetched"], result["locked"]), (1, 0))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self._book("20")["cover_status"], "fetched")
        self.assertEqual(sorted(p.name for p in (self.root / "covers").iterdir()), ["20.jpg"])

    def test_persistently_locked_book_is_left_untouched(self) -> None:
        self._add_book("21", "Held", isbn13="9780156001311")
        (self.root / "covers").mkdir()
        (self.root / "covers" / "21.png").write_bytes(PNG_BYTES)
        db.set_cover(
            self.conn,
            int(self._book("21")["id"]),
            cover_path="covers/21.png",
            cover_source="itunes:search",
            cover_source_url="https://example.com/old.png",
            cover_status="fetched",
        )

        with patch("adso.covers._request", side_effect=self._ol_isbn_hit), patch(
            "adso.covers.db.set_cover", side_effect=sqlite3.OperationalError("database is locked")
        ):
            result = fetch_covers(self.conn, self.root, refresh=True)

        self.assertEqual((result["fetched"], result["locked"]), (0, 1))
        book = self._book("21")
        self.assertEqual(book["cover_source"], "itunes:search")
        # The old file survives and no staged or new file is left behind.
        self.assertEqual(sorted(p.name for p in (self.root / "covers").iterdir()), ["21.png"])

    def test_run_stops_cleanly_when_catalogue_stays_locked(self) -> None:
        for n in range(5):
            self._add_book(str(30 + n), f"Book {n}", isbn13="9780156001311")

        with patch("adso.covers._request", side_effect=self._ol_isbn_hit), patch(
            "adso.covers.db.set_cover", side_effect=sqlite3.OperationalError("database is locked")
        ), self.assertRaisesRegex(CoversError, "stayed locked"):
            fetch_covers(self.conn, self.root)

    def test_other_database_errors_are_not_swallowed(self) -> None:
        self._add_book("22", "Broken", isbn13="9780156001311")
        with patch("adso.covers._request", side_effect=self._ol_isbn_hit), patch(
            "adso.covers.db.set_cover", side_effect=sqlite3.OperationalError("no such column: x")
        ), self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
            fetch_covers(self.conn, self.root)

    def test_migration_adds_cover_columns(self) -> None:
        # Build a pre-cover books table, then prove initialize() backfills columns.
        legacy = sqlite3.connect(self.root / "legacy.sqlite")
        legacy.row_factory = sqlite3.Row
        legacy.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goodreads_id TEXT UNIQUE,
                title TEXT NOT NULL,
                shelves_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        legacy.commit()
        self.assertNotIn(
            "cover_path", {r["name"] for r in legacy.execute("PRAGMA table_info(books)")}
        )

        db.initialize(legacy)

        columns = {r["name"] for r in legacy.execute("PRAGMA table_info(books)")}
        for column in ("cover_path", "cover_source", "cover_source_url", "cover_status", "cover_fetched_at"):
            self.assertIn(column, columns)
        legacy.close()


if __name__ == "__main__":
    unittest.main()


def goodreads_page(image_url: str) -> str:
    return f'<html><head><meta property="og:image" content="{image_url}"></head></html>'


GR_IMAGE = "https://m.media-amazon.com/images/S/compressed.photo.goodreads.com/books/1i/42.jpg"
GR_THUMB = "https://i.gr-assets.com/images/S/compressed.photo.goodreads.com/books/1i/42._SY75_.jpg"
GR_FULL = "https://i.gr-assets.com/images/S/compressed.photo.goodreads.com/books/1i/42.jpg"
GR_PAGE = "https://www.goodreads.com/book/show/42.xml"


class GoodreadsCoversTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(self.root / "adso.sqlite")
        self.conn.row_factory = sqlite3.Row
        db.initialize(self.conn)
        self._sleep_patch = patch("adso.covers.time.sleep", lambda *_a, **_k: None)
        self._sleep_patch.start()
        self.conn.execute(
            "INSERT INTO books (goodreads_id, title, author, isbn13) VALUES (?, ?, ?, ?)",
            ("42", "Red Dragon (Hannibal Lecter, #1)", "Thomas  Harris", "9780399124426"),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self._sleep_patch.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _run(self, fake_request):
        with patch("adso.covers._request", side_effect=fake_request):
            fetch_covers(self.conn, self.root)
        return db.get_book_by_goodreads_id(self.conn, "42")

    def test_book_page_og_image_wins(self) -> None:
        def fake_request(method, url, **kwargs):
            if url == GR_PAGE:
                return FakeResp(text=goodreads_page(GR_IMAGE))
            if url == GR_IMAGE:
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        book = self._run(fake_request)
        self.assertEqual(book["cover_source"], "goodreads:page")
        self.assertEqual(book["cover_source_url"], GR_IMAGE)
        self.assertTrue((self.root / "covers" / "42.jpg").is_file())

    def test_waf_challenge_falls_through_to_exact_id_autocomplete(self) -> None:
        queries = []

        def fake_request(method, url, **kwargs):
            if url == GR_PAGE:
                return FakeResp(status_code=202)  # AWS WAF challenge
            if url == GOODREADS_AUTOCOMPLETE:
                queries.append(kwargs["params"]["q"])
                return FakeResp(json_data=[
                    {"bookId": "999", "imageUrl": "https://example.com/wrong-edition.jpg"},
                    {"bookId": "42", "imageUrl": GR_THUMB},
                ])
            if url == GR_FULL:
                return FakeResp(content=PNG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        book = self._run(fake_request)
        self.assertEqual(book["cover_source"], "goodreads:search")
        self.assertEqual(book["cover_source_url"], GR_FULL)  # size suffix stripped
        self.assertEqual(queries, ["9780399124426"])

    def test_autocomplete_tries_bare_title_queries(self) -> None:
        queries = []

        def fake_request(method, url, **kwargs):
            if url == GR_PAGE:
                return FakeResp(status_code=202)
            if url == GOODREADS_AUTOCOMPLETE:
                queries.append(kwargs["params"]["q"])
                hit = kwargs["params"]["q"] == "Red Dragon"
                return FakeResp(json_data=[{"bookId": "42", "imageUrl": GR_THUMB}] if hit else [])
            if url == GR_FULL:
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        book = self._run(fake_request)
        self.assertEqual(book["cover_source"], "goodreads:search")
        self.assertEqual(queries, ["9780399124426", "Red Dragon Thomas Harris", "Red Dragon"])

    def test_placeholder_and_misses_fall_back_to_open_library(self) -> None:
        def fake_request(method, url, **kwargs):
            if url == GR_PAGE:
                return FakeResp(text=goodreads_page("https://s.gr-assets.com/assets/nophoto/book/111x148.png"))
            if url == GOODREADS_AUTOCOMPLETE:
                return FakeResp(json_data=[{"bookId": "999", "imageUrl": GR_THUMB}])
            if url.startswith("https://covers.openlibrary.org/b/isbn/"):
                return FakeResp(content=JPEG_BYTES)
            raise AssertionError(f"unexpected request to {url}")

        book = self._run(fake_request)
        self.assertEqual(book["cover_source"], "openlibrary:isbn")

    def test_manual_cover_is_still_never_overwritten(self) -> None:
        self.conn.execute("UPDATE books SET cover_status = 'manual' WHERE goodreads_id = '42'")
        self.conn.commit()
        with patch("adso.covers._request", side_effect=AssertionError("should not fetch")):
            result = fetch_covers(self.conn, self.root, refresh=True)
        self.assertEqual(result["fetched"], 0)

    def test_transient_503_is_retried(self) -> None:
        calls = []

        class FakeRequests:
            def request(self, method, url, **kwargs):
                calls.append(url)
                if url == GR_PAGE and len(calls) == 1:
                    return FakeResp(status_code=503)
                if url == GR_PAGE:
                    return FakeResp(text=goodreads_page(GR_IMAGE))
                return FakeResp(content=JPEG_BYTES)

        with patch("adso.ol_http.require_requests", return_value=FakeRequests()), patch(
            "adso.ol_http.time.sleep", lambda *_a, **_k: None
        ):
            fetch_covers(self.conn, self.root)

        self.assertEqual(calls[:2], [GR_PAGE, GR_PAGE])
        self.assertEqual(db.get_book_by_goodreads_id(self.conn, "42")["cover_source"], "goodreads:page")

    def test_refresh_miss_keeps_existing_cover(self) -> None:
        (self.root / "covers").mkdir()
        (self.root / "covers" / "42.jpg").write_bytes(JPEG_BYTES)
        db.set_cover(
            self.conn,
            1,
            cover_path="covers/42.jpg",
            cover_source="openlibrary:isbn",
            cover_source_url="https://covers.openlibrary.org/x.jpg",
            cover_status="fetched",
        )

        def all_miss(method, url, **kwargs):
            return FakeResp(status_code=404)

        with patch("adso.covers._request", side_effect=all_miss):
            result = fetch_covers(self.conn, self.root, refresh=True)

        self.assertEqual((result["kept"], result["not_found"]), (1, 0))
        book = db.get_book_by_goodreads_id(self.conn, "42")
        self.assertEqual(book["cover_status"], "fetched")
        self.assertEqual(book["cover_source"], "openlibrary:isbn")
        self.assertTrue((self.root / "covers" / "42.jpg").is_file())
