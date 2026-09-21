import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';

// Server-side bounds live in vouchers/engine.py (MIN_CODE_LENGTH / MAX_CODE_LENGTH);
// these are the offered steps, and the API re-validates whatever is sent.
const CODE_LENGTHS = [8, 10, 12, 14, 16, 18, 20, 22, 24];

// Mirrors generate_voucher_code(): groups of 4, dash separated, short last group.
function sampleCode(length) {
    const n = parseInt(length) || 16;
    return 'XXXXXXXXXXXXXXXXXXXXXXXX'.slice(0, n).match(/.{1,4}/g).join('-');
}

// Mirrors the API's source filter. Manual is the default because it is the
// operator's own stock; online purchases are already sold and reseller vouchers
// belong to a reseller, but both stay reachable here to look up or disable.
const SOURCES = [
    ['manual', 'Manual'],
    ['online', 'Online'],
    ['reseller', 'Reseller'],
    ['all', 'All sources'],
];
const SOURCE_BADGE = { manual: 'badge-blue', online: 'badge-green', reseller: 'badge-yellow' };

export default function Vouchers() {
    const [vouchers, setVouchers] = useState([]);
    const [total, setTotal] = useState(0);
    const [plans, setPlans] = useState([]);
    const [filters, setFilters] = useState({ status: '', plan_id: '', batch_id: '', source: 'manual' });
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState({ plan_id: '', quantity: 10, device_policy: 'single', code_length: 16 });
    const [loading, setLoading] = useState(true);
    const [generatedVouchers, setGeneratedVouchers] = useState(null);

    // Only the newest request may update the table. Changing filters quickly
    // can have an older, slower response land last and show the wrong list.
    const latestRequest = useRef(0);

    // Takes the filters explicitly rather than reading them from this render.
    // The dropdowns used to call setFilters(...) then setTimeout(fetchVouchers):
    // that fetchVouchers was the previous render's, so it sent the previous
    // filters and the table always lagged one selection behind.
    const fetchVouchers = (f = filters) => {
        const params = new URLSearchParams();
        if (f.status) params.set('status', f.status);
        if (f.plan_id) params.set('plan_id', f.plan_id);
        if (f.batch_id) params.set('batch_id', f.batch_id);
        params.set('source', f.source);
        const requestId = ++latestRequest.current;
        apiCall(`/vouchers?${params}`)
            .then(data => {
                if (requestId !== latestRequest.current) return;
                setVouchers(data?.vouchers || []);
                setTotal(data?.total || 0);
            })
            .catch(e => alert(e.message))
            .finally(() => setLoading(false));
    };

    useEffect(() => {
        apiCall('/plans').then(p => setPlans(p || [])).catch(() => setPlans([]));
    }, []);

    // The one place a filter change turns into a fetch — including the first
    // load — so the request always carries the filters just committed.
    useEffect(() => { fetchVouchers(filters); }, [filters]);

    const planNames = useMemo(() => Object.fromEntries(plans.map(p => [p.id, p.name])), [plans]);
    const setFilter = (key, value) => setFilters(prev => ({ ...prev, [key]: value }));

    const generateVouchers = async () => {
        const body = {
            plan_id: form.plan_id,
            quantity: parseInt(form.quantity),
            device_policy: form.device_policy,
            code_length: parseInt(form.code_length),
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
                <PageHeader title="Vouchers" />
                <div style={{ display: 'flex', gap: 8 }}>
                    <Link to="/vouchers/print" className="btn">Print vouchers</Link>
                    <button className="btn btn-primary" onClick={() => setShowForm(true)}>Generate Batch</button>
                </div>
            </div>

            {/* Filters */}
            <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
                <select value={filters.source} onChange={e => setFilter('source', e.target.value)} aria-label="Voucher source" style={{ padding: '6px 12px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 14 }}>
                    {SOURCES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
                <select value={filters.status} onChange={e => setFilter('status', e.target.value)} aria-label="Voucher status" style={{ padding: '6px 12px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 14 }}>
                    <option value="">All Statuses</option>
                    <option value="unused">Unused</option>
                    <option value="active">Active</option>
                    <option value="exhausted">Exhausted</option>
                    <option value="expired">Expired</option>
                    <option value="disabled">Disabled</option>
                </select>
                <select value={filters.plan_id} onChange={e => setFilter('plan_id', e.target.value)} aria-label="Plan" style={{ padding: '6px 12px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 14 }}>
                    <option value="">All Plans</option>
                    {plans.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
                {filters.batch_id && (
                    <button className="btn btn-sm" style={{ background: '#e5e7eb' }} onClick={() => setFilter('batch_id', '')}>Clear Batch Filter</button>
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
                            <label>Code Length</label>
                            <select value={form.code_length} onChange={e => setForm({...form, code_length: e.target.value})}>
                                {CODE_LENGTHS.map(n => (
                                    <option key={n} value={n}>{n} characters{n === 16 ? ' (default)' : ''}</option>
                                ))}
                            </select>
                            <small style={{ color: '#6b7280' }}>
                                Letters and digits only, printed in groups of 4 &mdash; e.g. {sampleCode(form.code_length)}
                            </small>
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
                        <thead><tr><th>Code</th><th>Username</th><th>Plan</th><th>Source</th><th>Status</th><th>Data Used</th><th>Expires</th><th>Actions</th></tr></thead>
                        <tbody>
                            {vouchers.map(v => (
                                <tr key={v.id}>
                                    <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{v.code}</td>
                                    <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{v.username}</td>
                                    <td>{planNames[v.plan_id] || `${v.plan_id?.slice(0, 8)}…`}</td>
                                    <td><span className={`badge ${SOURCE_BADGE[v.source] || 'badge-gray'}`}>{v.source}</span></td>
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
                            {vouchers.length === 0 && <tr><td colSpan="8" style={{ color: '#9ca3af' }}>No vouchers found</td></tr>}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}
