import asyncio
import hashlib
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator

from ..helpers import db, hardcover, metrics as metrics_helper
from ..helpers import auto_price as auto_price_helper
from ..helpers import auto_tags as auto_tags_helper
from ..helpers import auto_length as auto_length_helper
from ..helpers.auth import (
    library_owner,
    optional_user,
    require_owner,
    require_user,
)
from ..helpers.hardcover import _fuzzy_ratio, normalize_title
from ..helpers.metadata import (
    extract_epub_metadata,
    search_google_books,
    search_open_library,
)

from ..helpers.db import DATA_DIR

log = logging.getLogger(__name__)
router = APIRouter(tags=["books"])


class BookUpdate(BaseModel):
    title: str | None = None
    sort_title: str | None = None
    authors: str | None = None
    author_sort: str | None = None
    series: str | None = None
    series_index: float | None = None
    description: str | None = None
    review: str | None = None
    isbn: str | None = None
    published_date: str | None = None
    goodreads_id: str | None = None
    tags: list[str] | None = None
    date_finished: str | None = None
    rating: float | None = None
    reading_status: str | None = None
    progress: float | None = None
    is_favorite: bool | None = None
    is_all_time_fav: bool | None = None
    is_second_fav: bool | None = None
    is_third_fav: bool | None = None
    also_physical: bool | None = None
    is_owned: bool | None = None
    manual_category: str | None = None
    price: float | None = None

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: float | None) -> float | None:
        if v is None:
            return v
        # Allow 0.5-step half-star ratings in [0.5, 5.0].
        if v < 0.5 or v > 5.0 or (v * 2) % 1 != 0:
            raise ValueError("rating must be a half-star value in [0.5, 5.0]")
        return v

    @field_validator("reading_status")
    @classmethod
    def validate_reading_status(cls, v: str | None) -> str | None:
        if v is not None and v not in ("unread", "reading", "read"):
            raise ValueError(
                "reading_status must be 'unread', 'reading', or 'read'"
            )
        return v

    @field_validator("manual_category")
    @classmethod
    def validate_manual_category(
        cls, v: str | None,
    ) -> str | None:
        if v is None or v == "":
            return None
        if v not in ("Religious", "Fiction"):
            raise ValueError(
                "manual_category must be 'Religious', 'Fiction', or null"
            )
        return v


class MetadataSearchRequest(BaseModel):
    query: str
    source: str = "google"


def _is_owner(
    viewer: dict | None, owner: dict
) -> bool:
    """Check if the viewer is the library owner or a superuser."""
    if viewer is None:
        return False
    return viewer["user_id"] == owner["id"] or viewer.get("is_superuser")


@router.get("/metrics")
def get_metrics(
    owner: library_owner,
    viewer: optional_user,
) -> dict:
    """Library metrics. Anyone can see the non-money breakdowns
    (counts, pages read, hours listened, sub-genres, records, authors,
    ratings). Money sections (value totals, spend, prices, most-spent
    author column, top-by-price section) are stripped for non-owners.
    """
    is_owner = (
        viewer is not None
        and (viewer["user_id"] == owner["id"] or viewer.get("is_superuser"))
    )
    return metrics_helper.compute_metrics(
        owner["id"], is_owner=is_owner,
    )


@router.post("/metrics/auto-price")
async def post_auto_price(auth: require_owner) -> dict:
    """Bulk-fill prices for every unpriced book. Best-effort: tries
    Google Books for ISBN-listed books, falls back to format/category
    defaults for the rest. Returns a summary the page renders inline."""
    return await auto_price_helper.auto_price_user(auth["user_id"])


@router.post("/metrics/auto-tags")
async def post_auto_tags(auth: require_owner) -> dict:
    """Bulk-fill sub-genre tags from Hardcover for every untagged book."""
    return await auto_tags_helper.auto_tags_user(auth["user_id"])


@router.post("/metrics/auto-length")
async def post_auto_length(auth: require_owner) -> dict:
    """Bulk-fill pages + audio_seconds from Hardcover so the new
    Lifetime / By-year metrics can actually compute."""
    return await auto_length_helper.backfill_lengths(auth["user_id"])


# --- Read-only routes (anonymous or authenticated) ---


