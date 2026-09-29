import { getToken, clearAuth, showLoginPrompt } from './auth';

const API_BASE = '/api';

export async function apiFetch(
    path: string,
    options: RequestInit = {}
): Promise<any> {
    const token = getToken();
    const headers: Record<string, string> = {
        ...(options.headers as Record<string, string> || {}),
    };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    if (!(options.body instanceof FormData)) {
        headers['Content-Type'] = 'application/json';
    }

    const resp = await fetch(`${API_BASE}${path}`, {
        ...options,
        cache: 'no-store',
        headers,
    });

    if (resp.status === 401) {
        const refreshed = await showLoginPrompt();
        if (!refreshed) {
            clearAuth();
            // Do NOT redirect to "/" — Caddy redirects "/" back to "/{username}/"
            // which re-triggers this 401 path on anonymous endpoints. Loop.
            throw new Error('Not authenticated');
        }
        // Retry with new token
        const newToken = getToken();
        if (newToken) {
            headers['Authorization'] = `Bearer ${newToken}`;
        }
        const retry = await fetch(`${API_BASE}${path}`, { ...options, cache: 'no-store', headers });
        if (!retry.ok) {
            throw new Error('Request failed after re-authentication');
        }
        return retry.json();
    }

    if (resp.status === 403) {
        throw new Error('Not authorized');
    }

    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || 'Request failed');
    }

    return resp.json();
}

async function apiFetchRaw(
    path: string,
    options: RequestInit = {}
): Promise<Response> {
    const token = getToken();
    const headers: Record<string, string> = {
        ...(options.headers as Record<string, string> || {}),
    };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }

    const resp = await fetch(`${API_BASE}${path}`, {
        ...options,
        cache: 'no-store',
        headers,
    });

    if (resp.status === 401) {
        const refreshed = await showLoginPrompt();
        if (!refreshed) {
            clearAuth();
            // Do NOT redirect to "/" — Caddy redirects "/" back to "/{username}/"
            // which re-triggers this 401 path on anonymous endpoints. Loop.
            throw new Error('Not authenticated');
        }
        // Retry with new token
        const newToken = getToken();
        if (newToken) {
            headers['Authorization'] = `Bearer ${newToken}`;
        }
        const retry = await fetch(`${API_BASE}${path}`, { ...options, cache: 'no-store', headers });
        if (!retry.ok) {
            throw new Error('Request failed after re-authentication');
        }
        return retry;
    }

    if (!resp.ok) {
        throw new Error(`Download failed: ${resp.statusText}`);
    }

    return resp;
}

