import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from decouple import config

from .hardcover import normalize_title

log = logging.getLogger(__name__)

DATA_DIR = Path(config("BOOKS_DATA_DIR", default="/app/data"))
DB_PATH = DATA_DIR / "books.db"

EPUB_METADATA_FIELDS = {
    "title", "authors", "series", "series_index",
    "description", "isbn", "tags",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    kindle_email TEXT,
    is_superuser INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    sort_title TEXT NOT NULL,
    authors TEXT NOT NULL,
    author_sort TEXT,
    series TEXT,
    series_index REAL,
    series_link_id INTEGER REFERENCES series_link(id)
        ON DELETE SET NULL,
    description TEXT,
    cover_filename TEXT,
    cover_updated_at TEXT,
    file_path TEXT,
    isbn TEXT,
    goodreads_id TEXT,
    tags TEXT,
    date_added TEXT NOT NULL,
    date_finished TEXT,
    published_date TEXT,
    rating REAL,
    is_read INTEGER DEFAULT 0,
    reading_status TEXT DEFAULT 'unread',
    progress REAL,
    is_owned INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_books_user
    ON books(user_id);
CREATE INDEX IF NOT EXISTS idx_books_series
    ON books(user_id, series);
CREATE INDEX IF NOT EXISTS idx_books_read
    ON books(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_books_status
    ON books(user_id, reading_status);

CREATE TABLE IF NOT EXISTS series_link (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_name TEXT NOT NULL,
    hardcover_series_id INTEGER,
    hardcover_series_name TEXT,
    hardcover_slug TEXT,
    last_checked TEXT,
    data_hash TEXT
);

CREATE TABLE IF NOT EXISTS series_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_link_id INTEGER NOT NULL
        REFERENCES series_link(id) ON DELETE CASCADE,
    position REAL NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    hardcover_book_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_series_entries_link
    ON series_entries(series_link_id);
CREATE INDEX IF NOT EXISTS idx_series_entries_position
    ON series_entries(series_link_id, position);

CREATE TABLE IF NOT EXISTS user_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    series_link_id INTEGER NOT NULL
        REFERENCES series_link(id) ON DELETE CASCADE,
    monitored INTEGER NOT NULL DEFAULT 1,
    display_name TEXT,
    series_complete INTEGER NOT NULL DEFAULT 1,
    review TEXT,
    UNIQUE(user_id, series_link_id)
);

CREATE INDEX IF NOT EXISTS idx_user_series
    ON user_series(user_id, series_link_id);

CREATE TABLE IF NOT EXISTS user_entry_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    series_entry_id INTEGER NOT NULL
        REFERENCES series_entries(id) ON DELETE CASCADE,
    status TEXT NOT NULL
        CHECK(status IN ('linked', 'ignored')),
    UNIQUE(user_id, series_entry_id)
);

CREATE INDEX IF NOT EXISTS idx_user_entry_status
    ON user_entry_status(user_id, series_entry_id);

CREATE TABLE IF NOT EXISTS hc_series_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_link_id INTEGER NOT NULL
        REFERENCES series_link(id) ON DELETE CASCADE,
    position REAL,
    title TEXT NOT NULL,
    author TEXT,
    hardcover_book_id INTEGER,
    featured INTEGER DEFAULT 0,
    compilation INTEGER DEFAULT 0,
    ratings_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_hc_series_books_link
    ON hc_series_books(series_link_id);
"""


def sync_book_epub(book_id: int, user_id: int) -> None:
    """Sync EPUB file metadata to match DB for a book.

    Loads the book from DB, builds a metadata dict, and
    calls sync_epub_metadata(). Silently skips books with
    no file_path or missing files.
    """
    try:
        book = get_book(book_id, user_id)
        if not book or not book.get("file_path"):
            return
        epub_path = (
            DATA_DIR / "files" / str(user_id)
            / book["file_path"]
        )
        if not epub_path.exists():
            return

        tags = book.get("tags", [])
        if isinstance(tags, str):
            tags = json.loads(tags)

        meta = {
            "title": book["title"],
            "authors": book["authors"],
            "series": book.get("series"),
            "series_index": book.get("series_index"),
            "description": book.get("description"),
            "isbn": book.get("isbn"),
            "tags": tags,
        }

        from .metadata import sync_epub_metadata
        sync_epub_metadata(str(epub_path), meta)
    except Exception:
        log.exception(
            "Failed to sync EPUB for book %d user %d",
            book_id, user_id,
        )


def make_sort_title(title: str) -> str:
    """Strip leading articles for alphabetical sorting."""
    lower = title.lower()
    for prefix in ("the ", "a ", "an "):
        if lower.startswith(prefix):
            return title[len(prefix):] + ", " + title[:len(prefix) - 1]
    return title


def make_author_sort(authors: str) -> str:
    """Convert 'First Last' to 'Last, First' for sorting."""
    parts = []
    for author in authors.split(","):
        author = author.strip()
        names = author.split()
        if len(names) > 1:
            parts.append(
                f"{names[-1]}, {' '.join(names[:-1])}"
            )
        else:
            parts.append(author)
    return " & ".join(parts)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "covers").mkdir(exist_ok=True)
    (DATA_DIR / "files").mkdir(exist_ok=True)
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    _migrate_reading_status()
    _migrate_favorites()
    _migrate_series_data_hash()
    _migrate_is_owned()
    _migrate_missing_to_books()
    _migrate_drop_date_started()
    _migrate_series_link_id()
    _migrate_published_date()
    _migrate_cover_updated_at()
    _migrate_series_monitored()
    _migrate_global_series()
    _migrate_user_libraries()
    _migrate_superusers()
    _migrate_archive_user()
    _migrate_series_ignored()
    _migrate_koreader_sync()
    _migrate_epub_hash()
    _migrate_sync_version()
    _migrate_series_complete()
    _migrate_book_format()
    _migrate_also_physical()
    _migrate_book_tiers()
    _migrate_user_series_rating()
    _migrate_user_series_review()
    _migrate_third_fav()
    _migrate_price()
    _migrate_length_fields()
    _migrate_recommendation_dismissals()
    _migrate_review()
    _migrate_manual_category()
    _migrate_series_entries_cover_url()
    _migrate_asin()
    cleanup_stale_sync_data()
    log.info("Database initialized at %s", DB_PATH)


def cleanup_stale_sync_data() -> int:
    """Age out abandoned KOReader progress without touching completed books.

    A device may disappear or be reset while a book is marked as in progress.
    After ninety days that progress can no longer be treated as an active sync
    cursor.  Completed reading state, ratings, and all book metadata remain
    untouched.  A subsequent KOReader sync simply establishes a fresh cursor.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - (90 * 24 * 60 * 60)
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    conn = get_db()
    cursor = conn.execute(
        """UPDATE books
        SET progress = NULL, sync_updated_at = NULL
        WHERE reading_status = 'reading'
          AND sync_updated_at IS NOT NULL
          AND datetime(sync_updated_at) < datetime(?)""",
        (cutoff_iso,),
    )
    conn.commit()
    cleared = cursor.rowcount
    conn.close()
    if cleared:
        log.info("Cleared stale KOReader progress for %d book(s)", cleared)
    return cleared


def _migrate_reading_status() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "reading_status" in columns:
        conn.close()
        return

    log.info("Migrating is_read -> reading_status")
    conn.execute(
        "ALTER TABLE books ADD COLUMN reading_status"
        " TEXT DEFAULT 'unread'"
    )
    conn.execute(
        "ALTER TABLE books ADD COLUMN progress REAL"
    )
    conn.execute(
        "UPDATE books SET reading_status = 'read'"
        " WHERE is_read = 1"
    )
    conn.execute(
        "UPDATE books SET reading_status = 'unread'"
        " WHERE is_read = 0 OR is_read IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_books_status"
        " ON books(user_id, reading_status)"
    )
    conn.commit()
    conn.close()


def _migrate_favorites() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "is_favorite" in columns:
        conn.close()
        return

    log.info("Migrating rating to integer + favorites")
    conn.execute(
        "ALTER TABLE books ADD COLUMN"
        " is_favorite INTEGER DEFAULT 0"
    )
    conn.execute(
        "UPDATE books SET is_favorite = 1"
        " WHERE rating = 5.0"
    )
    conn.execute(
        "UPDATE books SET rating ="
        " CAST(rating AS INTEGER)"
        " WHERE rating IS NOT NULL"
    )
    conn.execute(
        "UPDATE books SET rating = 5, is_favorite = 0"
        " WHERE series = 'The Expanse'"
    )
    conn.execute(
        "UPDATE books SET rating = 5, is_favorite = 0"
        " WHERE series = 'The Foreworld Saga'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_books_favorite"
        " ON books(user_id, is_favorite)"
    )
    conn.commit()
    conn.close()


def _migrate_series_data_hash() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(series_link)"
        ).fetchall()
    ]
    if "data_hash" in columns:
        conn.close()
        return

    log.info("Adding data_hash to series_link")
    conn.execute(
        "ALTER TABLE series_link ADD COLUMN data_hash TEXT"
    )
    conn.commit()
    conn.close()


def _migrate_is_owned() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "is_owned" not in columns:
        log.info("Adding is_owned column to books")
        conn.execute(
            "ALTER TABLE books ADD COLUMN"
            " is_owned INTEGER DEFAULT 1"
        )
        conn.execute(
            "UPDATE books SET is_owned = 1"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_books_owned"
        " ON books(user_id, is_owned)"
    )
    conn.commit()
    conn.close()


def _migrate_missing_to_books() -> None:
    """Convert missing series entries to unowned book records.

    Finds all series_entries with status='missing' and no book_id,
    creates book records for them, and updates status to 'linked'.
    Also updates any existing 'owned' status to 'linked'.
    """
    conn = get_db()

    # Skip if status column no longer exists (post global-series)
    se_cols = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(series_entries)"
        ).fetchall()
    ]
    if "status" not in se_cols:
        conn.close()
        return

    # Check if there are any entries to migrate
    missing = conn.execute(
        """SELECT se.id, se.position, se.title, se.author,
                  sl.user_id, sl.series_name
           FROM series_entries se
           JOIN series_link sl ON se.series_link_id = sl.id
           WHERE se.status = 'missing' AND se.book_id IS NULL"""
    ).fetchall()

    if not missing:
        # Still update owned -> linked for consistency
        updated = conn.execute(
            "UPDATE series_entries SET status = 'linked'"
            " WHERE status = 'owned'"
        ).rowcount
        if updated:
            log.info(
                "Migrated %d series entries from"
                " 'owned' to 'linked'",
                updated,
            )
        conn.commit()
        conn.close()
        return

    log.info(
        "Migrating %d missing series entries to book records",
        len(missing),
    )

    for row in missing:
        entry = {
            "title": row["title"],
            "author": row["author"],
            "position": row["position"],
        }
        book_id = ensure_book_for_entry(
            conn,
            row["user_id"],
            row["series_name"],
            entry,
        )
        conn.execute(
            "UPDATE series_entries"
            " SET book_id = ?, status = 'linked'"
            " WHERE id = ?",
            (book_id, row["id"]),
        )

    # Update all 'owned' to 'linked'
    conn.execute(
        "UPDATE series_entries SET status = 'linked'"
        " WHERE status = 'owned'"
    )

    conn.commit()
    conn.close()
    log.info("Migration complete")


def _migrate_drop_date_started() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "date_started" not in columns:
        conn.close()
        return

    log.info("Dropping unused date_started column")
    conn.execute("ALTER TABLE books DROP COLUMN date_started")
    conn.commit()
    conn.close()


def _migrate_series_link_id() -> None:
    """Add series_link_id FK to books, rebuild series_link table.

    Makes HC fields nullable, drops UNIQUE(user_id, series_name),
    backfills series_link_id from series_entries, and creates
    stub series_link rows for orphan books with a series.
    """
    conn = get_db()

    # Check if books already has series_link_id column
    book_cols = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "series_link_id" in book_cols:
        conn.close()
        return

    log.info("Migrating: adding series_link_id to books")

    # Rebuild series_link: HC fields nullable, no UNIQUE
    # executescript runs in autocommit, so PRAGMA must be inside
    conn.executescript("""
        PRAGMA foreign_keys=OFF;
        CREATE TABLE series_link_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            series_name TEXT NOT NULL,
            hardcover_series_id INTEGER,
            hardcover_series_name TEXT,
            hardcover_slug TEXT,
            last_checked TEXT,
            data_hash TEXT,
            monitored INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO series_link_new
            (id, user_id, series_name, hardcover_series_id,
             hardcover_series_name, hardcover_slug,
             last_checked, data_hash)
            SELECT id, user_id, series_name,
                   hardcover_series_id,
                   hardcover_series_name, hardcover_slug,
                   last_checked, data_hash
            FROM series_link;
        DROP TABLE series_link;
        ALTER TABLE series_link_new
            RENAME TO series_link;
        PRAGMA foreign_keys=ON;
    """)

    # Add column + index to books
    conn.execute(
        "ALTER TABLE books ADD COLUMN series_link_id"
        " INTEGER REFERENCES series_link(id)"
        " ON DELETE SET NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_books_series_link"
        " ON books(series_link_id)"
    )

    # Backfill from series_entries (unambiguous HC-matched)
    conn.execute("""
        UPDATE books SET series_link_id = (
            SELECT se.series_link_id
            FROM series_entries se
            WHERE se.book_id = books.id
            LIMIT 1
        ) WHERE EXISTS (
            SELECT 1 FROM series_entries se
            WHERE se.book_id = books.id
        )
    """)

    # Create stub series_link rows for orphan books
    orphan_groups = conn.execute("""
        SELECT DISTINCT user_id, series
        FROM books
        WHERE series IS NOT NULL
            AND series_link_id IS NULL
    """).fetchall()

    for row in orphan_groups:
        uid, sname = row[0], row[1]
        cursor = conn.execute(
            """INSERT INTO series_link
               (user_id, series_name)
               VALUES (?, ?)""",
            (uid, sname),
        )
        new_link_id = cursor.lastrowid
        conn.execute(
            """UPDATE books
               SET series_link_id = ?
               WHERE user_id = ?
                   AND series = ?
                   AND series_link_id IS NULL""",
            (new_link_id, uid, sname),
        )

    conn.commit()
    conn.close()
    log.info("Migration complete: series_link_id added")