@router.get("/books")
def list_books(
    owner: library_owner,
    viewer: optional_user,
    q: str | None = None,
    series: str | None = None,
    reading_status: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    is_favorite: bool | None = None,
    is_owned: bool | None = None,
    has_series: bool | None = None,
    rated: bool | None = None,
    book_format: str | None = None,
    tag: str | None = None,
    sort: str = "title",
    order: str = "asc",
    limit: int = Query(default=50, le=1000),
    offset: int = 0,
    include_ghosts: bool = False,
    group_by_series: bool | None = None,
) -> dict:
    user_id = owner["id"]
    # No ownership filter means the complete personal series catalog. The
    # explicit Owned / Don't Own filters still narrow the view when chosen.
    effective_is_owned = is_owned
    # When grouping by series, fetch a wider page so we can
    # reorder client-side. The frontend currently paginates
    # via infinite scroll but the grouped view benefits from
    # bigger first batches; keep the cap from Query above.
    effective_sort = sort
    effective_has_series = has_series
    grouped = (
        group_by_series
        if group_by_series is not None
        else sort == "series"
    )
    # In grouped mode we still want standalones, so do NOT
    # force has_series=true upstream.
    if grouped:
        effective_sort = "series"
    books = db.get_books(
        user_id,
        q=q,
        series=series,
        reading_status=reading_status,
        min_rating=min_rating,
        max_rating=max_rating,
        is_favorite=is_favorite,
        is_owned=effective_is_owned,
        has_series=effective_has_series,
        rated=rated,
        book_format=book_format,
        tag=tag,
        sort=effective_sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    total = db.count_books(
        user_id,
        q=q,
        series=series,
        reading_status=reading_status,
        min_rating=min_rating,
        max_rating=max_rating,
        is_favorite=is_favorite,
        is_owned=effective_is_owned,
        has_series=effective_has_series,
        rated=rated,
        book_format=book_format,
        tag=tag,
    )

    if grouped:
        # Place all series books first (by series, series_index),
        # then standalones (by title) at the tail.
        series_books = [
            b for b in books
            if b.get("series_link_id") is not None
        ]
        standalone_books = [
            b for b in books
            if b.get("series_link_id") is None
        ]
        # Stable sort: series tuple, then title for standalones.
        series_books.sort(key=lambda b: (
            (b.get("series") or "").lower(),
            b.get("series_index") or 0,
        ))
        standalone_books.sort(key=lambda b: (
            (b.get("sort_title") or b.get("title") or "").lower()
        ))
        books = series_books + standalone_books

    # Optionally interleave ghost entries from series the user
    # owns at least one book in. Only the first batch carries
    # ghosts to avoid duplicating them across pagination.
    if include_ghosts and offset == 0:
        ghosts = db.get_ghost_entries_for_user(user_id)
        # Mark explicitly.
        for g in ghosts:
            g["is_ghost"] = True
        if grouped:
            # Merge ghosts into the series cluster by series name.
            from itertools import groupby
            owned_by_series: dict[int, list[dict]] = {}
            owned_seq_keys: list[int] = []
            for b in books:
                slid = b.get("series_link_id")
                if slid is None:
                    continue
                if slid not in owned_by_series:
                    owned_by_series[slid] = []
                    owned_seq_keys.append(slid)
                owned_by_series[slid].append(b)
            ghost_by_series: dict[int, list[dict]] = {}
            for g in ghosts:
                slid = g.get("series_link_id")
                if slid is None:
                    continue
                ghost_by_series.setdefault(slid, []).append(g)
            merged_series: list[dict] = []
            for slid in owned_seq_keys:
                combined = list(owned_by_series[slid])
                combined.extend(ghost_by_series.get(slid, []))
                combined.sort(key=lambda b: (
                    b.get("series_index") or 0,
                ))
                merged_series.extend(combined)
            standalone_books = [
                b for b in books
                if b.get("series_link_id") is None
            ]
            books = merged_series + standalone_books
        else:
            books = list(books) + ghosts
        # Don't inflate `total` with ghosts to avoid breaking
        # infinite-scroll exit conditions; the frontend treats
        # ghosts as ride-along only on the first batch.

    return {
        "books": books,
        "total": total,
        "is_owner": _is_owner(viewer, owner),
        "library_owner": {
            "username": owner["username"],
            "display_name": owner["display_name"],
        },
        "group_by_series": grouped,
    }


@router.get("/books/{book_id}")
def get_book(
    book_id: int,
    owner: library_owner,
    viewer: optional_user,
) -> dict:
    book = db.get_book(book_id, owner["id"])
    if book is None:
        raise HTTPException(
            status_code=404, detail="Book not found"
        )
    book["is_owner"] = _is_owner(viewer, owner)
    book["kindle_email"] = owner.get("kindle_email")
    book["library_owner"] = {
        "username": owner["username"],
        "display_name": owner["display_name"],
    }
    return book


@router.get("/books/{book_id}/cover")
def get_cover(
    book_id: int,
    owner: library_owner,
    _viewer: optional_user,
) -> FileResponse:
    book = db.get_book(book_id, owner["id"])
    if book is None or not book.get("cover_filename"):
        raise HTTPException(
            status_code=404, detail="Cover not found"
        )
    cover_path = (
        DATA_DIR
        / "covers"
        / str(owner["id"])
        / book["cover_filename"]
    )
    if not cover_path.exists():
        raise HTTPException(
            status_code=404, detail="Cover file missing"
        )
    return FileResponse(
        str(cover_path), media_type="image/jpeg"
    )


# --- Owner-only routes ---


@router.post("/books", response_model=None)
async def add_book(
    file: UploadFile,
    payload: require_owner,
    title: str | None = None,
    authors: str | None = None,
    series: str | None = None,
    series_index: float | None = None,
    merge_with: int | None = None,
    force: bool = False,
) -> dict | JSONResponse:
    user_id = payload["user_id"]

    # Save uploaded epub to temp location
    temp_path = DATA_DIR / f"temp_{uuid.uuid4()}.epub"
    try:
        with open(temp_path, "wb") as f:
            content = await file.read()
            f.write(content)

        epub_hash = hashlib.md5(content).hexdigest()

        # Extract metadata from epub
        meta = extract_epub_metadata(str(temp_path))

        book_title = title or meta["title"] or file.filename or "Unknown"
        book_authors = authors or meta["authors"] or "Unknown"
        sort_title = db.make_sort_title(book_title)

        now = datetime.now(timezone.utc).isoformat()

        # Resolve series -> series_link_id
        series_link_id = None
        if series:
            series_link_id = db.get_or_create_series_link(
                user_id, series
            )

        if merge_with is not None:
            # Merge into existing book
            existing = db.get_book(merge_with, user_id)
            if not existing:
                raise HTTPException(
                    status_code=404,
                    detail="Merge target not found",
                )
            book_id = merge_with
            merge_data = {
                "title": book_title,
                "sort_title": sort_title,
                "authors": book_authors,
                "author_sort": db.make_author_sort(
                    book_authors
                ),
                "series": series,
                "series_index": series_index,
                "series_link_id": series_link_id,
                "is_owned": 1,
            }
            if meta.get("description"):
                merge_data["description"] = meta["description"]
            if meta.get("isbn"):
                merge_data["isbn"] = meta["isbn"]
            if meta.get("tags"):
                merge_data["tags"] = meta["tags"]
            db.update_book(book_id, user_id, merge_data)
            log.info(
                "Merged upload into book %d for user %d",
                book_id, user_id,
            )
        else:
            # Check for existing unowned book to upgrade
            existing_id = db.find_unowned_match(
                user_id, book_title,
                series_link_id=series_link_id,
                series_index=series_index,
            )

            if existing_id:
                # Upgrade unowned placeholder to owned book
                book_id = existing_id
                upgrade = {
                    "title": book_title,
                    "sort_title": sort_title,
                    "authors": book_authors,
                    "author_sort": db.make_author_sort(
                        book_authors
                    ),
                    "series": series,
                    "series_index": series_index,
                    "series_link_id": series_link_id,
                    "is_owned": 1,
                }
                if meta.get("description"):
                    upgrade["description"] = meta["description"]
                if meta.get("isbn"):
                    upgrade["isbn"] = meta["isbn"]
                if meta.get("tags"):
                    upgrade["tags"] = meta["tags"]
                db.update_book(book_id, user_id, upgrade)
                log.info(
                    "Upgraded unowned book %d to owned for "
                    "user %d",
                    book_id, user_id,
                )
            else:
                # Collision check for owned duplicates
                if not force:
                    match = db.find_owned_match(
                        user_id, book_title, book_authors,
                        series_link_id=series_link_id,
                        series_index=series_index,
                    )
                    if match:
                        if temp_path.exists():
                            temp_path.unlink()
                        return JSONResponse(
                            status_code=409,
                            content={
                                "conflict": True,
                                "existing_book": {
                                    "id": match["id"],
                                    "title": match["title"],
                                    "authors": match["authors"],
                                    "series": match.get("series"),
                                    "series_index": match.get("series_index"),
                                    "rating": match.get("rating"),
                                    "reading_status": match.get("reading_status"),
                                    "cover_filename": match.get("cover_filename"),
                                    "user_id": match["user_id"],
                                },
                            },
                        )

                # Insert new book record
                book_id = db.insert_book(
                    user_id=user_id,
                    title=book_title,
                    sort_title=sort_title,
                    authors=book_authors,
                    author_sort=db.make_author_sort(
                        book_authors
                    ),
                    series=series,
                    series_index=series_index,
                    description=meta.get("description"),
                    cover_filename=None,
                    file_path=None,
                    isbn=meta.get("isbn"),
                    goodreads_id=None,
                    tags=meta.get("tags"),
                    date_added=now,
                    date_finished=None,
                    rating=None,
                    reading_status="unread",
                    series_link_id=series_link_id,
                )

        # Move epub to final location
        user_files = DATA_DIR / "files" / str(user_id)
        user_files.mkdir(parents=True, exist_ok=True)
        final_path = user_files / f"{book_id}.epub"
        shutil.move(str(temp_path), str(final_path))

        # Update file_path and hash in DB
        db.update_book(
            book_id, user_id, {
                "file_path": f"{book_id}.epub",
                "epub_hash": epub_hash,
            }
        )

        # Save cover if extracted
        if meta.get("cover_data"):
            user_covers = DATA_DIR / "covers" / str(user_id)
            user_covers.mkdir(parents=True, exist_ok=True)
            cover_path = user_covers / f"{book_id}.jpg"
            cover_path.write_bytes(meta["cover_data"])
            now = datetime.now(timezone.utc).isoformat()
            db.update_book(
                book_id,
                user_id,
                {
                    "cover_filename": f"{book_id}.jpg",
                    "cover_updated_at": now,
                },
            )

        # Sync EPUB metadata to match DB
        db.sync_book_epub(book_id, user_id)

        book = db.get_book(book_id, user_id)

        # Auto-link series in background if book has a series
        if series and series_link_id:
            asyncio.create_task(
                _link_series_background(
                    user_id, series_link_id
                )
            )

        return book
    finally:
        if temp_path.exists():
            temp_path.unlink()


@router.patch("/books/{book_id}")
def update_book(
    book_id: int,
    updates: BookUpdate,
    payload: require_owner,
) -> dict:
    user_id = payload["user_id"]
    update_data = updates.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=400, detail="No updates provided"
        )

    # Auto-compute sort fields when title or authors change
    if "title" in update_data and update_data["title"]:
        update_data["sort_title"] = db.make_sort_title(
            update_data["title"]
        )
    if "authors" in update_data and update_data["authors"]:
        update_data["author_sort"] = db.make_author_sort(
            update_data["authors"]
        )

    # Reset progress when moving from "read" back to "reading"/"unread"
    if "reading_status" in update_data and update_data[
        "reading_status"
    ] in ("reading", "unread"):
        current = db.get_book(book_id, user_id)
        if current and current.get("reading_status") == "read":
            update_data.setdefault("progress", 0.0)
            update_data.setdefault("date_finished", None)

    # Resolve series name to series_link_id
    if "series" in update_data:
        if update_data["series"]:
            update_data["series_link_id"] = (
                db.get_or_create_series_link(
                    user_id, update_data["series"]
                )
            )
        else:
            update_data["series_link_id"] = None

    # Tier invariants: gold/silver/bronze are mutually exclusive. Setting
    # any one of them forces is_favorite=true and clears the other two.
    if update_data.get("is_all_time_fav"):
        update_data["is_favorite"] = True
        update_data["is_second_fav"] = False
        update_data["is_third_fav"] = False
    if update_data.get("is_second_fav"):
        update_data["is_all_time_fav"] = False
        update_data["is_third_fav"] = False
        update_data.setdefault("is_favorite", True)
    if update_data.get("is_third_fav"):
        update_data["is_all_time_fav"] = False
        update_data["is_second_fav"] = False
        update_data.setdefault("is_favorite", True)
    # Coerce bools to 0/1 since the column is INTEGER.
    for k in ("is_favorite",
              "is_all_time_fav", "is_second_fav", "is_third_fav",
              "also_physical", "is_owned"):
        if k in update_data and isinstance(update_data[k], bool):
            update_data[k] = 1 if update_data[k] else 0

    success = db.update_book(book_id, user_id, update_data)
    if not success:
        raise HTTPException(
            status_code=404, detail="Book not found"
        )
    return db.get_book(book_id, user_id)


