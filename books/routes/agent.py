"""Narrow internal command endpoint for the personal MCP gateway.

This is deliberately not part of the public Library API.  Caddy exposes it
only on the private Finance edge and the request must also carry a shared,
deployment-only command secret.  The Finance gateway owns end-user OAuth,
scope checks, and the audit trail; this module enforces the final write
boundary and applies the same library invariants as the web UI.
"""

import asyncio
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..helpers import db
from ..helpers.hardcover import normalize_title
from ..helpers.metadata import search_google_books
from .books import _link_series_background


log = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])


class AgentCommand(BaseModel):
    """The small allowlist of mutations an approved personal agent may make."""

    action: Literal["add_owned_book", "track_series", "record_review"]
    title: str | None = Field(default=None, max_length=300)
    authors: str | None = Field(default=None, max_length=500)
    series: str | None = Field(default=None, max_length=300)
    series_index: float | None = Field(default=None, ge=0, le=10000)
    book_format: Literal["ebook", "audiobook", "physical", "other"] = "ebook"
    rating: float | None = Field(default=None, ge=0.5, le=5)
    review: str | None = Field(default=None, max_length=10000)
    mark_read: bool = False
    enrich: bool = True

    @model_validator(mode="after")
    def validate_command(self):
        if self.rating is not None and (self.rating * 2) % 1 != 0:
            raise ValueError("rating must use half-star increments")
        if not (self.title or "").strip() and self.action != "track_series":
            raise ValueError("title is required")
        if self.action == "track_series" and not (self.series or "").strip():
            raise ValueError("series is required")
        if self.action == "record_review" and self.rating is None and self.review is None and not self.mark_read:
            raise ValueError("provide a rating, review, or mark_read=true")
        return self


def _require_internal_command(
    internal: str | None, secret: str | None,
) -> None:
    expected = os.environ.get("BOOKS_MCP_COMMAND_SECRET", "")
    if (
        internal != "1"
        or not expected
        or not secret
        or not hmac.compare_digest(secret, expected)
    ):
        raise HTTPException(status_code=403, detail="internal agent command required")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


async def _google_metadata(title: str, authors: str | None) -> dict:
    """Best-effort metadata from the fixed Google Books endpoint.

    A title match is required before accepting a result.  The caller can
    always override any of this information later in Library.
    """
    try:
        results = await search_google_books(" ".join(filter(None, [title, authors])))
    except Exception:
        log.info("Agent metadata lookup unavailable for %r", title, exc_info=True)
        return {}
    title_folded = title.casefold()
    for result in results:
        candidate = str(result.get("title") or "").strip()
        if candidate.casefold() == title_folded:
            return result
    return {}


