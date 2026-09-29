import logging
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..helpers import db, hardcover
from ..helpers.auth import (
    library_owner,
    optional_user,
    require_owner,
    require_user,
)
from ..helpers.db import DATA_DIR

log = logging.getLogger(__name__)
router = APIRouter(prefix="/series", tags=["series"])


@router.get("")
def list_series(
    owner: library_owner,
    viewer: optional_user,
    include_unmonitored: bool = False,
) -> dict:
    monitored = None if include_unmonitored else True
    series = db.get_series_list(
        owner["id"], monitored=monitored
    )
    standalones = db.get_standalone_books_for_overview(
        owner["id"]
    )
    is_owner = (
        viewer is not None
        and (viewer["user_id"] == owner["id"] or viewer.get("is_superuser"))
    )
    return {
        "series": series,
        "standalones": standalones,
        "is_owner": is_owner,
    }


@router.get("/autocomplete")
def series_autocomplete(
    owner: library_owner,
    _viewer: optional_user,
) -> dict:
    return {
        "series": db.get_series_autocomplete(owner["id"])
    }


@router.get("/{series_link_id}")
def get_series(
    series_link_id: int,
    owner: library_owner,
    viewer: optional_user,
) -> dict:
    link = db.get_series_link_by_id(series_link_id)
    if not link:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )
    us = db.get_user_series(owner["id"], series_link_id)
    if not us:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )
    books = db.get_series_books(owner["id"], series_link_id)
    is_owner = (
        viewer is not None
        and (viewer["user_id"] == owner["id"] or viewer.get("is_superuser"))
    )
    series_name = (
        us.get("display_name") or link["series_name"]
    )
    hardcover_slug = link.get("hardcover_slug")
    # Ghost entries: series_entries rows the user doesn't own.
    # Falls back to empty list when series_entries isn't populated.
    ghosts = db.get_series_ghost_entries(
        owner["id"], series_link_id
    )
    return {
        "series_link_id": series_link_id,
        "series": series_name,
        "monitored": bool(us.get("monitored", 1)),
        "series_complete": bool(us.get("series_complete", 1)),
        "user_rating": us.get("rating"),
        "user_review": us.get("review"),
        "is_favorite": int(us.get("is_favorite") or 0) == 1,
        "is_all_time_fav": int(us.get("is_all_time_fav") or 0) == 1,
        "is_second_fav": int(us.get("is_second_fav") or 0) == 1,
        "is_third_fav": int(us.get("is_third_fav") or 0) == 1,
        "hardcover_url": (
            f"https://hardcover.app/series/{hardcover_slug}"
            if hardcover_slug else None
        ),
        "books": books,
        "ghost_entries": ghosts,
        "is_owner": is_owner,
    }


@router.get("/{series_link_id}/edit")
def get_series_edit(
    series_link_id: int,
    owner: library_owner,
    viewer: optional_user,
) -> dict:
    link = db.get_series_link_by_id(series_link_id)
    if not link:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )
    us = db.get_user_series(owner["id"], series_link_id)
    if not us:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )
    is_owner = (
        viewer is not None
        and (viewer["user_id"] == owner["id"] or viewer.get("is_superuser"))
    )
    series_name = (
        us.get("display_name") or link["series_name"]
    )
    entries = db.get_series_entries_with_books(
        series_link_id, owner["id"]
    )
    return {
        "series_link_id": series_link_id,
        "series_name": series_name,
        "hardcover_series_id": link.get(
            "hardcover_series_id"
        ),
        "hardcover_series_name": link.get(
            "hardcover_series_name"
        ),
        "entries": entries,
        "is_owner": is_owner,
    }


class EntryUpdate(BaseModel):
    entry_id: int
    position: float
    status: str


class BookIgnore(BaseModel):
    book_id: int
    ignored: bool


class SeriesUpdate(BaseModel):
    series_name: str | None = None
    monitored: bool | None = None
    series_complete: bool | None = None
    rating: float | None = None
    review: str | None = None
    is_favorite: bool | None = None
    is_all_time_fav: bool | None = None
    is_second_fav: bool | None = None
    is_third_fav: bool | None = None
    entries: list[EntryUpdate] | None = None
    book_ignores: list[BookIgnore] | None = None


