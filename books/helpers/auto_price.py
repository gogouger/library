"""Bulk auto-fill book prices from Google Books, falling back to
format/category defaults when no online price is available.

Strategy:
  1. Google Books `q=isbn:{isbn}` with country=US. If `saleInfo.listPrice`
     or `retailPrice` is set, use that (it's the publisher's list price
     in USD).
  2. Otherwise, apply a format + manual_category fallback. The defaults
     skew slightly toward Gordon's real shelf: theology hardcovers cost
     more than mass-market paperbacks.

Concurrency-controlled so a 600-book backfill takes ~30 seconds instead
of hammering Google. No API key needed — Google Books has an anonymous
quota of 1 000 req/day, well above one full library refresh.
"""

import asyncio
import logging
import re
from typing import Any

import httpx
from decouple import config

from . import db
from .hardcover import normalize_title


GOOGLE_BOOKS_API_KEY = config("GOOGLE_BOOKS_API_KEY", default="")

log = logging.getLogger(__name__)

GOOGLE_BOOKS_API = "https://www.googleapis.com/books/v1/volumes"

# Concurrency is gated on the auth state. With an API key Google gives
# us 1 000 req/day and tolerates bursts; anonymous gets ~10 before the
# 429-wall slams shut, so we throttle hard. The first run will tell us
# which regime we're in.
CONCURRENCY_AUTHED = 10
CONCURRENCY_ANON = 1
DELAY_ANON = 1.2  # seconds between requests when unauthenticated

# Format + manual_category → USD median retail. Theology hardcovers
# (commentaries, study Bibles) are markedly pricier than fiction
# paperbacks, so Religious physical is the high outlier here.
DEFAULTS: dict[tuple[str, str], float] = {
    ("Religious", "physical"):  22.0,
    ("Religious", "ebook"):     11.0,
    ("Religious", "audiobook"): 18.0,
    ("Fiction", "physical"):    12.0,
    ("Fiction", "ebook"):       9.99,
    ("Fiction", "audiobook"):   14.95,
    ("Other", "physical"):      15.0,
    ("Other", "ebook"):         9.99,
    ("Other", "audiobook"):     14.95,
}


def _clean_isbn(raw: str | None) -> str:
    if not raw:
        return ""
    return re.sub(r"[^0-9X]", "", raw.upper())


def _pick_priced_volume(
    items: list[dict[str, Any]], seed_title: str,
) -> float | None:
    """Walk a Google Books search response, find the first volume whose
    normalized title matches the seed AND has a real USD list price.

    Sale info varies by EDITION on Google Play. An ISBN-search hits one
    specific edition that may be NOT_FOR_SALE (e.g. a hardcover) while
    the ebook edition of the same book is FOR_SALE. The title-match
    guard stops us picking an unrelated book that just happens to be on
    sale and ranked highly in the search results.
    """
    seed_norm = normalize_title(seed_title)
    for item in items:
        vol = item.get("volumeInfo") or {}
        if normalize_title(vol.get("title", "")) != seed_norm:
            continue
        sale = item.get("saleInfo") or {}
        for key in ("listPrice", "retailPrice"):
            entry = sale.get(key) or {}
            if entry.get("currencyCode") != "USD":
                continue
            amount = entry.get("amount")
            if isinstance(amount, (int, float)) and amount > 0:
                return round(float(amount), 2)
    return None


