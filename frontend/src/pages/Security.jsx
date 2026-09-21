import React, { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { apiCall, useAuth } from '../App';
import PageHeader from '../components/PageHeader';
import PinPrompt, { usePinGate } from '../components/PinPrompt';
import { PASSWORD_RULES, passwordMeetsPolicy } from '../components/AuthFrame';

// Everything about this account's own security in one place: the password (an
// admin already had this page), the verified phone, the security question, and
// the PIN.
//
// The tab lives in the query string so "Security → PIN" is a link anyone can
// send — the PIN prompt in gated areas points people straight at ?tab=pin.

const TABS = [
    ['password', 'Password'],
    ['phone', 'Phone'],
    ['question', 'Security question'],
    ['pin', 'PIN'],
];

const Banner = ({ kind, children }) => children ? (
    <div style={{
        marginBottom: 16,
        padding: '10px 12px',
        borderRadius: 6,
        fontSize: 14,
        color: kind === 'bad' ? '#b42318' : '#166534',
        background: kind === 'bad' ? '#fef3f2' : '#dcfce7',
    }}>{children}</div>
) : null;

const Field = ({ id, label, hint, ...props }) => (
    <div className="form-group">
        <label htmlFor={id}>{label}</label>
        <input id={id} {...props} />
        {hint && <p style={{ margin: '4px 0 0', fontSize: 12.5, color: '#6b7280' }}>{hint}</p>}
    </div>
);

const onlyDigits = (v) => v.replace(/\D/g, '').slice(0, 8);
const PIN_HINT = '4 to 8 digits. Not one repeated digit, and not a run like 1234.';

// ── Password ──────────────────────────────────────────────────────────────
// Lifted from the old /admin/change-password page. Unchanged behaviour: the
// endpoint invalidates every other session and hands back a fresh token pair,
// which we store so this tab stays signed in.
function PasswordTab() {
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
        setError(''); setDone(false);
        if (!current) { setError('Enter your current password.'); return; }
        if (!passwordMeetsPolicy(password)) { setError('Your new password does not meet all the rules yet.'); return; }
        if (password !== confirm) { setError("The two passwords don't match."); return; }
        setSaving(true);
        try {
            const data = await apiCall('/auth/me/password', {
                method: 'POST',
                body: JSON.stringify({ current_password: current, new_password: password, confirm_password: confirm }),
            });
            login(data.access_token);
            setCurrent(''); setPassword(''); setConfirm(''); setDone(true);
        } catch (err) {
            setError(err.message || 'Could not change your password.');
        }
        setSaving(false);
    };

    const field = (id, label, value, onChange, autoComplete) => (
        <Field
            id={id} label={label} type={show ? 'text' : 'password'} autoComplete={autoComplete}
            value={value} onChange={(e) => { onChange(e.target.value); setError(''); setDone(false); }}
        />
    );

    return (
        <div className="card" style={{ maxWidth: 480 }}>
            <p style={{ color: '#6b7280', fontSize: 14, marginBottom: 18 }}>
                Choose a new password for your account. This signs you out on every other device;
                this one stays signed in.
            </p>
            <Banner kind="bad">{error}</Banner>
            <Banner>{done ? 'Password changed. Other sessions have been signed out.' : ''}</Banner>
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
    );
}