def _migrate_cover_updated_at() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "cover_updated_at" in columns:
        conn.close()
        return

    log.info("Adding cover_updated_at column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN cover_updated_at TEXT"
    )
    conn.commit()
    conn.close()


def _migrate_published_date() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "published_date" in columns:
        conn.close()
        return

    log.info("Adding published_date column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN published_date TEXT"
    )
    conn.commit()
    conn.close()


def _migrate_book_format() -> None:
    """Add a book_format column (ebook | audiobook | physical)."""
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "book_format" in columns:
        conn.close()
        return
    log.info("Adding book_format column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN book_format TEXT DEFAULT 'ebook'"
    )
    conn.commit()
    conn.close()


def _migrate_third_fav() -> None:
    """Add is_third_fav column (bronze tier) to both books and user_series.

    Bronze is the 3rd-place all-time tier — books beneath silver but
    above the bulk 5-star bucket. Mutually exclusive with gold/silver,
    same invariant pattern (setting any tier implies is_favorite=1).
    """
    conn = get_db()
    for table in ("books", "user_series"):
        cols = {
            row[1]
            for row in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if "is_third_fav" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                "is_third_fav INTEGER DEFAULT 0"
            )
            log.info("Added is_third_fav to %s", table)
    conn.commit()
    conn.close()


def _migrate_book_tiers() -> None:
    """Ensure the original gold/silver tier columns exist on books.

    Production databases predate the migration runner and already have
    these columns, but a database created from today's base schema does
    not. Keep this idempotent so both upgrade and clean-install paths work.
    """
    conn = get_db()
    cols = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    }
    for column in ("is_all_time_fav", "is_second_fav"):
        if column not in cols:
            conn.execute(
                f"ALTER TABLE books ADD COLUMN {column} "
                "INTEGER DEFAULT 0"
            )
            log.info("Added %s to books", column)
    conn.commit()
    conn.close()


def _migrate_price() -> None:
    """Add price REAL column to books (USD, nullable).

    Used by the /metrics page to compute library value. Existing books
    pre-this migration won't have a price set; they contribute 0 to the
    total and the metrics endpoint reports them as 'unpriced'.
    """
    conn = get_db()
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(books)").fetchall()
    }
    if "price" not in cols:
        conn.execute("ALTER TABLE books ADD COLUMN price REAL")
        log.info("Added price column to books")
    conn.commit()
    conn.close()


def _migrate_length_fields() -> None:
    """Add pages + audio_seconds + finish_year to books.

    pages — page count for physical / ebook (so we can sum pages-read).
    audio_seconds — duration of the audio edition (sum hours-listened).
    finish_year — denormalised year-of-date_finished for fast yearly
    grouping in /metrics. Computed from date_finished on insert/update.
    """
    conn = get_db()
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(books)").fetchall()
    }
    if "pages" not in cols:
        conn.execute("ALTER TABLE books ADD COLUMN pages INTEGER")
        log.info("Added pages column to books")
    if "audio_seconds" not in cols:
        conn.execute(
            "ALTER TABLE books ADD COLUMN audio_seconds INTEGER"
        )
        log.info("Added audio_seconds column to books")
    conn.commit()
    conn.close()


def _migrate_recommendation_dismissals() -> None:
    """One-row-per-dismissal table for the /recommendations page so a
    "not interested" click doesn't re-surface the book on every reload."""
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS recommendation_dismissals (
            user_id INTEGER NOT NULL,
            hc_book_id INTEGER NOT NULL,
            dismissed_at TEXT NOT NULL,
            PRIMARY KEY (user_id, hc_book_id)
        )"""
    )
    conn.commit()
    conn.close()


def get_dismissed_hc_ids(user_id: int) -> set[int]:
    conn = get_db()
    rows = conn.execute(
        "SELECT hc_book_id FROM recommendation_dismissals"
        " WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return {int(r["hc_book_id"]) for r in rows}


def dismiss_recommendation(user_id: int, hc_book_id: int) -> None:
    from datetime import datetime, timezone
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO recommendation_dismissals"
        " (user_id, hc_book_id, dismissed_at) VALUES (?, ?, ?)",
        (user_id, hc_book_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _migrate_user_series_rating() -> None:
    """Add per-user series ratings + favorite/tier flags on user_series."""
    conn = get_db()
    cols = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(user_series)"
        ).fetchall()
    }
    add = []
    if "rating" not in cols:
        add.append("ALTER TABLE user_series ADD COLUMN rating REAL")
    if "is_favorite" not in cols:
        add.append("ALTER TABLE user_series ADD COLUMN is_favorite INTEGER DEFAULT 0")
    if "is_all_time_fav" not in cols:
        add.append("ALTER TABLE user_series ADD COLUMN is_all_time_fav INTEGER DEFAULT 0")
    if "is_second_fav" not in cols:
        add.append("ALTER TABLE user_series ADD COLUMN is_second_fav INTEGER DEFAULT 0")
    if add:
        log.info("Adding rating/favorite columns to user_series")
        for stmt in add:
            conn.execute(stmt)
        conn.commit()
    conn.close()


def _migrate_user_series_review() -> None:
    """Add a private written review field to each user's series record."""
    conn = get_db()
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(user_series)"
        ).fetchall()
    }
    if "review" not in columns:
        conn.execute("ALTER TABLE user_series ADD COLUMN review TEXT")
        conn.commit()
    conn.close()


def _migrate_also_physical() -> None:
    """Add an also_physical flag — true when a book has both audio/ebook
    AND a physical copy. The card renders two badges in that case."""
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "also_physical" in columns:
        conn.close()
        return
    log.info("Adding also_physical column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN also_physical INTEGER DEFAULT 0"
    )
    conn.commit()
    conn.close()


def _migrate_review() -> None:
    """Add a review column (user's own free-text review)."""
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "review" in columns:
        conn.close()
        return
    log.info("Adding review column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN review TEXT"
    )
    conn.commit()
    conn.close()


def _migrate_manual_category() -> None:
    """Add a manual_category column (owner override for the heuristic).

    Values are kept text-free except via the API validator, which only
    accepts NULL, 'Religious', or 'Fiction'.
    """
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "manual_category" in columns:
        conn.close()
        return
    log.info("Adding manual_category column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN manual_category TEXT"
    )
    conn.commit()
    conn.close()


def _migrate_series_entries_cover_url() -> None:
    """Add cover_url column to series_entries (for ghost cards).

    Stores the Hardcover CDN image URL for each series entry so
    ghost cards (series books the user doesn't own) can render
    an actual cover instead of a generic placeholder.
    """
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(series_entries)"
        ).fetchall()
    ]
    if "cover_url" in columns:
        conn.close()
        return
    log.info("Adding cover_url column to series_entries")
    conn.execute(
        "ALTER TABLE series_entries ADD COLUMN cover_url TEXT"
    )
    conn.commit()
    conn.close()


def _migrate_asin() -> None:
    """Add asin column to books (Amazon Standard Identification Number).

    Royal Road / Kindle Unlimited / Audible titles often only have an
    ASIN — no ISBN. Storing it separately lets us fall back to the
    Amazon cover CDN when ISBN-based lookups dead-end.
    """
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "asin" in columns:
        conn.close()
        return
    log.info("Adding asin column to books")
    conn.execute("ALTER TABLE books ADD COLUMN asin TEXT")
    conn.commit()
    conn.close()


def _migrate_series_monitored() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(series_link)"
        ).fetchall()
    ]
    if "monitored" in columns or "user_id" not in columns:
        conn.close()
        return

    log.info("Adding monitored column to series_link")
    conn.execute(
        "ALTER TABLE series_link ADD COLUMN"
        " monitored INTEGER NOT NULL DEFAULT 1"
    )
    conn.commit()
    conn.close()


def _migrate_global_series() -> None:
    """Migrate series data to global model.

    Removes user_id/monitored from series_link, removes
    status/book_id from series_entries, creates user_series
    and user_entry_status tables.
    """
    conn = get_db()

    # Guard: skip if already migrated
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(series_link)"
        ).fetchall()
    ]
    if "user_id" not in columns:
        conn.close()
        return

    log.info("Migrating series to global model")

    # 1. Create new tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            series_link_id INTEGER NOT NULL
                REFERENCES series_link(id)
                ON DELETE CASCADE,
            monitored INTEGER NOT NULL DEFAULT 1,
            display_name TEXT,
            UNIQUE(user_id, series_link_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_entry_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            series_entry_id INTEGER NOT NULL
                REFERENCES series_entries(id)
                ON DELETE CASCADE,
            status TEXT NOT NULL
                CHECK(status IN ('linked', 'ignored')),
            UNIQUE(user_id, series_entry_id)
        )
    """)
    conn.commit()

    # 2. Deduplicate by hardcover_series_id
    groups = conn.execute("""
        SELECT hardcover_series_id,
               GROUP_CONCAT(id) as ids
        FROM series_link
        WHERE hardcover_series_id IS NOT NULL
        GROUP BY hardcover_series_id
        HAVING COUNT(*) > 1
    """).fetchall()

    for group in groups:
        ids = [int(x) for x in group["ids"].split(",")]
        placeholders = ",".join("?" * len(ids))

        # Pick canonical: most recent last_checked
        canonical_id = conn.execute(
            f"""SELECT id FROM series_link
                WHERE id IN ({placeholders})
                ORDER BY last_checked DESC, id DESC
                LIMIT 1""",
            ids,
        ).fetchone()["id"]

        all_rows = conn.execute(
            f"""SELECT id, user_id, monitored
                FROM series_link
                WHERE id IN ({placeholders})""",
            ids,
        ).fetchall()

        for row in all_rows:
            conn.execute(
                """INSERT OR IGNORE INTO user_series
                   (user_id, series_link_id, monitored)
                   VALUES (?, ?, ?)""",
                (row["user_id"], canonical_id,
                 row["monitored"]),
            )
            if row["id"] != canonical_id:
                conn.execute(
                    """UPDATE books
                       SET series_link_id = ?
                       WHERE series_link_id = ?""",
                    (canonical_id, row["id"]),
                )
                conn.execute(
                    "DELETE FROM series_link WHERE id = ?",
                    (row["id"],),
                )

    # 3. Deduplicate unlinked by LOWER(series_name)
    groups = conn.execute("""
        SELECT LOWER(series_name) as name_lower,
               GROUP_CONCAT(id) as ids
        FROM series_link
        WHERE hardcover_series_id IS NULL
        GROUP BY LOWER(series_name)
        HAVING COUNT(*) > 1
    """).fetchall()

    for group in groups:
        ids = [int(x) for x in group["ids"].split(",")]
        placeholders = ",".join("?" * len(ids))
        canonical_id = ids[0]

        all_rows = conn.execute(
            f"""SELECT id, user_id, monitored
                FROM series_link
                WHERE id IN ({placeholders})""",
            ids,
        ).fetchall()

        for row in all_rows:
            conn.execute(
                """INSERT OR IGNORE INTO user_series
                   (user_id, series_link_id, monitored)
                   VALUES (?, ?, ?)""",
                (row["user_id"], canonical_id,
                 row["monitored"]),
            )
            if row["id"] != canonical_id:
                conn.execute(
                    """UPDATE books
                       SET series_link_id = ?
                       WHERE series_link_id = ?""",
                    (canonical_id, row["id"]),
                )
                conn.execute(
                    "DELETE FROM series_link WHERE id = ?",
                    (row["id"],),
                )

    # 4. Populate user_series for remaining
    conn.execute("""
        INSERT OR IGNORE INTO user_series
            (user_id, series_link_id, monitored)
        SELECT user_id, id, monitored
        FROM series_link
    """)
    conn.commit()

    # 5. Populate user_entry_status for non-default statuses
    conn.execute("""
        INSERT OR IGNORE INTO user_entry_status
            (user_id, series_entry_id, status)
        SELECT sl.user_id, se.id, se.status
        FROM series_entries se
        JOIN series_link sl ON se.series_link_id = sl.id
        WHERE se.status IN ('linked', 'ignored')
          AND se.status != CASE
              WHEN se.position = CAST(se.position AS INTEGER)
              THEN 'linked' ELSE 'ignored' END
    """)
    conn.commit()

    # 6-7. Rebuild tables without removed columns
    conn.executescript("""
        PRAGMA foreign_keys=OFF;

        CREATE TABLE series_link_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_name TEXT NOT NULL,
            hardcover_series_id INTEGER,
            hardcover_series_name TEXT,
            hardcover_slug TEXT,
            last_checked TEXT,
            data_hash TEXT
        );
        INSERT INTO series_link_new
            (id, series_name, hardcover_series_id,
             hardcover_series_name, hardcover_slug,
             last_checked, data_hash)
        SELECT id, series_name, hardcover_series_id,
               hardcover_series_name, hardcover_slug,
               last_checked, data_hash
        FROM series_link;
        DROP TABLE series_link;
        ALTER TABLE series_link_new
            RENAME TO series_link;

        CREATE TABLE series_entries_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_link_id INTEGER NOT NULL
                REFERENCES series_link(id)
                ON DELETE CASCADE,
            position REAL NOT NULL,
            title TEXT NOT NULL,
            author TEXT,
            hardcover_book_id INTEGER
        );
        INSERT INTO series_entries_new
            (id, series_link_id, position, title,
             author, hardcover_book_id)
        SELECT id, series_link_id, position, title,
               author, hardcover_book_id
        FROM series_entries;
        DROP TABLE series_entries;
        ALTER TABLE series_entries_new
            RENAME TO series_entries;

        CREATE INDEX idx_series_entries_link
            ON series_entries(series_link_id);
        CREATE INDEX idx_series_entries_position
            ON series_entries(series_link_id, position);
        CREATE INDEX idx_user_series
            ON user_series(user_id, series_link_id);
        CREATE INDEX idx_user_entry_status
            ON user_entry_status(user_id, series_entry_id);

        PRAGMA foreign_keys=ON;
    """)

    conn.close()
    log.info("Global series migration complete")


def _migrate_user_libraries() -> None:
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
    ]
    if "libraries" in columns:
        conn.close()
        return

    log.info("Adding libraries column to users")
    conn.execute(
        "ALTER TABLE users ADD COLUMN libraries TEXT"
    )

    # Seed defaults
    jcpl = json.dumps([
        {"name": "JCPL", "url": "jefferson.overdrive.com"},
    ])
    dpl_jcpl = json.dumps([
        {"name": "DPL", "url": "denver.overdrive.com"},
        {"name": "JCPL", "url": "jefferson.overdrive.com"},
    ])
    conn.execute(
        "UPDATE users SET libraries = ?"
        " WHERE username = 'andy'",
        (jcpl,),
    )
    conn.execute(
        "UPDATE users SET libraries = ?"
        " WHERE username IN ('ada', 'liz')",
        (dpl_jcpl,),
    )
    conn.commit()
    conn.close()


def _migrate_superusers() -> None:
    """Ensure andy and liz are superusers."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET is_superuser = 1"
        " WHERE username IN ('andy', 'liz')"
        " AND is_superuser = 0"
    )
    conn.commit()
    conn.close()


