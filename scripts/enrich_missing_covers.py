#!/usr/bin/env python3
"""Fill in missing covers for books by searching external sources.

For each book with `cover_filename IS NULL`, try in order:
  1. Open Library search by title + author (cover_i -> covers.openlibrary.org)
  2. Google Books search by title + author (volumeInfo.imageLinks)
  3. Apple Books / iTunes search (artworkUrl100 upscaled to 1500x1500)
  4. Open Library /search.json with strict title-similarity scoring
     (catches books OL has under slightly different titles where the
      naive first-hit lookup in step 1 misses)

On success, writes the JPEG to DATA_DIR/covers/<user_id>/<book_id>.jpg
and updates `cover_filename` + `cover_updated_at` in the DB.

Run inside the books-api container:

    uv run python /app/scripts/enrich_missing_covers.py \\
        --user gordon

Pass `--limit N` to cap the number of books processed, `--dry-run`
to skip writes, `--user all` to walk every non-archive user.
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone

import httpx

from books.helpers import db, hardcover


USER_AGENT = "library-bot/1.0 gordon@ggouger.com"
OL_SEARCH = "https://openlibrary.org/search.json"
OL_COVER = "https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
GBOOKS = "https://www.googleapis.com/books/v1/volumes"
ITUNES_SEARCH = "https://itunes.apple.com/search"

# Amazon's image CDN serves cover art by ASIN. The CDN isn't a public
# API — these URL patterns aren't documented and may break — so we try
# them in fallback order and accept whichever returns a real JPEG.
AMAZON_COVER_URLS = (
    "https://images-na.ssl-images-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
    "https://m.media-amazon.com/images/I/{asin}.jpg",
    "https://images-na.ssl-images-amazon.com/images/P/{asin}.jpg",
)
# Min JPEG byte count to count as a real cover. Amazon's "no image"
# fallback served on a bad ASIN is ~2-3 KB; real covers are 30 KB+.
_AMAZON_COVER_MIN_BYTES = 5 * 1024

# Title-similarity floor for Apple Books matches. Lower than 0.5 lets
# in too many wrong matches (e.g. iTunes returns unrelated titles for
# generic queries); 0.6 keeps it strict.
_APPLE_TITLE_THRESHOLD = 0.6


def _first_author(authors: str) -> str:
    """Return the first author, splitting on comma / ampersand / 'and'."""
    if not authors:
        return ""
    parts = (
        authors.replace(" & ", ",")
        .replace(" and ", ",")
        .split(",")
    )
    return parts[0].strip()


def _try_openlibrary(
    title: str, authors: str, client: httpx.Client,
) -> bytes | None:
    """Search OpenLibrary by title+author, fetch the first cover."""
    if not title:
        return None
    params = {"title": title, "limit": 1}
    first = _first_author(authors)
    if first:
        params["author"] = first
    try:
        r = client.get(OL_SEARCH, params=params)
        if r.status_code != 200:
            return None
        docs = (r.json() or {}).get("docs") or []
        if not docs:
            return None
        cover_id = docs[0].get("cover_i")
        if not cover_id:
            return None
        cr = client.get(
            OL_COVER.format(cover_id=cover_id),
            params={"default": "false"},
        )
        if (
            cr.status_code == 200
            and len(cr.content) > 1500
            and cr.headers.get("content-type", "").startswith("image")
        ):
            return cr.content
    except Exception:
        return None
    return None


def _norm_title(s: str) -> str:
    """Lowercase and strip non-alnum for fuzzy comparisons."""
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch.isspace()).strip()


def _title_similarity(a: str, b: str) -> float:
    """Cheap token-overlap ratio. 1.0 == identical, 0.0 == disjoint."""
    aa, bb = _norm_title(a), _norm_title(b)
    if not aa or not bb:
        return 0.0
    ta, tb = set(aa.split()), set(bb.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _try_hardcover(
    title: str, authors: str, client: httpx.Client,
) -> bytes | None:
    """Fetch an exact-or-near-exact Hardcover cover before generic sources."""
    if not title:
        return None
    try:
        matches = asyncio.run(hardcover.search_books_rich(title, 8))
        expected_author = _norm_title(_first_author(authors))
        for match in matches:
            if _title_similarity(match.get("title", ""), title) < 0.8:
                continue
            match_authors = " ".join(match.get("author_names") or [])
            if expected_author and expected_author not in _norm_title(match_authors):
                continue
            url = match.get("cover_url")
            if not url:
                continue
            response = client.get(url)
            if (
                response.status_code == 200
                and len(response.content) > 1500
                and response.headers.get("content-type", "").startswith("image")
            ):
                return response.content
    except Exception:
        return None
    return None


def _try_apple_books(
    title: str, authors: str, client: httpx.Client,
) -> bytes | None:
    """Apple Books search fallback. Returns a high-res JPEG or None."""
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
    try:
        r = client.get(ITUNES_SEARCH, params=params)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        if not results:
            return None
        # Pick the best title match above the threshold.
        scored: list[tuple[float, dict]] = []
        for item in results:
            res_title = item.get("trackName") or item.get("collectionName") or ""
            sim = _title_similarity(res_title, title)
            scored.append((sim, item))
        scored.sort(key=lambda t: t[0], reverse=True)
        best_sim, best = scored[0]
        if best_sim < _APPLE_TITLE_THRESHOLD:
            return None
        artwork = best.get("artworkUrl100") or best.get("artworkUrl60")
        if not artwork:
            return None
        # iTunes serves 100x100; swap for a high-res 1500x1500 variant.
        hires = artwork.replace("100x100bb", "1500x1500bb")
        hires = hires.replace("100x100", "1500x1500")
        ir = client.get(hires)
        if ir.status_code == 200 and len(ir.content) > 1500:
            return ir.content
        # Fall back to the original size if the upscale isn't there.
        orig = client.get(artwork)
        if orig.status_code == 200 and len(orig.content) > 1500:
            return orig.content
    except Exception:
        return None
    return None


def _try_google_books(
    title: str, authors: str, client: httpx.Client,
) -> bytes | None:
    """Google Books fallback when OpenLibrary has no cover."""
    if not title:
        return None
    q = f"intitle:{title}"
    first = _first_author(authors)
    if first:
        q += f"+inauthor:{first}"
    try:
        r = client.get(
            GBOOKS,
            params={"q": q, "maxResults": 1, "country": "US"},
        )
        if r.status_code != 200:
            return None
        items = (r.json() or {}).get("items") or []
        if not items:
            return None
        links = items[0].get("volumeInfo", {}).get("imageLinks") or {}
        url = links.get("thumbnail") or links.get("smallThumbnail")
        if not url:
            return None
        # Google serves HTTP by default; upgrade and request a larger
        # image while we're at it.
        url = url.replace("http://", "https://")
        url = url.replace("&edge=curl", "")
        ir = client.get(url)
        if ir.status_code == 200 and len(ir.content) > 1000:
            return ir.content
    except Exception:
        return None
    return None


# OpenLibrary placeholder ("no cover available") image is ~3 KB.
# Anything > 5 KB is a real cover.
_OL_COVER_MIN_BYTES = 5 * 1024

# Title-similarity floor for the OL search.json fallback. Tighter than
# Apple (which uses 0.6) because OL routinely returns adjacent series
# entries when the exact title is missing — letting those through would
# happily save the wrong cover.
_OL_SEARCH_TITLE_THRESHOLD = 0.7


def _try_openlibrary_search(
    title: str, authors: str, client: httpx.Client,
) -> bytes | None:
    """OL /search.json fallback with strict title-similarity scoring.

    Different from `_try_openlibrary`: that one trusts OL's first hit
    blindly. This one fetches up to 3 candidates, scores each title
    against the query using the same normalize + fuzzy-ratio helpers
    Hardcover matching uses, and only accepts hits >= 0.7. Designed
    to catch books where OL has the title but indexed under a slight
    variant (e.g. with/without a subtitle).
    """
    if not title:
        return None
    first = _first_author(authors)
    query_norm = hardcover.normalize_title(title)

    # Try the full title first; if OL returns nothing, retry with the
    # subtitle-stripped normalized title (OL search is exact-tokens-y
    # and trips on long subtitles like ": A Doctrinal Study").
    search_titles = [title]
    if query_norm and query_norm != title.strip().lower():
        search_titles.append(query_norm)

    docs: list[dict] = []
    for search_title in search_titles:
        params = {
            "title": search_title,
            "limit": 3,
            "fields": "cover_i,title,author_name",
        }
        if first:
            params["author"] = first
        try:
            r = client.get(OL_SEARCH, params=params)
            if r.status_code != 200:
                continue
            docs = (r.json() or {}).get("docs") or []
        except Exception:
            continue
        if docs:
            break
        # Polite pause between OL calls.
        time.sleep(1.5)
    if not docs:
        return None

    best_cover_id = None
    best_score = 0.0
    for doc in docs:
        cover_id = doc.get("cover_i")
        if not cover_id:
            continue
        cand_title = doc.get("title") or ""
        score = hardcover._fuzzy_ratio(
            query_norm, hardcover.normalize_title(cand_title),
        )
        if score > best_score:
            best_score = score
            best_cover_id = cover_id

    if best_cover_id is None or best_score < _OL_SEARCH_TITLE_THRESHOLD:
        return None

    # Polite pause before hitting the cover CDN (separate origin but
    # still OL).
    time.sleep(1.5)
    try:
        cr = client.get(
            OL_COVER.format(cover_id=best_cover_id),
            params={"default": "false"},
        )
    except Exception:
        return None
    if cr.status_code != 200:
        return None
    if len(cr.content) <= _OL_COVER_MIN_BYTES:
        return None
    # Validate it's a real JPEG (OL serves JPEGs even at /b/id/*-L.jpg).
    if not cr.content.startswith(b"\xff\xd8\xff"):
        return None
    if not cr.headers.get("content-type", "").startswith("image"):
        return None
    return cr.content


def _try_amazon(
    asin: str, client: httpx.Client,
) -> bytes | None:
    """Try the Amazon image CDN by ASIN.

    Walks the documented-by-convention URL patterns in order until one
    returns a 200 with a real JPEG > 5 KB. Sleeps 1s between hits to
    stay polite — Amazon rate-limits aggressively if you hammer it.

    Returns None on any error / 404 / undersized image. The caller
    treats None as "fall through, no cover from this source".
    """
    if not asin:
        return None
    asin = asin.strip().upper()
    for tpl in AMAZON_COVER_URLS:
        url = tpl.format(asin=asin)
        try:
            r = client.get(url)
        except Exception:
            time.sleep(1.0)
            continue
        if r.status_code != 200:
            time.sleep(1.0)
            continue
        body = r.content
        if len(body) <= _AMAZON_COVER_MIN_BYTES:
            time.sleep(1.0)
            continue
        if not body.startswith(b"\xff\xd8\xff"):
            time.sleep(1.0)
            continue
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("image"):
            time.sleep(1.0)
            continue
        return body
    return None


def _missing_books_for(user_id: int, owned_only: bool = False) -> list[dict]:
    conn = db.get_db()
    ownership_clause = " AND is_owned = 1" if owned_only else ""
    rows = conn.execute(
        """SELECT id, user_id, title, authors, asin
           FROM books
           WHERE user_id = ? AND cover_filename IS NULL""" + ownership_clause,
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _all_user_ids() -> list[int]:
    """Every user except the archive user (system-managed)."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id FROM users WHERE username <> 'archive'"
        " ORDER BY id"
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill missing book covers via OpenLibrary + Google Books.",
    )
    parser.add_argument(
        "--user",
        default="gordon",
        help="Username to enrich, or 'all' to walk every user.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of books processed (0 = no cap).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write covers or DB updates.",
    )
    parser.add_argument(
        "--owned-only",
        action="store_true",
        help="Only repair books in the user's owned collection.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.4,
        help="Seconds to sleep between books (politeness).",
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

    total_filled_hardcover = 0
    total_filled_ol = 0
    total_filled_gb = 0
    total_filled_apple = 0
    total_filled_ol_search = 0
    total_filled_amazon = 0
    total_processed = 0
    total_failed = 0

    with httpx.Client(
        follow_redirects=True,
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for uid in user_ids:
            books = _missing_books_for(uid, owned_only=args.owned_only)
            if not books:
                continue
            covers_dir = db.DATA_DIR / "covers" / str(uid)
            if not args.dry_run:
                covers_dir.mkdir(parents=True, exist_ok=True)

            for book in books:
                if args.limit and total_processed >= args.limit:
                    break
                total_processed += 1
                title = book["title"] or ""
                authors = book["authors"] or ""

                source = None
                cover = _try_hardcover(title, authors, client)
                if cover:
                    source = "hardcover"
                if not cover:
                    cover = _try_openlibrary(title, authors, client)
                if cover:
                    source = source or "openlibrary"
                else:
                    time.sleep(args.sleep)
                    cover = _try_google_books(title, authors, client)
                    if cover:
                        source = "google"
                if not cover:
                    # Polite second-source sleep before Apple.
                    time.sleep(max(args.sleep, 1.0))
                    cover = _try_apple_books(title, authors, client)
                    if cover:
                        source = "apple"
                if not cover:
                    # Next fallback: OpenLibrary /search.json with
                    # title-similarity scoring (catches near-miss
                    # titles the first-hit OL lookup at step 1
                    # silently skipped).
                    time.sleep(1.5)
                    cover = _try_openlibrary_search(
                        title, authors, client,
                    )
                    if cover:
                        source = "openlibrary_search"

                if not cover and book.get("asin"):
                    # Last-resort: Amazon image CDN by ASIN. Royal
                    # Road / KU / Audible titles often have no ISBN
                    # but a valid ASIN, and the Amazon image servers
                    # will hand back the cover if you know the ID.
                    # Polite 1s pause before the first hit; the helper
                    # sleeps 1s between its own URL attempts.
                    time.sleep(1.0)
                    cover = _try_amazon(book["asin"], client)
                    if cover:
                        source = "amazon"

                if not cover:
                    total_failed += 1
                    print(f"  - {book['id']:>5} {title[:50]:50s} "
                          f"(no cover found)")
                    time.sleep(args.sleep)
                    continue

                if args.dry_run:
                    print(f"  ? {book['id']:>5} {title[:50]:50s} "
                          f"would fill from {source}")
                else:
                    cover_path = covers_dir / f"{book['id']}.jpg"
                    cover_path.write_bytes(cover)
                    db.update_book(
                        book["id"], uid,
                        {
                            "cover_filename": f"{book['id']}.jpg",
                            "cover_updated_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        },
                    )
                    print(f"  + {book['id']:>5} {title[:50]:50s} "
                          f"filled from {source}")

                if source == "hardcover":
                    total_filled_hardcover += 1
                elif source == "openlibrary":
                    total_filled_ol += 1
                elif source == "google":
                    total_filled_gb += 1
                elif source == "apple":
                    total_filled_apple += 1
                elif source == "openlibrary_search":
                    total_filled_ol_search += 1
                elif source == "amazon":
                    total_filled_amazon += 1

                time.sleep(args.sleep)

            if args.limit and total_processed >= args.limit:
                break

    total_filled = (
        total_filled_hardcover + total_filled_ol + total_filled_gb
        + total_filled_apple + total_filled_ol_search
        + total_filled_amazon
    )
    print(
        f"\nDone: processed {total_processed}, "
        f"filled {total_filled} ("
        f"{total_filled_hardcover} Hardcover, "
        f"{total_filled_ol} OpenLibrary, "
        f"{total_filled_gb} Google Books, "
        f"{total_filled_apple} Apple Books, "
        f"{total_filled_ol_search} OL search, "
        f"{total_filled_amazon} Amazon), "
        f"{total_failed} unresolved."
    )


if __name__ == "__main__":
    main()
