import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';
import PinPrompt, { usePinGate } from '../components/PinPrompt';

// Operator user management (superadmin only): add admin and staff users,
// change their role, and turn their sign-in off and on. Every write is
// PIN-gated on the server; the guards (own operator only, never a superadmin,
// never yourself) live there too — this page only mirrors them.

const ROLE_HELP = {
    admin: 'Everything except credentials, paying invoices and managing users.',
    staff: 'Day-to-day: views, printing vouchers, disabling vouchers, disconnecting users. Cannot generate vouchers or change settings.',
};

function statusOf(u) {
    if (!u.is_active) {
        return { label: u.disabled_by === 'platform' ? 'Disabled by platform' : 'Disabled', cls: 'badge-red' };
    }
    if (u.must_complete_onboarding) return { label: 'Pending setup', cls: 'badge-yellow' };
    return { label: 'Active', cls: 'badge-green' };
}

const errorBox = { background: '#fee2e2', color: '#b91c1c', padding: '10px 12px', borderRadius: 8, marginBottom: 12, fontSize: 14 };

export default function Users() {
    const [me, setMe] = useState(null);
    const [users, setUsers] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [showAdd, setShowAdd] = useState(false);
    const [form, setForm] = useState({ email: '', phone: '', role: 'staff' });
    const [formError, setFormError] = useState('');
    const [created, setCreated] = useState(null);
    const [disabling, setDisabling] = useState(null);
    const [reason, setReason] = useState('');
    const gate = usePinGate();

    const load = async () => {
        setError('');
        try {
            // /auth/me again rather than the shell's copy: a PIN set on the
            // Security page a moment ago should count here.
            const [who, list] = await Promise.all([apiCall('/auth/me'), apiCall('/admin/users')]);
            setMe(who);
            setUsers(Array.isArray(list) ? list : []);
        } catch (e) {
            setError(e.message || 'Could not load users');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => { load(); }, []);

    const replaceRow = (row) => setUsers((list) => list.map((u) => (u.id === row.id ? row : u)));

    // Each write runs inside gate.run, side effects included: after a PIN
    // prompt only the function passed to run() is replayed.
    const write = async (fn, onError = setError) => {
        setBusy(true);
        onError('');
        try {
            await gate.run(fn);
        } catch (e) {
            onError(e.message || 'Something went wrong');
        } finally {
            setBusy(false);
        }
    };

    const addUser = (e) => {
        e.preventDefault();
        write(async () => {
            const row = await apiCall('/admin/users', { method: 'POST', body: JSON.stringify(form) });
            setUsers((list) => [...list, row]);
            setShowAdd(false);
            setForm({ email: '', phone: '', role: 'staff' });
            setCreated(row);
        }, setFormError);
    };

    const changeRole = (u, role) => write(async () => {
        replaceRow(await apiCall(`/admin/users/${u.id}`, { method: 'PATCH', body: JSON.stringify({ role }) }));
    });

    const disableUser = (e) => {
        e.preventDefault();
        const u = disabling;
        write(async () => {
            replaceRow(await apiCall(`/admin/users/${u.id}/disable`, { method: 'POST', body: JSON.stringify({ reason: reason || null }) }));
            setDisabling(null);
            setReason('');
        });
    };

    const enableUser = (u) => write(async () => {
        replaceRow(await apiCall(`/admin/users/${u.id}/enable`, { method: 'POST', body: JSON.stringify({}) }));
    });

    if (loading) return <p>Loading...</p>;

    // No PIN yet: every change here needs one, so start at the Security page
    // instead of letting the first click fail.
    if (me && !me.has_pin) {
        return (
            <div>
                <PageHeader title="Users" />
                <div className="card" style={{ maxWidth: 560 }}>
                    <p style={{ marginBottom: 12 }}>
                        Managing users needs a security PIN, and your account doesn't have one yet.
                        Set one up on the Security page, then come back here.
                    </p>
                    <Link to="/security?tab=pin" className="btn btn-primary">Set up your PIN</Link>
                </div>
            </div>
        );
    }

    return (
        <div>
            <div className="flex-between">
                <PageHeader title="Users" />
                <button className="btn btn-primary" onClick={() => { setFormError(''); setShowAdd(true); }}>Add user</button>
            </div>

            <p style={{ fontSize: 13, color: '#6b7280', marginBottom: 12 }}>
                Admins and staff for your account. Superadmin accounts are managed by the platform.
                Your own account is on the Security page.
            </p>

            {error && <div style={errorBox}>{error}</div>}

            {created && (
                <div className="card" style={{ marginBottom: 16, background: '#f0fdf4' }}>
                    <p style={{ marginBottom: 6 }}>
                        <strong>{created.email}</strong> was added as {created.role}.{' '}
                        {created.temp_password_sms_sent
                            ? 'Their temporary password was sent by SMS.'
                            : `The SMS with their temporary password didn't go out${created.temp_password_sms_error ? ` (${created.temp_password_sms_error})` : ''}.`}
                    </p>
                    <p style={{ marginBottom: 8 }}>
                        Temporary password, shown once: <code>{created.temp_password}</code>
                    </p>
                    <button className="btn btn-sm" onClick={() => setCreated(null)}>Done</button>
                </div>
            )}

            <div className="card">
                <div className="table-wrap">
                    <table>
                        <thead><tr><th>Email</th><th>Role</th><th>Phone</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead>
                        <tbody>
                            {users.map((u) => {
                                const locked = u.role === 'superadmin' || u.id === me?.id;
                                const st = statusOf(u);
                                return (
                                    <tr key={u.id}>
                                        <td>{u.email}{u.id === me?.id && <span style={{ color: '#6b7280' }}> (you)</span>}</td>
                                        <td>
                                            {locked ? u.role : (
                                                <select value={u.role} disabled={busy} onChange={(e) => changeRole(u, e.target.value)}>
                                                    <option value="admin">admin</option>
                                                    <option value="staff">staff</option>
                                                </select>
                                            )}
                                        </td>
                                        <td style={{ fontSize: 13 }}>{u.phone || '—'}</td>
                                        <td>
                                            <span className={`badge ${st.cls}`}>{st.label}</span>
                                            {u.disabled_reason && <div style={{ fontSize: 12, color: '#6b7280' }}>{u.disabled_reason}</div>}
                                        </td>
                                        <td style={{ fontSize: 13 }}>{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : '—'}</td>
                                        <td>
                                            {!locked && u.is_active && (
                                                <button className="btn btn-sm" disabled={busy} onClick={() => { setReason(''); setDisabling(u); }}>Disable</button>
                                            )}
                                            {!locked && !u.is_active && u.disabled_by === 'operator' && (
                                                <button className="btn btn-sm" disabled={busy} onClick={() => enableUser(u)}>Enable</button>
                                            )}
                                        </td>
                                    </tr>
                                );
                            })}
                            {users.length === 0 && <tr><td colSpan="6" style={{ color: '#9ca3af' }}>No users</td></tr>}
                        </tbody>
                    </table>
                </div>
            </div>

            {showAdd && (
                <div className="modal-overlay" onClick={() => setShowAdd(false)}>
                    <div className="modal" onClick={(e) => e.stopPropagation()}>
                        <h2 style={{ marginBottom: 12 }}>Add user</h2>
                        {formError && <div style={errorBox}>{formError}</div>}
                        <form onSubmit={addUser}>
                            <div className="form-group">
                                <label>Email</label>
                                <input type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
                            </div>
                            <div className="form-group">
                                <label>Phone</label>
                                <input required value={form.phone} placeholder="0244123456" onChange={(e) => setForm({ ...form, phone: e.target.value })} />
                                <small style={{ color: '#6b7280' }}>Their temporary password is sent here by SMS.</small>
                            </div>
                            <div className="form-group">
                                <label>Role</label>
                                <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                                    <option value="staff">staff</option>
                                    <option value="admin">admin</option>
                                </select>
                                <small style={{ color: '#6b7280' }}>{ROLE_HELP[form.role]}</small>
                            </div>
                            <div className="gap-2">
                                <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? 'Adding…' : 'Add user'}</button>
                                <button type="button" className="btn" style={{ background: '#e5e7eb' }} onClick={() => setShowAdd(false)}>Cancel</button>
                            </div>
                        </form>
                    </div>
                </div>
            )}

            {disabling && (
                <div className="modal-overlay" onClick={() => setDisabling(null)}>
                    <div className="modal" onClick={(e) => e.stopPropagation()}>
                        <h2 style={{ marginBottom: 12 }}>Disable {disabling.email}?</h2>
                        <p style={{ marginBottom: 12, fontSize: 14 }}>
                            They are signed out everywhere and can't sign in until you enable them again.
                        </p>
                        <form onSubmit={disableUser}>
                            <div className="form-group">
                                <label>Reason (optional)</label>
                                <input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
                            </div>
                            <div className="gap-2">
                                <button type="submit" className="btn btn-primary" disabled={busy}>Disable</button>
                                <button type="button" className="btn" style={{ background: '#e5e7eb' }} onClick={() => setDisabling(null)}>Cancel</button>
                            </div>
                        </form>
                    </div>
                </div>
            )}

            {gate.reason && <PinPrompt reason={gate.reason} onCancel={gate.cancel} onUnlocked={gate.unlocked} />}
        </div>
    );
}