def _migrate_archive_user() -> None:
    """Create the archive pseudo-user if it doesn't exist."""
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM users WHERE username = 'archive'"
    ).fetchone()
    if row:
        conn.close()
        return
    log.info("Creating archive user")
    conn.execute(
        "INSERT INTO users (username, display_name,"
        " password_hash)"
        " VALUES ('archive', 'Archive', '!')"
    )
    conn.commit()
    conn.close()


def _migrate_series_ignored() -> None:
    """Add per-book series_ignored flag.

    Replaces entry-level ignore filtering with book-level.
    Converts existing entry-level ignores (both explicit via
    user_entry_status and default non-integer position) to
    series_ignored=1 on the corresponding books.
    """
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "series_ignored" in columns:
        conn.close()
        return

    log.info("Adding series_ignored column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN"
        " series_ignored INTEGER DEFAULT 0"
    )

    # Convert explicit entry-level ignores to book-level
    conn.execute("""
        UPDATE books SET series_ignored = 1
        WHERE id IN (
            SELECT b.id FROM books b
            JOIN series_entries se
                ON se.series_link_id = b.series_link_id
                AND se.position = b.series_index
            JOIN user_entry_status ues
                ON ues.series_entry_id = se.id
                AND ues.user_id = b.user_id
            WHERE ues.status = 'ignored'
        )
    """)

    # Convert default non-integer-position ignores
    conn.execute("""
        UPDATE books SET series_ignored = 1
        WHERE id IN (
            SELECT b.id FROM books b
            JOIN series_entries se
                ON se.series_link_id = b.series_link_id
                AND se.position = b.series_index
            LEFT JOIN user_entry_status ues
                ON ues.series_entry_id = se.id
                AND ues.user_id = b.user_id
            WHERE ues.status IS NULL
                AND se.position
                    != CAST(se.position AS INTEGER)
        )
    """)

    conn.commit()
    conn.close()


def _migrate_koreader_sync() -> None:
    """Add koreader_filename and sync_updated_at columns."""
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "koreader_filename" in columns:
        conn.close()
        return

    log.info("Adding KOReader sync columns to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN"
        " koreader_filename TEXT"
    )
    conn.execute(
        "ALTER TABLE books ADD COLUMN"
        " sync_updated_at TEXT"
    )
    conn.execute(
        "CREATE INDEX idx_books_koreader_filename"
        " ON books(user_id, koreader_filename)"
    )
    conn.commit()
    conn.close()


def _migrate_epub_hash() -> None:
    """Add epub_hash column with index and backfill from files."""
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "epub_hash" in columns:
        conn.close()
        return

    log.info("Adding epub_hash column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN epub_hash TEXT"
    )
    conn.execute(
        "CREATE INDEX idx_books_epub_hash"
        " ON books(user_id, epub_hash)"
    )

    # Backfill hashes for existing books with files
    rows = conn.execute(
        "SELECT id, user_id, file_path FROM books"
        " WHERE file_path IS NOT NULL"
    ).fetchall()
    backfilled = 0
    for row in rows:
        book_id, user_id, file_path = row
        epub_path = (
            DATA_DIR / "files" / str(user_id) / file_path
        )
        if epub_path.exists():
            md5 = hashlib.md5(
                epub_path.read_bytes()
            ).hexdigest()
            conn.execute(
                "UPDATE books SET epub_hash = ?"
                " WHERE id = ?",
                (md5, book_id),
            )
            backfilled += 1
    conn.commit()
    conn.close()
    log.info("Backfilled epub_hash for %d books", backfilled)


def _migrate_sync_version() -> None:
    """Add sync_version column for optimistic concurrency."""
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(books)"
        ).fetchall()
    ]
    if "sync_version" in columns:
        conn.close()
        return

    log.info("Adding sync_version column to books")
    conn.execute(
        "ALTER TABLE books ADD COLUMN"
        " sync_version INTEGER DEFAULT 0"
    )
    # All books start at version 0. Web-initiated changes
    # increment the version; clients start unversioned (0)
    # and receive the current version in sync responses.
    conn.commit()
    conn.close()


def _migrate_series_complete() -> None:
    """Add series_complete flag to user_series."""
    conn = get_db()
    columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(user_series)"
        ).fetchall()
    ]
    if "series_complete" in columns:
        conn.close()
        return

    log.info("Adding series_complete column to user_series")
    conn.execute(
        "ALTER TABLE user_series ADD COLUMN"
        " series_complete INTEGER NOT NULL DEFAULT 1"
    )
    conn.commit()
    conn.close()


def update_user_libraries(
    user_id: int, libraries_json: str
) -> None:
    """Update the libraries JSON for a user."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET libraries = ? WHERE id = ?",
        (libraries_json, user_id),
    )
    conn.commit()
    conn.close()


# --- Password helpers ---


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> bool:
    """Check password against stored hash.

    Supports both bcrypt ($2b$ prefix) and legacy SHA-256 hashes.
    """
    if stored_hash.startswith("$2b$"):
        return bcrypt.checkpw(
            password.encode("utf-8"),
            stored_hash.encode("utf-8"),
        )
    # Legacy: SHA-256 with static salt
    legacy = hashlib.sha256(
        (password + "books_salt").encode("utf-8")
    ).hexdigest()
    return legacy == stored_hash


def set_password_hash(user_id: int, password_hash: str) -> None:
    """Update the password_hash column for a user."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    conn.commit()
    conn.close()


# --- User queries ---


def get_user_by_username(username: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def get_user_by_id(user_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def create_user(
    username: str,
    display_name: str,
    password_hash: str,
    kindle_email: str | None = None,
) -> int:
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO users (username, display_name, password_hash,
           kindle_email)
           VALUES (?, ?, ?, ?)""",
        (username, display_name, password_hash, kindle_email),
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    return user_id


def update_user_kindle_email(
    user_id: int, kindle_email: str
) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE users SET kindle_email = ? WHERE id = ?",
        (kindle_email, user_id),
    )
    conn.commit()
    conn.close()


# --- Book queries ---


def _row_to_book(row: sqlite3.Row) -> dict:
    book = dict(row)
    if book.get("tags"):
        try:
            book["tags"] = json.loads(book["tags"])
        except (json.JSONDecodeError, TypeError):
            book["tags"] = []
    else:
        book["tags"] = []
    return book


def get_books(
    user_id: int,
    q: str | None = None,
    series: str | None = None,
    reading_status: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    is_favorite: bool | None = None,
    is_owned: bool | None = None,
    has_series: bool | None = None,
    rated: bool | None = None,
    letter: str | None = None,
    book_format: str | None = None,
    tag: str | None = None,
    sort: str = "title",
    order: str = "asc",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    conditions = [
        "user_id = ?", "series_ignored = 0",
        # Fractional series positions are novellas, excerpts, and other
        # supplementary entries. Keep them off the public shelf until the
        # reader has actually finished them; read entries remain visible.
        """(series_index IS NULL
            OR series_index = CAST(series_index AS INTEGER)
            OR reading_status = 'read')""",
        # Hide unowned+unread books from unmonitored series
        """NOT (is_owned = 0
            AND reading_status = 'unread'
            AND series_link_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM user_series us
                WHERE us.user_id = books.user_id
                    AND us.series_link_id
                        = books.series_link_id
                    AND us.monitored = 0
            ))""",
    ]
    params: list = [user_id]

    if q:
        conditions.append(
            "(title LIKE ? OR authors LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like])

    if series is not None:
        conditions.append("series = ?")
        params.append(series)

    if reading_status is not None:
        conditions.append("reading_status = ?")
        params.append(reading_status)

    if min_rating is not None:
        conditions.append("rating >= ?")
        params.append(min_rating)

    if max_rating is not None:
        conditions.append("rating <= ?")
        params.append(max_rating)

    if is_favorite is not None:
        conditions.append("is_favorite = ?")
        params.append(1 if is_favorite else 0)

    if is_owned is not None:
        conditions.append("is_owned = ?")
        params.append(1 if is_owned else 0)

    if book_format is not None:
        conditions.append("book_format = ?")
        params.append(book_format)

    if tag:
        conditions.append("tags LIKE ?")
        params.append(f'%"{tag}"%')

    if has_series is not None:
        if has_series:
            conditions.append("series IS NOT NULL")
        else:
            conditions.append("series IS NULL")

    if rated is not None:
        if rated:
            conditions.append("rating IS NOT NULL")
        else:
            conditions.append("rating IS NULL")

    if letter is not None:
        if letter == "#":
            conditions.append(
                "UPPER(SUBSTR(sort_title, 1, 1))"
                " NOT BETWEEN 'A' AND 'Z'"
            )
        else:
            conditions.append(
                "UPPER(SUBSTR(sort_title, 1, 1)) = ?"
            )
            params.append(letter.upper())

    # For sort=series we want series books clustered FIRST then
    # standalones, so push NULL series to the end via an explicit
    # `series IS NULL` ordering key (NULL is lowest in ASC, which
    # would otherwise put standalones at the top).
    allowed_sort = {
        "title": "sort_title",
        "author": "author_sort",
        "date_added": "date_added",
        "date_finished": "date_finished",
        "rating": "rating",
        "series": (
            "(series IS NULL), series, series_index, sort_title"
        ),
    }
    sort_col = allowed_sort.get(sort, "sort_title")
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    where = " AND ".join(conditions)
    query = f"""
        SELECT books.*,
               (SELECT se.cover_url
                FROM series_entries se
                WHERE se.series_link_id = books.series_link_id
                    AND se.position = books.series_index
                LIMIT 1) AS cover_url
        FROM books
        WHERE {where}
        ORDER BY {sort_col} {order_dir}
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_row_to_book(r) for r in rows]


