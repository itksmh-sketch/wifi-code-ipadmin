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

export function apiCall(endpoint, options = {}) {
    const token = localStorage.getItem('access_token');
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers,
    };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    return fetch(`${API_BASE}${endpoint}`, {
        ...options,
        headers,
    }).then(async (res) => {
        if (res.status === 401) {
            localStorage.removeItem('access_token');
            window.location.href = '/login';
            return null;
        }
        if (res.status === 204) return null;
        return res.json();
    });
}

export function platformApiCall(endpoint, options = {}) {
    const token = localStorage.getItem('platform_access_token');
    const headers = {
        'Content-Type': 'application/json',
        ...options.headers,
    };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    return fetch(`${API_BASE}${endpoint}`, {
        ...options,
        headers,
    }).then(async (res) => {
        if (res.status === 401) {
            localStorage.removeItem('platform_access_token');
            window.location.href = '/platform/login';
            return null;
        }
        if (res.status === 204) return null;
        return res.json();
    });
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
        window.location.href = '/login';
    };

    return (
        <AuthContext.Provider value={{ user, login, logout }}>
            <BrowserRouter>
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