@router.delete("/books/{book_id}")
def delete_book(
    book_id: int,
    payload: require_owner,
) -> dict:
    user_id = payload["user_id"]
    book = db.get_book(book_id, user_id)
    if book is None:
        raise HTTPException(
            status_code=404, detail="Book not found"
        )

    archive_user = db.get_user_by_username("archive")
    if not archive_user:
        raise HTTPException(
            status_code=500,
            detail="Archive user not found",
        )
    archive_id = archive_user["id"]

    # Move files to archive directories
    if book.get("cover_filename"):
        src = (
            DATA_DIR / "covers" / str(user_id)
            / book["cover_filename"]
        )
        if src.exists():
            dst_dir = (
                DATA_DIR / "covers" / str(archive_id)
            )
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(
                str(src), str(dst_dir / f"{book_id}.jpg")
            )
    if book.get("file_path"):
        src = (
            DATA_DIR / "files" / str(user_id)
            / book["file_path"]
        )
        if src.exists():
            dst_dir = (
                DATA_DIR / "files" / str(archive_id)
            )
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(
                str(src),
                str(dst_dir / f"{book_id}.epub"),
            )

    db.archive_book(book_id, user_id, archive_id)
    return {"success": True}


@router.get("/books/{book_id}/file")
def get_file(
    book_id: int,
    payload: require_owner,
) -> FileResponse:
    book = db.get_book(book_id, payload["user_id"])
    if book is None or not book.get("file_path"):
        raise HTTPException(
            status_code=404, detail="File not found"
        )
    file_path = (
        DATA_DIR
        / "files"
        / str(payload["user_id"])
        / book["file_path"]
    )
    if not file_path.exists():
        raise HTTPException(
            status_code=404, detail="File missing"
        )
    return FileResponse(
        str(file_path),
        media_type="application/epub+zip",
        filename=f"{book['title']}.epub",
    )


