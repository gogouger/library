import { getUser, clearAuth } from '../auth';
import { getLibraryUsername } from '../context';

export function renderLogin(): void {
    const app = document.getElementById('app')!;
    app.innerHTML = `
        <div class="login-container">
            <div class="card shadow-sm">
                <div class="card-body p-4">
                    <h3 class="text-center mb-4">
                        <i class="bi bi-bookshelf"></i> Library
                    </h3>
                    <a class="btn btn-primary w-100" href="https://auth.gordongouger.com/?rd=${encodeURIComponent(window.location.href)}">
                        Owner sign in
                    </a>
                </div>
            </div>
        </div>
    `;
}

export function updateNavbar(): void {
    const user = getUser();
    const libraryUser = getLibraryUsername();

    const navLinks = document.getElementById('nav-links')!;
    const navUser = document.getElementById('nav-user')!;
    const navUsername = document.getElementById('nav-username')!;
    const navLogout = document.getElementById('nav-logout') as HTMLAnchorElement;
    const navSignin = document.getElementById('nav-signin') as HTMLAnchorElement;
    const navMyLibrary = document.getElementById('nav-my-library') as HTMLAnchorElement;
    const navAddItem = document.getElementById('nav-add-item')!;
    // Scan was removed from the nav (collapsed into the Add Book page CTA).
    // Tolerate either DOM shape so any stale cached index.html still works.
    const navScanItem = document.getElementById('nav-scan-item');
    const navRecsItem = document.getElementById('nav-recs-item')!;
    const navMetricsItem = document.getElementById('nav-metrics-item')!;

    // Reset visibility
    navLinks.style.display = 'none';
    navUser.style.display = 'none';
    navAddItem.style.display = 'none';
    if (navScanItem) navScanItem.style.display = 'none';
    navRecsItem.style.display = 'none';
    navMetricsItem.style.display = 'none';
    navUsername.textContent = '';
    navLogout.classList.add('d-none');
    navSignin.classList.add('d-none');
    navMyLibrary.classList.add('d-none');

    if (!libraryUser) {
        // Root "/" -- login page
        if (user) {
            // Logged in but at root -- show minimal nav
            navUser.style.display = '';
            navUsername.textContent = user.display_name;
            navLogout.classList.remove('d-none');
            navLogout.onclick = (e) => {
                e.preventDefault();
                fetch('/__authlogout', {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                    body: '{}',
                }).catch(() => {}).finally(() => {
                    clearAuth();
                    window.location.href = '/';
                });
            };
        }
        // Not logged in at root: hide everything (login page handles itself)
        return;
    }

    // We're on a library page "/{username}/..."
    navLinks.style.display = '';
    navUser.style.display = '';

    // Metrics is publicly viewable — anonymous visitors see the
    // non-money sections. The backend strips price/spend before
    // returning the payload.
    navMetricsItem.style.display = '';

    const isOwner = user && (user.username === libraryUser || user.is_superuser);

    if (isOwner) {
        // Owner or superuser: full controls
        navAddItem.style.display = '';
        if (navScanItem) navScanItem.style.display = '';
        navRecsItem.style.display = '';
        navMetricsItem.style.display = '';
        navUsername.textContent = user.display_name;
        navLogout.classList.remove('d-none');
        navLogout.onclick = (e) => {
            e.preventDefault();
            // Tell Authelia to actually clear the session cookie before
            // clearing local state — otherwise SSO silently re-logs us in.
            fetch('/__authlogout', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: '{}',
            }).catch(() => {}).finally(() => {
                clearAuth();
                window.location.href = '/';
            });
        };
        // If superuser viewing another library, also show link to own
        if (user.username !== libraryUser) {
            navMyLibrary.classList.remove('d-none');
            navMyLibrary.href = `/${user.username}/`;
        }
    } else if (user) {
        // Logged-in non-owner: read-only, link to own library
        navMyLibrary.classList.remove('d-none');
        navMyLibrary.href = `/${user.username}/`;
        navUsername.textContent = user.display_name;
        navLogout.classList.remove('d-none');
        navLogout.onclick = (e) => {
            e.preventDefault();
            // Tell Authelia to actually clear the session cookie before
            // clearing local state — otherwise SSO silently re-logs us in.
            fetch('/__authlogout', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: '{}',
            }).catch(() => {}).finally(() => {
                clearAuth();
                window.location.href = '/';
            });
        };
    } else {
        // Anonymous visitor: all authentication happens at the shared portal.
        const rd = encodeURIComponent(window.location.href);
        navSignin.href = `https://auth.gordongouger.com/?rd=${rd}`;
        navSignin.onclick = null;
        navSignin.classList.remove('d-none');
    }
}
