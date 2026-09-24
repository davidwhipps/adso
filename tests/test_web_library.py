"""Library page (shelves, smart views, tags, sort, views) and bulk edit endpoints.

Skipped unless the optional web test stack (``httpx`` for the FastAPI
``TestClient``) is installed.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from adso import db
from adso.covers import image_size

try:  # TestClient requires httpx; the web extra may not be installed.
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient

    _HAS_TESTCLIENT = True
except Exception:  # pragma: no cover - exercised only without the web extra
    _HAS_TESTCLIENT = False

RECENT = (date.today() - timedelta(days=3)).isoformat()

BOOKS = [
    # goodreads_id, title, author, shelf, rating, date_added
    ("1", "The Clockwork Herbarium", "Mara Ellison", "read", 5, "2014-05-05"),
    ("2", "A Winter Almanac", "Mara Ellison", "read", 0, "2019-01-01"),
    ("3", "Zebra Crossings", "Ada Quill", "currently-reading", 0, RECENT),
    ("4", "Unread Pile", "Ada Quill", "to-read", 0, "2020-02-02"),
]


def _seed(db_path: str) -> None:
    conn = db.connect(db_path)
    db.initialize(conn)
    run = db.create_import_run(conn, source="goodreads", source_path="x.csv", mode="import", row_count=len(BOOKS))
    for gid, title, author, shelf, rating, added in BOOKS:
        db.insert_book_from_goodreads(
            conn,
            {"goodreads_id": gid, "title": title, "author": author, "exclusive_shelf": shelf,
             "rating": rating, "date_added": added, "shelves_json": "[]"},
            import_run_id=run,
        )
    db.update_local_fields(conn, "1", {"tags_json": ["botany"], "loaned_to": "Sam"})
    conn.commit()
    conn.close()


def _titles(body: str) -> list[str]:
    """Titles of the cards in the rendered grid, in order."""
    out, marker = [], 'class="cap">\n          <div class="t">'
    start = 0
    while (i := body.find(marker, start)) != -1:
        j = i + len(marker)
        out.append(body[j:body.index("<", j)])
        start = j
    return out


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class LibraryPageTests(unittest.TestCase):
    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        _seed(self.db_path)
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_library_shows_the_whole_collection(self) -> None:
        titles = _titles(self.client.get("/").text)
        self.assertEqual(titles, ["The Clockwork Herbarium", "Unread Pile", "A Winter Almanac", "Zebra Crossings"])

    def test_to_read_shelf_and_old_url_redirect(self) -> None:
        self.assertEqual(_titles(self.client.get("/", params={"shelf": "to-read"}).text), ["Unread Pile"])
        response = self.client.get("/to-read", params={"q": "pile"}, follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/?q=pile&shelf=to-read")

    def test_sidebar_counts_reflect_other_facets(self) -> None:
        body = self.client.get("/", params={"tag": "botany"}).text
        # With #botany active, Read would show 1 book and To read none.
        self.assertIn('href="/?shelf=read&amp;tag=botany" class="nav  ">Read<span class="n">1</span>', body)
        self.assertIn('class="nav  zero">To read<span class="n">0</span>', body)

    def test_smart_views(self) -> None:
        self.assertEqual(_titles(self.client.get("/", params={"smart": "loaned"}).text), ["The Clockwork Herbarium"])
        self.assertEqual(_titles(self.client.get("/", params={"smart": "unrated"}).text), ["A Winter Almanac"])
        self.assertEqual(_titles(self.client.get("/", params={"smart": "recent"}).text), ["Zebra Crossings"])
        self.assertEqual(_titles(self.client.get("/", params={"smart": "loved"}).text), ["The Clockwork Herbarium"])

    def test_sort_by_author_uses_last_name_then_title(self) -> None:
        titles = _titles(self.client.get("/", params={"sort": "author"}).text)
        self.assertEqual(titles, ["The Clockwork Herbarium", "A Winter Almanac", "Unread Pile", "Zebra Crossings"])
        titles = _titles(self.client.get("/", params={"sort": "added"}).text)
        self.assertEqual(titles[0], "Zebra Crossings")

    def test_unknown_params_fall_back_to_defaults(self) -> None:
        body = self.client.get("/", params={"shelf": "nope", "smart": "nope", "sort": "nope", "view": "nope"}).text
        self.assertIn("<h1>The Library</h1>", body)
        self.assertIn('id="masonry"', body)

    def test_search_and_heading(self) -> None:
        body = self.client.get("/", params={"q": "clockwork"}).text
        self.assertEqual(_titles(body), ["The Clockwork Herbarium"])
        self.assertIn("<h1>“clockwork”</h1>", body)

    def test_table_and_wall_views(self) -> None:
        table = self.client.get("/", params={"view": "table"}).text
        self.assertIn('class="libtable"', table)
        self.assertIn('id="row-format-1"', table)  # quick-edit OOB target survives
        wall = self.client.get("/", params={"view": "wall", "shelf": "read"}).text
        # The wall shows every book, highlighting only the matches.
        self.assertEqual(wall.count('class="w '), 4)
        self.assertEqual(wall.count('class="w match'), 2)

    def test_open_book_param_renders_sidebar(self) -> None:
        body = self.client.get("/", params={"book": "2"}).text
        self.assertIn('class="shell insp-open"', body)
        self.assertIn('id="insp-notes-2"', body)
        self.assertNotIn('class="shell insp-open"', self.client.get("/", params={"book": "missing"}).text)

    def test_empty_library_explains_how_to_import(self) -> None:
        from adso.web.app import create_app

        empty = str(Path(self.tmp.name) / "empty.sqlite")
        conn = db.connect(empty)
        db.initialize(conn)
        conn.close()
        body = TestClient(create_app(empty)).get("/").text
        self.assertIn("adso import goodreads", body)

    def test_book_page_lists_more_by_author(self) -> None:
        body = self.client.get("/book/1").text
        self.assertIn("More by <em>Mara Ellison</em>", body)
        self.assertIn('href="/book/2"', body)
        self.assertIn("Local catalogue", body)


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class BulkEditTests(unittest.TestCase):
    def setUp(self) -> None:
        from adso.web.app import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "adso.sqlite")
        _seed(self.db_path)
        self.client = TestClient(create_app(self.db_path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _book(self, gid: str) -> dict:
        conn = db.connect(self.db_path)
        row = dict(db.get_book_by_goodreads_id(conn, gid))
        conn.close()
        return row

    def test_bulk_tag_adds_normalised_tag_once(self) -> None:
        r = self.client.post("/books/bulk/tags", data={"ids": ["1", "2", "missing"], "tag": " Botany "})
        self.assertEqual(r.json(), {"updated": 1, "tag": "botany"})  # book 1 already had it
        self.assertIn('"botany"', self._book("2")["tags_json"])

    def test_bulk_tag_rejects_blank(self) -> None:
        self.assertEqual(self.client.post("/books/bulk/tags", data={"ids": ["1"], "tag": "  "}).status_code, 422)

    def test_bulk_format_sets_and_clears(self) -> None:
        r = self.client.post("/books/bulk/format", data={"ids": ["1", "3"], "format": "ebook"})
        self.assertEqual(r.json()["updated"], 2)
        self.assertEqual(self._book("3")["format"], "ebook")
        self.client.post("/books/bulk/format", data={"ids": ["3"], "format": ""})
        self.assertIsNone(self._book("3")["format"])
        self.assertEqual(self.client.post("/books/bulk/format", data={"ids": ["1"], "format": "scroll"}).status_code, 422)


class ImageSizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_png(self) -> None:
        ihdr = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 120, 180) + b"\x08\x02\x00\x00\x00"
        self.assertEqual(image_size(self._write("a.png", ihdr)), (120, 180))

    def test_gif(self) -> None:
        self.assertEqual(image_size(self._write("a.gif", b"GIF89a" + struct.pack("<HH", 30, 45) + b"\x00" * 8)), (30, 45))

    def test_jpeg_skips_segments_to_frame(self) -> None:
        app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
        sof0 = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 450, 300) + b"\x03" + b"\x00" * 9
        self.assertEqual(image_size(self._write("a.jpg", b"\xff\xd8" + app0 + sof0)), (300, 450))

    def test_unknown_or_missing(self) -> None:
        self.assertIsNone(image_size(self._write("a.txt", b"not an image at all")))
        self.assertIsNone(image_size(self.dir / "missing.jpg"))


if __name__ == "__main__":
    unittest.main()
