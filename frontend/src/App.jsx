import React, { createContext, useState, useContext, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Login from './pages/Login';
import Onboarding from './pages/Onboarding';
import ForgotPassword from './pages/ForgotPassword';
import Dashboard from './pages/Dashboard';
import Analytics from './pages/Analytics';
import TownsSites from './pages/TownsSites';
import Routers from './pages/Routers';
import Plans from './pages/Plans';
import Vouchers from './pages/Vouchers';
import VoucherPrint from './pages/VoucherPrint';
import Sessions from './pages/Sessions';
import PaymentCredentials from './pages/PaymentCredentials';
import SMSCredentials from './pages/SMSCredentials';
import Branding from './pages/Branding';
import Billing from './pages/Billing';
import Security from './pages/Security';
import Sidebar from './components/Sidebar';

const API_BASE = '/api/v1';

const AuthContext = createContext(null);

export function useAuth() {
    return useContext(AuthContext);
}

// Thrown for any non-2xx response (other than the 401 redirect case). Carries
// the HTTP status and the parsed error body so call sites can inspect them.
// `.message` defaults to the backend's `detail`/`message` field when present,
// so a bare `catch(e => alert(e.message))` shows something useful.
function messageFromBody(status, body) {
    if (typeof body === 'string' && body) return body;
    if (body && typeof body === 'object') {
        const detail = body.detail ?? body.message;
        if (typeof detail === 'string') return detail;
        // FastAPI/Pydantic request validation returns `detail` as an array of
        // { loc, msg, ... } — flatten it into a readable sentence.
        if (Array.isArray(detail)) {
            const parts = detail.map((d) => (d && typeof d === 'object' ? d.msg : String(d))).filter(Boolean);
            if (parts.length) return parts.join('; ');
        }
    }
    return `Request failed with status ${status}`;
}

export class ApiError extends Error {
    constructor(status, body) {
        super(messageFromBody(status, body));
        this.name = 'ApiError';
        this.status = status;
        this.body = body;
        // Set to 'setup' | 'verify' | 'locked' when the backend refused a
        // PIN-gated area. Absent on every other error, so `if (err.pinRequired)`
        // is the check — see the X-Pin-Required handling in request().
        this.pinRequired = null;
    }
}

async function request(endpoint, options, { tokenKey, loginPath }) {
    const token = localStorage.getItem(tokenKey);
    // Let the browser set the multipart boundary for FormData uploads; only force
    // JSON for regular bodies.
    const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
    const headers = {
        ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
        ...options.headers,
    };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    const res = await fetch(`${API_BASE}${endpoint}`, { ...options, headers });

    if (res.status === 403 && res.headers.get('X-Onboarding-Required')) {
        // Signed in on a temporary password: the API serves nothing else until
        // account setup is finished.
        window.location.href = '/admin/onboarding';
        throw new ApiError(403, null);
    }
    // A PIN-gated area refused the call. Deliberately NOT a redirect, unlike
    // the onboarding case above: the admin is in the middle of something on a
    // page they are allowed to be on, and navigating away would throw that work
    // out to re-authenticate. The page keeps its state, raises a PIN prompt
    // over itself, and retries the call once the PIN clears. The reason rides
    // on the error so the prompt knows whether to ask for a first PIN
    // ('setup'), an existing one ('verify'), or to show the lockout ('locked').
    const pinRequired = res.status === 403 ? res.headers.get('X-Pin-Required') : null;
    if (res.status === 401) {
        // Session expired/invalid — drop the token and bounce to login. We still
        // throw so callers don't proceed with a null/garbage value mid-redirect.
        localStorage.removeItem(tokenKey);
        window.location.href = loginPath;
        throw new ApiError(401, null);
    }
    if (res.status === 204) return null;

    // Parse the body once (tolerating empty / non-JSON responses), then decide
    // success vs. failure. Previously any non-2xx body was returned as if it were
    // valid data — masking 4xx/5xx errors at the call site.
    const text = await res.text();
    let body = null;
    if (text) {
        try { body = JSON.parse(text); } catch { body = text; }
    }
    if (!res.ok) {
        const error = new ApiError(res.status, body);
        error.pinRequired = pinRequired;
        throw error;
    }
    return body;
}

export function apiCall(endpoint, options = {}) {
    return request(endpoint, options, { tokenKey: 'access_token', loginPath: '/admin/login' });
}

// Retained with no callers: the React platform-owner pages that used it were
// retired in favour of the vanilla portal at /platform/*. Kept for feature #3.
// NOTE: loginPath points at /admin/platform/login, a route that no longer
// exists — fix it (to /platform/login) before wiring this up to anything.
export function platformApiCall(endpoint, options = {}) {
    return request(endpoint, options, { tokenKey: 'platform_access_token', loginPath: '/admin/platform/login' });
}

function ProtectedRoute({ children }) {
    const token = localStorage.getItem('access_token');
    return token ? children : <Navigate to="/login" />;
}

export default function App() {
    const [user, setUser] = useState(null);

    useEffect(() => {
        const token = localStorage.getItem('access_token');
        if (token) {
            try {
                const payload = JSON.parse(atob(token.split('.')[1]));
                setUser(payload);
            } catch (e) {
                localStorage.removeItem('access_token');
            }
        }
    }, []);

    const login = (accessToken) => {
        localStorage.setItem('access_token', accessToken);
        try {
            const payload = JSON.parse(atob(accessToken.split('.')[1]));
            setUser(payload);
        } catch (e) {}
    };

    const logout = () => {
        localStorage.removeItem('access_token');
        setUser(null);
        window.location.href = '/admin/login';
    };

    return (
        <AuthContext.Provider value={{ user, login, logout }}>
            <BrowserRouter basename="/admin">
                <Routes>
                    <Route path="/login" element={<Login />} />
                    <Route path="/forgot-password" element={<ForgotPassword />} />
                    <Route path="/onboarding" element={<ProtectedRoute><Onboarding /></ProtectedRoute>} />
                    <Route
                        path="/*"
                        element={
                            <ProtectedRoute>
                                <div style={{ display: 'flex', minHeight: '100vh' }}>
                                    <Sidebar />
                                    <div style={{ flex: 1, padding: 24, overflowY: 'auto' }}>
                                        <Routes>
                                            <Route path="/" element={<Dashboard />} />
                                            <Route path="/analytics" element={<Analytics />} />
                                            <Route path="/towns-sites" element={<TownsSites />} />
                                            <Route path="/routers" element={<Routers />} />
                                            <Route path="/plans" element={<Plans />} />
                                            <Route path="/vouchers" element={<Vouchers />} />
                                            <Route path="/vouchers/print" element={<VoucherPrint />} />
                                            <Route path="/sessions" element={<Sessions />} />
                                            <Route path="/payment-credentials" element={<PaymentCredentials />} />
                                            <Route path="/sms-credentials" element={<SMSCredentials />} />
                                            <Route path="/branding" element={<Branding />} />
                                            <Route path="/billing" element={<Billing />} />
                                            <Route path="/security" element={<Security />} />
                                            {/* The password form lived here before it became
                                                a tab; bookmarks and older links still work. */}
                                            <Route path="/change-password" element={<Navigate to="/security?tab=password" replace />} />
                                            <Route path="*" element={<Navigate to="/" />} />
                                        </Routes>
                                    </div>
                                </div>
                            </ProtectedRoute>
                        }
                    />
                </Routes>
            </BrowserRouter>
        </AuthContext.Provider>
    );
}
