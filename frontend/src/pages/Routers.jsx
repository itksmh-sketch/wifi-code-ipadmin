import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

export default function Routers() {
    const [sites, setSites] = useState([]);
    const [routers, setRouters] = useState([]);
    const [selectedSite, setSelectedSite] = useState('');
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState({ name: '', ip_address: '', nas_identifier: '', nas_secret: '', is_active: true });
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        const load = async () => {
            const t = await apiCall('/towns');
            const allSites = [];
            if (t) {
                for (const town of t) {
                    const s = await apiCall(`/towns/${town.id}/sites`);
                    if (s) allSites.push(...s);
                }
            }
            setSites(allSites);
            setLoading(false);
        };
        load();
    }, []);

    useEffect(() => {
        if (selectedSite) {
            apiCall(`/sites/${selectedSite}/routers`).then(r => setRouters(r || []));
        } else {
            setRouters([]);
        }
    }, [selectedSite]);

    const createRouter = async () => {
        await apiCall(`/sites/${selectedSite}/routers`, { method: 'POST', body: JSON.stringify(form) });
        setForm({ name: '', ip_address: '', nas_identifier: '', nas_secret: '', is_active: true });
        setShowForm(false);
        const r = await apiCall(`/sites/${selectedSite}/routers`);
        setRouters(r || []);
    };

    if (loading) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <h1 style={{ fontSize: 24, fontWeight: 700 }}>Routers</h1>
                <button className="btn btn-primary" onClick={() => setShowForm(true)} disabled={!selectedSite}>+ New Router</button>
            </div>

            <div className="form-group" style={{ maxWidth: 300 }}>
                <label>Select Site</label>
                <select value={selectedSite} onChange={e => setSelectedSite(e.target.value)}>
                    <option value="">-- Choose Site --</option>
                    {sites.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
            </div>

            {showForm && (
                <div className="modal-overlay" onClick={() => setShowForm(false)}>
                    <div className="modal" onClick={e => e.stopPropagation()}>
                        <h2>Add Router</h2>
                        <div className="form-group"><label>Name</label><input value={form.name} onChange={e => setForm({...form, name: e.target.value})} /></div>
                        <div className="form-group"><label>IP Address</label><input value={form.ip_address} onChange={e => setForm({...form, ip_address: e.target.value})} placeholder="192.168.x.x" /></div>
                        <div className="form-group"><label>NAS Identifier</label><input value={form.nas_identifier} onChange={e => setForm({...form, nas_identifier: e.target.value})} /></div>
                        <div className="form-group"><label>NAS Secret</label><input value={form.nas_secret} onChange={e => setForm({...form, nas_secret: e.target.value})} /></div>
                        <div className="gap-2">
                            <button className="btn btn-primary" onClick={createRouter}>Create</button>
                            <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setShowForm(false)}>Cancel</button>
                        </div>
                    </div>
                </div>
            )}

            <div className="card" style={{ marginTop: 16 }}>
                <div className="table-wrap">
                    <table>
                        <thead><tr><th>Name</th><th>IP Address</th><th>NAS Identifier</th><th>Status</th><th>Last Seen</th></tr></thead>
                        <tbody>
                            {routers.map(r => (
                                <tr key={r.id}>
                                    <td>{r.name}</td>
                                    <td><code>{r.ip_address}</code></td>
                                    <td><code>{r.nas_identifier}</code></td>
                                    <td><span className={`badge ${r.is_active ? 'badge-green' : 'badge-gray'}`}>{r.is_active ? 'Active' : 'Inactive'}</span></td>
                                    <td>{r.last_seen_at ? new Date(r.last_seen_at).toLocaleString() : 'Never'}</td>
                                </tr>
                            ))}
                            {routers.length === 0 && <tr><td colSpan="5" style={{ color: '#9ca3af' }}>No routers at this site</td></tr>}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}
