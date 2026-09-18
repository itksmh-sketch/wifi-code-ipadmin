import React, { useState } from 'react';
import { apiCall, useAuth } from '../App';
import PageHeader from '../components/PageHeader';
import { PASSWORD_RULES, passwordMeetsPolicy } from '../components/AuthFrame';

// Change your own password while signed in. The endpoint invalidates every other
// session and returns a fresh token pair, which we store so this tab stays
// signed in. Same rules the onboarding screen enforces.
export default function ChangePassword() {
    const { login } = useAuth();
    const [current, setCurrent] = useState('');
    const [password, setPassword] = useState('');
    const [confirm, setConfirm] = useState('');
    const [show, setShow] = useState(false);
    const [error, setError] = useState('');
    const [done, setDone] = useState(false);
    const [saving, setSaving] = useState(false);

    const submit = async (e) => {
        e.preventDefault();
        if (saving) return;
        setError('');
        setDone(false);
        if (!current) { setError('Enter your current password.'); return; }
        if (!passwordMeetsPolicy(password)) { setError('Your new password does not meet all the rules yet.'); return; }
        if (password !== confirm) { setError("The two passwords don't match."); return; }

        setSaving(true);
        try {
            const data = await apiCall('/auth/me/password', {
                method: 'POST',
                body: JSON.stringify({
                    current_password: current,
                    new_password: password,
                    confirm_password: confirm,
                }),
            });
            // Keep this tab signed in on the new token; the old one is dead.
            login(data.access_token);
            setCurrent('');
            setPassword('');
            setConfirm('');
            setDone(true);
        } catch (err) {
            setError(err.message || 'Could not change your password.');
        }
        setSaving(false);
    };

    const field = (id, label, value, onChange, autoComplete) => (
        <div className="form-group">
            <label htmlFor={id}>{label}</label>
            <input
                id={id}
                type={show ? 'text' : 'password'}
                autoComplete={autoComplete}
                value={value}
                onChange={(e) => { onChange(e.target.value); setError(''); setDone(false); }}
            />
        </div>
    );

    return (
        <>
            <PageHeader title="Change password" icon="lock" />
            <div className="card" style={{ maxWidth: 480 }}>
                <p style={{ color: '#6b7280', fontSize: 14, marginBottom: 18 }}>
                    Choose a new password for your account. This signs you out on every other device;
                    this one stays signed in.
                </p>

                {error && (
                    <div className="notice bad" style={{ marginBottom: 16, color: '#b42318', background: '#fef3f2', padding: '10px 12px', borderRadius: 6, fontSize: 14 }}>
                        {error}
                    </div>
                )}
                {done && (
                    <div className="notice good" style={{ marginBottom: 16, color: '#166534', background: '#dcfce7', padding: '10px 12px', borderRadius: 6, fontSize: 14 }}>
                        Password changed. Other sessions have been signed out.
                    </div>
                )}

                <form onSubmit={submit} noValidate>
                    {field('current-password', 'Current password', current, setCurrent, 'current-password')}
                    {field('new-password', 'New password', password, setPassword, 'new-password')}
                    <p style={{ margin: '-10px 0 14px', fontSize: 12.5, color: '#6b7280' }}>
                        {PASSWORD_RULES.map((rule, i) => (
                            <span key={rule.key} style={{ color: rule.test(password) ? '#166534' : undefined }}>
                                {i ? ' · ' : ''}{rule.label}
                            </span>
                        ))}
                    </p>
                    {field('confirm-password', 'Confirm new password', confirm, setConfirm, 'new-password')}

                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: '#6b7280', marginBottom: 16 }}>
                        <input type="checkbox" checked={show} onChange={() => setShow((s) => !s)} style={{ width: 'auto' }} />
                        Show passwords
                    </label>

                    <button type="submit" className="btn btn-primary" disabled={saving}>
                        {saving ? 'Changing…' : 'Change password'}
                    </button>
                </form>
            </div>
        </>
    );
}
