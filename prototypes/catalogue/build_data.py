"""Regenerate data.js (and cover thumbnails) for the catalogue prototype.

Reads a temporary copy of adso.sqlite, never the live file. Deliberately omits private_notes and my_review.
Thumbnails and aspect ratios use macOS `sips`.
"""
import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "data.js"
THUMBS = HERE / "thumbs"
STATUS_SHELVES = {"read", "to-read", "currently-reading", "did-not-finish"}


def ensure_thumbs(names):
    THUMBS.mkdir(exist_ok=True)
    missing = [n for n in names if not (THUMBS / n).exists()]
    for n in missing:
        subprocess.run(["sips", "-Z", "180", str(ROOT / "covers" / n), "--out", str(THUMBS / n)],
                       capture_output=True)
    if missing:
        print(f"made {len(missing)} thumbnails")


def aspect_ratios():
    """Map thumb filename -> height/width, via one sips call."""
    files = sorted(str(p) for p in THUMBS.iterdir() if p.suffix in (".jpg", ".png"))
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", *files],
                         capture_output=True, text=True).stdout
    ratios, name, w = {}, None, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("/"):
            name = Path(line).name
        elif line.startswith("pixelWidth:"):
            w = int(line.split()[1])
        elif line.startswith("pixelHeight:") and name and w:
            ratios[name] = round(int(line.split()[1]) / w, 3)
    return ratios


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "adso.sqlite"
        shutil.copy(ROOT / "adso.sqlite", db)
        for ext in ("-wal",):
            if (ROOT / f"adso.sqlite{ext}").exists():
                shutil.copy(ROOT / f"adso.sqlite{ext}", Path(f"{db}{ext}"))
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        rows = con.execute(
        """select id, title, author, original_publication_year, year_published,
                  number_of_pages, rating, average_rating, exclusive_shelf,
                  tags_json, shelves_json, loaned_to, date_added, date_read,
                  publisher, description, cover_path
           from books"""
        ).fetchall()
        con.close()
    ensure_thumbs([Path(r["cover_path"]).name for r in rows if r["cover_path"]])
    ratios = aspect_ratios()
    books = []
    for r in rows:
        shelves = [s for s in json.loads(r["shelves_json"] or "[]") if s not in STATUS_SHELVES]
        tags = sorted(set(json.loads(r["tags_json"] or "[]")) | set(shelves))
        fname = Path(r["cover_path"]).name if r["cover_path"] else ""
        books.append({
            "id": r["id"],
            "title": r["title"],
            "author": r["author"] or "",
            "year": r["original_publication_year"] or r["year_published"],
            "pages": r["number_of_pages"],
            "rating": r["rating"] or 0,
            "avg": r["average_rating"],
            "shelf": r["exclusive_shelf"] or "to-read",
            "tags": tags,
            "loaned": r["loaned_to"] or "",
            "added": r["date_added"] or "",
            "read": r["date_read"] or "",
            "publisher": r["publisher"] or "",
            "desc": (r["description"] or "")[:1500],
            "cover": f"../../covers/{fname}" if fname else "",
            "thumb": f"thumbs/{fname}" if fname else "",
            "ar": ratios.get(fname, 1.5),
        })
    OUT.write_text("window.BOOKS = " + json.dumps(books, ensure_ascii=False) + ";\n")
    print(f"wrote {len(books)} books to {OUT}")


if __name__ == "__main__":
    main()
