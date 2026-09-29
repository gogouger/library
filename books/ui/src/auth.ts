const LEGACY_TOKEN_KEY = 'books_id_token';
const LEGACY_EMAIL_HINT_KEY = 'books_email_hint';

function centralLoginUrl(): string {
    return `https://auth.gordongouger.com/?rd=${encodeURIComponent(window.location.href)}`;
}

// Compatibility shim for API helpers while the retired Google-token path is
// removed. Production identity comes only from Authelia through Caddy.
export function getToken(): null {
    return null;
}

export function clearAuth(): void {
    localStorage.removeItem(LEGACY_TOKEN_KEY);
    localStorage.removeItem(LEGACY_EMAIL_HINT_KEY);
    localStorage.removeItem('books_user');
}

export function getUser(): any | null {
    const raw = localStorage.getItem('books_user');
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { return null; }
}

export function setUser(user: any): void {
    if (user && typeof user.display_name === 'string' && user.display_name.indexOf('{') !== -1) {
        user.display_name = user.username || 'me';
    }
    localStorage.setItem('books_user', JSON.stringify(user));
}

export async function fetchMe(): Promise<any | null> {
    try {
        const resp = await fetch('/api/auth/me', { credentials: 'include' });
        if (!resp.ok) return null;
        return await resp.json();
    } catch {
        return null;
    }
}

export async function showLoginPrompt(): Promise<boolean> {
    window.location.assign(centralLoginUrl());
    return false;
}

export function bootstrapAuth(): void {
    // Do not leave a retired Google credential available to browser code.
    localStorage.removeItem(LEGACY_TOKEN_KEY);
    localStorage.removeItem(LEGACY_EMAIL_HINT_KEY);
}
