import { api } from '../api';
import { bookStarsHtml } from './star-helpers';

// Minimalist book card. Same visual vocabulary as the /recommendations
// cards: no card chrome, cover is the box, type carries the rest. Stars
// and heart are inline-interactive — click a star to rate-as-read, click
// the heart to toggle favourite. Click anywhere else on the card to open
// the detail page.
export function bookCardHtml(book: any): string {
    if (book.is_ghost) return ghostCardHtml(book);

    const coverImg = book.cover_filename
        ? `<img src="${api.coverUrl(book.user_id, book.cover_filename, book.cover_updated_at)}"
               alt="${escapeHtml(book.title)}" loading="lazy"
               onerror="this.outerHTML='&lt;div class=&quot;no-cover&quot;&gt;&lt;i class=&quot;bi bi-book&quot;&gt;&lt;/i&gt;&lt;/div&gt;'">`
        : book.cover_url
        ? `<img src="${escapeAttr(book.cover_url)}"
               alt="${escapeHtml(book.title)}" loading="lazy"
               referrerpolicy="no-referrer"
               onerror="this.outerHTML='&lt;div class=&quot;no-cover&quot;&gt;&lt;i class=&quot;bi bi-book&quot;&gt;&lt;/i&gt;&lt;/div&gt;'">`
        : `<div class="no-cover"><i class="bi bi-book"></i></div>`;

    const seriesText = book.series
        ? `${escapeHtml(book.series)}${book.series_index ? ` #${book.series_index}` : ''}`
        : '';

    // Stamp = small sea-green caps with a hairline tail-rule. Prefer
    // series name when present (covers the common case); fall back to
    // the manual category. Linkified when we have a series_link_id.
    let stampHtml = '';
    if (book.series) {
        stampHtml = book.series_link_id
            ? `<a class="card-stamp card-series-link" href="#/series/${book.series_link_id}">${seriesText}</a>`
            : `<span class="card-stamp">${seriesText}</span>`;
    } else if (book.manual_category) {
        stampHtml = `<span class="card-stamp">${escapeHtml(book.manual_category)}</span>`;
    }

    const ownedClass = book.is_owned === 0 ? ' not-owned' : '';
    const allTimeClass = book.is_all_time_fav === 1
        ? ' all-time-fav'
        : book.is_second_fav === 1 ? ' second-fav'
        : book.is_third_fav === 1 ? ' third-fav' : '';

    const formatBadge = formatBadgesHtml(book);
    const tierCrown = tierCrownHtml(book);

    // Cover status accent — thin horizontal bar pinned to the bottom of
    // the cover that replaces the old side gutter.
    const statusClass = book.reading_status === 'read'
        ? 'status-read'
        : book.reading_status === 'reading'
            ? 'status-reading'
            : 'status-unread';
    const statusBar = `<div class="cover-status ${statusClass}"></div>`;

    const progressBar = book.reading_status === 'reading' && book.progress
        ? `<div class="card-progress"><div class="card-progress-fill" style="width:${(book.progress * 100).toFixed(1)}%"></div></div>`
        : '';

    // Reading progress and ownership are independent facts. Display both so
    // a borrowed/read book never looks like an unread catalog placeholder.
    let readingBadge = '';
    if (book.reading_status === 'read') {
        readingBadge = '<span class="card-status status-read"><i class="bi bi-check-circle-fill"></i> Read</span>';
    } else if (book.reading_status === 'reading') {
        const pct = book.progress ? ` ${Math.round(book.progress * 100)}%` : '';
        readingBadge = `<span class="card-status status-reading"><i class="bi bi-book-half"></i> Reading${pct}</span>`;
    }
    const ownershipBadge = book.is_owned === 0
        ? '<span class="card-status status-not-owned"><i class="bi bi-bookmark-x"></i> Not owned</span>'
        : '<span class="card-status status-owned"><i class="bi bi-bookmark-check"></i> Owned</span>';
    const statusBadges = `<div class="card-statuses">${readingBadge}${ownershipBadge}</div>`;

    const rating = Number(book.rating) || 0;
    const favOn = book.is_favorite ? ' on' : '';
    const stars = bookStarsHtml(rating);

    return `
        <div class="book-card${ownedClass}${allTimeClass}"
             data-book-id="${book.id}"
             data-rating="${rating}"
             data-favorite="${book.is_favorite ? 1 : 0}"
             role="button">
            <div class="cover-container">
                ${coverImg}
                ${tierCrown}
                ${formatBadge}
                ${progressBar}
                ${statusBar}
            </div>
            <div class="card-body">
                ${stampHtml}
                <div class="card-title">${escapeHtml(book.title)}</div>
                <div class="card-author">${authorsHtml(book.authors)}</div>
                <div class="card-bottom">
                    ${statusBadges}
                    <div class="card-meta-row">
                        <div class="card-stars" data-rating="${rating}">${stars}</div>
                        <button class="card-heart${favOn}" data-action="heart"
                                type="button" aria-label="Favourite">♥</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// Ghost: a series_entries row the user doesn't have a book for. Same
// minimalist shell as a real card; cover is a dashed-outline rectangle,
// "Don't own" stamp below the title. Click the cover/title → /add prefilled.
export function ghostCardHtml(book: any): string {
    const seriesText = book.series
        ? `${escapeHtml(book.series)}${book.series_index ? ` #${book.series_index}` : ''}`
        : '';
    const stampHtml = book.series
        ? (book.series_link_id
            ? `<a class="card-stamp card-series-link" href="#/series/${book.series_link_id}">${seriesText}</a>`
            : `<span class="card-stamp">${seriesText}</span>`)
        : '';

    const params = new URLSearchParams();
    if (book.title) params.set('title', book.title);
    if (book.authors) params.set('authors', book.authors);
    if (book.series) params.set('series', book.series);
    if (book.series_index != null) {
        params.set('series_index', String(book.series_index));
    }
    const addHref = `#/add?${params.toString()}`;

    const coverInner = book.cover_url
        ? `<img src="${escapeAttr(book.cover_url)}"
                alt="${escapeHtml(book.title || '')}" loading="lazy"
                onerror="this.outerHTML='&lt;div class=&quot;no-cover&quot;&gt;&lt;i class=&quot;bi bi-plus-circle&quot;&gt;&lt;/i&gt;&lt;/div&gt;'">`
        : `<div class="no-cover"><i class="bi bi-plus-circle"></i></div>`;

    return `
        <div class="book-card ghost-card"
             data-add-href="${escapeAttr(addHref)}"
             role="button"
             title="Add to library">
            <div class="cover-container ghost-cover">
                ${coverInner}
                <span class="ghost-badge">Don&rsquo;t own</span>
            </div>
            <div class="card-body">
                ${stampHtml}
                <div class="card-title">${escapeHtml(book.title || '')}</div>
                <div class="card-author">${escapeHtml(book.authors || '')}</div>
            </div>
        </div>
    `;
}

