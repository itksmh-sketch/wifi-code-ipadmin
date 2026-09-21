import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';

export default function Plans() {
    const [plans, setPlans] = useState([]);
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState({
        name: '', type: 'time', duration_minutes: '', data_limit_mb: '',
        download_speed_kbps: 1024, upload_speed_kbps: 512, price_ghs: 0, is_active: true,
    });
    const [loading, setLoading] = useState(true);
    // Surfaced inside the modal so a rejected create (e.g. duplicate settings)
    // is shown next to the form the operator has to fix, not in a popup.
    const [formError, setFormError] = useState('');
    // The plan currently open in the Edit dialog, plus its draft values.
    const [editing, setEditing] = useState(null);
    const [editForm, setEditForm] = useState(null);
    const [editError, setEditError] = useState('');
    const [savingEdit, setSavingEdit] = useState(false);

    const fetchPlans = () => {
        apiCall('/plans')
            .then(p => setPlans(p || []))
            .catch(e => alert(e.message))
            .finally(() => setLoading(false));
    };

    useEffect(() => { fetchPlans(); }, []);

    const openForm = () => {
        setFormError('');
        setShowForm(true);
    };

    const createPlan = async () => {
        setFormError('');
        const body = {
            ...form,
            duration_minutes: form.duration_minutes ? parseInt(form.duration_minutes) : null,
            data_limit_mb: form.data_limit_mb ? parseInt(form.data_limit_mb) : null,
            download_speed_kbps: parseInt(form.download_speed_kbps),
            upload_speed_kbps: parseInt(form.upload_speed_kbps),
            price_ghs: parseFloat(form.price_ghs),
        };
        try {
            await apiCall('/plans', { method: 'POST', body: JSON.stringify(body) });
            setForm({ name: '', type: 'time', duration_minutes: '', data_limit_mb: '', download_speed_kbps: 1024, upload_speed_kbps: 512, price_ghs: 0, is_active: true });
            setShowForm(false);
            fetchPlans();
        } catch (e) {
            setFormError(e.message);
        }
    };

    // Only these four are editable; the entitlement fields are shown read-only
    // in the dialog (see the note there) because already-issued vouchers are
    // measured against them live.
    const openEdit = (plan) => {
        setEditError('');
        setEditing(plan);
        setEditForm({
            name: plan.name,
            price_ghs: String(plan.price_ghs),
            download_speed_kbps: String(plan.download_speed_kbps),
            upload_speed_kbps: String(plan.upload_speed_kbps),
        });
    };

    const closeEdit = () => {
        // Same discard behaviour as the Add Plan dialog: overlay click or Cancel
        // drops the draft, no confirmation step.
        setEditing(null);
        setEditForm(null);
        setEditError('');
    };

    // Mirrors PlanProfileUpdate on the server: name trimmed and non-blank,
    // price >= 0, speeds > 0. The server still enforces all of it — this just
    // catches it before a round trip.
    const editValidationError = () => {
        if (!editForm.name.trim()) return 'Name cannot be blank.';
        if (editForm.name.trim().length > 255) return 'Name must be 255 characters or fewer.';
        const price = parseFloat(editForm.price_ghs);
        if (!Number.isFinite(price) || price < 0) return 'Price must be a number of 0 or more.';
        for (const [field, label] of [['download_speed_kbps', 'Download speed'], ['upload_speed_kbps', 'Upload speed']]) {
            const value = Number(editForm[field]);
            if (!Number.isInteger(value) || value <= 0) return `${label} must be a whole number above 0.`;
        }
        return '';
    };

    // PATCH carries only what actually changed, so an untouched field is never
    // sent and cannot trip the duplicate-plan check on its own.
    const editChanges = () => {
        if (!editing || !editForm) return {};
        const changes = {};
        if (editForm.name.trim() !== editing.name) changes.name = editForm.name.trim();
        if (parseFloat(editForm.price_ghs) !== parseFloat(editing.price_ghs)) {
            changes.price_ghs = parseFloat(editForm.price_ghs);
        }
        for (const field of ['download_speed_kbps', 'upload_speed_kbps']) {
            if (Number(editForm[field]) !== Number(editing[field])) changes[field] = Number(editForm[field]);
        }
        return changes;
    };

    const saveEdit = async () => {
        const invalid = editValidationError();
        if (invalid) { setEditError(invalid); return; }
        const changes = editChanges();
        if (Object.keys(changes).length === 0) { closeEdit(); return; }

        setSavingEdit(true);
        setEditError('');
        try {
            await apiCall(`/plans/${editing.id}`, { method: 'PATCH', body: JSON.stringify(changes) });
            closeEdit();
            fetchPlans();
        } catch (e) {
            // 400 (validation / a field that cannot be changed) and 409 (this
            // edit would duplicate another plan) both carry a specific message;
            // show it against the form rather than a generic failure.
            setEditError(e.message);
        } finally {
            setSavingEdit(false);
        }
    };

    const togglePlan = async (plan) => {
        try {
            const action = plan.is_active ? 'deactivate' : 'activate';
            await apiCall(`/plans/${plan.id}/${action}`, { method: 'POST' });
            fetchPlans();
        } catch (e) {
            alert(e.message);
        }
    };

    // Delete removes the plan and its untouched vouchers. The API refuses (409)
    // as soon as any voucher has been paid for, allocated, used or disconnected;
    // deactivating is the answer in that case, so offer it right there.
    const deletePlan = async (plan) => {
        if (!window.confirm(
            `Delete the plan "${plan.name}"?\n\n`
            + 'This also deletes vouchers generated from it that were never sold or used. '
            + 'It is refused if any voucher has payment, reseller or usage history.'
        )) return;
        try {
            await apiCall(`/plans/${plan.id}`, { method: 'DELETE' });
            fetchPlans();
        } catch (e) {
            if (e.status === 409) {
                const deactivate = plan.is_active && window.confirm(
                    `${e.message}\n\nDeactivate "${plan.name}" now instead?`
                );
                if (deactivate) {
                    await togglePlan(plan);
                    return;
                }
                alert(e.message);
                return;
            }
            alert(e.message);
        }
    };

    if (loading) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <PageHeader title="Plans" />
                <button className="btn btn-primary" onClick={openForm}>+ New Plan</button>
            </div>

            {showForm && (
                <div className="modal-overlay" onClick={() => setShowForm(false)}>
                    <div className="modal" onClick={e => e.stopPropagation()}>
                        <h2>Add Plan</h2>
                        {formError && (
                            <div className="alert alert-error" role="alert" style={{ background: '#fee2e2', color: '#991b1b', padding: '8px 12px', borderRadius: 6, marginBottom: 12 }}>
                                {formError}
                            </div>
                        )}
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

            {editing && editForm && (
                <div className="modal-overlay" onClick={closeEdit}>
                    <div className="modal" onClick={e => e.stopPropagation()}>
                        <h2>Edit Plan</h2>
                        {editError && (
                            <div className="alert alert-error" role="alert" style={{ background: '#fee2e2', color: '#991b1b', padding: '8px 12px', borderRadius: 6, marginBottom: 12 }}>
                                {editError}
                            </div>
                        )}

                        <div className="form-group">
                            <label>Name</label>
                            <input
                                value={editForm.name}
                                maxLength={255}
                                onChange={e => { setEditForm({ ...editForm, name: e.target.value }); setEditError(''); }}
                            />
                        </div>
                        <div className="form-group">
                            <label>Price (GHS)</label>
                            <input
                                type="number" step="0.01" min="0"
                                value={editForm.price_ghs}
                                onChange={e => { setEditForm({ ...editForm, price_ghs: e.target.value }); setEditError(''); }}
                            />
                        </div>
                        <div className="form-group">
                            <label>Download Speed (kbps)</label>
                            <input
                                type="number" min="1"
                                value={editForm.download_speed_kbps}
                                onChange={e => { setEditForm({ ...editForm, download_speed_kbps: e.target.value }); setEditError(''); }}
                            />
                        </div>
                        <div className="form-group">
                            <label>Upload Speed (kbps)</label>
                            <input
                                type="number" min="1"
                                value={editForm.upload_speed_kbps}
                                onChange={e => { setEditForm({ ...editForm, upload_speed_kbps: e.target.value }); setEditError(''); }}
                            />
                        </div>

                        {/* Shown, not hidden: the operator needs to see what this plan
                            actually grants while renaming or repricing it. */}
                        <div style={{ background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 8, padding: '12px 14px', marginBottom: 16 }}>
                            <div style={{ fontSize: 13, fontWeight: 600, color: '#374151', marginBottom: 8 }}>
                                What this plan grants
                            </div>
                            <dl style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 14px', margin: 0, fontSize: 13 }}>
                                <dt style={{ color: '#6b7280' }}>Type</dt>
                                <dd style={{ margin: 0 }}>{editing.type}</dd>
                                <dt style={{ color: '#6b7280' }}>Duration</dt>
                                <dd style={{ margin: 0 }}>{editing.duration_minutes ? `${editing.duration_minutes} min` : '—'}</dd>
                                <dt style={{ color: '#6b7280' }}>Data cap</dt>
                                <dd style={{ margin: 0 }}>{editing.data_limit_mb ? `${editing.data_limit_mb} MB` : '—'}</dd>
                            </dl>
                            <p style={{ color: '#6b7280', fontSize: 12, margin: '10px 0 0', lineHeight: 1.5 }}>
                                Duration and data cap can&rsquo;t be changed after a plan is created — vouchers
                                already sold are measured against them every time a customer logs in, so an edit
                                would rewrite what those customers bought. Create a new plan instead, and
                                deactivate this one.
                            </p>
                        </div>

                        <div className="gap-2">
                            <button className="btn btn-primary" onClick={saveEdit} disabled={savingEdit}>
                                {savingEdit ? 'Saving…' : 'Save changes'}
                            </button>
                            <button className="btn" style={{ background: '#e5e7eb' }} onClick={closeEdit} disabled={savingEdit}>
                                Cancel
                            </button>
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
                                    <td style={{ display: 'flex', gap: 6 }}>
                                        <button className="btn btn-sm" style={{ background: '#e5e7eb' }} onClick={() => openEdit(p)}>Edit</button>
                                        <button className="btn btn-sm" style={{ background: '#e5e7eb' }} onClick={() => togglePlan(p)}>
                                            {p.is_active ? 'Deactivate' : 'Activate'}
                                        </button>
                                        <button className="btn btn-sm btn-danger" onClick={() => deletePlan(p)}>Delete</button>
                                    </td>
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