// ── Phone ─────────────────────────────────────────────────────────────────
function PhoneTab({ status, reload, gate }) {
    const [phone, setPhone] = useState('');
    const [code, setCode] = useState('');
    const [sentTo, setSentTo] = useState(null);
    const [error, setError] = useState('');
    const [done, setDone] = useState('');
    const [busy, setBusy] = useState(false);

    const used = status.phone_changes_used;
    const limit = status.phone_changes_limit;
    const exhausted = used >= limit;
    const resets = status.phone_change_window_resets_at
        ? new Date(status.phone_change_window_resets_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
        : null;

    const send = async (e) => {
        e.preventDefault();
        setError(''); setDone(''); setBusy(true);
        try {
            const res = await gate.run(() => apiCall('/auth/me/phone', { method: 'POST', body: JSON.stringify({ phone }) }));
            if (res) setSentTo(res.phone);
        } catch (err) { setError(err.message); }
        setBusy(false);
    };

    const verify = async (e) => {
        e.preventDefault();
        setError(''); setBusy(true);
        try {
            const res = await gate.run(() => apiCall('/auth/me/phone/verify', { method: 'POST', body: JSON.stringify({ code }) }));
            if (res) {
                setDone(res.changed ? `Your number is now ${res.phone}.` : 'Your number has been re-confirmed.');
                setSentTo(null); setPhone(''); setCode('');
                reload();
            }
        } catch (err) { setError(err.message); }
        setBusy(false);
    };

    return (
        <div className="card" style={{ maxWidth: 520 }}>
            <div className="flex-between" style={{ marginBottom: 12 }}>
                <div>
                    <p style={{ margin: 0, fontSize: 14, color: '#6b7280' }}>Verified number</p>
                    <p style={{ margin: '2px 0 0', fontSize: 16, fontWeight: 600 }}>
                        {status.phone || 'None on file'}
                    </p>
                </div>
                <span className={`badge ${status.phone_verified ? 'badge-green' : 'badge-gray'}`}>
                    {status.phone_verified ? 'Verified' : 'Unverified'}
                </span>
            </div>

            <p style={{ color: '#6b7280', fontSize: 13.5 }}>
                This is where password and PIN reset codes are sent, so it's the most important
                detail on your account. You can change it {limit} times every{' '}
                {status.phone_change_window_days} days — <strong>{used} of {limit} used</strong>
                {exhausted && resets ? `, next available ${resets}` : ''}. Whenever it changes, the
                old number is texted so you find out if it wasn't you.
            </p>

            <Banner kind="bad">{error}</Banner>
            <Banner>{done}</Banner>

            {exhausted && !sentTo ? (
                <p style={{ color: '#92400e', background: '#fffbeb', padding: '10px 12px', borderRadius: 6, fontSize: 14 }}>
                    You've used all {limit} changes for now{resets ? `. You can change it again after ${resets}.` : '.'}{' '}
                    You can still re-confirm the number already on file.
                </p>
            ) : null}

            {!sentTo ? (
                <form onSubmit={send} noValidate>
                    <Field
                        id="new-phone" label="Phone number" type="tel" autoComplete="tel"
                        placeholder="0244123456" value={phone}
                        hint="Ghana mobile number. We'll text a code to confirm it."
                        onChange={(e) => { setPhone(e.target.value); setError(''); }}
                    />
                    <button type="submit" className="btn btn-primary" disabled={busy || !phone}>
                        {busy ? 'Sending…' : 'Send code'}
                    </button>
                </form>
            ) : (
                <form onSubmit={verify} noValidate>
                    <p style={{ fontSize: 14 }}>We sent a code to <strong>{sentTo}</strong>.</p>
                    <Field
                        id="phone-code" label="6-digit code" inputMode="numeric" autoComplete="one-time-code"
                        value={code} onChange={(e) => { setCode(e.target.value.replace(/\D/g, '').slice(0, 6)); setError(''); }}
                    />
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button type="submit" className="btn btn-primary" disabled={busy || code.length < 6}>
                            {busy ? 'Confirming…' : 'Confirm number'}
                        </button>
                        <button type="button" className="btn" onClick={() => { setSentTo(null); setCode(''); }} disabled={busy}>
                            Start over
                        </button>
                    </div>
                </form>
            )}
        </div>
    );
}

// ── Security question ─────────────────────────────────────────────────────
function QuestionTab({ status, reload, gate }) {
    const [question, setQuestion] = useState(status.security_question || '');
    const [answer, setAnswer] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [done, setDone] = useState(false);
    const [busy, setBusy] = useState(false);

    const current = status.security_questions.find((q) => q.key === status.security_question);

    const submit = async (e) => {
        e.preventDefault();
        setError(''); setDone(false); setBusy(true);
        try {
            const res = await gate.run(() => apiCall('/auth/me/security-question', {
                method: 'POST',
                body: JSON.stringify({ current_password: password, security_question: question, security_answer: answer }),
            }));
            if (res) { setDone(true); setAnswer(''); setPassword(''); reload(); }
        } catch (err) { setError(err.message); }
        setBusy(false);
    };

    return (
        <div className="card" style={{ maxWidth: 520 }}>
            <p style={{ color: '#6b7280', fontSize: 14 }}>
                Used to confirm it's you if you ever forget your password.
                {current ? <> Your current question is <strong>{current.text}</strong></> : ' You haven\'t set one yet.'}
            </p>
            <Banner kind="bad">{error}</Banner>
            <Banner>{done ? 'Security question updated.' : ''}</Banner>
            <form onSubmit={submit} noValidate>
                <div className="form-group">
                    <label htmlFor="sq-question">Question</label>
                    <select id="sq-question" value={question} onChange={(e) => { setQuestion(e.target.value); setError(''); }}>
                        <option value="" disabled>Choose a question…</option>
                        {status.security_questions.map((q) => <option key={q.key} value={q.key}>{q.text}</option>)}
                    </select>
                </div>
                <Field
                    id="sq-answer" label="Answer" autoComplete="off" value={answer}
                    hint="Not case-sensitive. Spacing and accents are ignored."
                    onChange={(e) => { setAnswer(e.target.value); setError(''); }}
                />
                <Field
                    id="sq-password" label="Your password" type="password" autoComplete="current-password"
                    value={password} onChange={(e) => { setPassword(e.target.value); setError(''); }}
                />
                <button type="submit" className="btn btn-primary" disabled={busy || !question || !answer || !password}>
                    {busy ? 'Saving…' : 'Save question'}
                </button>
            </form>
        </div>
    );
}

// ── PIN ───────────────────────────────────────────────────────────────────
function PinTab({ status, reload, gate }) {
    const [mode, setMode] = useState('set');          // 'set' | 'reset'
    const [currentPin, setCurrentPin] = useState('');
    const [pin, setPin] = useState('');
    const [confirm, setConfirm] = useState('');
    const [password, setPassword] = useState('');
    const [code, setCode] = useState('');
    const [sentTo, setSentTo] = useState(null);
    const [error, setError] = useState('');
    const [done, setDone] = useState('');
    const [busy, setBusy] = useState(false);

    const hasPin = status.has_pin;
    const lockedUntil = status.pin_locked_until ? new Date(status.pin_locked_until) : null;
    const locked = lockedUntil && lockedUntil > new Date();

    const clear = () => { setCurrentPin(''); setPin(''); setConfirm(''); setPassword(''); setCode(''); };

    const save = async (e) => {
        e.preventDefault();
        setError(''); setDone(''); setBusy(true);
        try {
            const body = { current_password: password, new_pin: pin, confirm_pin: confirm };
            if (hasPin) body.current_pin = currentPin;
            // Setting a FIRST pin is never gated (there is no PIN to prove
            // yet); replacing one is, so this goes through the gate either way.
            const res = await gate.run(() => apiCall('/auth/me/pin', { method: 'POST', body: JSON.stringify(body) }));
            if (res) { setDone(hasPin ? 'PIN changed.' : 'PIN set. You can now manage payment settings.'); clear(); reload(); }
        } catch (err) { setError(err.message); }
        setBusy(false);
    };

    const sendCode = async () => {
        setError(''); setDone(''); setBusy(true);
        try {
            const res = await apiCall('/auth/me/pin/forgot', { method: 'POST' });
            setSentTo(res.phone);
        } catch (err) { setError(err.message); }
        setBusy(false);
    };

    const resetPin = async (e) => {
        e.preventDefault();
        setError(''); setBusy(true);
        try {
            await apiCall('/auth/me/pin/reset', {
                method: 'POST',
                body: JSON.stringify({ code, current_password: password, new_pin: pin, confirm_pin: confirm }),
            });
            setDone('PIN reset. Any lock has been cleared.');
            setSentTo(null); setMode('set'); clear(); reload();
        } catch (err) { setError(err.message); }
        setBusy(false);
    };

    return (
        <div className="card" style={{ maxWidth: 520 }}>
            <div className="flex-between" style={{ marginBottom: 12 }}>
                <div>
                    <p style={{ margin: 0, fontSize: 14, color: '#6b7280' }}>Security PIN</p>
                    <p style={{ margin: '2px 0 0', fontSize: 16, fontWeight: 600 }}>
                        {hasPin ? 'Set' : 'Not set yet'}
                    </p>
                </div>
                <span className={`badge ${locked ? 'badge-red' : hasPin ? 'badge-green' : 'badge-gray'}`}>
                    {locked ? 'Locked' : hasPin ? 'Active' : 'None'}
                </span>
            </div>

            <p style={{ color: '#6b7280', fontSize: 13.5 }}>
                Asked for before changes to your payment or SMS settings, before paying an invoice,
                and before changing your phone or security question. One entry covers the next 15
                minutes.
            </p>

            {locked && (
                <p style={{ color: '#92400e', background: '#fffbeb', padding: '10px 12px', borderRadius: 6, fontSize: 14 }}>
                    Locked after too many incorrect entries until{' '}
                    {lockedUntil.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}.
                    Reset it below to unlock straight away.
                </p>
            )}

            <Banner kind="bad">{error}</Banner>
            <Banner>{done}</Banner>

            {mode === 'set' ? (
                <form onSubmit={save} noValidate>
                    {hasPin && (
                        <Field
                            id="pin-current" label="Current PIN" type="password" inputMode="numeric" autoComplete="off"
                            value={currentPin} onChange={(e) => { setCurrentPin(onlyDigits(e.target.value)); setError(''); }}
                        />
                    )}
                    <Field
                        id="pin-new" label={hasPin ? 'New PIN' : 'Choose a PIN'} type="password" inputMode="numeric"
                        autoComplete="off" hint={PIN_HINT} value={pin}
                        onChange={(e) => { setPin(onlyDigits(e.target.value)); setError(''); }}
                    />
                    <Field
                        id="pin-confirm2" label="Confirm PIN" type="password" inputMode="numeric" autoComplete="off"
                        value={confirm} onChange={(e) => { setConfirm(onlyDigits(e.target.value)); setError(''); }}
                    />
                    <Field
                        id="pin-pw" label="Your password" type="password" autoComplete="current-password"
                        value={password} onChange={(e) => { setPassword(e.target.value); setError(''); }}
                    />
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button type="submit" className="btn btn-primary" disabled={busy || !pin || !confirm || !password}>
                            {busy ? 'Saving…' : hasPin ? 'Change PIN' : 'Set PIN'}
                        </button>
                        {hasPin && (
                            <button type="button" className="btn" onClick={() => { setMode('reset'); setError(''); clear(); }}>
                                I've forgotten it
                            </button>
                        )}
                    </div>
                </form>
            ) : !sentTo ? (
                <div>
                    <p style={{ fontSize: 14 }}>
                        We'll text a code to your verified phone{status.phone ? <> ({status.phone})</> : ''}.
                        With that code and your password you can set a new PIN, even while it's locked.
                    </p>
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button type="button" className="btn btn-primary" onClick={sendCode} disabled={busy || !status.phone_verified}>
                            {busy ? 'Sending…' : 'Send code'}
                        </button>
                        <button type="button" className="btn" onClick={() => { setMode('set'); setError(''); }}>Back</button>
                    </div>
                    {!status.phone_verified && (
                        <p style={{ marginTop: 10, fontSize: 13.5, color: '#92400e' }}>
                            Your phone isn't verified, so we can't send a code. Ask platform support to reset your account.
                        </p>
                    )}
                </div>
            ) : (
                <form onSubmit={resetPin} noValidate>
                    <p style={{ fontSize: 14 }}>We sent a code to <strong>{sentTo}</strong>.</p>
                    <Field
                        id="pin-code" label="6-digit code" inputMode="numeric" autoComplete="one-time-code"
                        value={code} onChange={(e) => { setCode(e.target.value.replace(/\D/g, '').slice(0, 6)); setError(''); }}
                    />
                    <Field
                        id="pin-reset-new" label="New PIN" type="password" inputMode="numeric" autoComplete="off"
                        hint={PIN_HINT} value={pin} onChange={(e) => { setPin(onlyDigits(e.target.value)); setError(''); }}
                    />
                    <Field
                        id="pin-reset-confirm" label="Confirm PIN" type="password" inputMode="numeric" autoComplete="off"
                        value={confirm} onChange={(e) => { setConfirm(onlyDigits(e.target.value)); setError(''); }}
                    />
                    <Field
                        id="pin-reset-pw" label="Your password" type="password" autoComplete="current-password"
                        value={password} onChange={(e) => { setPassword(e.target.value); setError(''); }}
                    />
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button type="submit" className="btn btn-primary" disabled={busy || code.length < 6 || !pin || !password}>
                            {busy ? 'Saving…' : 'Set new PIN'}
                        </button>
                        <button type="button" className="btn" onClick={() => { setSentTo(null); setMode('set'); clear(); }} disabled={busy}>
                            Cancel
                        </button>
                    </div>
                </form>
            )}
        </div>
    );
}

export default function Security() {
    const [params, setParams] = useSearchParams();
    const tab = TABS.some(([k]) => k === params.get('tab')) ? params.get('tab') : 'password';
    const [status, setStatus] = useState(null);
    const [error, setError] = useState('');
    const gate = usePinGate();

    const load = useCallback(async () => {
        try {
            setStatus(await apiCall('/auth/me/security'));
        } catch (err) {
            setError(err.message || 'Could not load your security settings.');
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const setTab = (key) => setParams(key === 'password' ? {} : { tab: key }, { replace: true });

    return (
        <>
            <PageHeader title="Security" icon="lock" />

            <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid #e2e8f0', marginBottom: 20, flexWrap: 'wrap' }}>
                {TABS.map(([key, label]) => (
                    <button
                        key={key}
                        onClick={() => setTab(key)}
                        style={{
                            background: 'none',
                            border: 'none',
                            borderBottom: tab === key ? '2px solid #2563eb' : '2px solid transparent',
                            color: tab === key ? '#1e293b' : '#64748b',
                            fontWeight: tab === key ? 600 : 400,
                            fontSize: 14,
                            padding: '8px 14px',
                            cursor: 'pointer',
                            marginBottom: -1,
                        }}
                    >
                        {label}
                    </button>
                ))}
            </div>

            <Banner kind="bad">{error}</Banner>

            {tab === 'password' && <PasswordTab />}
            {tab !== 'password' && !status && !error && <p style={{ color: '#64748b' }}>Loading…</p>}
            {tab === 'phone' && status && <PhoneTab status={status} reload={load} gate={gate} />}
            {tab === 'question' && status && <QuestionTab status={status} reload={load} gate={gate} />}
            {tab === 'pin' && status && <PinTab status={status} reload={load} gate={gate} />}

            {gate.reason && (
                <PinPrompt
                    reason={gate.reason}
                    onCancel={gate.cancel}
                    onUnlocked={async () => { await gate.unlocked(); load(); }}
                />
            )}
        </>
    );
}
