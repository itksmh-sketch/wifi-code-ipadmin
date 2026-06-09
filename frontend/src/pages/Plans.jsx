import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

export default function Plans() {
    const [plans, setPlans] = useState([]);
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState({
        name: '', type: 'time', duration_minutes: '', data_limit_mb: '',
        download_speed_kbps: 1024, upload_speed_kbps: 512, price_ghs: 0, is_active: true,
    });
    const [loading, setLoading] = useState(true);

    const fetchPlans = () => {
        apiCall('/plans').then(p => { setPlans(p || []); setLoading(false); });
    };

    useEffect(() => { fetchPlans(); }, []);

    const createPlan = async () => {
        const body = {
            ...form,
            duration_minutes: form.duration_minutes ? parseInt(form.duration_minutes) : null,
            data_limit_mb: form.data_limit_mb ? parseInt(form.data_limit_mb) : null,
            download_speed_kbps: parseInt(form.download_speed_kbps),
            upload_speed_kbps: parseInt(form.upload_speed_kbps),
            price_ghs: parseFloat(form.price_ghs),
        };
        const res = await apiCall('/plans', { method: 'POST', body: JSON.stringify(body) });
        if (res) {
            setForm({ name: '', type: 'time', duration_minutes: '', data_limit_mb: '', download_speed_kbps: 1024, upload_speed_kbps: 512, price_ghs: 0, is_active: true });
            setShowForm(false);
            fetchPlans();
        }
    };

    const togglePlan = async (plan) => {
        await apiCall(`/plans/${plan.id}`, { method: 'PUT', body: JSON.stringify({ is_active: !plan.is_active }) });
        fetchPlans();
    };

    if (loading) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <h1 style={{ fontSize: 24, fontWeight: 700 }}>Plans</h1>
                <button className="btn btn-primary" onClick={() => setShowForm(true)}>+ New Plan</button>
            </div>

            {showForm && (
                <div className="modal-overlay" onClick={() => setShowForm(false)}>
                    <div className="modal" onClick={e => e.stopPropagation()}>
                        <h2>Add Plan</h2>
                        <div className="form-group"><label>Name</label><input value={form.name} onChange={e => setForm({...form, name: e.target.value})} /></div>
                        <div className="form-group">
                            <label>Type</label>
                            <select value={form.type} onChange={e => setForm({...form, type: e.target.value})}>
                                <option value="time">Time-Based</option>
                                <option value="data">Data-Based</option>
                                <option value="hybrid">Hybrid</option>
                            </select>
                        </div>
                        {(form.type === 'time' || form.type === 'hybrid') && (
                            <div className="form-group"><label>Duration (minutes)</label><input type="number" value={form.duration_minutes} onChange={e => setForm({...form, duration_minutes: e.target.value})} /></div>
                        )}
                        {(form.type === 'data' || form.type === 'hybrid') && (
                            <div className="form-group"><label>Data Limit (MB)</label><input type="number" value={form.data_limit_mb} onChange={e => setForm({...form, data_limit_mb: e.target.value})} /></div>
                        )}
                        <div className="form-group"><label>Download Speed (kbps)</label><input type="number" value={form.download_speed_kbps} onChange={e => setForm({...form, download_speed_kbps: e.target.value})} /></div>
                        <div className="form-group"><label>Upload Speed (kbps)</label><input type="number" value={form.upload_speed_kbps} onChange={e => setForm({...form, upload_speed_kbps: e.target.value})} /></div>
                        <div className="form-group"><label>Price (GHS)</label><input type="number" step="0.01" value={form.price_ghs} onChange={e => setForm({...form, price_ghs: e.target.value})} /></div>
                        <div className="gap-2">
                            <button className="btn btn-primary" onClick={createPlan}>Create</button>
                            <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setShowForm(false)}>Cancel</button>
                        </div>
                    </div>
                </div>
            )}

            <div className="card" style={{ marginTop: 16 }}>
                <div className="table-wrap">
                    <table>
                        <thead><tr><th>Name</th><th>Type</th><th>Duration</th><th>Data Limit</th><th>Speed (↓/↑)</th><th>Price</th><th>Status</th><th>Actions</th></tr></thead>
                        <tbody>
                            {plans.map(p => (
                                <tr key={p.id}>
                                    <td>{p.name}</td>
                                    <td><span className="badge badge-blue">{p.type}</span></td>
                                    <td>{p.duration_minutes ? `${p.duration_minutes} min` : '—'}</td>
                                    <td>{p.data_limit_mb ? `${p.data_limit_mb} MB` : '—'}</td>
                                    <td>{p.download_speed_kbps}/{p.upload_speed_kbps} kbps</td>
                                    <td>GH₵ {parseFloat(p.price_ghs).toFixed(2)}</td>
                                    <td><span className={`badge ${p.is_active ? 'badge-green' : 'badge-gray'}`}>{p.is_active ? 'Active' : 'Inactive'}</span></td>
                                    <td><button className="btn btn-sm" style={{ background: '#e5e7eb' }} onClick={() => togglePlan(p)}>{p.is_active ? 'Disable' : 'Enable'}</button></td>
                                </tr>
                            ))}
                            {plans.length === 0 && <tr><td colSpan="8" style={{ color: '#9ca3af' }}>No plans yet</td></tr>}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}