// Crown / wreath badge on the cover top-left. Gold = #1 all-time
// (is_all_time_fav), silver = #2 (is_second_fav), bronze = #3 (is_third_fav).
// Distinct icon per tier so the rank is unmistakable at a glance.
export function tierCrownHtml(b: any): string {
    if (b.is_all_time_fav === 1) {
        return '<span class="cover-tier-crown tier-gold" title="#1 all-time favorite"><i class="bi bi-crown-fill"></i></span>';
    }
    if (b.is_second_fav === 1) {
        return '<span class="cover-tier-crown tier-silver" title="#2 all-time favorite"><i class="bi bi-crown"></i></span>';
    }
    if (b.is_third_fav === 1) {
        return '<span class="cover-tier-crown tier-bronze" title="#3 all-time favorite"><i class="bi bi-award-fill"></i></span>';
    }
    return '';
}

// Back-compat alias — older call sites expect this name. The crown is the
// new visual but the function still answers "is this an all-time fav badge?".
export function allTimeFavBadgeHtml(b: any): string {
    return tierCrownHtml(b);
}

// Format pip on the cover corner.
export function formatBadgeHtml(format: string | undefined | null): string {
    if (format === 'physical') {
        return '<span class="cover-format-badge fmt-physical" title="Physical"><i class="bi bi-book-half"></i></span>';
    }
    if (format === 'audiobook') {
        return '<span class="cover-format-badge fmt-audiobook" title="Audiobook"><i class="bi bi-headphones"></i></span>';
    }
    if (format === 'ebook') {
        return '<span class="cover-format-badge fmt-ebook" title="Ebook"><i class="bi bi-tablet"></i></span>';
    }
    return '';
}

export function formatBadgesHtml(book: any): string {
    const primary = formatBadgeHtml(book.book_format);
    if (book.also_physical === 1 && book.book_format !== 'physical') {
        const physical = formatBadgeHtml('physical');
        return `<span class="cover-format-badges">${primary}${physical}</span>`;
    }
    return primary;
}

// Books whose series position is fractional (novellas/short stories) are
// noise in series views unless the user has read them.
export function isHiddenShortStory(b: any): boolean {
    const raw = b.series_index ?? b.hc_position ?? b.position;
    if (raw == null) return false;
    const idx = Number(raw);
    if (!Number.isFinite(idx) || Number.isInteger(idx)) return false;
    return b.reading_status !== 'read';
}

function escapeAttr(text: string): string {
    return escapeHtml(text).replace(/"/g, '&quot;');
}

function authorsHtml(authors: string): string {
    return authors.split(',').map(a => {
        const trimmed = a.trim();
        return `<span class="card-author-link" data-author="${escapeHtml(trimmed)}">${escapeHtml(trimmed)}</span>`;
    }).join(', ');
}

function escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
