import React, { useCallback, useEffect, useRef, useState } from 'react';
import { apiCall } from '../App';

// Wraps a call that may be PIN-gated. On a 403 carrying X-Pin-Required it
// parks the call, shows <PinPrompt>, and re-runs it once the PIN clears — so
// the admin finishes what they started instead of losing it to a prompt. Any
// other error propagates untouched to the caller's own handling.
//
//   const gate = usePinGate();
//   await gate.run(() => apiCall(...));
//   {gate.reason && <PinPrompt reason={gate.reason} onCancel={gate.cancel}
//                              onUnlocked={gate.unlocked} />}
export function usePinGate() {
    const [reason, setReason] = useState(null);
    const [pending, setPending] = useState(null);

    const run = useCallback(async (fn) => {
        try {
            return await fn();
        } catch (err) {
            if (err && err.pinRequired) {
                // Stored behind an arrow: useState treats a bare function as
                // a lazy initialiser and would call it immediately.
                setPending(() => fn);
                setReason(err.pinRequired);
                return undefined;
            }
            throw err;
        }
    }, []);

    const cancel = useCallback(() => { setReason(null); setPending(null); }, []);

    const unlocked = useCallback(async () => {
        const fn = pending;
        setReason(null);
        setPending(null);
        // Back through run(), so a retry that is refused again (a PIN that
        // locked between the two calls) re-prompts rather than throwing.
        if (fn) await run(fn);
    }, [pending, run]);

    return { reason, run, cancel, unlocked };
}

// The prompt a PIN-gated page raises over itself when the API answers 403 with
// X-Pin-Required. It never navigates: the caller keeps its own state, and on
// success re-runs the request that was refused. `reason` is the header value.
//
//   setup   no PIN on the account yet  -> current password + a new PIN
//   verify  PIN exists, elevation gone -> just the PIN
//   locked  too many wrong entries     -> nothing to type; point at recovery
//
// Resetting a forgotten PIN needs an SMS round trip, so this does not try to
// inline it — it sends people to the Security page's PIN tab, which owns that
// flow, rather than growing a second copy of it inside a modal.

const PIN_HINT = '4 to 8 digits. Not one repeated digit, and not a run like 1234.';

export default function PinPrompt({ reason, onCancel, onUnlocked }) {
    const [pin, setPin] = useState('');
    const [confirm, setConfirm] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const firstField = useRef(null);

    useEffect(() => { firstField.current?.focus(); }, []);

    // Escape closes, matching the sidebar's off-canvas behaviour.
    useEffect(() => {
        const onKey = (e) => { if (e.key === 'Escape' && !busy) onCancel(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [busy, onCancel]);

    const submit = async (e) => {
        e.preventDefault();
        if (busy) return;
        setError('');
        if (reason === 'setup' && pin !== confirm) {
            setError("The two PINs don't match.");
            return;
        }
        setBusy(true);
        try {
            if (reason === 'setup') {
                await apiCall('/auth/me/pin', {
                    method: 'POST',
                    body: JSON.stringify({ current_password: password, new_pin: pin, confirm_pin: confirm }),
                });
            } else {
                await apiCall('/auth/me/pin/verify', { method: 'POST', body: JSON.stringify({ pin }) });
            }
            onUnlocked();
        } catch (err) {
            // A wrong PIN that trips the lockout comes back as pinRequired
            // 'locked' — surface that rather than the generic message, since
            // the account state just changed underneath this form.
            setError(err.message || 'That did not work.');
            setBusy(false);
        }
    };

    const digits = (value) => value.replace(/\D/g, '').slice(0, 8);

    if (reason === 'locked') {
        return (
            <div className="modal-overlay" onClick={onCancel}>
                <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
                    <h2>PIN locked</h2>
                    <p style={{ color: '#6b7280', fontSize: 14 }}>
                        Too many incorrect PIN entries, so this area is locked for a few hours.
                        We've sent a text to your verified phone.
                    </p>
                    <p style={{ color: '#6b7280', fontSize: 14 }}>
                        If it was you and you've forgotten the PIN, you can reset it now with a code
                        sent to your phone — go to <strong>Security → PIN</strong>.
                    </p>
                    <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
                        <a className="btn btn-primary" href="/admin/security?tab=pin">Reset my PIN</a>
                        <button type="button" className="btn" onClick={onCancel}>Close</button>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="modal-overlay" onClick={() => !busy && onCancel()}>
            <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
                <h2>{reason === 'setup' ? 'Set a security PIN' : 'Enter your PIN'}</h2>
                <p style={{ color: '#6b7280', fontSize: 14, marginTop: 0 }}>
                    {reason === 'setup'
                        ? 'This area needs a PIN. Choose one now — you\'ll be asked for it before changes to your payment settings or your account security.'
                        : 'For your security, please confirm your PIN to continue.'}
                </p>

                {error && (
                    <div style={{ marginBottom: 14, color: '#b42318', background: '#fef3f2', padding: '10px 12px', borderRadius: 6, fontSize: 14 }}>
                        {error}
                    </div>
                )}

                <form onSubmit={submit} noValidate>
                    {reason === 'setup' && (
                        <div className="form-group">
                            <label htmlFor="pin-password">Your password</label>
                            <input
                                id="pin-password"
                                ref={firstField}
                                type="password"
                                autoComplete="current-password"
                                value={password}
                                onChange={(e) => { setPassword(e.target.value); setError(''); }}
                            />
                        </div>
                    )}
                    <div className="form-group">
                        <label htmlFor="pin-value">{reason === 'setup' ? 'New PIN' : 'PIN'}</label>
                        <input
                            id="pin-value"
                            ref={reason === 'setup' ? undefined : firstField}
                            type="password"
                            inputMode="numeric"
                            autoComplete="off"
                            value={pin}
                            onChange={(e) => { setPin(digits(e.target.value)); setError(''); }}
                        />
                        {reason === 'setup' && (
                            <p style={{ margin: '4px 0 0', fontSize: 12.5, color: '#6b7280' }}>{PIN_HINT}</p>
                        )}
                    </div>
                    {reason === 'setup' && (
                        <div className="form-group">
                            <label htmlFor="pin-confirm">Confirm PIN</label>
                            <input
                                id="pin-confirm"
                                type="password"
                                inputMode="numeric"
                                autoComplete="off"
                                value={confirm}
                                onChange={(e) => { setConfirm(digits(e.target.value)); setError(''); }}
                            />
                        </div>
                    )}
                    <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
                        <button type="submit" className="btn btn-primary" disabled={busy || !pin}>
                            {busy ? 'Checking…' : reason === 'setup' ? 'Set PIN' : 'Continue'}
                        </button>
                        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    );
}
