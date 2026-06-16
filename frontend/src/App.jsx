import React, { createContext, useState, useContext, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import TownsSites from './pages/TownsSites';
import Routers from './pages/Routers';
import Plans from './pages/Plans';
import Vouchers from './pages/Vouchers';
import Sessions from './pages/Sessions';
import PaymentCredentials from './pages/PaymentCredentials';
import Branding from './pages/Branding';
import PlatformLogin from './pages/platform/PlatformLogin';
import PlatformOperators from './pages/platform/PlatformOperators';
import PlatformOperatorNew from './pages/platform/PlatformOperatorNew';
import PlatformOperatorDetail from './pages/platform/PlatformOperatorDetail';
import PlatformApplications from './pages/platform/PlatformApplications';
import PlatformBilling from './pages/platform/PlatformBilling';
import Billing from './pages/Billing';
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
    if (!res.ok) throw new ApiError(res.status, body);
    return body;
}

export function apiCall(endpoint, options = {}) {
    return request(endpoint, options, { tokenKey: 'access_token', loginPath: '/admin/login' });
}

export function platformApiCall(endpoint, options = {}) {
    return request(endpoint, options, { tokenKey: 'platform_access_token', loginPath: '/admin/platform/login' });
}

function ProtectedRoute({ children }) {
    const token = localStorage.getItem('access_token');
    return token ? children : <Navigate to="/login" />;
}

function PlatformRoute({ children }) {
    const token = localStorage.getItem('platform_access_token');
    return token ? children : <Navigate to="/platform/login" />;
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
                    <Route path="/platform/login" element={<PlatformLogin />} />
                    <Route
                        path="/platform/operators"
                        element={
                            <PlatformRoute>
                                <PlatformOperators />
                            </PlatformRoute>
                        }
                    />
                    <Route
                        path="/platform/operators/new"
                        element={
                            <PlatformRoute>
                                <PlatformOperatorNew />
                            </PlatformRoute>
                        }
                    />
                    <Route
                        path="/platform/operators/:id"
                        element={
                            <PlatformRoute>
                                <PlatformOperatorDetail />
                            </PlatformRoute>
                        }
                    />
                    <Route
                        path="/platform/applications"
                        element={
                            <PlatformRoute>
                                <PlatformApplications />
                            </PlatformRoute>
                        }
                    />
                    <Route
                        path="/platform/billing"
                        element={
                            <PlatformRoute>
                                <PlatformBilling />
                            </PlatformRoute>
                        }
                    />
                    <Route path="/login" element={<Login />} />
                    <Route
                        path="/*"
                        element={
                            <ProtectedRoute>
                                <div style={{ display: 'flex', minHeight: '100vh' }}>
                                    <Sidebar />
                                    <div style={{ flex: 1, padding: 24, overflowY: 'auto' }}>
                                        <Routes>
                                            <Route path="/" element={<Dashboard />} />
                                            <Route path="/towns-sites" element={<TownsSites />} />
                                            <Route path="/routers" element={<Routers />} />
                                            <Route path="/plans" element={<Plans />} />
                                            <Route path="/vouchers" element={<Vouchers />} />
                                            <Route path="/sessions" element={<Sessions />} />
                                            <Route path="/payment-credentials" element={<PaymentCredentials />} />
                                            <Route path="/branding" element={<Branding />} />
                                            <Route path="/billing" element={<Billing />} />
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