@router.patch("/{series_link_id}")
def update_series(
    series_link_id: int,
    updates: SeriesUpdate,
    payload: require_owner,
) -> dict:
    user_id = payload["user_id"]
    us = db.get_user_series(user_id, series_link_id)
    if not us:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )

    if updates.series_name is not None:
        db.update_series_display_name(
            user_id, series_link_id, updates.series_name
        )

    if updates.monitored is not None:
        db.update_series_monitored(
            user_id, series_link_id, updates.monitored
        )

    if updates.series_complete is not None:
        db.update_series_complete(
            user_id, series_link_id, updates.series_complete
        )

    # Rating + favorite + tier — same model as book-level toggles, single
    # call covers all four to keep the invariants (gold ⇒ favorite,
    # silver ⇒ favorite, mutual exclusion) consistent.
    rating_or_fav = (
        updates.rating is not None
        or updates.is_favorite is not None
        or updates.is_all_time_fav is not None
        or updates.is_second_fav is not None
        or updates.is_third_fav is not None
    )
    if rating_or_fav:
        fields: dict = {}
        if updates.rating is not None:
            fields["rating"] = updates.rating
        if updates.is_favorite is not None:
            fields["is_favorite"] = updates.is_favorite
        if updates.is_all_time_fav is not None:
            fields["is_all_time_fav"] = updates.is_all_time_fav
        if updates.is_second_fav is not None:
            fields["is_second_fav"] = updates.is_second_fav
        if updates.is_third_fav is not None:
            fields["is_third_fav"] = updates.is_third_fav

        # Tier invariants — mirror the book-side enforcement in
        # routes/books.py: any tier set to true forces is_favorite=true
        # and clears the other two tier bits.
        if fields.get("is_all_time_fav"):
            fields["is_favorite"] = True
            fields["is_second_fav"] = False
            fields["is_third_fav"] = False
        elif fields.get("is_second_fav"):
            fields["is_favorite"] = True
            fields["is_all_time_fav"] = False
            fields["is_third_fav"] = False
        elif fields.get("is_third_fav"):
            fields["is_favorite"] = True
            fields["is_all_time_fav"] = False
            fields["is_second_fav"] = False

        db.update_user_series_fields(user_id, series_link_id, fields)

    # An empty review deliberately clears the text. `model_fields_set`
    # distinguishes that from an omitted review field.
    if "review" in updates.model_fields_set:
        db.update_user_series_fields(
            user_id, series_link_id,
            {"review": (updates.review or "").strip()},
        )

    if updates.entries:
        for entry in updates.entries:
            if entry.status not in ("linked", "ignored"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status: {entry.status}",
                )
            db.update_series_entry(
                user_id, entry.entry_id,
                entry.position, entry.status,
            )

    if updates.book_ignores:
        for bi in updates.book_ignores:
            db.update_book(
                bi.book_id, user_id,
                {"series_ignored": 1 if bi.ignored else 0},
            )

    return {"success": True}


class EntryCreate(BaseModel):
    title: str
    position: float
    author: str | None = None


class EntryFieldsUpdate(BaseModel):
    title: str | None = None
    position: float | None = None
    author: str | None = None


@router.post("/{series_link_id}/entries")
def add_series_entry(
    series_link_id: int,
    payload: EntryCreate,
    auth: require_owner,
) -> dict:
    """Manually add a ghost entry to a series.

    Owner-only — the route is gated on `require_owner` (which already
    enforces the auth'd user matches the {username} path), and we
    additionally verify a user_series subscription exists.
    """
    user_id = auth["user_id"]
    us = db.get_user_series(user_id, series_link_id)
    if not us:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )
    title = payload.title.strip()
    if not title:
        raise HTTPException(
            status_code=400, detail="Title is required"
        )
    author = payload.author.strip() if payload.author else None
    entry_id = db.insert_series_entry(
        series_link_id, title, payload.position, author or None,
    )
    return {
        "entry_id": entry_id,
        "series_link_id": series_link_id,
        "title": title,
        "position": payload.position,
        "author": author,
    }


@router.patch("/entries/{entry_id}")
def patch_series_entry(
    entry_id: int,
    payload: EntryFieldsUpdate,
    auth: require_owner,
) -> dict:
    """Edit a ghost entry's title/position/author.

    Owner-only. Validates the entry belongs to a series the user has a
    subscription to before mutating.
    """
    user_id = auth["user_id"]
    entry = db.get_series_entry_by_id(entry_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail="Entry not found"
        )
    us = db.get_user_series(user_id, entry["series_link_id"])
    if not us:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )
    fields: dict = {}
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(
                status_code=400, detail="Title cannot be empty"
            )
        fields["title"] = title
    if payload.position is not None:
        fields["position"] = payload.position
    if payload.author is not None:
        # Empty string clears the author -> NULL.
        a = payload.author.strip()
        fields["author"] = a or None
    changed = db.update_series_entry_fields(entry_id, **fields)
    return {"success": True, "changed": changed}


@router.delete("/entries/{entry_id}")
def remove_series_entry(
    entry_id: int,
    auth: require_owner,
) -> dict:
    """Delete a ghost entry. user_entry_status follows via CASCADE."""
    user_id = auth["user_id"]
    entry = db.get_series_entry_by_id(entry_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail="Entry not found"
        )
    us = db.get_user_series(user_id, entry["series_link_id"])
    if not us:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )
    db.delete_series_entry(entry_id)
    return {"success": True}


