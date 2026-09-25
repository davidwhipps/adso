"""Tests for what-to-read-next: taste, series order, filters, variety, related, insights."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from adso import categorize as cat
from adso import cli, db, mcp_server
from adso import recommend as rec
from adso.sync import import_goodreads_csv

HEADERS = ["Book Id", "Title", "Author", "Bookshelves", "Exclusive Shelf", "My Rating", "Date Added",
           "Date Read", "Number of Pages", "Average Rating"]


def row(gid, title, shelf, *, rating=0, author=None, pages="", avg="", read=""):
    return {"Book Id": gid, "Title": title, "Author": author or f"Author {gid}", "Bookshelves": shelf, "Exclusive Shelf": shelf.split(",")[0],
            "My Rating": str(rating), "Date Added": "2024/01/01", "Date Read": read,
            "Number of Pages": pages, "Average Rating": avg}


class RecommendTestCase(unittest.TestCase):
    rows: list[dict[str, str]] = []
    genres: dict[str, list[str]] = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "adso.sqlite"
        self.conn = db.connect(self.db_path)
        db.initialize(self.conn)
        path = Path(self.tmp.name) / "export.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(self.rows)
        import_goodreads_csv(self.conn, path, mode="import")
        for child in ("Science Fiction > Space Opera", "Science Fiction > Cyberpunk", "Fantasy > Epic Fantasy"):
            cat.add_category(self.conn, child)
        cat.categorize(self.conn)
        for gid, refs in self.genres.items():
            for ref in refs:
                cat.add_book_category(self.conn, self.book_id(gid), ref)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def book_id(self, gid: str) -> int:
        return int(self.conn.execute("SELECT id FROM books WHERE goodreads_id = ?", (gid,)).fetchone()[0])

    def ranked(self, **kwargs) -> list[str]:
        return [p["goodreads_id"] for p in rec.next_reads(self.conn, **kwargs)]


class TasteTests(RecommendTestCase):
    rows = [
        row("1", "Loved SF 1", "read", rating=5), row("2", "Loved SF 2", "read", rating=5),
        row("3", "Loved SF 3", "read", rating=4),
        row("4", "Meh Romance 1", "read", rating=2), row("5", "Meh Romance 2", "read", rating=1),
        row("6", "Dropped Horror 1", "did-not-finish"), row("7", "Dropped Horror 2", "did-not-finish"),
        row("10", "Pile SF", "to-read", pages="250"), row("11", "Pile Romance", "to-read"),
        row("12", "Pile Horror", "to-read", pages="600"), row("13", "Pile SF Owned", "to-read"),
    ]
    genres = {
        "1": ["Space Opera"], "2": ["Space Opera"], "3": ["Space Opera"],
        "4": ["Romance"], "5": ["Romance"], "6": ["Horror"], "7": ["Horror"],
        "10": ["Space Opera"], "11": ["Romance"], "12": ["Horror"], "13": ["Space Opera"],
    }

    def setUp(self) -> None:
        super().setUp()
        db.update_local_fields(self.conn, "13", {"format": "ebook"})

    def test_loved_genres_rank_first_and_disliked_last(self):
        order = self.ranked(variety=False)
        self.assertEqual(order[:2], ["13", "10"])  # owned breaks the tie
        self.assertEqual(order[-1], "11")  # rated 1-2 stars
        self.assertLess(order.index("12"), order.index("11"))  # DNF hurts, low ratings hurt more

    def test_reasons_are_plain_and_specific(self):
        top = rec.next_reads(self.conn, limit=1)[0]
        self.assertIn("You rate Space Opera 4.7★ (3 read)", top["reasons"])
        self.assertIn("You own it (ebook)", top["reasons"])
        self.assertFalse(any("Science Fiction" in r for r in top["reasons"]))  # ancestors not repeated

    def test_filters(self):
        self.assertEqual(self.ranked(owned_only=True), ["13"])
        self.assertEqual(self.ranked(max_pages=300), ["10"])
        self.assertEqual(sorted(self.ranked(category="Science Fiction")), ["10", "13"])
        with self.assertRaises(cat.CategoryError):
            rec.next_reads(self.conn, category="Nope")

    def test_only_the_to_read_shelf_is_ranked(self):
        self.assertEqual(sorted(self.ranked()), ["10", "11", "12", "13"])

    def test_insights_by_genre(self):
        data = rec.insights(self.conn)
        self.assertEqual((data["read"], data["dnf"], data["to_read"]), (5, 2, 4))
        rows = {r["genre"]: r for r in data["genres"]}
        self.assertEqual(rows["Science Fiction"]["avg_rating"], 4.67)
        self.assertEqual(rows["Horror & Gothic"]["dnf_rate"], 1.0)
        self.assertEqual(rows["Love Stories"]["avg_rating"], 1.5)


class SeriesTests(RecommendTestCase):
    rows = [
        row("1", "Leviathan Wakes (The Expanse, #1)", "read", rating=5),
        row("2", "Caliban's War (The Expanse, #2)", "to-read"),
        row("3", "Abaddon's Gate (The Expanse, #3)", "to-read"),
        row("4", "A Standalone", "to-read"),
    ]

    def test_next_in_series_leads_and_skipping_ahead_waits(self):
        picks = {p["goodreads_id"]: p for p in rec.next_reads(self.conn)}
        order = self.ranked()
        self.assertEqual(order[0], "2")
        self.assertEqual(order[-1], "3")
        self.assertIn("Next in The Expanse (you rated #1 5★)", picks["2"]["reasons"])
        self.assertIn("Wait: #2 in The Expanse comes first", picks["3"]["reasons"])

    def test_related_puts_the_next_book_first(self):
        related = rec.related_books(self.conn, "1")
        self.assertEqual([r["goodreads_id"] for r in related][:2], ["2", "3"])
        self.assertEqual(related[0]["reasons"][0], "#2 in The Expanse")
        with self.assertRaises(ValueError):
            rec.related_books(self.conn, "nope")


class AuthorTests(RecommendTestCase):
    rows = [
        row("1", "The Left Hand of Darkness", "read", rating=5, author="Ursula K. Le Guin"),
        row("2", "The Dispossessed", "to-read", author="Ursula K. Le Guin"),
        row("3", "Something Else", "to-read", author="A. N. Other"),
    ]

    def test_authors_you_rate_highly(self):
        picks = rec.next_reads(self.conn)
        self.assertEqual(picks[0]["goodreads_id"], "2")
        self.assertIn("By Ursula K. Le Guin, whom you rate 5.0★ (1 read)", picks[0]["reasons"])


class VarietyAndPathsTests(RecommendTestCase):
    rows = [
        *[row(str(i), f"Opera {i}", "read", rating=5) for i in range(1, 5)],
        *[row(str(i), f"Pile Opera {i}", "to-read") for i in range(10, 16)],
        row("20", "Pile Cyberpunk", "to-read"),
        row("21", "Pile Epic", "to-read"),
    ]
    genres = {
        **{str(i): ["Space Opera"] for i in range(1, 5)},
        **{str(i): ["Space Opera"] for i in range(10, 16)},
        "20": ["Cyberpunk"],
        "21": ["Epic Fantasy"],
    }

    def test_variety_lets_another_genre_in(self):
        self.assertNotIn("20", self.ranked(limit=5, variety=False))
        self.assertIn("20", self.ranked(limit=5))

    def test_paths_suggest_unread_neighbours_of_loved_genres(self):
        paths = rec.explore_paths(self.conn)
        genres = [p["genre"] for p in paths]
        self.assertIn("Science Fiction > Cyberpunk", genres)
        self.assertNotIn("Fantasy > Epic Fantasy", genres)  # not a neighbour
        cyber = next(p for p in paths if p["genre"].endswith("Cyberpunk"))
        self.assertEqual([b["goodreads_id"] for b in cyber["books"]], ["20"])
        self.assertIn("Space Opera", cyber["because"])


class ShelvesAndTraditionTests(RecommendTestCase):
    rows = [
        row("1", "Anna Karenina", "read", rating=5), row("2", "Fathers and Sons", "read", rating=5),
        row("3", "Oblomov", "read,attempted", rating=2),
        row("10", "The Master and Margarita", "to-read"), row("11", "Plain Pile", "to-read"),
        row("12", "Shortlisted", "to-read,shortlist"),
    ]
    genres = {gid: ["tradition:Russian"] for gid in ("1", "2", "3", "10")}

    def setUp(self) -> None:
        super().setUp()
        self.conn.execute(
            "UPDATE books SET subjects_json = ? WHERE goodreads_id IN ('1', '2', '10')",
            (json.dumps(["New York Times reviewed"]),),
        )
        self.conn.commit()

    def test_attempted_counts_as_did_not_finish(self):
        data = rec.insights(self.conn)
        self.assertEqual((data["read"], data["dnf"]), (2, 1))

    def test_tradition_reasons_read_naturally(self):
        picks = rec.next_reads(self.conn, variety=False)
        top = next(p for p in picks if p["goodreads_id"] == "10")
        self.assertLess(picks.index(top), [p["goodreads_id"] for p in picks].index("11"))
        self.assertTrue(any(r.startswith("You rate Russian books") for r in top["reasons"]), top["reasons"])
        self.assertFalse(any("New York Times" in r for r in top["reasons"]))

    def test_shortlist_nudges_a_book_up(self):
        picks = {p["goodreads_id"]: p for p in rec.next_reads(self.conn)}
        self.assertIn("On your shortlist", picks["12"]["reasons"])
        self.assertGreater(picks["12"]["score"], picks["11"]["score"])


class FlatGenrePathsTests(RecommendTestCase):
    rows = [
        *[row(str(i), f"Loved {i}", "read", rating=5) for i in range(1, 4)],
        row("10", "Pile Ideas", "to-read"), row("11", "Pile Horror", "to-read"),
    ]
    genres = {
        **{str(i): ["Psychological Fiction", "Novel of Ideas"] for i in (1, 2)},
        "3": ["Psychological Fiction"],
        "10": ["Novel of Ideas"],
        "11": ["Horror"],
    }

    def test_top_level_neighbours_come_from_shared_books(self):
        # Novel of Ideas shares books with a loved genre; Horror shares none.
        self.conn.execute(
            "DELETE FROM book_categories WHERE category_id = (SELECT id FROM categories WHERE label = 'Novel of Ideas') "
            "AND book_id = (SELECT id FROM books WHERE goodreads_id = '2')"
        )
        self.conn.commit()
        genres = [p["genre"] for p in rec.explore_paths(self.conn)]
        self.assertIn("Novel of Ideas", genres)
        self.assertNotIn("Horror & Gothic", genres)


class SurfaceTests(TasteTests):
    def run_cli(self, *argv: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--db", str(self.db_path), *argv]), 0)
        return out.getvalue()

    def test_cli(self):
        out = self.run_cli("next", "--limit", "2")
        self.assertIn(" 1. Pile SF Owned", out)
        self.assertIn("· You rate Space Opera 4.7★ (3 read)", out)
        self.assertIn("Loved SF", self.run_cli("related", "10"))
        insights = self.run_cli("insights")
        self.assertIn("Read 5 · did not finish 2 · to read 4", insights)

    def test_mcp_tools_are_allowlisted(self):
        picks = mcp_server.recommend_next(self.conn, limit=2)["picks"]
        self.assertEqual(picks[0]["title"], "Pile SF Owned")
        self.assertNotIn("cover_url", picks[0])
        self.assertIn("paths", mcp_server.explore_paths(self.conn))
        self.assertTrue(mcp_server.related_books(self.conn, "10")["related"])
        self.assertIn("genres", mcp_server.reading_insights(self.conn))
        with self.assertRaises(ValueError):
            mcp_server.recommend_next(self.conn, category="Nope")


try:  # TestClient requires httpx; the web extra may not be installed.
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient

    _HAS_TESTCLIENT = True
except Exception:  # pragma: no cover - exercised only without the web extra
    _HAS_TESTCLIENT = False


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi TestClient (httpx) not installed")
class WebTests(TasteTests):
    def setUp(self) -> None:
        super().setUp()
        from adso.web.app import create_app

        self.client = TestClient(create_app(str(self.db_path)))

    def test_next_page(self):
        body = self.client.get("/next").text
        self.assertIn("What to read next", body)
        self.assertLess(body.index("Pile SF Owned"), body.index("Pile Romance"))
        self.assertIn("You rate Space Opera 4.7★ (3 read)", body)
        self.assertIn("Your reading", body)
        owned = self.client.get("/next?owned=1").text
        self.assertNotIn("Pile Romance", owned)
        self.assertIn("alert-destructive", self.client.get("/next?category=Nope").text)
        self.assertEqual(self.client.get("/api/next?limit=1").json()["picks"][0]["goodreads_id"], "13")
        self.assertEqual(self.client.get("/api/next?category=Nope").status_code, 422)

    def test_book_page_related_strip_and_links(self):
        body = self.client.get("/book/10").text
        self.assertIn("Related", body)
        self.assertIn("Loved SF", body.split("Related", 1)[1])
        self.assertIn('href="/next"', self.client.get("/").text)


if __name__ == "__main__":
    unittest.main()
