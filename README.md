# Books

Personal ebook library webapp for managing your EPUB collection. Multi-user support with per-user libraries, reading progress tracking, series management, and e-reader integration.

## Features

- Browse books with covers in a responsive grid
- Track reading status (unread/reading/read), dates, and ratings (half-star increments)
- Group and browse by series with position tracking
- Search and filter by title, author, status, rating, favorites
- Upload EPUBs with automatic metadata extraction
- Fetch metadata from Google Books, Open Library, and Hardcover.app
- Send books to Kindle via email
- OPDS catalog for e-reader apps (KOReader, Moon+ Reader, etc.)
- KOReader plugin for two-way reading progress sync (Kobo and Android)
- Series metadata integration with Hardcover.app (auto-refresh)
- Per-user libraries with Google OAuth (web) and HTTP Basic Auth (OPDS/devices)

## Stack

- **Backend**: FastAPI + SQLite (raw sqlite3, WAL mode)
- **Frontend**: TypeScript SPA + Bootstrap 5 (esbuild bundled)
- **Serving**: nginx (static files + covers) + gunicorn/uvicorn (API)
- **Auth**: Google OAuth (web UI), HTTP Basic Auth (OPDS, KOReader plugin)

## Prerequisites

- Python 3.13+
- Node.js (for esbuild)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Docker and Docker Compose (for deployment)

## Quick Start

```bash
# Clone and install
git clone <repo-url> && cd books
uv sync
cd books/ui && npm install && cd ../..

# Configure
cp .env.example .env
# Edit .env -- at minimum set BOOKS_GOOGLE_CLIENT_ID (see Google OAuth Setup below)

# Create data directory
mkdir -p data/covers data/files

# Create users.json (maps Google email to local username)
echo '{"you@gmail.com": "yourname"}' > data/users.json

# Create your user in the database
python scripts/set_password.py yourname
# (This will prompt you to set a password for OPDS/device access)

# Start dev servers
uv run uvicorn books.main:app --reload --port 8000
# In another terminal:
cd books/ui && npx esbuild src/main.ts --bundle --outfile=dist/app.js --watch --sourcemap
```

Note: Before `set_password.py` works, the database needs a user row. On first API startup, the DB schema is created automatically. You can create users by inserting into the `users` table:

```sql
INSERT INTO users (username, display_name, password_hash) VALUES ('yourname', 'Your Name', '');
```

Then use `scripts/set_password.py yourname` to set the password.

## Configuration

All configuration is via environment variables in `.env`. See `.env.example` for the full list with descriptions.

| Variable | Required | Description |
|----------|----------|-------------|
| `BOOKS_GOOGLE_CLIENT_ID` | Yes | Google OAuth Client ID for web login |
| `BOOKS_DATA_DIR` | No | Data directory path (default: `./data` dev, `/app/data` Docker) |
| `GOOGLE_BOOKS_API_KEY` | No | Google Books API key for metadata search |
| `HARDCOVER_API_TOKEN` | No | Hardcover.app API token for series metadata |
| `smtp_host` | No | SMTP server for Kindle email sending |
| `smtp_port` | No | SMTP port (default: 25) |
| `smtp_from` | No | From address for Kindle emails |
| `BOOKS_SERVER_URL` | No | Server URL for plugin publishing |
| `BOOKS_DEPLOY_USER` | No | Basic Auth user for plugin publish endpoint |
| `BOOKS_DEPLOY_PASS` | No | Basic Auth password for plugin publish endpoint |

### Data Directory

All runtime data lives under `$BOOKS_DATA_DIR`:

```
data/
  books.db          # SQLite database
  users.json        # Google email -> username mapping
  covers/{user_id}/ # Book cover images (JPEG)
  files/{user_id}/  # EPUB files
  plugin/           # Published KOReader plugin files
```

## Google OAuth Setup

The web UI uses Google Sign-In for authentication.

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Navigate to **APIs & Services > Credentials**
4. Click **Create Credentials > OAuth 2.0 Client ID**
5. Application type: **Web application**
6. Add your domain to **Authorized JavaScript origins** (e.g., `https://books.example.com` and `http://localhost:8000` for dev)
7. Copy the Client ID and set `BOOKS_GOOGLE_CLIENT_ID` in `.env`

### User Registration

Users are not self-service. To add a user:

1. Insert a row in the `users` table (happens automatically on first API start):
   ```sql
   INSERT INTO users (username, display_name, password_hash)
   VALUES ('username', 'Display Name', '');
   ```
2. Map their Google email in `$BOOKS_DATA_DIR/users.json`:
   ```json
   {"their.email@gmail.com": "username"}
   ```
3. Set their password (for OPDS/device access):
   ```bash
   python scripts/set_password.py username
   ```

## OPDS Access