@router.post("/{series_link_id}/refresh")
async def refresh_series(
    series_link_id: int,
    payload: require_owner,
) -> dict:
    user_id = payload["user_id"]
    us = db.get_user_series(user_id, series_link_id)
    if not us:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )

    link = db.get_series_link_by_id(series_link_id)
    if not link:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )

    hc_series_id = link.get("hardcover_series_id")
    if not hc_series_id:
        raise HTTPException(
            status_code=400,
            detail="Series not linked to Hardcover",
        )

    raw_books = await hardcover.fetch_series_books(
        hc_series_id
    )
    if not raw_books:
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch Hardcover data",
        )

    data_hash = hardcover.compute_data_hash(raw_books)
    if data_hash == link.get("data_hash"):
        return {"changed": False}

    series_name = (
        us.get("display_name") or link["series_name"]
    )

    # Store raw HC data
    db.store_hc_series_books(series_link_id, raw_books)

    # Match HC entries against user's library books
    deduped = hardcover.dedup_series_books(raw_books)
    library_books = db.get_series_books(
        user_id, series_link_id
    )
    entries = hardcover.match_books(deduped, library_books)

    # Sync matched books' positions
    db.sync_book_positions(user_id, entries)

    # Global entry upsert (preserves IDs)
    db.upsert_series_entries(series_link_id, entries)

    # Per-user: create placeholder books for linked entries
    db.ensure_user_books_for_series(
        user_id, series_link_id, series_name
    )

    # Fetch slug if missing
    hc_slug = link.get("hardcover_slug")
    if not hc_slug:
        slugs = await hardcover.fetch_series_slugs(
            [hc_series_id]
        )
        hc_slug = slugs.get(hc_series_id)

    db.link_series(
        series_link_id,
        hc_series_id,
        link.get("hardcover_series_name") or "",
        data_hash=data_hash,
        hardcover_slug=hc_slug,
    )

    # Return fresh edit data
    fresh_entries = db.get_series_entries_with_books(
        series_link_id, user_id
    )
    return {
        "changed": True,
        "series_link_id": series_link_id,
        "series_name": series_name,
        "hardcover_series_id": hc_series_id,
        "hardcover_series_name": link.get(
            "hardcover_series_name"
        ),
        "entries": fresh_entries,
        "is_owner": True,
    }


@router.post("/{series_link_id}/copy-to-library")
def copy_series_to_library(
    series_link_id: int,
    owner: library_owner,
    viewer: require_user,
) -> dict:
    """Bulk-copy a series from another user's library."""
    link = db.get_series_link_by_id(series_link_id)
    if not link:
        raise HTTPException(
            status_code=404, detail="Series not found"
        )

    source_id = owner["id"]
    target_id = viewer["user_id"]

    if source_id == target_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot copy from your own library",
        )

    # Get source books for this series
    source_books = db.get_series_books(
        source_id, series_link_id
    )
    if not source_books:
        raise HTTPException(
            status_code=404,
            detail="No books in this series",
        )

    # Ensure target has user_series subscription
    db.ensure_user_series(target_id, series_link_id)

    # Get target's existing books to check for dupes
    target_books = db.get_series_books(
        target_id, series_link_id
    )
    target_positions = {
        b["series_index"]
        for b in target_books
        if b.get("series_index") is not None
    }

    series_name = link["series_name"]
    now = datetime.now(timezone.utc).isoformat()
    copied = 0
    skipped = 0

    for book in source_books:
        # Skip if target already has a book at this pos
        if book.get("series_index") in target_positions:
            skipped += 1
            continue

        # Insert book record for target user
        new_book_id = db.insert_book(
            user_id=target_id,
            title=book["title"],
            sort_title=db.make_sort_title(book["title"]),
            authors=book["authors"],
            author_sort=db.make_author_sort(
                book["authors"]
            ),
            series=book.get("series") or series_name,
            series_index=book.get("series_index"),
            description=book.get("description"),
            cover_filename=None,
            file_path=None,
            isbn=book.get("isbn"),
            goodreads_id=book.get("goodreads_id"),
            tags=book.get("tags"),
            date_added=now,
            date_finished=None,
            rating=None,
            reading_status="unread",
            series_link_id=series_link_id,
            published_date=book.get("published_date"),
            is_owned=1 if book.get("is_owned") else 0,
        )

        # Copy epub if source has one
        if book.get("file_path"):
            src = (
                DATA_DIR / "files" / str(source_id)
                / book["file_path"]
            )
            if src.exists():
                dst_dir = (
                    DATA_DIR / "files" / str(target_id)
                )
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / f"{new_book_id}.epub"
                shutil.copy2(str(src), str(dst))
                db.update_book(
                    new_book_id, target_id,
                    {"file_path": f"{new_book_id}.epub"},
                )

        # Copy cover if source has one
        if book.get("cover_filename"):
            src = (
                DATA_DIR / "covers" / str(source_id)
                / book["cover_filename"]
            )
            if src.exists():
                dst_dir = (
                    DATA_DIR / "covers" / str(target_id)
                )
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / f"{new_book_id}.jpg"
                shutil.copy2(str(src), str(dst))
                cover_ts = datetime.now(
                    timezone.utc
                ).isoformat()
                db.update_book(
                    new_book_id, target_id,
                    {
                        "cover_filename": (
                            f"{new_book_id}.jpg"
                        ),
                        "cover_updated_at": cover_ts,
                    },
                )

        copied += 1

    # Create unowned placeholders for remaining entries
    db.ensure_user_books_for_series(
        target_id, series_link_id, series_name
    )

    return {"copied": copied, "skipped": skipped}
