import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

export default function Vouchers() {
    const [vouchers, setVouchers] = useState([]);
    const [total, setTotal] = useState(0);
    const [plans, setPlans] = useState([]);
    const [filters, setFilters] = useState({ status: '', plan_id: '', batch_id: '' });
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState({ plan_id: '', quantity: 10, device_policy: 'single' });
    const [loading, setLoading] = useState(true);
    const [generatedVouchers, setGeneratedVouchers] = useState(null);

    const fetchVouchers = () => {
        const params = new URLSearchParams();
        if (filters.status) params.set('status', filters.status);
        if (filters.plan_id) params.set('plan_id', filters.plan_id);
        if (filters.batch_id) params.set('batch_id', filters.batch_id);
        apiCall(`/vouchers?${params}`)
            .then(data => {
                setVouchers(data?.vouchers || []);
                setTotal(data?.total || 0);
            })
            .catch(e => alert(e.message))
            .finally(() => setLoading(false));
    };

    useEffect(() => {
        apiCall('/plans').then(p => setPlans(p || [])).catch(() => setPlans([]));
        fetchVouchers();
    }, []);

    const generateVouchers = async () => {
        const body = {
            plan_id: form.plan_id,
            quantity: parseInt(form.quantity),
            device_policy: form.device_policy,
        };
        try {
            const res = await apiCall('/vouchers/generate', { method: 'POST', body: JSON.stringify(body) });
            setGeneratedVouchers(res);
            setShowForm(false);
            fetchVouchers();
        } catch (e) {
            alert(e.message);
        }
    };

    const disableVoucher = async (id) => {
        try {
            await apiCall(`/vouchers/${id}/disable`, { method: 'PUT' });
            fetchVouchers();
        } catch (e) {
            alert(e.message);
        }
    };

    const reactivateVoucher = async (id) => {
        try {
            await apiCall(`/vouchers/${id}/reactivate`, { method: 'PUT' });
            fetchVouchers();
        } catch (e) {
            alert(e.message);
        }
    };

    const statusBadge = (status) => {
        const map = {
            unused: 'badge-gray',
            active: 'badge-green',
            exhausted: 'badge-yellow',
            expired: 'badge-red',
            disabled: 'badge-gray',
        };
        return <span className={`badge ${map[status] || 'badge-gray'}`}>{status}</span>;
    };

    if (loading) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <h1 style={{ fontSize: 24, fontWeight: 700 }}>Vouchers</h1>
                <button className="btn btn-primary" onClick={() => setShowForm(true)}>Generate Batch</button>
            </div>

            {/* Filters */}
            <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
                <select value={filters.status} onChange={e => { setFilters({ ...filters, status: e.target.value }); setTimeout(fetchVouchers, 0); }} style={{ padding: '6px 12px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 14 }}>
                    <option value="">All Statuses</option>
                    <option value="unused">Unused</option>
                    <option value="active">Active</option>
                    <option value="exhausted">Exhausted</option>
                    <option value="expired">Expired</option>
                    <option value="disabled">Disabled</option>
                </select>
                <select value={filters.plan_id} onChange={e => { setFilters({ ...filters, plan_id: e.target.value }); setTimeout(fetchVouchers, 0); }} style={{ padding: '6px 12px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 14 }}>
                    <option value="">All Plans</option>
                    {plans.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
                {filters.batch_id && (
                    <button className="btn btn-sm" style={{ background: '#e5e7eb' }} onClick={() => { setFilters({ ...filters, batch_id: '' }); setTimeout(fetchVouchers, 0); }}>Clear Batch Filter</button>
                )}
            </div>

            {/* Generation Modal */}
            {showForm && (
                <div className="modal-overlay" onClick={() => setShowForm(false)}>
                    <div className="modal" onClick={e => e.stopPropagation()}>
                        <h2>Generate Voucher Batch</h2>
                        <div className="form-group">
                            <label>Plan</label>
                            <select value={form.plan_id} onChange={e => setForm({...form, plan_id: e.target.value})}>
                                <option value="">Select Plan</option>
                                {plans.filter(p => p.is_active).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                            </select>
                        </div>
                        <div className="form-group">
                            <label>Quantity</label>
                            <input type="number" min="1" max="500" value={form.quantity} onChange={e => setForm({...form, quantity: e.target.value})} />
                        </div>
                        <div className="form-group">
                            <label>Device Policy</label>
                            <select value={form.device_policy} onChange={e => setForm({...form, device_policy: e.target.value})}>
                                <option value="single">Single Device</option>
                                <option value="multi">Multi Device</option>
                            </select>
                        </div>
                        <div className="gap-2">
                            <button className="btn btn-primary" onClick={generateVouchers}>Generate</button>
                            <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setShowForm(false)}>Cancel</button>
                        </div>
                    </div>
                </div>
            )}

            {/* Generated Vouchers Display */}
            {generatedVouchers && (
                <div className="card" style={{ marginBottom: 16, background: '#f0fdf4' }}>
                    <h3 style={{ marginBottom: 8 }}>✅ {generatedVouchers.length} vouchers generated</h3>
                    <div style={{ maxHeight: 200, overflowY: 'auto', fontFamily: 'monospace', fontSize: 13 }}>
                        {generatedVouchers.map(v => (
                            <div key={v.id} style={{ padding: '4px 0' }}>
                                <strong>{v.code}</strong> — User: {v.username} / Pass: {v.password}
                            </div>
                        ))}
                    </div>
                    <button className="btn btn-sm" style={{ marginTop: 8, background: '#e5e7eb' }} onClick={() => setGeneratedVouchers(null)}>Close</button>
                </div>
            )}

            {/* Vouchers Table */}
            <div className="card">
                <p style={{ fontSize: 13, color: '#6b7280', marginBottom: 8 }}>Total: {total} vouchers</p>
                <div className="table-wrap">
                    <table>
                        <thead><tr><th>Code</th><th>Username</th><th>Plan</th><th>Status</th><th>Data Used</th><th>Expires</th><th>Actions</th></tr></thead>
                        <tbody>
                            {vouchers.map(v => (
                                <tr key={v.id}>
                                    <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{v.code}</td>
                                    <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{v.username}</td>
                                    <td>{v.plan_id?.slice(0, 8)}...</td>
                                    <td>{statusBadge(v.status)}</td>
                                    <td>{v.data_used_mb} MB</td>
                                    <td>{v.expires_at ? new Date(v.expires_at).toLocaleDateString() : '—'}</td>
                                    <td>
                                        <div className="gap-2">
                                            {v.status !== 'disabled' && (
                                                <button className="btn btn-danger btn-sm" onClick={() => disableVoucher(v.id)}>Disable</button>
                                            )}
                                            {v.status === 'disabled' && (
                                                <button className="btn btn-primary btn-sm" onClick={() => reactivateVoucher(v.id)}>Reactivate</button>
                                            )}
                                        </div>
                                    </td>
                                </tr>
                            ))}
                            {vouchers.length === 0 && <tr><td colSpan="7" style={{ color: '#9ca3af' }}>No vouchers found</td></tr>}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}