async def _save_google_cover(user_id: int, book_id: int, cover_url: str | None) -> bool:
    """Download only a Google Books cover chosen by our fixed metadata lookup."""
    if not cover_url:
        return False
    from urllib.parse import urlparse

    host = (urlparse(cover_url).hostname or "").lower()
    if host not in {"books.google.com", "books.googleusercontent.com"}:
        return False
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(cover_url)
            response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("image/"):
            return False
        cover_dir = db.DATA_DIR / "covers" / str(user_id)
        cover_dir.mkdir(parents=True, exist_ok=True)
        (cover_dir / f"{book_id}.jpg").write_bytes(response.content)
        db.update_book(book_id, user_id, {
            "cover_filename": f"{book_id}.jpg",
            "cover_updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return True
    except Exception:
        log.info("Agent cover download unavailable for book %d", book_id, exc_info=True)
        return False


def _matching_owned_book(user_id: int, title: str, authors: str | None) -> dict | None:
    if authors:
        found = db.find_owned_match(
            user_id, title, authors, series_link_id=None, series_index=None,
        )
        if found:
            return found
    # A conversational agent often has a title but not the author.  Permit an
    # exact normalized-title match only when it is unambiguous; never guess
    # between two editions or two books with the same title.
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, title FROM books WHERE user_id = ? AND is_owned = 1",
        (user_id,),
    ).fetchall()
    conn.close()
    matches = [row["id"] for row in rows if normalize_title(row["title"]) == normalize_title(title)]
    return db.get_book(matches[0], user_id) if len(matches) == 1 else None


async def _add_owned_book(user_id: int, command: AgentCommand) -> dict:
    title = _clean(command.title)
    assert title is not None
    authors = _clean(command.authors)
    metadata = await _google_metadata(title, authors) if command.enrich else {}
    authors = authors or _clean(metadata.get("authors")) or "Unknown"
    series = _clean(command.series) or _clean(metadata.get("series"))
    series_index = command.series_index
    if series_index is None:
        raw_index = metadata.get("series_index")
        try:
            series_index = float(raw_index) if raw_index is not None else None
        except (TypeError, ValueError):
            series_index = None

    series_link_id = db.get_or_create_series_link(user_id, series) if series else None
    existing = db.find_owned_match(
        user_id, title, authors, series_link_id=series_link_id, series_index=series_index,
    )
    if existing:
        return {"created": False, "reason": "already_owned", "book": existing}

    # Tracking a series first creates unowned reference entries so the owner can
    # see what is missing. When an agent then adds one of those titles, promote
    # the matching placeholder instead of inserting a second card at the same
    # series position. Match the slot (rather than the catalog author) because
    # a narrator/edition mismatch must not create a duplicate owned record.
    placeholder = None
    if series_link_id is not None and series_index is not None:
        conn = db.get_db()
        placeholder = conn.execute(
            """SELECT id FROM books
               WHERE user_id = ? AND series_link_id = ?
                 AND series_index = ? AND is_owned = 0
               LIMIT 1""",
            (user_id, series_link_id, series_index),
        ).fetchone()
        conn.close()
    if placeholder:
        book_id = placeholder["id"]
        db.update_book(book_id, user_id, {
            "title": title,
            "sort_title": db.make_sort_title(title),
            "authors": authors,
            "author_sort": db.make_author_sort(authors),
            "description": _clean(metadata.get("description")),
            "isbn": _clean(metadata.get("isbn")),
            "tags": metadata.get("categories") or [],
            "published_date": _clean(metadata.get("published_date")),
            "book_format": command.book_format,
            "is_owned": 1,
        })
        cover_saved = await _save_google_cover(
            user_id, book_id, metadata.get("cover_url"),
        )
        return {
            "created": False,
            "reason": "promoted_series_placeholder",
            "cover_saved": cover_saved,
            "book": db.get_book(book_id, user_id),
    }

    now = datetime.now(timezone.utc).isoformat()
    book_id = db.insert_book(
        user_id=user_id,
        title=title,
        sort_title=db.make_sort_title(title),
        authors=authors,
        author_sort=db.make_author_sort(authors),
        series=series,
        series_index=series_index,
        description=_clean(metadata.get("description")),
        cover_filename=None,
        file_path=None,
        isbn=_clean(metadata.get("isbn")),
        goodreads_id=None,
        tags=metadata.get("categories") or [],
        date_added=now,
        date_finished=None,
        rating=None,
        reading_status="unread",
        is_owned=1,
        series_link_id=series_link_id,
        published_date=_clean(metadata.get("published_date")),
        book_format=command.book_format,
    )
    cover_saved = await _save_google_cover(user_id, book_id, metadata.get("cover_url"))
    if series_link_id:
        asyncio.create_task(_link_series_background(user_id, series_link_id))
    return {"created": True, "cover_saved": cover_saved, "book": db.get_book(book_id, user_id)}


async def _track_series(user_id: int, command: AgentCommand) -> dict:
    series = _clean(command.series)
    assert series is not None
    series_link_id = db.get_or_create_series_link(user_id, series)
    asyncio.create_task(_link_series_background(user_id, series_link_id))
    return {"tracked": True, "series_link_id": series_link_id, "series": series}


def _record_review(user_id: int, command: AgentCommand) -> dict:
    title = _clean(command.title)
    assert title is not None
    book = _matching_owned_book(user_id, title, _clean(command.authors))
    if not book:
        raise HTTPException(status_code=404, detail="No owned book matched this title; add it first or include the author")
    updates: dict = {}
    if command.rating is not None:
        updates["rating"] = command.rating
    if command.review is not None:
        updates["review"] = command.review.strip()
    if command.mark_read:
        updates["reading_status"] = "read"
        updates["progress"] = 1.0
        updates["date_finished"] = datetime.now(timezone.utc).date().isoformat()
    db.update_book(book["id"], user_id, updates)
    return {"updated": True, "book": db.get_book(book["id"], user_id)}


@router.post("/agent-command")
async def agent_command(
    command: AgentCommand,
    x_internal_mcp_module: str | None = Header(default=None),
    x_mcp_command_secret: str | None = Header(default=None),
) -> dict:
    """Execute one allowlisted command for the fixed owner library only."""
    _require_internal_command(x_internal_mcp_module, x_mcp_command_secret)
    owner = db.get_user_by_username("ggouger")
    if not owner:
        raise HTTPException(status_code=503, detail="Library owner is not configured")
    if command.action == "add_owned_book":
        return await _add_owned_book(owner["id"], command)
    if command.action == "track_series":
        return await _track_series(owner["id"], command)
    return _record_review(owner["id"], command)
