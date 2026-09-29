#!/usr/bin/env python3
"""Backfill empty book descriptions from external sources.

For each book with `description IS NULL` (or empty/whitespace), try
in order:

  1. Google Books by ISBN, then by title + author fallback.
  2. Open Library works/editions description.
  3. Apple Books (iTunes Search API) description field.

The result is HTML-stripped and control-character-cleaned. We only
UPDATE when the fetched text is at least 80 characters to avoid
saving one-line stubs.

Run from the repo root (or inside the books-api container after
copying scripts/ in):

    uv run python scripts/fill_descriptions.py --user gordon

Flags: `--limit N` to cap, `--dry-run`, `--user all`, `--sleep S`.
"""

import argparse
import re
import sys
import time

import httpx

from books.helpers import db


USER_AGENT = "library-bot/1.0 gordon@ggouger.com"
GBOOKS = "https://www.googleapis.com/books/v1/volumes"
OL_SEARCH = "https://openlibrary.org/search.json"
OL_WORK = "https://openlibrary.org/works/{work_id}.json"
ITUNES_SEARCH = "https://itunes.apple.com/search"

MIN_DESCRIPTION = 80


def _strip_html(text: str) -> str:
    """Strip HTML tags + collapse control whitespace."""
    if not text:
        return ""
    out = re.sub(r"<[^>]+>", "", text)
    # Replace control chars with a single space.
    out = re.sub(r"[\x00-\x1f\x7f]+", " ", out)
    # Collapse repeated whitespace runs.
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _first_author(authors: str) -> str:
    if not authors:
        return ""
    parts = (
        authors.replace(" & ", ",")
        .replace(" and ", ",")
        .split(",")
    )
    return parts[0].strip()


def _try_google_books(
    title: str, authors: str, isbn: str | None, client: httpx.Client,
) -> str | None:
    """Google Books: ISBN first, fall back to title+author."""
    try:
        if isbn:
            r = client.get(
                GBOOKS,
                params={"q": f"isbn:{isbn}", "maxResults": 1, "country": "US"},
            )
            if r.status_code == 200:
                items = (r.json() or {}).get("items") or []
                if items:
                    desc = items[0].get("volumeInfo", {}).get("description")
                    cleaned = _strip_html(desc or "")
                    if cleaned and len(cleaned) >= MIN_DESCRIPTION:
                        return cleaned
        if not title:
            return None
        q = f"intitle:{title}"
        first = _first_author(authors)
        if first:
            q += f"+inauthor:{first}"
        r = client.get(
            GBOOKS,
            params={"q": q, "maxResults": 3, "country": "US"},
        )
        if r.status_code != 200:
            return None
        items = (r.json() or {}).get("items") or []
        for item in items:
            desc = item.get("volumeInfo", {}).get("description")
            cleaned = _strip_html(desc or "")
            if cleaned and len(cleaned) >= MIN_DESCRIPTION:
                return cleaned
    except Exception:
        return None
    return None


def _try_openlibrary(
    title: str, authors: str, client: httpx.Client,
) -> str | None:
    """Open Library: search -> work doc -> description (string or dict)."""
    try:
        if not title:
            return None
        params = {"title": title, "limit": 3}
        first = _first_author(authors)
        if first:
            params["author"] = first
        r = client.get(OL_SEARCH, params=params)
        if r.status_code != 200:
            return None
        docs = (r.json() or {}).get("docs") or []
        for d in docs:
            key = d.get("key", "")
            if not key.startswith("/works/"):
                continue
            work_id = key.replace("/works/", "")
            wr = client.get(OL_WORK.format(work_id=work_id))
            if wr.status_code != 200:
                continue
            work = wr.json() or {}
            desc = work.get("description")
            if isinstance(desc, dict):
                desc = desc.get("value", "")
            cleaned = _strip_html(desc or "")
            if cleaned and len(cleaned) >= MIN_DESCRIPTION:
                return cleaned
    except Exception:
        return None
    return None