@router.post("/books/{book_id}/copy-to-temp")
async def copy_to_temp(
    book_id: int,
    owner: library_owner,
    viewer: require_user,
) -> dict:
    """Copy a book's epub to temp and return preview data.

    Allows any logged-in user to start copying a book from
    another user's library into their own via the metadata
    picker flow.
    """
    book = db.get_book(book_id, owner["id"])
    if book is None:
        raise HTTPException(
            status_code=404, detail="Book not found"
        )

    temp_id = str(uuid.uuid4())
    epub_source = None
    epub_cover_url = None

    # Copy epub to temp if source has one
    if book.get("file_path"):
        src_epub = (
            DATA_DIR / "files" / str(owner["id"])
            / book["file_path"]
        )
        if src_epub.exists():
            temp_path = DATA_DIR / f"temp_{temp_id}.epub"
            shutil.copy2(str(src_epub), str(temp_path))

            meta = extract_epub_metadata(str(temp_path))

            if meta.get("cover_data"):
                cover_path = (
                    DATA_DIR / f"temp_{temp_id}_cover.jpg"
                )
                cover_path.write_bytes(meta["cover_data"])
                epub_cover_url = (
                    f"/api/{viewer['username']}/metadata"
                    f"/preview-cover/{temp_id}"
                )

            epub_source = _normalize_result({
                "title": meta.get("title") or "",
                "authors": meta.get("authors") or "",
                "description": (
                    meta.get("description") or ""
                ),
                "isbn": meta.get("isbn") or "",
                "published_date": "",
                "cover_url": epub_cover_url,
                "series": meta.get("series") or "",
                "series_index": meta.get("series_index"),
            })

    # Build "current" source from the source book's data
    current_cover_url = None
    if book.get("cover_filename"):
        current_cover_url = (
            f"/covers/{owner['id']}"
            f"/{book['cover_filename']}"
        )
    current = _normalize_result({
        "title": book.get("title") or "",
        "authors": book.get("authors") or "",
        "description": book.get("description") or "",
        "isbn": book.get("isbn") or "",
        "published_date": (
            book.get("published_date") or ""
        ),
        "cover_url": current_cover_url,
        "series": book.get("series") or "",
        "series_index": book.get("series_index"),
    })

    # Fetch external sources
    title = book.get("title") or ""
    authors = book.get("authors") or ""
    isbn = book.get("isbn") or ""
    google, hc, ol, errors = await _fetch_all_sources(
        title, authors, isbn
    )

    return {
        "temp_id": temp_id,
        "current": current,
        "epub": epub_source,
        "google": google,
        "hardcover": hc,
        "openlibrary": ol,
        "errors": {
            "google": errors.get("google"),
            "hardcover": errors.get("hardcover"),
            "openlibrary": errors.get("openlibrary"),
        },
    }


@router.post("/metadata/search")
async def search_metadata(
    req: MetadataSearchRequest,
    _payload: require_owner,
) -> dict:
    if req.source == "openlibrary":
        results = await search_open_library(req.query)
    else:
        results = await search_google_books(req.query)
    return {"results": results}