async def _query_google(
    client: httpx.AsyncClient, q: str,
) -> list[dict[str, Any]] | None:
    """Single Google Books search. Returns items list or raises on 429."""
    params: dict[str, str] = {"q": q, "country": "US", "maxResults": "10"}
    if GOOGLE_BOOKS_API_KEY:
        params["key"] = GOOGLE_BOOKS_API_KEY
    try:
        resp = await client.get(
            GOOGLE_BOOKS_API, params=params, timeout=10.0,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        if resp.status_code == 429:
            raise httpx.HTTPError("Google Books rate limit hit (429)")
        return None
    try:
        return (resp.json() or {}).get("items") or []
    except Exception:
        return None


async def _google_books_price(
    client: httpx.AsyncClient, title: str, isbn: str,
) -> float | None:
    """Multi-strategy Google Books lookup.

      1. ISBN search — the fast direct hit. Often returns ONE edition,
         which may or may not be on sale.
      2. Title search — pulls 10 mixed editions; we filter for the one
         that title-matches the seed AND has a real list price. This is
         the workhorse because the salable ebook edition usually has a
         different ISBN than the print edition we have on file.
    """
    if not title:
        return None

    # Step 1: ISBN-direct.
    clean = _clean_isbn(isbn)
    if len(clean) in (10, 13):
        items = await _query_google(client, f"isbn:{clean}")
        if items is None:
            return None
        price = _pick_priced_volume(items, title)
        if price is not None:
            return price

    # Step 2: Title-only.
    items = await _query_google(client, title)
    if items is None:
        return None
    return _pick_priced_volume(items, title)


def _fallback_price(book: dict[str, Any]) -> float:
    cat = book.get("manual_category") or "Other"
    fmt = book.get("book_format") or "ebook"
    return DEFAULTS.get((cat, fmt), 15.0)


async def auto_price_user(user_id: int) -> dict:
    """Auto-fill prices for every unpriced book the user owns.

    Returns a summary dict:
      {filled, from_google, from_default, skipped, total_unpriced}
    """
    conn = db.get_db()
    rows = conn.execute(
        """
        SELECT id, title, authors, isbn, book_format, manual_category
        FROM books
        WHERE user_id = ? AND price IS NULL
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    books = [dict(r) for r in rows]

    summary = {
        "filled": 0,
        "from_google": 0,
        "from_default": 0,
        "skipped": 0,
        "total_unpriced": len(books),
        "rate_limited": False,
        "has_api_key": bool(GOOGLE_BOOKS_API_KEY),
    }
    if not books:
        return summary

    # Per-minute / per-user throttle on Books API is tight even with a
    # key — keep concurrency low and add a polite spacing. Empirically
    # 2 concurrent + 0.7s after every request keeps us comfortably
    # under the per-minute cap during a ~600-book backfill.
    sem = asyncio.Semaphore(
        2 if GOOGLE_BOOKS_API_KEY else CONCURRENCY_ANON,
    )
    delay = 0.7 if GOOGLE_BOOKS_API_KEY else DELAY_ANON
    # Once Google hits us with a 429 we stop trying online lookups for
    # the remainder of the batch — there's no point burning more time
    # waiting on a quota we've already exhausted. Every remaining book
    # silently falls through to format defaults.
    rate_limited = False
    results: list[tuple[int, float, str]] = []

    async def process(client: httpx.AsyncClient, book: dict) -> None:
        """Google Books primarily sells ebooks — its listPrice on a
        salable volume is the ebook price. For physical and audiobook
        rows, the ebook price would be wrong (audiobooks cost ~2x,
        hardcovers cost ~1.5x), so we skip the API call entirely and
        go straight to format defaults. The fallback is honest about
        not knowing the real new-book retail."""
        nonlocal rate_limited
        price: float | None = None
        fmt = (book.get("book_format") or "").lower()
        if fmt == "ebook":
            async with sem:
                if not rate_limited:
                    try:
                        price = await _google_books_price(
                            client,
                            book.get("title") or "",
                            book.get("isbn") or "",
                        )
                    except httpx.HTTPError:
                        rate_limited = True
                    await asyncio.sleep(delay)
        if price is not None:
            results.append((book["id"], price, "google"))
        else:
            results.append((
                book["id"], _fallback_price(book), "default",
            ))

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "library/1.0 gordon@ggouger.com"},
    ) as client:
        await asyncio.gather(*(process(client, b) for b in books))

    summary["rate_limited"] = rate_limited
    for book_id, price, source in results:
        try:
            db.update_book(book_id, user_id, {"price": price})
            summary["filled"] += 1
            summary[f"from_{source}"] += 1
        except Exception:
            log.exception("update failed for book %d", book_id)
            summary["skipped"] += 1

    return summary
