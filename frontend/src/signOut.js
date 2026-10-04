// Sign-out that also ends the session on the server: POST /api/v1/auth/logout
// bumps the admin's token_version, which signs them out on every device and
// clears their PIN elevation.
//
// It never waits on that call. The token is cleared first, then the browser
// leaves for the login page when the call settles (success or failure), if it
// throws, or after SIGN_OUT_TIMEOUT_MS, whichever comes first. keepalive lets
// the call finish after the page has gone.
//
// Only for a deliberate sign-out (the sidebar button, the onboarding page's
// link). The 401 handlers and the after-password-change redirects stay
// local-only: the server has already ended those sessions.
//
// Plain fetch, not apiCall: apiCall's 401 and onboarding redirects must not
// race this one.
const SIGN_OUT_TIMEOUT_MS = 3000;
let signingOut = false;

export function signOutEverywhere() {
    if (signingOut) return;
    signingOut = true;
    const token = localStorage.getItem('access_token');
    localStorage.removeItem('access_token');
    let left = false;
    const leave = () => {
        if (left) return;
        left = true;
        window.location.replace('/admin/login');
    };
    if (!token) return leave();
    setTimeout(leave, SIGN_OUT_TIMEOUT_MS);
    try {
        fetch('/api/v1/auth/logout', {
            method: 'POST',
            keepalive: true,
            headers: { Authorization: `Bearer ${token}` },
        }).then(leave, leave);
    } catch (e) {
        leave();
    }
}