@router.post("/metadata/extract")
async def extract_metadata(
    file: UploadFile,
    _payload: require_owner,
) -> dict:
    temp_path = DATA_DIR / f"temp_{uuid.uuid4()}.epub"
    try:
        with open(temp_path, "wb") as f:
            content = await file.read()
            f.write(content)
        meta = extract_epub_metadata(str(temp_path))
        meta.pop("cover_data", None)
        return meta
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _strip_html(text: str) -> str:
    """Strip HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text)


def _title_matches(
    result_title: str, book_title: str
) -> bool:
    """Check if an ISBN lookup result title matches the book.

    Uses a low threshold (0.5) since we just want to catch
    clearly wrong books (e.g. ISBN returning a different book).
    """
    return (
        _fuzzy_ratio(
            normalize_title(result_title),
            normalize_title(book_title),
        )
        > 0.5
    )


def _pick_best_result(
    results: list[dict], book_title: str
) -> dict:
    """Pick the result whose title best matches book_title."""
    book_norm = normalize_title(book_title)
    best = results[0]
    best_score = _fuzzy_ratio(
        normalize_title(best.get("title", "")), book_norm
    )
    for r in results[1:]:
        score = _fuzzy_ratio(
            normalize_title(r.get("title", "")), book_norm
        )
        if score > best_score:
            best_score = score
            best = r
    return best


def _normalize_result(r: dict) -> dict:
    """Normalize a metadata result to standard fields."""
    return {
        "title": r.get("title") or "",
        "authors": r.get("authors") or "",
        "description": _strip_html(
            r.get("description") or ""
        ),
        "isbn": r.get("isbn") or "",
        "published_date": (
            r.get("published_date") or ""
        ),
        "cover_url": r.get("cover_url") or None,
        "series": (
            r.get("series_name") or r.get("series") or ""
        ),
        "series_index": (
            str(r["series_index"])
            if r.get("series_index") is not None
            else ""
        ),
    }


async def _fetch_google(
    title: str,
    authors: str,
    isbn: str,
    errors: dict[str, str | None],
) -> dict | None:
    """Fetch metadata from Google Books."""
    try:
        query = f"{title} {authors}"
        if isbn:
            log.info(
                "Google Books: trying ISBN lookup %r",
                isbn,
            )
            results = await search_google_books(
                f"isbn:{isbn}", max_results=1
            )
            if results:
                r = results[0]
                if _title_matches(
                    r.get("title", ""), title
                ):
                    return _normalize_result(r)
                log.warning(
                    "Google Books: ISBN %r returned "
                    "%r but expected %r, "
                    "falling back to query",
                    isbn,
                    r.get("title", ""),
                    title,
                )
            else:
                log.info(
                    "Google Books: ISBN lookup empty, "
                    "falling back to query"
                )
        log.info("Google Books: searching %r", query)
        results = await search_google_books(query)
        if not results:
            errors["google"] = "No results found"
            return None
        return _normalize_result(
            _pick_best_result(results, title)
        )
    except Exception as exc:
        msg = str(exc)
        log.exception("Google metadata fetch failed")
        errors["google"] = msg
        return None


async def _fetch_hardcover(
    title: str,
    authors: str,
    _isbn: str,
    errors: dict[str, str | None],
) -> dict | None:
    """Fetch metadata from Hardcover."""
    try:
        hc_query = f"{title} {authors}"
        log.info("Hardcover: searching %r", hc_query)
        results = await hardcover.search_books(hc_query)
        best = hardcover.pick_best_book(
            title, authors, results
        )
        if not best:
            errors["hardcover"] = (
                "No matching book found"
            )
            return None
        detail = await hardcover.fetch_book_detail(
            best["id"]
        )
        if not detail:
            errors["hardcover"] = (
                "Book detail fetch returned empty"
            )
            return None
        log.info(
            "Hardcover: got detail for %r",
            detail.get("title"),
        )
        return _normalize_result(detail)
    except Exception as exc:
        msg = str(exc)
        log.exception("Hardcover metadata fetch failed")
        errors["hardcover"] = msg
        return None


async def _fetch_openlibrary(
    title: str,
    authors: str,
    isbn: str,
    errors: dict[str, str | None],
) -> dict | None:
    """Fetch metadata from Open Library."""
    try:
        query = f"{title} {authors}"
        ol_query = query
        if isbn:
            ol_query = f"isbn:{isbn}"
            log.info(
                "Open Library: searching by ISBN %r",
                isbn,
            )
        else:
            log.info(
                "Open Library: searching %r", ol_query
            )
        results = await search_open_library(
            ol_query, max_results=3
        )
        if (
            results
            and isbn
            and ol_query.startswith("isbn:")
        ):
            r = results[0]
            if not _title_matches(
                r.get("title", ""), title
            ):
                log.warning(
                    "Open Library: ISBN %r returned "
                    "%r but expected %r, "
                    "falling back to query",
                    isbn,
                    r.get("title", ""),
                    title,
                )
                results = await search_open_library(
                    query, max_results=3
                )
        if not results:
            errors["openlibrary"] = "No results found"
            return None
        r = _pick_best_result(results, title)
        cover_url = None
        if r.get("cover_id"):
            cover_url = (
                "https://covers.openlibrary.org/b/id/"
                f"{r['cover_id']}-L.jpg"
            )
        return _normalize_result({
            "title": r.get("title") or "",
            "authors": r.get("authors") or "",
            "description": "",
            "isbn": r.get("isbn") or "",
            "published_date": str(
                r.get("first_publish_year") or ""
            ),
            "cover_url": cover_url,
        })
    except Exception as exc:
        msg = str(exc)
        log.exception(
            "Open Library metadata fetch failed"
        )
        errors["openlibrary"] = msg
        return None


async def _fetch_all_sources(
    title: str, authors: str, isbn: str
) -> tuple[
    dict | None, dict | None, dict | None,
    dict[str, str | None],
]:
    """Fetch metadata from all external sources."""
    errors: dict[str, str | None] = {}
    google, hc, ol = await asyncio.gather(
        _fetch_google(title, authors, isbn, errors),
        _fetch_hardcover(title, authors, isbn, errors),
        _fetch_openlibrary(
            title, authors, isbn, errors
        ),
    )
    return google, hc, ol, errors


def _cleanup_old_temp_files() -> None:
    """Delete temp_* files in DATA_DIR older than 1 hour."""
    cutoff = time.time() - 3600
    for entry in DATA_DIR.iterdir():
        if (
            entry.name.startswith("temp_")
            and entry.is_file()
            and entry.stat().st_mtime < cutoff
        ):
            try:
                entry.unlink()
            except OSError:
                pass


class BookFromPreview(BaseModel):
    temp_id: str
    title: str
    authors: str
    series: str | None = None
    series_index: float | None = None
    description: str | None = None
    isbn: str | None = None
    published_date: str | None = None
    cover_url: str | None = None
    merge_with: int | None = None
    force: bool = False
    manual: bool = False
    format: str | None = None
    is_owned: int | None = None
    reading_status: str | None = None
    rating: float | None = None
    date_finished: str | None = None


class ManualSearchRequest(BaseModel):
    title: str
    authors: str


@router.post("/metadata/preview")
async def preview_metadata(
    file: UploadFile,
    payload: require_owner,
) -> dict:
    """Upload EPUB and get metadata from all sources."""
    _cleanup_old_temp_files()

    temp_id = str(uuid.uuid4())
    temp_path = DATA_DIR / f"temp_{temp_id}.epub"

    content = await file.read()
    with open(temp_path, "wb") as f:
        f.write(content)

    meta = extract_epub_metadata(str(temp_path))

    # Save EPUB cover to temp file if present
    if meta.get("cover_data"):
        cover_path = DATA_DIR / f"temp_{temp_id}_cover.jpg"
        cover_path.write_bytes(meta["cover_data"])

    title = meta.get("title") or file.filename or "Unknown"
    authors = meta.get("authors") or "Unknown"
    isbn = meta.get("isbn") or ""

    # Build EPUB source
    username = payload["username"]
    epub_cover_url = None
    if meta.get("cover_data"):
        epub_cover_url = (
            f"/api/{username}/metadata"
            f"/preview-cover/{temp_id}"
        )

    epub_source = _normalize_result({
        "title": meta.get("title") or "",
        "authors": meta.get("authors") or "",
        "description": meta.get("description") or "",
        "isbn": isbn,
        "published_date": "",
        "cover_url": epub_cover_url,
        "series": meta.get("series") or "",
        "series_index": meta.get("series_index"),
    })

    # Fetch all external sources in parallel
    google, hc, ol, errors = await _fetch_all_sources(
        title, authors, isbn
    )

    return {
        "temp_id": temp_id,
        "epub": epub_source,
        "google": google,
        "hardcover": hc,
        "openlibrary": ol,
        "errors": {
            "google": errors.get("google"),
            "hardcover": errors.get("hardcover"),
            "openlibrary": errors.get("openlibrary"),
        },
    }


@router.post("/metadata/search-all")
async def search_all_metadata(
    req: ManualSearchRequest,
    _payload: require_owner,
) -> dict:
    """Search all external sources by title and author."""
    temp_id = str(uuid.uuid4())

    google, hc, ol, errors = await _fetch_all_sources(
        req.title, req.authors, isbn=""
    )

    return {
        "temp_id": temp_id,
        "epub": None,
        "google": google,
        "hardcover": hc,
        "openlibrary": ol,
        "errors": {
            "google": errors.get("google"),
            "hardcover": errors.get("hardcover"),
            "openlibrary": errors.get("openlibrary"),
        },
    }


@router.get("/metadata/preview-cover/{temp_id}")
async def preview_cover(
    temp_id: str,
    _owner: library_owner,
    _viewer: optional_user,
) -> FileResponse:
    """Serve a temp EPUB cover image."""
    # Validate UUID format to prevent path traversal
    try:
        uuid.UUID(temp_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid temp_id"
        )

    cover_path = DATA_DIR / f"temp_{temp_id}_cover.jpg"
    if not cover_path.exists():
        raise HTTPException(
            status_code=404, detail="Cover not found"
        )
    return FileResponse(
        str(cover_path), media_type="image/jpeg"
    )


@router.post("/books/from-preview", response_model=None)
async def add_book_from_preview(
    req: BookFromPreview,
    payload: require_owner,
) -> dict | JSONResponse:
    """Create a book from a preview temp file."""
    # Validate temp_id
    try:
        uuid.UUID(req.temp_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid temp_id"
        )

    user_id = payload["user_id"]
    temp_path = DATA_DIR / f"temp_{req.temp_id}.epub"
    temp_cover = DATA_DIR / f"temp_{req.temp_id}_cover.jpg"
    has_epub = temp_path.exists()

    if not has_epub and not req.manual:
        raise HTTPException(
            status_code=404,
            detail="Preview expired or not found",
        )

    try:
        # Re-extract cover_data from temp epub for saving
        meta = (
            extract_epub_metadata(str(temp_path))
            if has_epub
            else {}
        )

        book_title = req.title
        book_authors = req.authors
        sort_title = db.make_sort_title(book_title)
        now = datetime.now(timezone.utc).isoformat()

        # Hardcover series enrichment for manual-mode adds (typically /scan).
        # Google Books / Open Library — the initial metadata sources — have
        # weak series coverage, especially for SFF and theology. When the
        # caller didn't supply a series, ask Hardcover before we INSERT so
        # the book lands in its cluster on first render. Non-fatal: any
        # Hardcover error just leaves req.series untouched and the weekly
        # sync-series cron will pick it up later.
        if req.manual and not req.series:
            try:
                first_author = (
                    book_authors.split(",", 1)[0]
                    if book_authors else ""
                ).strip()
                hc_q = f"{book_title} {first_author}".strip()
                hc_hits = await hardcover.search_books(hc_q, per_page=5)
                nt = (book_title or "").lower().strip()
                na = first_author.lower().strip()
                for r in hc_hits:
                    rt = (r.get("title") or "").lower().strip()
                    ra = (r.get("author") or "").lower().strip()
                    if rt == nt and (not na or na in ra or ra in na):
                        det = await hardcover.fetch_book_detail(r["id"])
                        if det and det.get("series_name"):
                            req.series = det["series_name"].strip()
                            if req.series_index is None:
                                req.series_index = det.get("series_index")
                            log.info(
                                "Hardcover enriched %r → %r #%s",
                                book_title, req.series,
                                req.series_index,
                            )
                        break
            except Exception as e:
                log.warning(
                    "Hardcover series enrichment failed for %r: %s",
                    book_title, e,
                )

        series_link_id = None
        if req.series:
            series_link_id = (
                db.get_or_create_series_link(
                    user_id, req.series
                )
            )

        # Manual (no-EPUB) adds default to OWNED. format/status come from the
        # request (manual UI + bulk importers); EPUB uploads stay 'ebook'.
        owned = (req.is_owned if req.is_owned is not None
                 else (1 if (has_epub or req.manual) else 0))
        book_format = (req.format or
                       ("ebook" if has_epub
                        else ("physical" if req.manual else "ebook")))
        reading_status = req.reading_status or "unread"

        if req.merge_with is not None:
            existing = db.get_book(
                req.merge_with, user_id
            )
            if not existing:
                raise HTTPException(
                    status_code=404,
                    detail="Merge target not found",
                )
            book_id = req.merge_with
            merge_data = {
                "title": book_title,
                "sort_title": sort_title,
                "authors": book_authors,
                "author_sort": db.make_author_sort(
                    book_authors
                ),
                "series": req.series,
                "series_index": req.series_index,
                "series_link_id": series_link_id,
                "is_owned": owned,
                "book_format": book_format,
                "reading_status": reading_status,
            }
            if req.rating is not None:
                merge_data["rating"] = req.rating
            if req.date_finished:
                merge_data["date_finished"] = req.date_finished
            if req.description:
                merge_data["description"] = req.description
            if req.isbn:
                merge_data["isbn"] = req.isbn
            if req.published_date:
                merge_data["published_date"] = (
                    req.published_date
                )
            if meta.get("tags"):
                merge_data["tags"] = meta["tags"]
            db.update_book(book_id, user_id, merge_data)
            log.info(
                "Merged preview into book %d for user %d",
                book_id, user_id,
            )
        else:
            existing_id = db.find_unowned_match(
                user_id,
                book_title,
                series_link_id=series_link_id,
                series_index=req.series_index,
            )

            if existing_id:
                book_id = existing_id
                upgrade = {
                    "title": book_title,
                    "sort_title": sort_title,
                    "authors": book_authors,
                    "author_sort": db.make_author_sort(
                        book_authors
                    ),
                    "series": req.series,
                    "series_index": req.series_index,
                    "series_link_id": series_link_id,
                    "is_owned": owned,
                    "book_format": book_format,
                    "reading_status": reading_status,
                }
                if req.rating is not None:
                    upgrade["rating"] = req.rating
                if req.date_finished:
                    upgrade["date_finished"] = req.date_finished
                if req.description:
                    upgrade["description"] = (
                        req.description
                    )
                if req.isbn:
                    upgrade["isbn"] = req.isbn
                if req.published_date:
                    upgrade["published_date"] = (
                        req.published_date
                    )
                if meta.get("tags"):
                    upgrade["tags"] = meta["tags"]
                db.update_book(
                    book_id, user_id, upgrade
                )
                log.info(
                    "Upgraded unowned book %d to owned "
                    "for user %d",
                    book_id,
                    user_id,
                )
            else:
                if not req.force:
                    match = db.find_owned_match(
                        user_id,
                        book_title,
                        book_authors,
                        series_link_id=series_link_id,
                        series_index=req.series_index,
                    )
                    if match:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "conflict": True,
                                "existing_book": {
                                    "id": match["id"],
                                    "title": match[
                                        "title"
                                    ],
                                    "authors": match[
                                        "authors"
                                    ],
                                    "series": match.get(
                                        "series"
                                    ),
                                    "series_index": match.get(
                                        "series_index"
                                    ),
                                    "rating": match.get(
                                        "rating"
                                    ),
                                    "reading_status": match.get(
                                        "reading_status"
                                    ),
                                    "cover_filename": match.get(
                                        "cover_filename"
                                    ),
                                    "user_id": match[
                                        "user_id"
                                    ],
                                },
                            },
                        )

                book_id = db.insert_book(
                    user_id=user_id,
                    title=book_title,
                    sort_title=sort_title,
                    authors=book_authors,
                    author_sort=db.make_author_sort(
                        book_authors
                    ),
                    series=req.series,
                    series_index=req.series_index,
                    description=req.description,
                    cover_filename=None,
                    file_path=None,
                    isbn=req.isbn,
                    goodreads_id=None,
                    tags=meta.get("tags"),
                    date_added=now,
                    date_finished=req.date_finished,
                    rating=req.rating,
                    reading_status=reading_status,
                    series_link_id=series_link_id,
                    is_owned=owned,
                    book_format=book_format,
                )

        # Move epub to final location
        if has_epub:
            user_files = (
                DATA_DIR / "files" / str(user_id)
            )
            user_files.mkdir(parents=True, exist_ok=True)
            final_path = user_files / f"{book_id}.epub"
            shutil.move(str(temp_path), str(final_path))

            db.update_book(
                book_id,
                user_id,
                {"file_path": f"{book_id}.epub"},
            )

            # Save EPUB cover
            if meta.get("cover_data"):
                user_covers = (
                    DATA_DIR / "covers" / str(user_id)
                )
                user_covers.mkdir(
                    parents=True, exist_ok=True
                )
                cover_path = (
                    user_covers / f"{book_id}.jpg"
                )
                cover_path.write_bytes(meta["cover_data"])
                db.update_book(
                    book_id,
                    user_id,
                    {
                        "cover_filename": f"{book_id}.jpg",
                        "cover_updated_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    },
                )

        # If external cover URL selected, download it
        if req.cover_url and not req.cover_url.startswith(
            "/api/"
        ):
            try:
                async with httpx.AsyncClient(
                    timeout=15, follow_redirects=True
                ) as client:
                    resp = await client.get(req.cover_url)
                    resp.raise_for_status()
                    ct = resp.headers.get(
                        "content-type", ""
                    )
                    if ct.startswith("image/"):
                        user_covers = (
                            DATA_DIR
                            / "covers"
                            / str(user_id)
                        )
                        user_covers.mkdir(
                            parents=True, exist_ok=True
                        )
                        cover_p = (
                            user_covers / f"{book_id}.jpg"
                        )
                        cover_p.write_bytes(resp.content)
                        db.update_book(
                            book_id,
                            user_id,
                            {
                                "cover_filename": f"{book_id}.jpg",
                                "cover_updated_at": datetime.now(
                                    timezone.utc
                                ).isoformat(),
                            },
                        )
            except Exception:
                log.exception(
                    "Failed to download cover from %s",
                    req.cover_url,
                )

        # Sync EPUB metadata to match DB
        if has_epub:
            db.sync_book_epub(book_id, user_id)

        book = db.get_book(book_id, user_id)

        if req.series and series_link_id:
            asyncio.create_task(
                _link_series_background(
                    user_id, series_link_id
                )
            )

        return book
    finally:
        if temp_path.exists():
            temp_path.unlink()
        if temp_cover.exists():
            temp_cover.unlink()


@router.post("/books/{book_id}/refresh-metadata")
async def refresh_metadata(
    book_id: int,
    payload: require_owner,
) -> dict:
    user_id = payload["user_id"]
    book = db.get_book(book_id, user_id)
    if book is None:
        raise HTTPException(
            status_code=404, detail="Book not found"
        )

    # Build current metadata
    cover_url = None
    if book.get("cover_filename"):
        cover_url = (
            f"/covers/{user_id}/{book['cover_filename']}"
        )
    current = {
        "title": book.get("title") or "",
        "authors": book.get("authors") or "",
        "description": book.get("description") or "",
        "isbn": book.get("isbn") or "",
        "published_date": book.get("published_date") or "",
        "cover_url": cover_url,
        "series": book.get("series") or "",
        "series_index": (
            str(book["series_index"])
            if book.get("series_index") is not None
            else ""
        ),
    }

    # EPUB source: extract metadata from owned file
    epub_source = None
    if book.get("file_path"):
        epub_path = (
            DATA_DIR
            / "files"
            / str(user_id)
            / book["file_path"]
        )
        if epub_path.exists():
            try:
                meta = extract_epub_metadata(
                    str(epub_path)
                )
                epub_source = _normalize_result({
                    "title": meta.get("title") or "",
                    "authors": meta.get("authors") or "",
                    "description": (
                        meta.get("description") or ""
                    ),
                    "isbn": meta.get("isbn") or "",
                    "published_date": "",
                    "cover_url": None,
                    "series": meta.get("series") or "",
                    "series_index": meta.get(
                        "series_index"
                    ),
                })
            except Exception:
                log.exception(
                    "Failed to extract EPUB metadata "
                    "for book %d",
                    book_id,
                )

    title = book.get("title") or ""
    authors = book.get("authors") or ""
    isbn = book.get("isbn") or ""

    google, hc, ol, errors = await _fetch_all_sources(
        title, authors, isbn
    )

    return {
        "current": current,
        "epub": epub_source,
        "google": google,
        "hardcover": hc,
        "openlibrary": ol,
        "errors": {
            "google": errors.get("google"),
            "hardcover": errors.get("hardcover"),
            "openlibrary": errors.get("openlibrary"),
        },
    }


class CoverFromUrlRequest(BaseModel):
    url: str


@router.post("/books/{book_id}/cover-from-url")
async def cover_from_url(
    book_id: int,
    req: CoverFromUrlRequest,
    payload: require_owner,
) -> dict:
    user_id = payload["user_id"]
    book = db.get_book(book_id, user_id)
    if book is None:
        raise HTTPException(
            status_code=404, detail="Book not found"
        )

    # Download image
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=True
    ) as client:
        resp = await client.get(req.url)
        resp.raise_for_status()
        content_type = resp.headers.get(
            "content-type", ""
        )
        if not content_type.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail="URL is not an image",
            )
        if len(resp.content) > 5 * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail="Image too large (>5MB)",
            )

    # Save cover
    user_covers = DATA_DIR / "covers" / str(user_id)
    user_covers.mkdir(parents=True, exist_ok=True)
    cover_path = user_covers / f"{book_id}.jpg"
    cover_path.write_bytes(resp.content)

    now = datetime.now(timezone.utc).isoformat()
    cover_filename = f"{book_id}.jpg"
    db.update_book(
        book_id, user_id,
        {
            "cover_filename": cover_filename,
            "cover_updated_at": now,
        },
    )
    return {"cover_filename": cover_filename}


_ALLOWED_COVER_MIME = {"image/jpeg", "image/png", "image/webp"}


@router.post("/books/{book_id}/cover")
async def upload_cover(
    book_id: int,
    file: UploadFile,
    payload: require_owner,
) -> dict:
    """Owner-only manual cover upload.

    Accepts JPEG, PNG, or WebP up to 5 MB. PNG/WebP are converted to
    JPEG to match the on-disk filename convention (`<book_id>.jpg`).
    """
    user_id = payload["user_id"]
    book = db.get_book(book_id, user_id)
    if book is None:
        raise HTTPException(
            status_code=404, detail="Book not found"
        )

    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_COVER_MIME:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported image type "
                "(use JPEG, PNG, or WebP)"
            ),
        )

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="Image too large (>5MB)",
        )
    if not content:
        raise HTTPException(
            status_code=400, detail="Empty file",
        )

    # Normalize to JPEG via Pillow so the on-disk filename
    # convention (`<book_id>.jpg`) holds regardless of upload format.
    from io import BytesIO

    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        # If Pillow is unavailable for any reason, fall back to
        # writing JPEGs verbatim (PNG/WebP would already be rejected
        # above if normalization is required).
        if content_type != "image/jpeg":
            raise HTTPException(
                status_code=500,
                detail=(
                    "Image normalization unavailable "
                    "on this server"
                ),
            )
        jpeg_bytes = content
    else:
        try:
            img = Image.open(BytesIO(content))
            img.load()
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid image: {exc}",
            ) from exc
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        jpeg_bytes = buf.getvalue()

    user_covers = DATA_DIR / "covers" / str(user_id)
    user_covers.mkdir(parents=True, exist_ok=True)
    cover_path = user_covers / f"{book_id}.jpg"
    cover_path.write_bytes(jpeg_bytes)

    now = datetime.now(timezone.utc).isoformat()
    cover_filename = f"{book_id}.jpg"
    db.update_book(
        book_id, user_id,
        {
            "cover_filename": cover_filename,
            "cover_updated_at": now,
        },
    )
    return {
        "cover_filename": cover_filename,
        "cover_updated_at": now,
    }


async def _link_series_background(
    user_id: int, series_link_id: int
) -> None:
    """Link a series to Hardcover data in the background.

    If the series is not yet linked, searches Hardcover, fetches
    the book list, matches against library books, and writes to DB.
    If already linked, re-runs matching to pick up the new book.
    """
    try:
        link = db.get_series_link_by_id(series_link_id)
        if not link:
            return
        series_name = link["series_name"]

        if link.get("hardcover_series_id"):
            hc_series_id = link["hardcover_series_id"]
            hc_series_name = link["hardcover_series_name"]
            hc_slug = link.get("hardcover_slug")
        else:
            results = await hardcover.search_series(
                series_name
            )
            best = hardcover.pick_best_series(
                series_name, results
            )
            if not best:
                return
            hc_series_id = best["id"]
            hc_series_name = best["name"]
            hc_slug = best.get("slug")

        raw_books = await hardcover.fetch_series_books(
            hc_series_id
        )
        if not raw_books:
            return

        data_hash = hardcover.compute_data_hash(raw_books)
        db.link_series(
            series_link_id,
            hc_series_id, hc_series_name,
            data_hash=data_hash,
            hardcover_slug=hc_slug,
        )
        db.store_hc_series_books(series_link_id, raw_books)

        deduped = hardcover.dedup_series_books(raw_books)
        library_books = db.get_series_books(
            user_id, series_link_id
        )
        entries = hardcover.match_books(
            deduped, library_books
        )

        # Sync matched books' positions
        db.sync_book_positions(user_id, entries)

        # Global entry upsert (preserves IDs)
        db.upsert_series_entries(series_link_id, entries)

        # Per-user: create placeholder books
        db.ensure_user_books_for_series(
            user_id, series_link_id, series_name
        )

        log.info(
            "Auto-linked series %s (id=%d) for user %d",
            series_name, series_link_id, user_id,
        )
    except Exception:
        log.exception(
            "Failed to auto-link series id=%d for user %d",
            series_link_id, user_id,
        )