export const api = {
    async getMe(): Promise<any> {
        return apiFetch('/auth/me');
    },

    async updateKindleEmail(email: string): Promise<any> {
        return apiFetch('/auth/me/kindle', {
            method: 'PATCH',
            body: JSON.stringify({ kindle_email: email }),
        });
    },

    async getBooks(username: string, params: Record<string, any> = {}): Promise<any> {
        const qs = new URLSearchParams();
        for (const [k, v] of Object.entries(params)) {
            if (v !== undefined && v !== null && v !== '') {
                qs.set(k, String(v));
            }
        }
        return apiFetch(`/${username}/books?${qs.toString()}`);
    },

    async getBook(username: string, id: number): Promise<any> {
        return apiFetch(`/${username}/books/${id}`);
    },

    async updateBook(username: string, id: number, updates: Record<string, any>): Promise<any> {
        return apiFetch(`/${username}/books/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(updates),
        });
    },

    async sendToKindle(username: string, bookId: number, email?: string): Promise<any> {
        return apiFetch(`/${username}/books/${bookId}/kindle`, {
            method: 'POST',
            body: JSON.stringify(email ? { email } : {}),
        });
    },

    async getSeries(username: string, includeUnmonitored: boolean = false): Promise<any> {
        const qs = includeUnmonitored ? '?include_unmonitored=true' : '';
        return apiFetch(`/${username}/series${qs}`);
    },

    async getSeriesAutocomplete(username: string): Promise<any> {
        return apiFetch(`/${username}/series/autocomplete`);
    },

    async getSeriesBooks(username: string, id: number): Promise<any> {
        return apiFetch(`/${username}/series/${id}`);
    },

    async getSeriesEdit(username: string, id: number): Promise<any> {
        return apiFetch(`/${username}/series/${id}/edit`);
    },

    async updateSeries(username: string, id: number, data: Record<string, any>): Promise<any> {
        return apiFetch(`/${username}/series/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    },

    async refreshSeries(username: string, id: number): Promise<any> {
        return apiFetch(`/${username}/series/${id}/refresh`, {
            method: 'POST',
        });
    },

    async addSeriesEntry(
        username: string,
        seriesLinkId: number,
        data: { title: string; position: number; author?: string | null },
    ): Promise<any> {
        return apiFetch(`/${username}/series/${seriesLinkId}/entries`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    async updateSeriesEntry(
        username: string,
        entryId: number,
        data: { title?: string; position?: number; author?: string | null },
    ): Promise<any> {
        return apiFetch(`/${username}/series/entries/${entryId}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    },

    async deleteSeriesEntry(
        username: string,
        entryId: number,
    ): Promise<any> {
        return apiFetch(`/${username}/series/entries/${entryId}`, {
            method: 'DELETE',
        });
    },

    async refreshMetadata(username: string, bookId: number): Promise<any> {
        return apiFetch(`/${username}/books/${bookId}/refresh-metadata`, {
            method: 'POST',
        });
    },

    async setCoverFromUrl(username: string, bookId: number, url: string): Promise<any> {
        return apiFetch(`/${username}/books/${bookId}/cover-from-url`, {
            method: 'POST',
            body: JSON.stringify({ url }),
        });
    },

    async uploadCover(
        username: string,
        bookId: number,
        file: File,
    ): Promise<any> {
        const formData = new FormData();
        formData.append('file', file);
        return apiFetch(`/${username}/books/${bookId}/cover`, {
            method: 'POST',
            body: formData,
        });
    },

    async previewMetadata(username: string, file: File): Promise<any> {
        const formData = new FormData();
        formData.append('file', file);
        const token = getToken();
        const headers: Record<string, string> = {};
        if (token) headers['Authorization'] = `Bearer ${token}`;
        const resp = await fetch(`${API_BASE}/${username}/metadata/preview`, {
            method: 'POST',
            headers,
            body: formData,
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || 'Preview failed');
        }
        return resp.json();
    },

    async searchAllMetadata(username: string, title: string, authors: string): Promise<any> {
        return apiFetch(`/${username}/metadata/search-all`, {
            method: 'POST',
            body: JSON.stringify({ title, authors }),
        });
    },

    async addBookFromPreview(username: string, data: Record<string, any>): Promise<any> {
        const resp = await apiFetch(`/${username}/books/from-preview`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
        return resp;
    },

    async addBookFromPreviewRaw(username: string, data: Record<string, any>): Promise<Response> {
        const token = getToken();
        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
        };
        if (token) headers['Authorization'] = `Bearer ${token}`;
        return fetch(`${API_BASE}/${username}/books/from-preview`, {
            method: 'POST',
            headers,
            body: JSON.stringify(data),
        });
    },

    async copyToTemp(sourceUsername: string, bookId: number): Promise<any> {
        return apiFetch(`/${sourceUsername}/books/${bookId}/copy-to-temp`, {
            method: 'POST',
        });
    },

    async copySeriesFromLibrary(
        sourceUsername: string,
        seriesLinkId: number,
    ): Promise<any> {
        return apiFetch(`/${sourceUsername}/series/${seriesLinkId}/copy-to-library`, {
            method: 'POST',
        });
    },

    async getMetrics(username: string): Promise<any> {
        return apiFetch(`/${username}/metrics`);
    },

    async autoPriceLibrary(username: string): Promise<any> {
        return apiFetch(`/${username}/metrics/auto-price`, {
            method: 'POST',
        });
    },

    async autoTagsLibrary(username: string): Promise<any> {
        return apiFetch(`/${username}/metrics/auto-tags`, {
            method: 'POST',
        });
    },

    async autoLengthLibrary(username: string): Promise<any> {
        return apiFetch(`/${username}/metrics/auto-length`, {
            method: 'POST',
        });
    },

    async getRecommendations(username: string): Promise<any> {
        return apiFetch(`/${username}/recommendations`);
    },

    async dismissRecommendation(
        username: string, hcBookId: number,
    ): Promise<any> {
        return apiFetch(`/${username}/recommendations/dismiss`, {
            method: 'POST',
            body: JSON.stringify({ hc_book_id: hcBookId }),
        });
    },

    async addRecommendationToLibrary(
        username: string, hcBookId: number,
        opts?: {
            rating?: number;
            is_favorite?: boolean;
            is_owned?: number;
            reading_status?: string;
        },
    ): Promise<any> {
        return apiFetch(`/${username}/recommendations/add-from-hc`, {
            method: 'POST',
            body: JSON.stringify({
                hc_book_id: hcBookId,
                ...(opts || {}),
            }),
        });
    },

    async refreshRecommendations(username: string): Promise<any> {
        return apiFetch(`/${username}/recommendations/refresh`, {
            method: 'POST',
        });
    },

    async deleteBook(username: string, bookId: number): Promise<any> {
        return apiFetch(`/${username}/books/${bookId}`, {
            method: 'DELETE',
        });
    },

    async searchMetadata(username: string, query: string, source: string = 'google'): Promise<any> {
        return apiFetch(`/${username}/metadata/search`, {
            method: 'POST',
            body: JSON.stringify({ query, source }),
        });
    },

    async uploadBook(
        username: string,
        file: File,
        metadata?: Record<string, any>,
        options?: { merge_with?: number; force?: boolean },
    ): Promise<any> {
        const formData = new FormData();
        formData.append('file', file);
        const qs = new URLSearchParams();
        if (metadata) {
            for (const [k, v] of Object.entries(metadata)) {
                if (v !== undefined && v !== null && v !== '') {
                    qs.set(k, String(v));
                }
            }
        }
        if (options?.merge_with !== undefined) {
            qs.set('merge_with', String(options.merge_with));
        }
        if (options?.force) {
            qs.set('force', 'true');
        }
        const token = getToken();
        const headers: Record<string, string> = {};
        if (token) headers['Authorization'] = `Bearer ${token}`;

        const resp = await fetch(`${API_BASE}/${username}/books?${qs.toString()}`, {
            method: 'POST',
            headers,
            body: formData,
        });
        if (resp.status === 409) {
            return resp.json();
        }
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || 'Upload failed');
        }
        return resp.json();
    },

    coverUrl(userId: number, coverFilename: string, coverUpdatedAt?: string | null): string {
        const url = `/covers/${userId}/${coverFilename}`;
        return coverUpdatedAt ? `${url}?v=${coverUpdatedAt}` : url;
    },

    async downloadFile(username: string, bookId: number, title: string): Promise<void> {
        const resp = await apiFetchRaw(`/${username}/books/${bookId}/file`);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${title}.epub`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    },
};
