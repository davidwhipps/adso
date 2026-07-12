"""Goodreads CSV parsing and normalization."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import GoodreadsImportError

STATUS_MAP = {
    "to-read": "To Read",
    "currently-reading": "Currently Reading",
    "read": "Read",
}


@dataclass(frozen=True)
class GoodreadsRecord:
    raw: dict[str, str]
    normalized: dict[str, Any]


def clean_isbn(raw: str | None) -> str:
    if not raw:
        return ""
    return re.sub(r"[^0-9X]", "", raw.upper())


def parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_shelves(raw: str | None) -> list[str]:
    if not raw:
        return []
    shelves: list[str] = []
    for item in raw.split(","):
        item = re.sub(r"\s*\(#\d+\)", "", item.strip())
        if item:
            shelves.append(item.lower())
    return shelves


def parse_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    # Goodreads exports some integer columns as float strings — "My Rating"
    # started arriving as "5.0" in July 2026, which silently nulled every
    # rating on sync. Accept integral floats; anything else is still None.
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value) if value.is_integer() else None


def normalize_row(row: dict[str, str]) -> dict[str, Any]:
    exclusive_shelf = row.get("Exclusive Shelf", "").strip()
    shelves = parse_shelves(row.get("Bookshelves", ""))
    rating = parse_int(row.get("My Rating"))
    read_count = parse_int(row.get("Read Count"))

    return {
        "goodreads_id": str(row.get("Book Id", "")).strip(),
        "title": row.get("Title", "").strip(),
        "author": row.get("Author", "").strip(),
        "additional_authors": row.get("Additional Authors", "").strip(),
        "isbn10": clean_isbn(row.get("ISBN")),
        "isbn13": clean_isbn(row.get("ISBN13")),
        "publisher": row.get("Publisher", "").strip(),
        "binding": row.get("Binding", "").strip(),
        "number_of_pages": parse_int(row.get("Number of Pages")),
        "year_published": parse_int(row.get("Year Published")),
        "original_publication_year": parse_int(row.get("Original Publication Year")),
        "rating": rating,
        "average_rating": row.get("Average Rating", "").strip(),
        "reading_status": STATUS_MAP.get(exclusive_shelf, exclusive_shelf or None),
        "exclusive_shelf": exclusive_shelf,
        "shelves": shelves,
        "shelves_json": json.dumps(shelves, sort_keys=True),
        "date_read": parse_date(row.get("Date Read")),
        "date_added": parse_date(row.get("Date Added")),
        "my_review": row.get("My Review", "").strip(),
        "private_notes": row.get("Private Notes", "").strip(),
        "read_count": read_count,
        "owned_copies": parse_int(row.get("Owned Copies")),
    }


def read_goodreads_csv(path: str | Path) -> list[GoodreadsRecord]:
    csv_path = Path(path)
    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return [
                GoodreadsRecord(raw=dict(row), normalized=normalize_row(row))
                for row in reader
            ]
    except FileNotFoundError as exc:
        raise GoodreadsImportError(
            f"Could not find a Goodreads CSV at {csv_path}.",
            hint="Check the path, or run `adso doctor` to find nearby CSV exports.",
        ) from exc
    except IsADirectoryError as exc:
        raise GoodreadsImportError(
            f"{csv_path} is a directory, not a Goodreads CSV file.",
        ) from exc
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise GoodreadsImportError(
            f"Could not read the Goodreads CSV at {csv_path}: {exc}",
            hint="Make sure it is a Goodreads CSV export. Run `adso doctor` to scan for valid files.",
        ) from exc
