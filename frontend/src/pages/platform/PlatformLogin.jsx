import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';

export default function PlatformLogin() {
    const navigate = useNavigate();
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);

    const submit = async (event) => {
        event.preventDefault();
        setLoading(true);
        setError('');
        try {
            const res = await fetch('/api/v1/platform/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });
            const data = await res.json();
            if (!res.ok) {
                setError(data.detail || 'Login failed');
                return;
            }
            localStorage.setItem('platform_access_token', data.access_token);
            navigate('/platform/operators');
        } catch (err) {
            setError('Network error');
        } finally {
            setLoading(false);
        }
    };

    return (
        <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#111827' }}>
            <div className="card" style={{ width: 420, maxWidth: '90%' }}>
                <h1 style={{ textAlign: 'center', marginBottom: 8 }}>Platform Login</h1>
                <p style={{ textAlign: 'center', color: '#6b7280', marginBottom: 24, fontSize: 14 }}>Owner console</p>
                {error && <div className="badge badge-red" style={{ marginBottom: 16 }}>{error}</div>}
                <form onSubmit={submit}>
                    <div className="form-group">
                        <label>Email</label>
                        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
                    </div>
                    <div className="form-group">
                        <label>Password</label>
                        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
                    </div>
                    <button className="btn btn-primary" style={{ width: '100%', justifyContent: 'center' }} disabled={loading}>
                        {loading ? 'Signing in...' : 'Sign In'}
                    </button>
                </form>
            </div>
        </div>
    );
}
