#!/usr/bin/env python3
"""Realign owned-book covers to the Hardcover canonical image.

For every book with `series_link_id IS NOT NULL`, find the matching
`series_entries` row and (when it has a `cover_url`) re-download that
JPEG, re-encode at quality 88, and overwrite the local cover. The
cover_url comes from Hardcover's series API and is the canonical
edition image for that slot in the series.

The matching strategy is:
  1. Match `series_entries.position == books.series_index` (the common
     case — Hardcover positions are integer-major-version aligned).
  2. Fall back to normalized title + first-author match within the
     same series_link_id (covers split numbering, novellas, etc.).

Validation: each downloaded byte stream must (a) open in Pillow as a
real image, (b) be > 5 KB after re-encoding. Anything that fails falls
through gracefully and is reported as a failure at the end.

Skip-detection: if the local cover already matches the Hardcover one
byte-for-byte (SHA-256 of the freshly downloaded bytes vs. the on-disk
file), we skip the re-encode + DB update — saves churning
`cover_updated_at` for books we've already realigned.

Run inside the books-api container:

    uv run python /app/scripts/realign_covers_to_hardcover.py \
        --user gordon

Add `--dry-run` to preview; otherwise it writes real files + bumps
`cover_updated_at`.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import time
from datetime import datetime, timezone

import httpx
from PIL import Image

from books.helpers import db, hardcover


USER_AGENT = "library-bot/1.0 gordon@ggouger.com"
JPEG_QUALITY = 88
MIN_BYTES = 5 * 1024


def _first_author_norm(authors: str) -> str:
    """Normalized first author for fuzzy matching."""
    if not authors:
        return ""
    head = (
        authors.replace(" & ", ",")
        .replace(" and ", ",")
        .split(",")[0]
        .strip()
        .lower()
    )
    return head


def _candidate_books(conn, user_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, user_id, title, authors,
                  series_link_id, series_index,
                  cover_filename
           FROM books
           WHERE user_id = ?
               AND series_link_id IS NOT NULL""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _entries_for_link(
    conn, series_link_id: int,
) -> list[dict]:
    rows = conn.execute(
        """SELECT id, position, title, author, cover_url
           FROM series_entries
           WHERE series_link_id = ?
               AND cover_url IS NOT NULL""",
        (series_link_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _match_entry(
    book: dict, entries: list[dict],
) -> dict | None:
    """Best matching series_entries row, or None."""
    if not entries:
        return None
    # 1) Position match (preferred — covers most cases).
    if book.get("series_index") is not None:
        for e in entries:
            if (
                e.get("position") is not None
                and abs(
                    float(e["position"]) - float(book["series_index"])
                ) < 0.0001
            ):
                return e
    # 2) Normalized title+author fallback.
    bt = hardcover.normalize_title(book.get("title") or "")
    ba = _first_author_norm(book.get("authors") or "")
    if not bt:
        return None
    best = None
    best_score = 0.0
    for e in entries:
        et = hardcover.normalize_title(e.get("title") or "")
        ea = _first_author_norm(e.get("author") or "")
        if not et:
            continue
        score = hardcover._fuzzy_ratio(bt, et)
        if ea and ba and ea != ba:
            score *= 0.75  # author mismatch penalty
        if score > best_score:
            best_score = score
            best = e
    # Require fairly strong match for the fallback path.
    if best is not None and best_score >= 0.85:
        return best
    return None


def _validate_jpeg(buf: bytes) -> Image.Image | None:
    """Open the bytes as a PIL image; return it or None on failure."""
    try:
        img = Image.open(io.BytesIO(buf))
        # Force load + validate by accessing pixel data.
        img.load()
        return img
    except Exception:
        return None


def _reencode_jpeg(img: Image.Image) -> bytes:
    """Re-encode to JPEG at the configured quality."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(
        out, format="JPEG", quality=JPEG_QUALITY,
        optimize=True, progressive=True,
    )
    return out.getvalue()


