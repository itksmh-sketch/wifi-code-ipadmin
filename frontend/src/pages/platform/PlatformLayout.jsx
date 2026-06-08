import React from 'react';
import { Link, useNavigate } from 'react-router-dom';

export default function PlatformLayout({ children }) {
    const navigate = useNavigate();
    const logout = () => {
        localStorage.removeItem('platform_access_token');
        navigate('/platform/login');
    };

    return (
        <div style={{ minHeight: '100vh', background: '#f3f4f6' }}>
            <div style={{ background: '#111827', color: 'white', padding: '16px 24px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                    <h1 style={{ fontSize: 18, fontWeight: 700 }}>Platform Owner</h1>
                    <p style={{ color: '#9ca3af', fontSize: 12 }}>Operator management</p>
                </div>
                <div className="gap-2">
                    <Link className="btn" style={{ background: '#374151', color: 'white' }} to="/platform/operators">Operators</Link>
                    <Link className="btn btn-primary" to="/platform/operators/new">New Operator</Link>
                    <button className="btn btn-danger" onClick={logout}>Logout</button>
                </div>
            </div>
            <main style={{ padding: 24 }}>{children}</main>
        </div>
    );
}