def count_books(
    user_id: int,
    q: str | None = None,
    series: str | None = None,
    reading_status: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    is_favorite: bool | None = None,
    is_owned: bool | None = None,
    has_series: bool | None = None,
    rated: bool | None = None,
    letter: str | None = None,
    book_format: str | None = None,
    tag: str | None = None,
) -> int:
    conditions = [
        "user_id = ?", "series_ignored = 0",
        # Must mirror get_books() so pagination totals describe the cards
        # the API can actually return.
        """(series_index IS NULL
            OR series_index = CAST(series_index AS INTEGER)
            OR reading_status = 'read')""",
        # Hide unowned+unread books from unmonitored series
        """NOT (is_owned = 0
            AND reading_status = 'unread'
            AND series_link_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM user_series us
                WHERE us.user_id = books.user_id
                    AND us.series_link_id
                        = books.series_link_id
                    AND us.monitored = 0
            ))""",
    ]
    params: list = [user_id]

    if q:
        conditions.append(
            "(title LIKE ? OR authors LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like])

    if series is not None:
        conditions.append("series = ?")
        params.append(series)

    if reading_status is not None:
        conditions.append("reading_status = ?")
        params.append(reading_status)

    if min_rating is not None:
        conditions.append("rating >= ?")
        params.append(min_rating)

    if max_rating is not None:
        conditions.append("rating <= ?")
        params.append(max_rating)

    if is_favorite is not None:
        conditions.append("is_favorite = ?")
        params.append(1 if is_favorite else 0)

    if is_owned is not None:
        conditions.append("is_owned = ?")
        params.append(1 if is_owned else 0)

    if book_format is not None:
        conditions.append("book_format = ?")
        params.append(book_format)

    if tag:
        conditions.append("tags LIKE ?")
        params.append(f'%"{tag}"%')

    if has_series is not None:
        if has_series:
            conditions.append("series IS NOT NULL")
        else:
            conditions.append("series IS NULL")

    if rated is not None:
        if rated:
            conditions.append("rating IS NOT NULL")
        else:
            conditions.append("rating IS NULL")

    if letter is not None:
        if letter == "#":
            conditions.append(
                "UPPER(SUBSTR(sort_title, 1, 1))"
                " NOT BETWEEN 'A' AND 'Z'"
            )
        else:
            conditions.append(
                "UPPER(SUBSTR(sort_title, 1, 1)) = ?"
            )
            params.append(letter.upper())

    where = " AND ".join(conditions)
    query = f"SELECT COUNT(*) FROM books WHERE {where}"

    conn = get_db()
    count = conn.execute(query, params).fetchone()[0]
    conn.close()
    return count


def count_books_by_letter(
    user_id: int,
    reading_status: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    is_favorite: bool | None = None,
    is_owned: bool | None = None,
    rated: bool | None = None,
) -> dict[str, int]:
    """Count books grouped by first letter of sort_title.

    Returns {"A": 23, "B": 15, ..., "#": 4} where "#"
    covers non-alpha first characters.
    """
    conditions = [
        "user_id = ?", "series_ignored = 0",
        """NOT (is_owned = 0
            AND reading_status = 'unread'
            AND series_link_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM user_series us
                WHERE us.user_id = books.user_id
                    AND us.series_link_id
                        = books.series_link_id
                    AND us.monitored = 0
            ))""",
    ]
    params: list = [user_id]

    if reading_status is not None:
        conditions.append("reading_status = ?")
        params.append(reading_status)
    if min_rating is not None:
        conditions.append("rating >= ?")
        params.append(min_rating)
    if max_rating is not None:
        conditions.append("rating <= ?")
        params.append(max_rating)
    if is_favorite is not None:
        conditions.append("is_favorite = ?")
        params.append(1 if is_favorite else 0)
    if is_owned is not None:
        conditions.append("is_owned = ?")
        params.append(1 if is_owned else 0)
    if rated is not None:
        if rated:
            conditions.append("rating IS NOT NULL")
        else:
            conditions.append("rating IS NULL")

    where = " AND ".join(conditions)
    query = f"""
        SELECT CASE
            WHEN UPPER(SUBSTR(sort_title, 1, 1))
                BETWEEN 'A' AND 'Z'
            THEN UPPER(SUBSTR(sort_title, 1, 1))
            ELSE '#'
        END as letter, COUNT(*) as count
        FROM books WHERE {where}
        GROUP BY letter ORDER BY letter
    """
    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {r["letter"]: r["count"] for r in rows}


def get_book(book_id: int, user_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM books WHERE id = ? AND user_id = ?",
        (book_id, user_id),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_book(row)


def find_unowned_match(
    user_id: int,
    title: str,
    series_link_id: int | None = None,
    series_index: float | None = None,
) -> int | None:
    """Find an existing unowned book matching the given criteria.

    Checks series position first (if provided), then falls back
    to case-insensitive title matching. Returns book_id or None.
    """
    conn = get_db()

    # Match by series position (most reliable)
    if series_link_id is not None and series_index is not None:
        row = conn.execute(
            """SELECT id FROM books
               WHERE user_id = ? AND series_link_id = ?
                   AND series_index = ? AND is_owned = 0
               LIMIT 1""",
            (user_id, series_link_id, series_index),
        ).fetchone()
        if row:
            conn.close()
            return row[0]

    # Match by normalized title
    norm_title = normalize_title(title)
    rows = conn.execute(
        "SELECT id, title FROM books"
        " WHERE user_id = ? AND is_owned = 0",
        (user_id,),
    ).fetchall()
    conn.close()

    for row in rows:
        if normalize_title(row["title"]) == norm_title:
            return row["id"]

    return None


def find_owned_match(
    user_id: int,
    title: str,
    authors: str,
    series_link_id: int | None = None,
    series_index: float | None = None,
) -> dict | None:
    """Find an existing owned book matching the given criteria.

    Checks series position first (if provided), then falls back
    to normalized title + author first-token matching.
    Returns full book dict or None.
    """
    conn = get_db()

    # Tier 1: match by series position
    if series_link_id is not None and series_index is not None:
        row = conn.execute(
            """SELECT * FROM books
               WHERE user_id = ? AND series_link_id = ?
                   AND series_index = ? AND is_owned = 1
               LIMIT 1""",
            (user_id, series_link_id, series_index),
        ).fetchone()
        if row:
            conn.close()
            return _row_to_book(row)

    # Tier 2: normalized title + author first-token
    norm_title = normalize_title(title)
    # Extract first token from incoming author for comparison
    author_first = authors.strip().split(",")[0].strip()
    author_tokens = set(
        t.lower() for t in author_first.split()
    )

    rows = conn.execute(
        "SELECT * FROM books WHERE user_id = ? AND is_owned = 1",
        (user_id,),
    ).fetchall()
    conn.close()

    for row in rows:
        book = _row_to_book(row)
        if normalize_title(book["title"]) != norm_title:
            continue
        # Compare author: check if any token from the new
        # author appears in existing (handles "Gibson, William"
        # vs "William Gibson")
        existing_tokens = set(
            t.lower() for t in book["authors"].split(",")[0]
            .strip().split()
        )
        if author_tokens & existing_tokens:
            return book

    return None


def insert_book(
    user_id: int,
    title: str,
    sort_title: str,
    authors: str,
    author_sort: str | None,
    series: str | None,
    series_index: float | None,
    description: str | None,
    cover_filename: str | None,
    file_path: str | None,
    isbn: str | None,
    goodreads_id: str | None,
    tags: list[str] | None,
    date_added: str,
    date_finished: str | None,
    rating: float | None,
    reading_status: str = "unread",
    progress: float | None = None,
    is_favorite: int = 0,
    is_owned: int = 1,
    series_link_id: int | None = None,
    published_date: str | None = None,
    book_format: str = "ebook",
) -> int:
    tags_json = json.dumps(tags) if tags else "[]"
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO books (
            user_id, title, sort_title, authors, author_sort,
            series, series_index, series_link_id,
            description, cover_filename,
            file_path, isbn, goodreads_id, tags, date_added,
            date_finished, published_date, rating,
            reading_status, progress, is_favorite, is_owned,
            book_format
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, title, sort_title, authors, author_sort,
            series, series_index, series_link_id,
            description, cover_filename,
            file_path, isbn, goodreads_id, tags_json,
            date_added, date_finished, published_date, rating,
            reading_status, progress, is_favorite, is_owned,
            book_format,
        ),
    )
    conn.commit()
    book_id = cursor.lastrowid
    conn.close()
    return book_id


def update_book(
    book_id: int, user_id: int, updates: dict
) -> bool:
    if not updates:
        return False

    if "tags" in updates and isinstance(updates["tags"], list):
        updates["tags"] = json.dumps(updates["tags"])

    # Compat shim: map old is_read to reading_status
    if "is_read" in updates and "reading_status" not in updates:
        updates["reading_status"] = (
            "read" if updates["is_read"] else "unread"
        )
    updates.pop("is_read", None)

    allowed = {
        "title", "sort_title", "authors", "author_sort",
        "series", "series_index", "series_link_id",
        "description", "review",
        "cover_filename", "cover_updated_at",
        "file_path", "epub_hash", "isbn", "asin", "goodreads_id",
        "tags", "date_finished", "published_date",
        "rating", "reading_status", "book_format",
        "progress", "is_favorite", "is_owned",
        "is_all_time_fav", "is_second_fav", "is_third_fav",
        "also_physical",
        "series_ignored", "manual_category",
        "price", "pages", "audio_seconds",
    }
    filtered = {
        k: v for k, v in updates.items() if k in allowed
    }
    if not filtered:
        return False

    # When reading state changes via web UI, bump sync
    # timestamp and set override flag so KOReader defers
    # to the server on the next sync
    sync_fields = {"reading_status", "rating", "progress"}
    if filtered.keys() & sync_fields:
        filtered["sync_updated_at"] = (
            datetime.now(timezone.utc).isoformat()
        )
        filtered["sync_version"] = 1

    sets = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values())
    values.extend([book_id, user_id])

    conn = get_db()
    cursor = conn.execute(
        f"UPDATE books SET {sets} WHERE id = ? AND user_id = ?",
        values,
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()

    if changed and filtered.keys() & EPUB_METADATA_FIELDS:
        sync_book_epub(book_id, user_id)

    return changed


def delete_book(book_id: int, user_id: int) -> bool:
    conn = get_db()
    cursor = conn.execute(
        "DELETE FROM books WHERE id = ? AND user_id = ?",
        (book_id, user_id),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def archive_book(
    book_id: int, user_id: int, archive_user_id: int
) -> bool:
    """Transfer a book to the archive user.

    Clears series_link_id since archive doesn't have
    user_series subscriptions.
    """
    conn = get_db()
    cursor = conn.execute(
        "UPDATE books SET user_id = ?,"
        " series_link_id = NULL"
        " WHERE id = ? AND user_id = ?",
        (archive_user_id, book_id, user_id),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


# --- Series queries ---


# Effective status SQL expression: uses user override if present,
# otherwise defaults to 'linked' for every entry. The user explicitly
# wants novellas + supplementary entries (.5 positions like Edgedancer,
# Secret History, Backup, etc.) counted toward the series so they
# appear as ghosts when not owned. Anything truly noisy can still be
# manually flipped to 'ignored' per-entry via the series-edit page.
_EFFECTIVE_STATUS = "COALESCE(ues.status, 'linked')"


def _compute_default_status(position: float) -> str:
    """Return default entry status — 'linked' for every position.

    See the comment on _EFFECTIVE_STATUS for the rationale (novellas
    should count by default; user can flip individual entries to
    'ignored' via the series-edit page).
    """
    return "linked"


def get_user_series(
    user_id: int, series_link_id: int
) -> dict | None:
    """Get user's subscription to a series."""
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM user_series
           WHERE user_id = ? AND series_link_id = ?""",
        (user_id, series_link_id),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def ensure_user_series(
    user_id: int, series_link_id: int
) -> None:
    """Ensure user has a subscription to a series."""
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO user_series
           (user_id, series_link_id)
           VALUES (?, ?)""",
        (user_id, series_link_id),
    )
    conn.commit()
    conn.close()


def ensure_user_books_for_series(
    user_id: int,
    series_link_id: int,
    series_name: str,
) -> None:
    """Create unowned placeholder books for linked entries.

    For each series entry with effective status 'linked' that
    has no matching book for this user, creates an unowned
    book record. Only creates placeholders if the user
    subscribes to this series via user_series.
    """
    conn = get_db()

    # Guard: only create placeholders for subscribed series
    subscribed = conn.execute(
        """SELECT 1 FROM user_series
           WHERE user_id = ? AND series_link_id = ?""",
        (user_id, series_link_id),
    ).fetchone()
    if not subscribed:
        conn.close()
        return

    entries = conn.execute(
        f"""SELECT se.id, se.position, se.title, se.author
           FROM series_entries se
           LEFT JOIN user_entry_status ues
               ON ues.series_entry_id = se.id
               AND ues.user_id = ?
           WHERE se.series_link_id = ?
               AND {_EFFECTIVE_STATUS} = 'linked'
               AND NOT EXISTS (
                   SELECT 1 FROM books b
                   WHERE b.user_id = ?
                       AND b.series_link_id = ?
                       AND b.series_index = se.position
               )
               AND NOT EXISTS (
                   SELECT 1 FROM books b
                   WHERE b.user_id = ?
                       AND b.is_owned = 1
                       AND LOWER(TRIM(b.title))
                           = LOWER(TRIM(se.title))
                       AND LOWER(TRIM(b.authors))
                           = LOWER(TRIM(se.author))
               )""",
        (user_id, series_link_id,
         user_id, series_link_id,
         user_id),
    ).fetchall()

    for entry in entries:
        ensure_book_for_entry(
            conn, user_id, series_name,
            {
                "title": entry["title"],
                "author": entry["author"],
                "position": entry["position"],
            },
            series_link_id=series_link_id,
        )

    conn.commit()
    conn.close()


def sync_book_positions(
    user_id: int, entries: list[dict]
) -> None:
    """Update books' series_index to match HC entry positions.

    Called after match_books to sync matched library books
    with their Hardcover positions.
    """
    conn = get_db()
    synced_ids = []
    for entry in entries:
        if entry.get("book_id"):
            conn.execute(
                """UPDATE books SET series_index = ?
                   WHERE id = ? AND user_id = ?""",
                (entry["position"], entry["book_id"],
                 user_id),
            )
            synced_ids.append(entry["book_id"])
    conn.commit()
    conn.close()

    for bid in synced_ids:
        sync_book_epub(bid, user_id)


_RELIGIOUS_TAGS = frozenset({
    "christian", "christianity", "religion", "religious",
    "theology", "biblical", "bible", "prayer", "sermon",
    "church", "discipleship", "apologetics", "spirituality",
})

_FICTION_TAGS = frozenset({
    "fiction", "fantasy", "sci-fi", "science fiction", "scifi",
    "romance", "mystery", "thriller", "young adult", "ya",
    "dystopia", "epic fantasy", "urban fantasy", "paranormal",
})

# Authors normalized to lowercase. Match is case-insensitive substring
# on the (also lowercased) authors string. Single-token entries like
# "tripp" match the last name anywhere in the author string; longer
# entries pin a fuller form to avoid first-name false positives.
_RELIGIOUS_AUTHORS = frozenset({
    "paul david tripp", "tripp",
    "d. martyn lloyd-jones", "martyn lloyd-jones", "lloyd-jones",
    "francis chan",
    "d.a. carson", "d. a. carson",
    "dane c. ortlund", "dane ortlund",
    "don richardson",
    "edmund p. clowney", "edmund clowney",
    "darrell l. bock", "darrell bock",
    "craig a. blaising", "craig blaising",
    "dan g. mccartney", "charles clayton",
    "charles leiter",
    "bruce olson",
    "george muller", "george müller",
    "brother andrew", "elizabeth sherrill", "john sherrill",
    "tim keller", "timothy keller",
    "john piper", "jen wilkin", "r.c. sproul", "rc sproul",
    "ji packer", "j.i. packer",
    "nt wright", "n.t. wright",
    "john macarthur", "wayne grudem",
    "charles spurgeon", "a.w. tozer", "aw tozer",
    "dietrich bonhoeffer", "g.k. chesterton",
    "henri nouwen", "eugene peterson", "dallas willard",
    "richard foster", "philip yancey", "lee strobel",
    "max lucado", "david platt", "matt chandler",
    "andrew murray", "watchman nee", "elisabeth elliot",
    "ravi zacharias", "alistair begg",
})

_RELIGIOUS_SERIES_NAME_HINTS = (
    "bible", "gospel", "biblical", "theology", "christian",
)

# C.S. Lewis straddles fiction (Narnia) and theology. Title substrings
# that mark a Lewis book as theology/religious.
_LEWIS_RELIGIOUS_TITLE_HINTS = (
    "mere christianity", "screwtape", "problem of pain",
    "miracles", "great divorce", "abolition of man",
)


def _derive_category(
    manual_category: str | None = None,
    tags_blob: str | None = None,
    authors: str | None = None,
    series_name: str | None = None,
    title: str | None = None,
    *,
    in_series: bool = False,
) -> str:
    """Classify a book or series into Religious / Fiction.

    Order (highest priority first):
      0. Owner-supplied manual_category (if 'Religious' or 'Fiction').
      1. Religious-author allowlist (with C.S. Lewis special case).
      2. Religious series-name hint (e.g. "Bible Study").
      3. Tag-based: Religious tags win over Fiction tags.
      4. Fallback -> Fiction (the user's library is religious + fiction;
         non-fiction outside religion isn't a category they read in, so
         "Other" was just creating a confusing third bucket of fiction
         standalones the heuristics couldn't otherwise attribute).
    """
    if manual_category in ("Religious", "Fiction"):
        return manual_category
    authors_l = (authors or "").lower()
    series_l = (series_name or "").lower()
    title_l = (title or "").lower()

    # 1. Author allowlist.
    if authors_l:
        # C.S. Lewis special case: Narnia is Fiction; theology titles
        # are Religious; otherwise default Lewis to Religious.
        if (
            "c.s. lewis" in authors_l
            or "c. s. lewis" in authors_l
            or "cs lewis" in authors_l
        ):
            if "narnia" in series_l:
                return "Fiction"
            for hint in _LEWIS_RELIGIOUS_TITLE_HINTS:
                if hint in title_l:
                    return "Religious"
            return "Religious"
        for a in _RELIGIOUS_AUTHORS:
            if a in authors_l:
                return "Religious"

    # 2. Religious series name hints.
    if series_l:
        for hint in _RELIGIOUS_SERIES_NAME_HINTS:
            if hint in series_l:
                return "Religious"

    # 3. Tag-based detection (legacy behavior).
    if tags_blob:
        try:
            lower = tags_blob.lower()
        except Exception:
            lower = ""
        if lower:
            for t in _RELIGIOUS_TAGS:
                if f'"{t}"' in lower:
                    return "Religious"
            for t in _FICTION_TAGS:
                if f'"{t}"' in lower:
                    return "Fiction"
            for t in _RELIGIOUS_TAGS:
                if t in lower:
                    return "Religious"
            for t in _FICTION_TAGS:
                if t in lower:
                    return "Fiction"

    # 4. Fallback for both series and standalones.
    return "Fiction"


def _categorize_series(tag_blob: str | None) -> str:
    """Back-compat wrapper around _derive_category for series-only callers."""
    return _derive_category(
        manual_category=None,
        tags_blob=tag_blob,
        in_series=True,
    )


def get_series_list(
    user_id: int,
    monitored: bool | None = None,
) -> list[dict]:
    monitored_filter = ""
    if monitored is not None:
        monitored_filter = (
            " AND us.monitored = 1"
            if monitored
            else " AND us.monitored = 0"
        )

    conn = get_db()
    rows = conn.execute(
        f"""SELECT b.series_link_id,
                  COALESCE(us.display_name,
                      sl.series_name) as series,
                  us.monitored,
                  us.series_complete,
                  us.rating as user_rating,
                  us.is_favorite,
                  us.is_all_time_fav,
                  us.is_second_fav,
                  us.is_third_fav,
                  COUNT(*) as total_books,
                  SUM(CASE WHEN b.reading_status = 'read'
                      THEN 1 ELSE 0 END) as read_count,
                  COUNT(*) - SUM(CASE WHEN b.reading_status
                      = 'read' THEN 1 ELSE 0 END)
                      as unread_count,
                  SUM(CASE WHEN b.reading_status = 'reading'
                      THEN 1 ELSE 0 END) as reading_count,
                  SUM(CASE WHEN b.is_owned = 0
                      THEN 1 ELSE 0 END) as not_owned_count,
                  SUM(CASE WHEN b.is_owned = 0
                      AND b.reading_status NOT IN
                          ('read', 'reading')
                      THEN 1 ELSE 0 END)
                      as not_owned_unread_count,
                  MIN(CASE WHEN b.is_owned = 1
                      THEN b.rating END) as min_rating,
                  MAX(CASE WHEN b.is_owned = 1
                      THEN b.rating END) as max_rating,
                  AVG(CASE WHEN b.is_owned = 1
                      THEN b.rating END) as avg_rating,
                  (SELECT b2.authors FROM books b2
                   WHERE b2.series_link_id
                       = b.series_link_id
                       AND b2.user_id = b.user_id
                       AND b2.is_owned = 1
                       AND b2.series_ignored = 0
                   GROUP BY b2.authors
                   ORDER BY COUNT(*) DESC
                   LIMIT 1) as authors,
                  (SELECT b2.author_sort FROM books b2
                   WHERE b2.series_link_id
                       = b.series_link_id
                       AND b2.user_id = b.user_id
                       AND b2.is_owned = 1
                       AND b2.series_ignored = 0
                   GROUP BY b2.author_sort
                   ORDER BY COUNT(*) DESC
                   LIMIT 1) as author_sort,
                  (SELECT b2.id FROM books b2
                   WHERE b2.series_link_id
                       = b.series_link_id
                       AND b2.user_id = b.user_id
                       AND b2.series_ignored = 0
                       AND (b2.series_index IS NULL
                            OR b2.series_index =
                                CAST(b2.series_index AS INTEGER)
                            OR b2.reading_status = 'read')
                   ORDER BY (b2.series_index IS NULL),
                            b2.series_index ASC,
                            b2.id ASC
                   LIMIT 1) as first_book_id,
                  (SELECT b2.user_id FROM books b2
                   WHERE b2.series_link_id
                       = b.series_link_id
                       AND b2.user_id = b.user_id
                       AND b2.series_ignored = 0
                       AND (b2.series_index IS NULL
                            OR b2.series_index =
                                CAST(b2.series_index AS INTEGER)
                            OR b2.reading_status = 'read')
                   ORDER BY (b2.series_index IS NULL),
                            b2.series_index ASC,
                            b2.id ASC
                   LIMIT 1) as first_book_user_id,
                  (SELECT b2.cover_filename FROM books b2
                   WHERE b2.series_link_id
                       = b.series_link_id
                       AND b2.user_id = b.user_id
                       AND b2.series_ignored = 0
                       AND (b2.series_index IS NULL
                            OR b2.series_index =
                                CAST(b2.series_index AS INTEGER)
                            OR b2.reading_status = 'read')
                   ORDER BY (b2.series_index IS NULL),
                            b2.series_index ASC,
                            b2.id ASC
                   LIMIT 1) as first_book_cover_filename,
                  (SELECT b2.cover_updated_at FROM books b2
                   WHERE b2.series_link_id
                       = b.series_link_id
                       AND b2.user_id = b.user_id
                       AND b2.series_ignored = 0
                       AND (b2.series_index IS NULL
                            OR b2.series_index =
                                CAST(b2.series_index AS INTEGER)
                            OR b2.reading_status = 'read')
                   ORDER BY (b2.series_index IS NULL),
                            b2.series_index ASC,
                            b2.id ASC
                   LIMIT 1) as first_book_cover_updated_at,
                  (SELECT se2.cover_url
                   FROM books b2
                   LEFT JOIN series_entries se2
                       ON se2.series_link_id = b2.series_link_id
                       AND se2.position = b2.series_index
                   WHERE b2.series_link_id = b.series_link_id
                       AND b2.user_id = b.user_id
                       AND b2.series_ignored = 0
                       AND (b2.series_index IS NULL
                            OR b2.series_index =
                                CAST(b2.series_index AS INTEGER)
                            OR b2.reading_status = 'read')
                   ORDER BY (b2.series_index IS NULL),
                            b2.series_index ASC,
                            b2.id ASC
                   LIMIT 1) as first_book_cover_url
           FROM books b
           JOIN series_link sl
               ON b.series_link_id = sl.id
           JOIN user_series us
               ON us.series_link_id = sl.id
               AND us.user_id = ?
           WHERE b.user_id = ?
               AND b.series_ignored = 0
               AND (b.series_index IS NULL
                    OR b.series_index =
                        CAST(b.series_index AS INTEGER)
                    OR b.reading_status = 'read')
               {monitored_filter}
           GROUP BY b.series_link_id
           HAVING COUNT(*) > 1
           ORDER BY COALESCE(us.display_name,
               sl.series_name)""",
        (user_id, user_id),
    ).fetchall()
    series_list = [dict(r) for r in rows]

    # Build ordered status/ownership sequences for segment bars
    book_rows = conn.execute(
        """SELECT b.series_link_id,
                  CASE b.reading_status
                      WHEN 'read' THEN 'r'
                      WHEN 'reading' THEN 'b'
                      ELSE 'u'
                  END as status_char,
                  CASE WHEN b.is_owned = 1
                      THEN '1' ELSE '0'
                  END as owned_char,
                  COALESCE(b.progress, 0) as progress
           FROM books b
           LEFT JOIN series_entries se
               ON se.series_link_id = b.series_link_id
               AND se.position = b.series_index
           WHERE b.user_id = ?
               AND b.series_link_id IS NOT NULL
               AND b.series_ignored = 0
               AND (b.series_index IS NULL
                    OR b.series_index =
                        CAST(b.series_index AS INTEGER)
                    OR b.reading_status = 'read')
           ORDER BY b.series_link_id,
               COALESCE(se.position,
                   b.series_index, 999)""",
        (user_id,),
    ).fetchall()

    # Concatenated tag blob per series for category derivation.
    # Each book's `tags` column is a JSON array string (e.g.
    # '["theology", "ethics"]') or NULL. Joining with '||' keeps the
    # cheap substring match safe — no tag contains '||'.
    tag_rows = conn.execute(
        """SELECT b.series_link_id,
                  GROUP_CONCAT(LOWER(b.tags), '||') as tag_blob
           FROM books b
           WHERE b.user_id = ?
               AND b.series_link_id IS NOT NULL
               AND b.series_ignored = 0
               AND b.tags IS NOT NULL
               AND b.tags <> '[]'
           GROUP BY b.series_link_id""",
        (user_id,),
    ).fetchall()

    # Manual category override per series: take any non-null value
    # the owner may have set on one of the books (rare, but allowed).
    manual_rows = conn.execute(
        """SELECT b.series_link_id,
                  MAX(b.manual_category) as manual_category
           FROM books b
           WHERE b.user_id = ?
               AND b.series_link_id IS NOT NULL
               AND b.series_ignored = 0
               AND b.manual_category IS NOT NULL
           GROUP BY b.series_link_id""",
        (user_id,),
    ).fetchall()
    conn.close()

    seqs: dict[int, tuple[list[str], list[str], list[float]]] = {}
    for r in book_rows:
        s, o, p = seqs.setdefault(
            r["series_link_id"], ([], [], [])
        )
        s.append(r["status_char"])
        o.append(r["owned_char"])
        p.append(r["progress"])

    tag_blobs: dict[int, str] = {
        r["series_link_id"]: r["tag_blob"] or ""
        for r in tag_rows
    }
    manual_overrides: dict[int, str] = {
        r["series_link_id"]: r["manual_category"]
        for r in manual_rows
        if r["manual_category"]
    }

    # Ghost-aware counts: total_books reflects the full
    # series via series_entries; falls back to owned count.
    entry_counts = get_series_entry_counts(user_id)

    for s in series_list:
        trip = seqs.get(s["series_link_id"], ([], [], []))
        s["status_seq"] = "".join(trip[0])
        s["owned_seq"] = "".join(trip[1])
        s["progress_seq"] = ",".join(
            f"{v:.2f}" for v in trip[2]
        )
        s["category"] = _derive_category(
            manual_category=manual_overrides.get(s["series_link_id"]),
            tags_blob=tag_blobs.get(s["series_link_id"]),
            authors=s.get("authors"),
            series_name=s.get("series"),
            in_series=True,
        )

        ec = entry_counts.get(s["series_link_id"])
        owned_count = s.get("total_books") or 0
        if ec and ec["total"] >= owned_count and ec["total"] > 0:
            s["entries_total"] = ec["total"]
            s["entries_owned"] = owned_count
            s["ghost_count"] = max(0, ec["total"] - owned_count)
            # Expose the entry total as total_books so tile UI
            # shows full-series counts; preserve owned via
            # entries_owned for callers that want the original.
            s["total_books"] = ec["total"]
        else:
            s["entries_total"] = owned_count
            s["entries_owned"] = owned_count
            s["ghost_count"] = 0

        # Extend the segmented bar to include ghost slots so
        # the visualization reflects the full series.
        ghost_count = s["ghost_count"]
        if ghost_count > 0:
            s["status_seq"] = s["status_seq"] + ("u" * ghost_count)
            s["owned_seq"] = s["owned_seq"] + ("0" * ghost_count)
            s["progress_seq"] = (
                s["progress_seq"]
                + ("," if s["progress_seq"] else "")
                + ",".join(["0.00"] * ghost_count)
            )

    return series_list


def get_series_entry_counts(
    user_id: int,
) -> dict[int, dict]:
    """Per series_link_id, return ghost-aware counts.

    Returns {series_link_id: {"total": N, "owned": N,
    "ghost": N}} where total uses `series_entries` when
    populated and falls back to the user's owned count
    otherwise. `ghost` is the number of entries the user
    doesn't have a book for. Counts effective-linked
    entries only (integer-position entries unless overridden).

    Filters out unreleased "Untitled X #N" placeholders so
    series totals reflect what's actually publishable and
    don't render as ghost cards in the UI.
    """
    conn = get_db()
    rows = conn.execute(
        f"""SELECT se.series_link_id,
                  COUNT(*) as entry_total,
                  SUM(
                      CASE WHEN EXISTS (
                          SELECT 1 FROM books b
                          WHERE b.user_id = ?
                              AND b.series_link_id = se.series_link_id
                              AND (
                                  b.series_index = se.position
                                  OR (
                                      LOWER(TRIM(b.title)) = LOWER(TRIM(se.title))
                                      AND b.authors IS NOT NULL
                                      AND se.author IS NOT NULL
                                      AND LOWER(TRIM(b.authors))
                                          = LOWER(TRIM(se.author))
                                  )
                              )
                              AND b.series_ignored = 0
                      ) THEN 1 ELSE 0 END
                  ) as entry_owned
           FROM series_entries se
           LEFT JOIN user_entry_status ues
               ON ues.series_entry_id = se.id
               AND ues.user_id = ?
           WHERE {_EFFECTIVE_STATUS} = 'linked'
               AND LOWER(se.title) NOT LIKE 'untitled%'
               AND (se.position IS NULL
                    OR se.position = CAST(se.position AS INTEGER))
           GROUP BY se.series_link_id""",
        (user_id, user_id),
    ).fetchall()
    conn.close()
    out: dict[int, dict] = {}
    for r in rows:
        total = r["entry_total"] or 0
        owned = r["entry_owned"] or 0
        out[r["series_link_id"]] = {
            "total": total,
            "owned": owned,
            "ghost": max(0, total - owned),
        }
    return out


def get_series_ghost_entries(
    user_id: int, series_link_id: int,
) -> list[dict]:
    """Return ghost (unowned) series entries for a series.

    A ghost is a series_entries row whose effective status
    is 'linked' but the user has no matching book (by
    series_index/position or normalized title+author). One
    dict per ghost with fields {position, title, author,
    hardcover_book_id}.

    Filters out unreleased "Untitled X #N" placeholders so
    they don't render as ghost cards. They stay in the DB
    so when Hardcover catalogs the real title, they re-emerge
    with their actual name.
    """
    conn = get_db()
    rows = conn.execute(
        f"""SELECT se.position, se.title, se.author,
                  se.hardcover_book_id, se.cover_url
           FROM series_entries se
           LEFT JOIN user_entry_status ues
               ON ues.series_entry_id = se.id
               AND ues.user_id = ?
           WHERE se.series_link_id = ?
               AND {_EFFECTIVE_STATUS} = 'linked'
               AND LOWER(se.title) NOT LIKE 'untitled%'
               AND (se.position IS NULL
                    OR se.position = CAST(se.position AS INTEGER))
               AND NOT EXISTS (
                   SELECT 1 FROM books b
                   WHERE b.user_id = ?
                       AND b.series_link_id = se.series_link_id
                       AND (
                           b.series_index = se.position
                           OR (
                               LOWER(TRIM(b.title)) = LOWER(TRIM(se.title))
                               AND b.authors IS NOT NULL
                               AND se.author IS NOT NULL
                               AND LOWER(TRIM(b.authors))
                                   = LOWER(TRIM(se.author))
                           )
                       )
                       AND b.series_ignored = 0
               )
           ORDER BY se.position""",
        (user_id, series_link_id, user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ghost_entries_for_user(
    user_id: int,
) -> list[dict]:
    """Return ghost entries across all series the user owns at least one book in.

    Used for the library flat-list ghost overlay. Returns
    dicts shaped like book records so the frontend can
    render them through the same grid component.

    Filters out unreleased "Untitled X #N" placeholders so
    they don't pollute the flat-list overlay.
    """
    conn = get_db()
    rows = conn.execute(
        f"""SELECT se.position as series_index,
                  se.title,
                  se.author as authors,
                  se.series_link_id,
                  sl.series_name as series,
                  se.hardcover_book_id,
                  se.cover_url
           FROM series_entries se
           JOIN series_link sl ON sl.id = se.series_link_id
           LEFT JOIN user_entry_status ues
               ON ues.series_entry_id = se.id
               AND ues.user_id = ?
           WHERE {_EFFECTIVE_STATUS} = 'linked'
               AND LOWER(se.title) NOT LIKE 'untitled%'
               AND (se.position IS NULL
                    OR se.position = CAST(se.position AS INTEGER))
               AND EXISTS (
                   SELECT 1 FROM books b
                   WHERE b.user_id = ?
                       AND b.series_link_id = se.series_link_id
                       AND b.series_ignored = 0
               )
               AND NOT EXISTS (
                   SELECT 1 FROM books b
                   WHERE b.user_id = ?
                       AND b.series_link_id = se.series_link_id
                       AND (
                           b.series_index = se.position
                           OR (
                               LOWER(TRIM(b.title)) = LOWER(TRIM(se.title))
                               AND b.authors IS NOT NULL
                               AND se.author IS NOT NULL
                               AND LOWER(TRIM(b.authors))
                                   = LOWER(TRIM(se.author))
                           )
                       )
                       AND b.series_ignored = 0
               )
           ORDER BY sl.series_name, se.position""",
        (user_id, user_id, user_id),
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": None,
            "is_ghost": True,
            "title": r["title"],
            "authors": r["authors"] or "",
            "series": r["series"],
            "series_link_id": r["series_link_id"],
            "series_index": r["series_index"],
            "hardcover_book_id": r["hardcover_book_id"],
            "cover_filename": None,
            "cover_url": r["cover_url"],
            "user_id": user_id,
            "is_owned": 0,
            "reading_status": "unread",
            "tags": [],
        })
    return out


def get_standalone_books_for_overview(
    user_id: int,
) -> list[dict]:
    """Return one tile-shaped dict per standalone owned book.

    A "standalone" is any book with `series_link_id IS NULL` (i.e. not
    attached to any series). Used alongside `get_series_list` to power
    the main library overview so non-series books still surface as
    tiles bucketed by category.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT id, user_id, title, authors, author_sort,
                  cover_filename, cover_updated_at,
                  rating, reading_status, is_owned, tags,
                  manual_category
           FROM books
           WHERE user_id = ?
               AND series_link_id IS NULL
               AND is_owned = 1
           ORDER BY COALESCE(author_sort, authors, title)""",
        (user_id,),
    ).fetchall()
    conn.close()

    out: list[dict] = []
    for r in rows:
        book = dict(r)
        category = _derive_category(
            manual_category=book.get("manual_category"),
            tags_blob=book.get("tags"),
            authors=book.get("authors"),
            title=book.get("title"),
            in_series=False,
        )
        out.append({
            "standalone_book_id": book["id"],
            "title": book["title"],
            "authors": book.get("authors"),
            "author_sort": book.get("author_sort"),
            "category": category,
            "cover_filename": book.get("cover_filename"),
            "cover_updated_at": book.get("cover_updated_at"),
            "cover_user_id": book["user_id"],
            "rating": book.get("rating"),
            "reading_status": book.get("reading_status"),
            "is_owned": book.get("is_owned"),
            "tags": book.get("tags"),
        })
    return out


def get_filtered_series(
    user_id: int,
    reading_status: str | None = None,
    rated: bool | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    is_favorite: bool | None = None,
    letter: str | None = None,
) -> list[dict]:
    """Return series with book counts, respecting filters.

    Returns [{"series": name, "series_link_id": id,
    "count": N}, ...] ordered by series name.
    """
    conditions = [
        "b.user_id = ?",
        "b.is_owned = 1",
        "b.series_link_id IS NOT NULL",
        "b.series_ignored = 0",
    ]
    params: list = [user_id]

    if reading_status is not None:
        conditions.append("b.reading_status = ?")
        params.append(reading_status)
    if rated is not None:
        if rated:
            conditions.append("b.rating IS NOT NULL")
        else:
            conditions.append("b.rating IS NULL")
    if min_rating is not None:
        conditions.append("b.rating >= ?")
        params.append(min_rating)
    if max_rating is not None:
        conditions.append("b.rating <= ?")
        params.append(max_rating)
    if is_favorite is not None:
        conditions.append("b.is_favorite = ?")
        params.append(1 if is_favorite else 0)

    having = ""
    series_name_expr = (
        "COALESCE(us.display_name, sl.series_name)"
    )
    if letter is not None:
        if letter == "#":
            having = (
                f" HAVING UPPER(SUBSTR("
                f"{series_name_expr}, 1, 1))"
                f" NOT BETWEEN 'A' AND 'Z'"
            )
        else:
            having = (
                f" HAVING UPPER(SUBSTR("
                f"{series_name_expr}, 1, 1)) = ?"
            )
            params.append(letter.upper())

    where = " AND ".join(conditions)
    conn = get_db()
    rows = conn.execute(
        f"""SELECT {series_name_expr} as series,
                b.series_link_id,
                COUNT(*) as count
            FROM books b
            JOIN series_link sl
                ON b.series_link_id = sl.id
            LEFT JOIN user_series us
                ON us.series_link_id = sl.id
                AND us.user_id = b.user_id
            WHERE {where}
            GROUP BY b.series_link_id{having}
            ORDER BY {series_name_expr}""",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_series_by_letter(
    user_id: int,
    reading_status: str | None = None,
    rated: bool | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    is_favorite: bool | None = None,
) -> dict[str, int]:
    """Count series grouped by first letter of series name.

    Returns {"A": 5, "B": 3, ..., "#": 1}.
    """
    conditions = [
        "b.user_id = ?",
        "b.is_owned = 1",
        "b.series_link_id IS NOT NULL",
        "b.series_ignored = 0",
    ]
    params: list = [user_id]

    if reading_status is not None:
        conditions.append("b.reading_status = ?")
        params.append(reading_status)
    if rated is not None:
        if rated:
            conditions.append("b.rating IS NOT NULL")
        else:
            conditions.append("b.rating IS NULL")
    if min_rating is not None:
        conditions.append("b.rating >= ?")
        params.append(min_rating)
    if max_rating is not None:
        conditions.append("b.rating <= ?")
        params.append(max_rating)
    if is_favorite is not None:
        conditions.append("b.is_favorite = ?")
        params.append(1 if is_favorite else 0)

    series_name_expr = (
        "COALESCE(us.display_name, sl.series_name)"
    )
    where = " AND ".join(conditions)
    query = f"""
        SELECT CASE
            WHEN UPPER(SUBSTR({series_name_expr}, 1, 1))
                BETWEEN 'A' AND 'Z'
            THEN UPPER(SUBSTR({series_name_expr}, 1, 1))
            ELSE '#'
        END as letter,
        COUNT(DISTINCT b.series_link_id) as count
        FROM books b
        JOIN series_link sl
            ON b.series_link_id = sl.id
        LEFT JOIN user_series us
            ON us.series_link_id = sl.id
            AND us.user_id = b.user_id
        WHERE {where}
        GROUP BY letter ORDER BY letter
    """
    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {r["letter"]: r["count"] for r in rows}


def get_distinct_authors(
    user_id: int,
    reading_status: str | None = None,
    rated: bool | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    is_favorite: bool | None = None,
    letter: str | None = None,
) -> list[dict]:
    """Return distinct authors with book counts.

    Returns [{"authors": name, "count": N}, ...] ordered
    by count desc. Treats multi-author strings as single
    entries.
    """
    conditions = [
        "user_id = ?",
        "is_owned = 1",
        "series_ignored = 0",
    ]
    params: list = [user_id]

    if reading_status is not None:
        conditions.append("reading_status = ?")
        params.append(reading_status)
    if rated is not None:
        if rated:
            conditions.append("rating IS NOT NULL")
        else:
            conditions.append("rating IS NULL")
    if min_rating is not None:
        conditions.append("rating >= ?")
        params.append(min_rating)
    if max_rating is not None:
        conditions.append("rating <= ?")
        params.append(max_rating)
    if is_favorite is not None:
        conditions.append("is_favorite = ?")
        params.append(1 if is_favorite else 0)
    if letter is not None:
        if letter == "#":
            conditions.append(
                "UPPER(SUBSTR(authors, 1, 1))"
                " NOT BETWEEN 'A' AND 'Z'"
            )
        else:
            conditions.append(
                "UPPER(SUBSTR(authors, 1, 1)) = ?"
            )
            params.append(letter.upper())

    where = " AND ".join(conditions)
    conn = get_db()
    rows = conn.execute(
        f"""SELECT authors, COUNT(*) as count
            FROM books
            WHERE {where}
            GROUP BY authors
            ORDER BY count DESC, authors""",
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_authors_by_letter(
    user_id: int,
    reading_status: str | None = None,
    rated: bool | None = None,
    min_rating: float | None = None,
    max_rating: float | None = None,
    is_favorite: bool | None = None,
) -> dict[str, int]:
    """Count distinct authors grouped by first letter.

    Returns {"A": 5, "B": 3, ..., "#": 1}.
    """
    conditions = [
        "user_id = ?",
        "is_owned = 1",
        "series_ignored = 0",
    ]
    params: list = [user_id]

    if reading_status is not None:
        conditions.append("reading_status = ?")
        params.append(reading_status)
    if rated is not None:
        if rated:
            conditions.append("rating IS NOT NULL")
        else:
            conditions.append("rating IS NULL")
    if min_rating is not None:
        conditions.append("rating >= ?")
        params.append(min_rating)
    if max_rating is not None:
        conditions.append("rating <= ?")
        params.append(max_rating)
    if is_favorite is not None:
        conditions.append("is_favorite = ?")
        params.append(1 if is_favorite else 0)

    where = " AND ".join(conditions)
    query = f"""
        SELECT CASE
            WHEN UPPER(SUBSTR(authors, 1, 1))
                BETWEEN 'A' AND 'Z'
            THEN UPPER(SUBSTR(authors, 1, 1))
            ELSE '#'
        END as letter,
        COUNT(DISTINCT authors) as count
        FROM books
        WHERE {where}
        GROUP BY letter ORDER BY letter
    """
    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return {r["letter"]: r["count"] for r in rows}


def get_series_autocomplete(user_id: int) -> list[dict]:
    """Get all series with authors and book indices for autocomplete."""
    conn = get_db()
    series_rows = conn.execute(
        """SELECT sl.id,
                  COALESCE(us.display_name,
                      sl.series_name) as series_name,
                  (SELECT b2.authors FROM books b2
                   WHERE b2.series_link_id = sl.id
                       AND b2.user_id = ?
                   GROUP BY b2.authors
                   ORDER BY COUNT(*) DESC
                   LIMIT 1) as authors
           FROM series_link sl
           JOIN user_series us
               ON us.series_link_id = sl.id
               AND us.user_id = ?
           ORDER BY COALESCE(us.display_name,
               sl.series_name)""",
        (user_id, user_id),
    ).fetchall()
    book_rows = conn.execute(
        """SELECT series_link_id, series_index, title
           FROM books
           WHERE user_id = ?
             AND series_link_id IS NOT NULL
           ORDER BY series_link_id, series_index""",
        (user_id,),
    ).fetchall()
    conn.close()

    books_by_series: dict[int, list[dict]] = {}
    for r in book_rows:
        sid = r["series_link_id"]
        books_by_series.setdefault(sid, []).append({
            "index": r["series_index"],
            "title": r["title"],
        })

    return [
        {
            "id": r["id"],
            "name": r["series_name"],
            "authors": r["authors"] or "",
            "books": books_by_series.get(r["id"], []),
        }
        for r in series_rows
    ]


def get_series_books(
    user_id: int, series_link_id: int
) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT b.*, se.position as hc_position,
                  se.cover_url as cover_url
           FROM books b
           LEFT JOIN series_entries se
               ON se.series_link_id = b.series_link_id
               AND se.position = b.series_index
           WHERE b.user_id = ?
               AND b.series_link_id = ?
               AND b.series_ignored = 0
           ORDER BY COALESCE(se.position,
               b.series_index)""",
        (user_id, series_link_id),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        book = _row_to_book(r)
        book["hc_position"] = r["hc_position"]
        result.append(book)
    return result


# --- Series link queries ---


def get_series_link(series_name: str) -> dict | None:
    """Find a series_link by name (global, not per-user)."""
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM series_link
           WHERE series_name = ?
           LIMIT 1""",
        (series_name,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def get_series_link_by_id(
    series_link_id: int,
) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM series_link WHERE id = ?",
        (series_link_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def get_or_create_series_link(
    user_id: int, series_name: str
) -> int:
    """Find or create a global series_link, ensure user subscription.

    Catalog sources mix typographic and ASCII apostrophes.  Canonicalizing
    them here prevents one series from being split into duplicate tracks.
    """
    series_name = " ".join(
        series_name.replace("’", "'").replace("‘", "'").split()
    )
    conn = get_db()
    row = conn.execute(
        """SELECT id FROM series_link
           WHERE series_name = ?
           LIMIT 1""",
        (series_name,),
    ).fetchone()
    if row:
        link_id = row[0]
    else:
        cursor = conn.execute(
            "INSERT INTO series_link (series_name)"
            " VALUES (?)",
            (series_name,),
        )
        link_id = cursor.lastrowid

    # Ensure user subscription
    conn.execute(
        """INSERT OR IGNORE INTO user_series
           (user_id, series_link_id)
           VALUES (?, ?)""",
        (user_id, link_id),
    )
    conn.commit()
    conn.close()
    return link_id


def link_series(
    series_link_id: int,
    hc_series_id: int,
    hc_series_name: str,
    data_hash: str | None = None,
    hardcover_slug: str | None = None,
) -> None:
    conn = get_db()
    conn.execute(
        """UPDATE series_link SET
               hardcover_series_id = ?,
               hardcover_series_name = ?,
               hardcover_slug = ?,
               last_checked = datetime('now'),
               data_hash = ?
           WHERE id = ?""",
        (
            hc_series_id, hc_series_name, hardcover_slug,
            data_hash, series_link_id,
        ),
    )
    conn.commit()
    conn.close()


def ensure_book_for_entry(
    conn: sqlite3.Connection,
    user_id: int,
    series_name: str,
    entry: dict,
    series_link_id: int | None = None,
) -> int:
    """Ensure a books row exists for a series entry.

    If entry already has a book_id, returns it. Otherwise checks
    for an existing is_owned=0 book with matching title+series,
    or inserts a new one. Returns the book_id.
    """
    if entry.get("book_id"):
        return entry["book_id"]

    # Check for existing unowned book with same title+series
    row = conn.execute(
        """SELECT id FROM books
           WHERE user_id = ? AND series = ?
               AND title = ? AND is_owned = 0""",
        (user_id, series_name, entry["title"]),
    ).fetchone()
    if row:
        return row[0]

    # Insert new unowned book
    now = datetime.now(timezone.utc).isoformat()
    author = entry.get("author", "Unknown")
    cursor = conn.execute(
        """INSERT INTO books (
            user_id, title, sort_title, authors, author_sort,
            series, series_index, series_link_id,
            description, cover_filename,
            file_path, isbn, goodreads_id, tags, date_added,
            date_finished, rating, reading_status, is_owned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?)""",
        (
            user_id,
            entry["title"],
            make_sort_title(entry["title"]),
            author,
            make_author_sort(author),
            series_name,
            entry.get("position"),
            series_link_id,
            None,  # description
            None,  # cover_filename
            None,  # file_path
            None,  # isbn
            None,  # goodreads_id
            "[]",  # tags
            now,
            None,  # date_finished
            None,  # rating
            "unread",
            0,  # is_owned
        ),
    )
    return cursor.lastrowid


def upsert_series_entries(
    series_link_id: int,
    entries: list[dict],
) -> None:
    """Upsert global series entries, preserving IDs.

    Matches incoming entries to existing ones by
    hardcover_book_id so that entry IDs are stable across
    position changes (protects user_entry_status FKs).
    Only stores global data (no status, no book_id).
    """
    conn = get_db()

    existing = conn.execute(
        """SELECT id, position, hardcover_book_id
           FROM series_entries
           WHERE series_link_id = ?""",
        (series_link_id,),
    ).fetchall()

    existing_by_hc_id: dict[int, int] = {}
    for row in existing:
        hc_id = row["hardcover_book_id"]
        if hc_id:
            existing_by_hc_id[hc_id] = row["id"]

    seen_ids: set[int] = set()
    for entry in entries:
        hc_id = entry.get("hardcover_book_id")
        existing_id = (
            existing_by_hc_id.get(hc_id)
            if hc_id else None
        )

        cover_url = entry.get("cover_url")
        if cover_url == "":
            cover_url = None

        if existing_id:
            seen_ids.add(existing_id)
            conn.execute(
                """UPDATE series_entries
                   SET position = ?, title = ?,
                       author = ?,
                       hardcover_book_id = ?,
                       cover_url = ?
                   WHERE id = ?""",
                (
                    entry["position"],
                    entry["title"],
                    entry.get("author"),
                    hc_id,
                    cover_url,
                    existing_id,
                ),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO series_entries
                   (series_link_id, position, title,
                    author, hardcover_book_id, cover_url)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    series_link_id,
                    entry["position"],
                    entry["title"],
                    entry.get("author"),
                    entry.get("hardcover_book_id"),
                    cover_url,
                ),
            )
            seen_ids.add(cursor.lastrowid)

    # DELETE entries no longer present
    for row in existing:
        if row["id"] not in seen_ids:
            conn.execute(
                "DELETE FROM series_entries WHERE id = ?",
                (row["id"],),
            )

    conn.commit()
    conn.close()


def get_series_entries(series_link_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM series_entries
           WHERE series_link_id = ?
           ORDER BY position""",
        (series_link_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Raw Hardcover data storage ---


def store_hc_series_books(
    series_link_id: int, raw_entries: list[dict]
) -> None:
    """Replace all raw HC entries for a series link."""
    conn = get_db()
    conn.execute(
        "DELETE FROM hc_series_books"
        " WHERE series_link_id = ?",
        (series_link_id,),
    )
    for entry in raw_entries:
        conn.execute(
            """INSERT INTO hc_series_books
               (series_link_id, position, title, author,
                hardcover_book_id, featured, compilation,
                ratings_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                series_link_id,
                entry.get("position"),
                entry["title"],
                entry.get("author"),
                entry.get("hardcover_book_id"),
                1 if entry.get("featured") else 0,
                1 if entry.get("compilation") else 0,
                entry.get("ratings_count", 0),
            ),
        )
    conn.commit()
    conn.close()


def get_hc_series_books(
    series_link_id: int,
) -> list[dict]:
    """Retrieve all raw HC entries for a series link."""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM hc_series_books
           WHERE series_link_id = ?
           ORDER BY position""",
        (series_link_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_series_due_for_refresh(
    max_age_days: int = 30,
    limit: int = 50,
) -> list[dict]:
    """Find series needing a refresh from Hardcover.

    Returns series_link rows that have a hardcover_series_id,
    at least one monitored user_series subscription, and
    last_checked older than max_age_days (or NULL).
    Ordered oldest-first.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT sl.*
           FROM series_link sl
           WHERE sl.hardcover_series_id IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM user_series us
                   WHERE us.series_link_id = sl.id
                       AND us.monitored = 1
               )
               AND (
                   sl.last_checked IS NULL
                   OR sl.last_checked < datetime(
                       'now', ? || ' days')
               )
           ORDER BY sl.last_checked ASC
           LIMIT ?""",
        (str(-max_age_days), limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_monitoring_users(
    series_link_id: int,
) -> list[dict]:
    """Get users monitoring a series.

    Returns list of {user_id, display_name} for users
    with monitored=1 subscriptions to this series.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT us.user_id,
                  COALESCE(us.display_name,
                      sl.series_name) as display_name
           FROM user_series us
           JOIN series_link sl ON sl.id = us.series_link_id
           WHERE us.series_link_id = ?
               AND us.monitored = 1""",
        (series_link_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def touch_series_last_checked(series_link_id: int) -> None:
    """Update last_checked without changing data_hash."""
    conn = get_db()
    conn.execute(
        """UPDATE series_link
           SET last_checked = datetime('now')
           WHERE id = ?""",
        (series_link_id,),
    )
    conn.commit()
    conn.close()


def get_series_entries_with_books(
    series_link_id: int,
    user_id: int,
) -> list[dict]:
    """Get all series entries with per-user book data.

    Returns all entries including ignored ones, with linked
    book details via position-based join and computed
    effective status. Ordered by series_entries.position.
    """
    conn = get_db()
    rows = conn.execute(
        f"""SELECT se.id as entry_id,
                  se.position,
                  se.title as hc_title,
                  se.author as hc_author,
                  {_EFFECTIVE_STATUS} as entry_status,
                  b.id as book_id,
                  b.title as book_title,
                  b.authors as book_authors,
                  b.cover_filename
                      as book_cover_filename,
                  b.cover_updated_at
                      as book_cover_updated_at,
                  b.user_id as book_user_id,
                  b.reading_status
                      as book_reading_status,
                  b.is_owned as book_is_owned,
                  b.rating as book_rating,
                  COALESCE(b.series_ignored, 0)
                      as book_ignored
           FROM series_entries se
           LEFT JOIN books b
               ON b.series_link_id
                   = se.series_link_id
               AND b.series_index = se.position
               AND b.user_id = ?
           LEFT JOIN user_entry_status ues
               ON ues.series_entry_id = se.id
               AND ues.user_id = ?
           WHERE se.series_link_id = ?
           ORDER BY se.position""",
        (user_id, user_id, series_link_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_series_display_name(
    user_id: int,
    series_link_id: int,
    new_name: str,
) -> None:
    """Update per-user display name and user's books."""
    conn = get_db()
    conn.execute(
        """UPDATE user_series SET display_name = ?
           WHERE user_id = ? AND series_link_id = ?""",
        (new_name, user_id, series_link_id),
    )
    # Get affected book IDs before updating
    affected = conn.execute(
        """SELECT id FROM books
           WHERE series_link_id = ? AND user_id = ?
               AND file_path IS NOT NULL""",
        (series_link_id, user_id),
    ).fetchall()
    conn.execute(
        """UPDATE books SET series = ?
           WHERE series_link_id = ? AND user_id = ?""",
        (new_name, series_link_id, user_id),
    )
    conn.commit()
    conn.close()

    for row in affected:
        sync_book_epub(row["id"], user_id)


def update_series_monitored(
    user_id: int,
    series_link_id: int,
    monitored: bool,
) -> None:
    """Set the monitored flag on user's series subscription."""
    conn = get_db()
    conn.execute(
        """UPDATE user_series SET monitored = ?
           WHERE user_id = ? AND series_link_id = ?""",
        (1 if monitored else 0, user_id, series_link_id),
    )
    conn.commit()
    conn.close()


def update_series_complete(
    user_id: int,
    series_link_id: int,
    series_complete: bool,
) -> None:
    """Set the series_complete flag on user's series."""
    conn = get_db()
    conn.execute(
        """UPDATE user_series SET series_complete = ?
           WHERE user_id = ? AND series_link_id = ?""",
        (1 if series_complete else 0, user_id, series_link_id),
    )
    conn.commit()
    conn.close()


def update_user_series_fields(
    user_id: int,
    series_link_id: int,
    fields: dict,
) -> bool:
    """Patch arbitrary fields on user_series. Filters by an allow-list.

    Use this for the rating + favorite + tier toggles on the series view —
    same pattern as db.update_book. Pass {"rating": 5, "is_favorite": True}.
    """
    allowed = {
        "rating", "is_favorite",
        "is_all_time_fav", "is_second_fav", "is_third_fav",
        "monitored", "series_complete", "display_name", "review",
    }
    filtered = {k: v for k, v in fields.items() if k in allowed}
    # Coerce bools to 0/1 for INTEGER columns
    for k in ("is_favorite",
              "is_all_time_fav", "is_second_fav", "is_third_fav",
              "monitored", "series_complete"):
        if k in filtered and isinstance(filtered[k], bool):
            filtered[k] = 1 if filtered[k] else 0
    # Mutual exclusion + favorite implication: setting any one tier
    # clears the other two and forces is_favorite=1.
    if filtered.get("is_all_time_fav"):
        filtered["is_favorite"] = 1
        filtered["is_second_fav"] = 0
        filtered["is_third_fav"] = 0
    if filtered.get("is_second_fav"):
        filtered["is_all_time_fav"] = 0
        filtered["is_third_fav"] = 0
        filtered.setdefault("is_favorite", 1)
    if filtered.get("is_third_fav"):
        filtered["is_all_time_fav"] = 0
        filtered["is_second_fav"] = 0
        filtered.setdefault("is_favorite", 1)
    if not filtered:
        return False
    sets = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [user_id, series_link_id]
    conn = get_db()
    cursor = conn.execute(
        f"UPDATE user_series SET {sets}"
        f" WHERE user_id = ? AND series_link_id = ?",
        values,
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def update_series_entry(
    user_id: int,
    entry_id: int,
    position: float,
    status: str,
) -> None:
    """Update position (global) and status (per-user).

    Position changes update the entry and all books at the
    old position. Status changes upsert/delete per-user
    overrides in user_entry_status.
    """
    conn = get_db()

    # Position update is global
    old = conn.execute(
        """SELECT position, series_link_id
           FROM series_entries WHERE id = ?""",
        (entry_id,),
    ).fetchone()
    if old and old["position"] != position:
        conn.execute(
            "UPDATE series_entries SET position = ?"
            " WHERE id = ?",
            (position, entry_id),
        )
        # Sync all books at old position for this series
        conn.execute(
            """UPDATE books SET series_index = ?
               WHERE series_link_id = ?
                   AND series_index = ?""",
            (position, old["series_link_id"],
             old["position"]),
        )

    # Status: upsert or delete user_entry_status
    default = _compute_default_status(position)
    if status == default:
        # Return to default: remove override
        conn.execute(
            """DELETE FROM user_entry_status
               WHERE user_id = ?
                   AND series_entry_id = ?""",
            (user_id, entry_id),
        )
    else:
        # Set override
        conn.execute(
            """INSERT INTO user_entry_status
                   (user_id, series_entry_id, status)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, series_entry_id)
               DO UPDATE SET status = ?""",
            (user_id, entry_id, status, status),
        )

    conn.commit()
    conn.close()


def get_series_entry_by_id(entry_id: int) -> dict | None:
    """Fetch a single series_entries row by id."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM series_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def insert_series_entry(
    series_link_id: int,
    title: str,
    position: float,
    author: str | None = None,
) -> int:
    """Insert a manual ghost entry into series_entries.

    Used by the owner-only "add ghost entry" UI on series-edit when a
    book belongs to a series that Hardcover doesn't catalog yet (e.g.
    announced future books, niche Royal Road titles).

    Returns the new entry id.
    """
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO series_entries
           (series_link_id, position, title, author,
            hardcover_book_id)
           VALUES (?, ?, ?, ?, NULL)""",
        (series_link_id, position, title, author),
    )
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return new_id


_UNSET: object = object()


def update_series_entry_fields(
    entry_id: int,
    *,
    title: str | None = None,
    position: float | None = None,
    author: object = _UNSET,
) -> bool:
    """Update a series_entries row's mutable fields.

    Any of title/position/author may be omitted; only provided fields
    are written. Pass `author=None` to explicitly clear the field;
    omit it entirely to leave as-is. Unlike update_series_entry (which
    mixes position + per-user status), this is a thin global field
    update for the ghost-entry edit UI. Returns True if a row was
    updated.
    """
    sets: list[str] = []
    values: list = []
    if title is not None:
        sets.append("title = ?")
        values.append(title)
    if position is not None:
        sets.append("position = ?")
        values.append(position)
    if author is not _UNSET:
        sets.append("author = ?")
        values.append(author)
    if not sets:
        return False

    values.append(entry_id)
    conn = get_db()

    # If position changes, sync any books at the old position so the
    # entry-to-book join continues to match.
    if position is not None:
        old = conn.execute(
            """SELECT position, series_link_id
               FROM series_entries WHERE id = ?""",
            (entry_id,),
        ).fetchone()
        if old and old["position"] != position:
            conn.execute(
                """UPDATE books SET series_index = ?
                   WHERE series_link_id = ?
                       AND series_index = ?""",
                (position, old["series_link_id"],
                 old["position"]),
            )

    cursor = conn.execute(
        f"UPDATE series_entries SET {', '.join(sets)}"
        f" WHERE id = ?",
        values,
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def delete_series_entry(entry_id: int) -> bool:
    """Delete a series_entries row.

    `user_entry_status` has ON DELETE CASCADE, so per-user overrides
    follow. Returns True if a row was deleted.
    """
    conn = get_db()
    cursor = conn.execute(
        "DELETE FROM series_entries WHERE id = ?",
        (entry_id,),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


# --- KOReader sync queries ---


def get_book_by_koreader_filename(
    user_id: int, filename: str
) -> dict | None:
    """Fast lookup by stored KOReader filename mapping."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM books"
        " WHERE user_id = ? AND koreader_filename = ?",
        (user_id, filename),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_book(row)


def get_book_by_epub_hash(
    user_id: int, epub_hash: str
) -> dict | None:
    """Fast lookup by EPUB file MD5 hash."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM books"
        " WHERE user_id = ? AND epub_hash = ?",
        (user_id, epub_hash),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_book(row)


def set_koreader_filename(
    book_id: int, user_id: int, filename: str
) -> None:
    """Store the KOReader filename mapping on a book."""
    conn = get_db()
    conn.execute(
        "UPDATE books SET koreader_filename = ?"
        " WHERE id = ? AND user_id = ?",
        (filename, book_id, user_id),
    )
    conn.commit()
    conn.close()




def update_book_sync(
    book_id: int, user_id: int, updates: dict
) -> bool:
    """Update sync-related fields on a book.

    Unlike update_book(), does not trigger EPUB metadata
    sync since reading state is not EPUB metadata.
    """
    allowed = {
        "reading_status", "progress", "rating",
        "date_finished", "sync_updated_at",
        "sync_version",
    }
    filtered = {
        k: v for k, v in updates.items() if k in allowed
    }
    if not filtered:
        return False

    sets = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values())
    values.extend([book_id, user_id])

    conn = get_db()
    cursor = conn.execute(
        f"UPDATE books SET {sets}"
        f" WHERE id = ? AND user_id = ?",
        values,
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def prune_empty_series_links() -> int:
    """Drop series_link rows no user has any *engagement* with.

    "Engagement" = the row is referenced by at least one book that is
    owned OR read/reading. The original predicate required is_owned=1
    only, which nuked series where every book was a library/borrowed
    read (is_owned=0, reading_status='read') — a real pattern that
    cost ~50 series-links the first time the sweep ran.

    Cascade still handles user_series + series_entries.
    """
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM series_link"
        " WHERE id NOT IN ("
        "    SELECT DISTINCT series_link_id FROM books"
        "    WHERE series_link_id IS NOT NULL"
        "      AND (is_owned = 1 OR reading_status IN ('read', 'reading'))"
        " )"
    )
    pruned = cur.rowcount
    conn.commit()
    conn.close()
    return pruned