def _all_user_ids(conn) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM users WHERE username <> 'archive'"
        " ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows]


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Realign owned-book covers to Hardcover canonical edition."
        ),
    )
    p.add_argument(
        "--user", default="gordon",
        help="Username, or 'all' to walk every non-archive user.",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Cap on books processed (0 = no cap).",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--sleep", type=float, default=0.2,
        help="Seconds between downloads.",
    )
    args = p.parse_args()

    db.init_db()
    conn = db.get_db()
    if args.user == "all":
        user_ids = _all_user_ids(conn)
    else:
        user = db.get_user_by_username(args.user)
        if not user:
            print(f"ERROR: user {args.user!r} not found")
            sys.exit(1)
        user_ids = [user["id"]]

    total_processed = 0
    total_no_entry = 0
    total_skipped_same = 0
    total_replaced = 0
    total_failed = 0
    examples_replaced: list[tuple[int, str]] = []

    with httpx.Client(
        follow_redirects=True, timeout=20,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for uid in user_ids:
            covers_dir = db.DATA_DIR / "covers" / str(uid)
            if not args.dry_run:
                covers_dir.mkdir(parents=True, exist_ok=True)

            books = _candidate_books(conn, uid)
            # Cache entries per series_link_id to avoid re-querying.
            entries_cache: dict[int, list[dict]] = {}

            for book in books:
                if args.limit and total_processed >= args.limit:
                    break
                total_processed += 1
                slid = book["series_link_id"]
                if slid not in entries_cache:
                    entries_cache[slid] = _entries_for_link(conn, slid)
                entry = _match_entry(book, entries_cache[slid])
                if not entry or not entry.get("cover_url"):
                    total_no_entry += 1
                    continue

                url = entry["cover_url"]
                try:
                    r = client.get(url)
                except Exception as exc:
                    total_failed += 1
                    print(
                        f"  ! {book['id']:>5} "
                        f"{(book['title'] or '')[:50]:50s} "
                        f"download error: {exc}"
                    )
                    continue
                if r.status_code != 200:
                    total_failed += 1
                    print(
                        f"  ! {book['id']:>5} "
                        f"{(book['title'] or '')[:50]:50s} "
                        f"HTTP {r.status_code}"
                    )
                    continue
                raw = r.content
                if len(raw) <= MIN_BYTES:
                    total_failed += 1
                    print(
                        f"  ! {book['id']:>5} "
                        f"{(book['title'] or '')[:50]:50s} "
                        f"too small ({len(raw)}B)"
                    )
                    continue
                img = _validate_jpeg(raw)
                if img is None:
                    total_failed += 1
                    print(
                        f"  ! {book['id']:>5} "
                        f"{(book['title'] or '')[:50]:50s} "
                        f"not a valid image"
                    )
                    continue
                jpeg = _reencode_jpeg(img)
                if len(jpeg) <= MIN_BYTES:
                    total_failed += 1
                    print(
                        f"  ! {book['id']:>5} "
                        f"{(book['title'] or '')[:50]:50s} "
                        f"re-encoded too small ({len(jpeg)}B)"
                    )
                    continue

                cover_path = covers_dir / f"{book['id']}.jpg"
                # Dedup: skip if the re-encoded JPEG is byte-identical
                # to what's already on disk. Hash compare is cheap.
                if cover_path.exists():
                    existing = cover_path.read_bytes()
                    if (
                        hashlib.sha256(existing).digest()
                        == hashlib.sha256(jpeg).digest()
                    ):
                        total_skipped_same += 1
                        time.sleep(args.sleep)
                        continue

                if args.dry_run:
                    print(
                        f"  ? {book['id']:>5} "
                        f"{(book['title'] or '')[:50]:50s} "
                        f"would replace from {url[:60]}"
                    )
                else:
                    cover_path.write_bytes(jpeg)
                    db.update_book(
                        book["id"], uid,
                        {
                            "cover_filename": f"{book['id']}.jpg",
                            "cover_updated_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        },
                    )
                    print(
                        f"  + {book['id']:>5} "
                        f"{(book['title'] or '')[:50]:50s} "
                        f"realigned"
                    )
                total_replaced += 1
                if len(examples_replaced) < 8:
                    examples_replaced.append(
                        (book["id"], book["title"] or "")
                    )
                time.sleep(args.sleep)

            if args.limit and total_processed >= args.limit:
                break

    conn.close()
    print(
        f"\nDone: processed {total_processed}, "
        f"replaced {total_replaced}, "
        f"skipped {total_skipped_same} (already canonical), "
        f"no-entry {total_no_entry}, "
        f"failed {total_failed}."
    )


if __name__ == "__main__":
    main()
