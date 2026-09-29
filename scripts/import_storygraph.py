#!/usr/bin/env python3
"""Import a StoryGraph CSV export into a user's Books library.

Maps StoryGraph fields → Books:
  Format  hardcover/paperback → physical · digital → ebook · audio → audiobook
  Read Status  read → reading_status=read (others → unread)
  Owned?  Yes/No → is_owned
  Tags    "theology" → tags=["theology"]
Covers are fetched by ISBN (Open Library) with a Google Books fallback by
title. Series is not in the StoryGraph export, so it's left unset (enrich later).

Run inside the books-api container (so `books` + DATA_DIR are available):
    uv run python /tmp/import_storygraph.py /app/data/storygraph_export.csv gordon
"""

import csv
import sys
import time
from datetime import datetime, timezone

import httpx

from books.helpers import db

FORMAT_MAP = {
    "hardcover": "physical",
    "paperback": "physical",
    "digital": "ebook",
    "audio": "audiobook",
}


def _iso(date_str: str) -> str | None:
    date_str = (date_str or "").strip()
    if not date_str:
        return None
    try:
        return (
            datetime.strptime(date_str, "%Y/%m/%d")
            .replace(tzinfo=timezone.utc)
            .isoformat()
        )
    except ValueError:
        return None


def _is_asin(uid: str) -> bool:
    uid = (uid or "").strip().upper()
    return uid.startswith("B0") and len(uid) == 10


def _fetch_cover(uid: str, title: str, authors: str, client: httpx.Client) -> bytes | None:
    uid = (uid or "").strip()
    # 1) Open Library by ISBN (no API key; default=false → 404 when missing)
    if uid and not _is_asin(uid):
        try:
            r = client.get(
                f"https://covers.openlibrary.org/b/isbn/{uid}-L.jpg",
                params={"default": "false"},
            )
            if (
                r.status_code == 200
                and len(r.content) > 1500
                and r.headers.get("content-type", "").startswith("image")
            ):
                return r.content
        except Exception:
            pass
    # 2) Google Books by title (+ first author)
    try:
        q = f"intitle:{title}"
        if authors:
            q += f"+inauthor:{authors.split(',')[0].strip()}"
        r = client.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": q, "maxResults": 1, "country": "US"},
        )
        if r.status_code == 200:
            items = r.json().get("items") or []
            if items:
                links = items[0].get("volumeInfo", {}).get("imageLinks", {})
                url = links.get("thumbnail") or links.get("smallThumbnail")
                if url:
                    ir = client.get(url.replace("http://", "https://"))
                    if ir.status_code == 200 and len(ir.content) > 1000:
                        return ir.content
    except Exception:
        pass
    return None


def main() -> None:
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "/app/data/storygraph_export.csv"
    username = sys.argv[2] if len(sys.argv) > 2 else "gordon"

    db.init_db()
    user = db.get_user_by_username(username)
    if not user:
        print(f"ERROR: user '{username}' not found")
        sys.exit(1)
    uid = user["id"]

    covers_dir = db.DATA_DIR / "covers" / str(uid)
    covers_dir.mkdir(parents=True, exist_ok=True)

    # Dedup against existing titles+authors
    conn = db.get_db()
    existing = {
        (str(r[0]).strip().lower(), str(r[1] or "").strip().lower())
        for r in conn.execute(
            "SELECT title, authors FROM books WHERE user_id = ?", (uid,)
        ).fetchall()
    }
    conn.close()

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    added = skipped = covered = 0
    with httpx.Client(
        follow_redirects=True, timeout=20,
        headers={"User-Agent": "library-import/1.0"},
    ) as client:
        for row in rows:
            title = (row.get("Title") or "").strip()
            authors = (row.get("Authors") or "").strip()
            if not title:
                continue
            if (title.lower(), authors.lower()) in existing:
                skipped += 1
                continue

            book_uid = (row.get("ISBN/UID") or "").strip()
            fmt = FORMAT_MAP.get((row.get("Format") or "").strip().lower(), "ebook")
            status = "read" if (row.get("Read Status") or "").strip().lower() == "read" else "unread"
            owned = 1 if (row.get("Owned?") or "").strip().lower() == "yes" else 0
            tags = ["theology"] if (row.get("Tags") or "").strip().lower() == "theology" else None
            date_added = _iso(row.get("Date Added")) or datetime.now(timezone.utc).isoformat()
            date_finished = _iso(row.get("Last Date Read")) or (date_added if status == "read" else None)
            isbn = book_uid if (book_uid and not _is_asin(book_uid)) else None

            book_id = db.insert_book(
                user_id=uid,
                title=title,
                sort_title=db.make_sort_title(title),
                authors=authors or "Unknown",
                author_sort=db.make_author_sort(authors or "Unknown"),
                series=None,
                series_index=None,
                description=None,
                cover_filename=None,
                file_path=None,
                isbn=isbn,
                goodreads_id=None,
                tags=tags,
                date_added=date_added,
                date_finished=date_finished,
                rating=None,
                reading_status=status,
                is_owned=owned,
                book_format=fmt,
            )
            if status == "read":
                db.update_book(book_id, uid, {"is_read": 1})

            cover = _fetch_cover(book_uid, title, authors, client)
            if cover:
                (covers_dir / f"{book_id}.jpg").write_bytes(cover)
                db.update_book(
                    book_id, uid,
                    {
                        "cover_filename": f"{book_id}.jpg",
                        "cover_updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                covered += 1
            added += 1
            print(f"  + {title[:48]:48s} [{fmt:9s} {status:6s} owned={owned} cover={'Y' if cover else '-'}]")
            time.sleep(0.05)

    print(f"\nDone: {added} added, {skipped} skipped, {covered} covers, {len(rows)} rows")


if __name__ == "__main__":
    main()
