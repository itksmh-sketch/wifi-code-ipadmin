import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

export default function Sessions() {
    const [sessions, setSessions] = useState([]);
    const [total, setTotal] = useState(0);
    const [activeOnly, setActiveOnly] = useState(true);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    const fetchSessions = async () => {
        setLoading(true);
        setError('');
        try {
            const data = await apiCall(`/sessions?active_only=${activeOnly}`);
            if (!data || !Array.isArray(data.sessions)) {
                throw new Error(data?.detail || 'Failed to load sessions');
            }
            setSessions(data?.sessions || []);
            setTotal(data?.total || 0);
        } catch (err) {
            setSessions([]);
            setTotal(0);
            setError(err instanceof Error ? err.message : 'Failed to load sessions');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => { fetchSessions(); }, [activeOnly]);

    const formatBytes = (bytes) => {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
        return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
    };

    if (loading) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <h1 style={{ fontSize: 24, fontWeight: 700 }}>Sessions</h1>
                <div className="gap-2">
                    <label style={{ fontSize: 14, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <input type="checkbox" checked={activeOnly} onChange={e => setActiveOnly(e.target.checked)} />
                        Active only
                    </label>
                    <button className="btn btn-primary btn-sm" onClick={fetchSessions}>Refresh</button>
                </div>
            </div>

            <p style={{ fontSize: 13, color: '#6b7280', marginBottom: 8 }}>Total: {total} sessions</p>

            {error && (
                <div style={{
                    background: '#fee2e2',
                    color: '#b91c1c',
                    padding: '10px 12px',
                    borderRadius: 8,
                    marginBottom: 12,
                    fontSize: 14,
                }}>
                    {error}
                </div>
            )}

            <div className="card">
                <div className="table-wrap">
                    <table>
                        <thead><tr><th>Username</th><th>Session ID</th><th>NAS IP</th><th>MAC</th><th>IP</th><th>Upload</th><th>Download</th><th>Started</th><th>Status</th></tr></thead>
                        <tbody>
                            {sessions.map(s => (
                                <tr key={s.id}>
                                    <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{s.username}</td>
                                    <td style={{ fontFamily: 'monospace', fontSize: 12 }}>{s.session_id}</td>
                                    <td><code>{s.nas_ip}</code></td>
                                    <td style={{ fontSize: 13 }}>{s.mac_address || '—'}</td>
                                    <td><code>{s.ip_address || '—'}</code></td>
                                    <td>{formatBytes(s.upload_bytes)}</td>
                                    <td>{formatBytes(s.download_bytes)}</td>
                                    <td style={{ fontSize: 13 }}>{new Date(s.started_at).toLocaleString()}</td>
                                    <td>
                                        <span className={`badge ${!s.stopped_at ? 'badge-green' : 'badge-gray'}`}>
                                            {!s.stopped_at ? 'Active' : 'Ended'}
                                        </span>
                                    </td>
                                </tr>
                            ))}
                            {sessions.length === 0 && <tr><td colSpan="9" style={{ color: '#9ca3af' }}>No sessions found</td></tr>}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}