def _try_apple_books(
    title: str, authors: str, client: httpx.Client,
) -> str | None:
    """Apple Books: search ebooks, return best title-match description."""
    try:
        if not title:
            return None
        first = _first_author(authors)
        term = f"{title} {first}".strip()
        params = {
            "term": term,
            "entity": "ebook",
            "limit": 5,
            "country": "US",
        }
        r = client.get(ITUNES_SEARCH, params=params)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        for item in results:
            res_title = item.get("trackName") or item.get("collectionName") or ""
            # Cheap token-overlap check: at least one shared keyword.
            t_tokens = {w.lower() for w in re.findall(r"\w+", title)}
            r_tokens = {w.lower() for w in re.findall(r"\w+", res_title)}
            if not t_tokens & r_tokens:
                continue
            desc = item.get("description") or ""
            cleaned = _strip_html(desc)
            if cleaned and len(cleaned) >= MIN_DESCRIPTION:
                return cleaned
    except Exception:
        return None
    return None


def _missing_descriptions_for(user_id: int) -> list[dict]:
    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, user_id, title, authors, isbn
           FROM books
           WHERE user_id = ?
               AND (description IS NULL OR TRIM(description) = '')""",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _all_user_ids() -> list[int]:
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id FROM users WHERE username <> 'archive'"
        " ORDER BY id"
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill book descriptions from external sources.",
    )
    parser.add_argument("--user", default="gordon")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sleep", type=float, default=0.5,
        help="Seconds between books (politeness).",
    )
    args = parser.parse_args()

    db.init_db()

    if args.user == "all":
        user_ids = _all_user_ids()
    else:
        user = db.get_user_by_username(args.user)
        if not user:
            print(f"ERROR: user {args.user!r} not found")
            sys.exit(1)
        user_ids = [user["id"]]

    filled_g = filled_ol = filled_apple = 0
    processed = failed = 0

    with httpx.Client(
        follow_redirects=True,
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for uid in user_ids:
            books = _missing_descriptions_for(uid)
            if not books:
                continue
            for book in books:
                if args.limit and processed >= args.limit:
                    break
                processed += 1
                title = book["title"] or ""
                authors = book["authors"] or ""
                isbn = book.get("isbn")

                desc = _try_google_books(title, authors, isbn, client)
                source = "google" if desc else None
                if not desc:
                    time.sleep(args.sleep)
                    desc = _try_openlibrary(title, authors, client)
                    if desc:
                        source = "openlibrary"
                if not desc:
                    time.sleep(args.sleep)
                    desc = _try_apple_books(title, authors, client)
                    if desc:
                        source = "apple"

                if not desc:
                    failed += 1
                    print(
                        f"  - {book['id']:>5} {title[:50]:50s} "
                        f"(no description found)"
                    )
                    time.sleep(args.sleep)
                    continue

                if args.dry_run:
                    print(
                        f"  ? {book['id']:>5} {title[:50]:50s} "
                        f"would fill {len(desc)} chars from {source}"
                    )
                else:
                    db.update_book(
                        book["id"], uid,
                        {"description": desc},
                    )
                    print(
                        f"  + {book['id']:>5} {title[:50]:50s} "
                        f"filled {len(desc)} chars from {source}"
                    )

                if source == "google":
                    filled_g += 1
                elif source == "openlibrary":
                    filled_ol += 1
                else:
                    filled_apple += 1

                time.sleep(args.sleep)

            if args.limit and processed >= args.limit:
                break

    total = filled_g + filled_ol + filled_apple
    print(
        f"\nDone: processed {processed}, "
        f"filled {total} descriptions ("
        f"{filled_g} Google Books, "
        f"{filled_ol} Open Library, "
        f"{filled_apple} Apple Books), "
        f"{failed} unresolved."
    )


if __name__ == "__main__":
    main()