The app serves an [OPDS](https://opds.io/) catalog at `/opds/` for browsing and downloading books from e-reader apps.

**Supported clients**: KOReader (Kobo, Android), Moon+ Reader, ReadEra, Thorium, or any OPDS 1.2 compatible app.

**Setup in your OPDS client:**

- Catalog URL: `https://your-server/opds/`
- Authentication: HTTP Basic Auth
- Username: your username
- Password: set via `scripts/set_password.py`

**Features:**

- Browse all books, by series, by author
- Filter by reading status, rating, favorites
- Alphabetical sub-browsing (A-Z) for large collections
- Full-text search
- Direct EPUB download

## KOReader Plugin

The included KOReader plugin syncs reading progress, status, and ratings between your devices and the server.

**What it syncs:**

- Reading progress (percentage, forward-only -- keeps the highest value)
- Reading status (unread, reading, read)
- Ratings (1-5 stars)
- "Continue from" prompt when another device is further ahead

**Book matching:** Uses EPUB content hash (MD5) for cross-device matching, with filename and fuzzy title/author fallback.

### Installation on Kobo

1. Ensure [KOReader](https://koreader.rocks/) is installed on your Kobo
2. Connect Kobo via USB or SSH
3. Copy the plugin directory:
   ```bash
   cp -r koreader/plugins/booksync.koplugin /path/to/kobo/.adds/koreader/plugins/
   ```
4. Restart KOReader
5. Open the plugin menu: **Top menu > Settings (gear icon) > BookSync**
6. Configure server URL (e.g., `https://books.example.com`) and credentials

### Installation on Android

1. Install [KOReader](https://koreader.rocks/) from F-Droid or GitHub releases
2. Copy the `booksync.koplugin` folder to your KOReader plugins directory (typically `/storage/emulated/0/koreader/plugins/`)
3. Configure the same way as Kobo

The plugin handles Android-specific limitations (synchronous HTTP instead of forked subprocesses, shorter timeouts).

### Plugin Auto-Updates

Once installed, the plugin checks for updates from the server. Admins can publish new versions with:

```bash
./scripts/publish_plugin.sh
```

This requires `BOOKS_SERVER_URL`, `BOOKS_DEPLOY_USER`, and `BOOKS_DEPLOY_PASS` in `.env`, and the user must have `is_superuser` set in the database.

## Kindle Email

Send books directly to your Kindle device via email.

1. Configure SMTP in `.env` (`smtp_host`, `smtp_port`, `smtp_from`). Uses unauthenticated SMTP -- configure your mail server to allow relay from the app server.
2. Set your Kindle email address in the web UI (user settings).
3. Add `smtp_from` address to your [Amazon Approved Senders list](https://www.amazon.com/hz/mycd/myx#/home/settings/payment).
4. Click "Send to Kindle" on any book detail page.

## Development

```bash
# Install Python dependencies
uv sync

# Run API dev server (auto-reload)
uv run uvicorn books.main:app --reload --port 8000

# Build/watch frontend (in another terminal)
cd books/ui && npx esbuild src/main.ts --bundle --outfile=dist/app.js --watch --sourcemap
```

### Project Structure

```
books/
  main.py              # FastAPI app, lifespan, middleware
  routes/              # API route handlers
    auth.py            # Google OAuth + Basic Auth
    books.py           # Book CRUD, upload, metadata
    series.py          # Series management
    kindle.py          # Send to Kindle
    kobo.py            # KOReader sync + plugin publishing
    opds.py            # OPDS catalog feeds
  helpers/
    db.py              # SQLite schema, queries
    auth.py            # Auth dependencies
    email.py           # SMTP for Kindle
    metadata.py        # EPUB extraction, Google Books, Open Library
    hardcover.py       # Hardcover.app GraphQL client
    refresh.py         # Background series auto-refresh
  ui/
    index.html         # SPA shell
    style.css
    src/
      main.ts          # Entry point + router
      auth.ts          # Google Sign-In integration
      api.ts           # API client with auth
      pages/           # Page components
      components/      # Reusable UI components
koreader/
  plugins/booksync.koplugin/   # KOReader sync plugin (Lua)
scripts/
  set_password.py      # Set user password for OPDS/devices
  publish_plugin.sh    # Publish plugin to server
  import_calibre.py    # One-time Calibre library import
```

## Deployment

The app deploys as two Docker containers behind nginx.

### Docker Compose

```bash
# Build and start
docker compose build
docker compose up -d
```

The default port is `60022`. Change it in `docker-compose.yml` if needed.

**Services:**

- **nginx** (Alpine) - serves static frontend, proxies `/api/` and `/opds/` to the API, serves cover images with 7-day cache
- **api** (Python) - gunicorn with uvicorn workers, built with uv

### deploy.sh

The included `deploy.sh` script builds the frontend, syncs files to a deployment directory, and restarts containers:

```bash
# Uses project root by default, override with env vars:
PROJ=/path/to/source DEST=/path/to/deploy ./deploy.sh
```

### Production Notes

- Use a reverse proxy (nginx, Caddy) in front for HTTPS termination
- The SQLite database uses WAL mode for concurrent reads
- Max upload size is 50MB (configured in nginx)
- Cover images are cached for 7 days by nginx

## Series Management

Books can be grouped into series with decimal position tracking (e.g., 1, 1.5, 2).

### Hardcover.app Integration

If you have a [Hardcover.app](https://hardcover.app/) account and set `HARDCOVER_API_TOKEN`:

- Link series to Hardcover for metadata (book positions, missing books in series)
- Auto-refresh runs daily, checking series not updated in 30 days
- Unowned books appear as placeholders in your series view
- Per-user series customization (display names, monitored/complete status)

## Calibre Import

One-time import from existing Calibre libraries:

```bash
python scripts/import_calibre.py --db /path/to/calibre/metadata.db
```

## Deployed as Library

This codebase is deployed publicly as **Library** at <https://library.gordongouger.com> behind Authelia SSO. Anonymous visitors can browse the collection read-only; mutations + file downloads require login. The production wiring lives in [gogouger/infra](https://github.com/gogouger/infra).

## License

[MIT](LICENSE). Borrow, fork, ship your own. Personal reading data is yours alone — none of it is in this repo.
