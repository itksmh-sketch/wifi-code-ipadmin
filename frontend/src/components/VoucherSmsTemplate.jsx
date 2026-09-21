import React, { useCallback, useEffect, useRef, useState } from 'react';
import { apiCall } from '../App';

// Editor for the purchase-confirmation SMS (GET/PUT /sms-template/voucher).
//
// Every length rule lives on the server — the same validation the save path
// runs is what /voucher/preview reports — so this never re-implements the
// segment maths. It debounces like the branding page's draft push (that one is
// 500ms for an iframe; 400ms here, since the result lands in-place) and shows
// the server's own message rather than a generic "save failed": an operator who
// typed {vouchercode} needs to be told which placeholder is wrong.
const DEBOUNCE_MS = 400;

const cardStyle = { flex: '1 1 420px', maxWidth: 560 };
const monoStyle = { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' };

export default function VoucherSmsTemplate() {
    const [data, setData] = useState(null);       // last server payload
    const [text, setText] = useState('');         // what's in the textarea
    const [preview, setPreview] = useState(null); // {valid, error, preview:{...}}
    const [checking, setChecking] = useState(false);
    const [saving, setSaving] = useState(false);
    const [loadError, setLoadError] = useState('');
    const [message, setMessage] = useState('');
    const textareaRef = useRef(null);

    const applyPayload = useCallback((payload) => {
        setData(payload);
        setText(payload.effective_template);
        // The GET/PUT payload already carries a measured preview of the stored
        // text, so the counters are correct before the first keystroke.
        setPreview({ valid: true, error: null, preview: payload.preview });
    }, []);

    useEffect(() => {
        apiCall('/sms-template/voucher')
            .then(applyPayload)
            .catch((err) => setLoadError(err.message || 'Could not load your SMS template.'));
    }, [applyPayload]);

    // Debounced measure. Skipped while the text still equals what the server
    // last told us, so opening the page issues no extra request.
    useEffect(() => {
        if (!data || text === data.effective_template) return;
        setChecking(true);
        const timer = setTimeout(() => {
            apiCall('/sms-template/voucher/preview', {
                method: 'POST',
                body: JSON.stringify({ template: text }),
            })
                .then(setPreview)
                .catch((err) => setPreview({
                    valid: false,
                    error: err.message || 'Could not check this message.',
                    preview: null,
                }))
                .finally(() => setChecking(false));
        }, DEBOUNCE_MS);
        return () => { clearTimeout(timer); setChecking(false); };
    }, [text, data]);

    // Insert at the caret rather than appending: an operator adding {operator}
    // mid-sentence should not have to retype the rest.
    const insertPlaceholder = (key) => {
        const token = `{${key}}`;
        const el = textareaRef.current;
        if (!el) { setText((cur) => cur + token); return; }
        const start = el.selectionStart ?? text.length;
        const end = el.selectionEnd ?? text.length;
        const next = text.slice(0, start) + token + text.slice(end);
        setText(next);
        setMessage('');
        requestAnimationFrame(() => {
            el.focus();
            el.setSelectionRange(start + token.length, start + token.length);
        });
    };

    const save = async (event) => {
        event.preventDefault();
        setSaving(true);
        setMessage('');
        try {
            applyPayload(await apiCall('/sms-template/voucher', {
                method: 'PUT',
                body: JSON.stringify({ template: text }),
            }));
            setMessage('Message saved. New voucher purchases will use it.');
        } catch (err) {
            // A 400 here is the server's specific validation text; show it where
            // the live checker's messages appear, not as a toast.
            setPreview({ valid: false, error: err.message || 'Could not save this message.', preview: null });
        } finally {
            setSaving(false);
        }
    };

    const resetToDefault = async () => {
        setSaving(true);
        setMessage('');
        try {
            applyPayload(await apiCall('/sms-template/voucher', {
                method: 'PUT',
                body: JSON.stringify({ template: null }),
            }));
            setMessage('Reset to the platform default message.');
        } catch (err) {
            setPreview({ valid: false, error: err.message || 'Could not reset this message.', preview: null });
        } finally {
            setSaving(false);
        }
    };

    if (loadError) return <div className="badge badge-red" style={{ marginTop: 24 }}>{loadError}</div>;
    if (!data) return null;

    const measured = preview && preview.preview;
    const invalid = preview && preview.valid === false;
    const dirty = text !== data.effective_template;
    const overOneSegment = measured && measured.segment_count > 1;

    return (
        <div style={{ marginTop: 36 }}>
            <div className="flex-between">
                <div>
                    <h2 style={{ fontSize: 18, fontWeight: 600, margin: 0 }}>Purchase confirmation SMS</h2>
                    <p style={{ color: '#6b7280', fontSize: 14, marginTop: 4 }}>
                        The message a customer receives with their voucher code after paying.
                        {data.is_default
                            ? ' You are using the platform default.'
                            : ' You are using your own wording.'}
                    </p>
                </div>
                <span className={`badge ${data.is_default ? 'badge-gray' : 'badge-green'}`} style={{ whiteSpace: 'nowrap' }}>
                    {data.is_default ? 'Platform default' : 'Customised'}
                </span>
            </div>

            {message && <div className="badge badge-green" style={{ marginBottom: 16 }}>{message}</div>}

            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'flex-start' }}>
                {/* ── Editor ────────────────────────────────────────────────── */}
                <div className="card" style={cardStyle}>
                    <form onSubmit={save}>
                        <div className="form-group">
                            <label htmlFor="voucher-sms-template">Message</label>
                            <textarea
                                id="voucher-sms-template"
                                ref={textareaRef}
                                value={text}
                                onChange={(e) => { setText(e.target.value); setMessage(''); }}
                                rows={5}
                                maxLength={320}
                                spellCheck
                                style={{
                                    width: '100%', padding: 12, borderRadius: 8, fontSize: 14,
                                    fontFamily: 'inherit', resize: 'vertical',
                                    border: `2px solid ${invalid ? '#fca5a5' : '#e5e7eb'}`,
                                }}
                            />
                        </div>

                        <label style={{ display: 'block', fontSize: 13, fontWeight: 500, marginBottom: 6 }}>
                            Insert a placeholder
                        </label>
                        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
                            {data.placeholders.map((p) => (
                                <button
                                    key={p.key}
                                    type="button"
                                    onClick={() => insertPlaceholder(p.key)}
                                    title={p.description}
                                    style={{
                                        ...monoStyle, cursor: 'pointer', fontSize: 13, padding: '5px 10px',
                                        borderRadius: 6, border: '1px solid #d1d5db', background: '#f9fafb', color: '#374151',
                                    }}
                                >
                                    {`{${p.key}}`}
                                </button>
                            ))}
                        </div>
                        <ul style={{ color: '#6b7280', fontSize: 12.5, margin: '0 0 18px', paddingLeft: 18 }}>
                            {data.placeholders.map((p) => (
                                <li key={p.key} style={{ marginBottom: 2 }}>
                                    <code style={monoStyle}>{`{${p.key}}`}</code> — {p.description}
                                </li>
                            ))}
                        </ul>

                        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                            <button
                                type="submit"
                                className="btn btn-primary"
                                disabled={saving || checking || invalid || !dirty}
                            >
                                {saving ? 'Saving…' : 'Save message'}
                            </button>
                            <button
                                type="button"
                                className="btn"
                                onClick={resetToDefault}
                                disabled={saving || data.is_default}
                                title={data.is_default ? 'Already using the platform default' : 'Restore the platform default wording'}
                            >
                                Reset to default
                            </button>
                            {dirty && !saving && (
                                <button
                                    type="button"
                                    className="btn"
                                    onClick={() => { setText(data.effective_template); setPreview({ valid: true, error: null, preview: data.preview }); }}
                                >
                                    Discard changes
                                </button>
                            )}
                        </div>
                    </form>
                </div>

                {/* ── Live measurement ──────────────────────────────────────── */}
                <div className="card" style={cardStyle}>
                    <h3 style={{ fontSize: 15, fontWeight: 600, margin: '0 0 4px' }}>Preview</h3>
                    <p style={{ color: '#6b7280', fontSize: 12.5, margin: '0 0 14px' }}>
                        Measured with the longest plan name and business name, so a real send cannot come out longer.
                    </p>

                    {invalid ? (
                        <div style={{
                            color: '#b42318', background: '#fef3f2', border: '1px solid #fecdca',
                            padding: '10px 12px', borderRadius: 8, fontSize: 13.5, marginBottom: 14,
                        }}>
                            {preview.error}
                        </div>
                    ) : (
                        <div style={{
                            ...monoStyle, background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 8,
                            padding: 12, fontSize: 13, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                            marginBottom: 14, opacity: checking ? 0.55 : 1,
                        }}>
                            {measured ? measured.text : ''}
                        </div>
                    )}

                    {measured && (
                        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
                            <Stat label="Characters" value={measured.character_count} />
                            <Stat
                                label="SMS segments"
                                value={measured.segment_count}
                                tone={overOneSegment ? 'bad' : 'good'}
                            />
                            <Stat label="Encoding" value={measured.encoding === 'gsm7' ? 'GSM-7' : 'Unicode'} />
                        </div>
                    )}

                    {measured && measured.encoding === 'ucs2' && !invalid && (
                        <p style={{ color: '#92400e', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: 8, padding: '8px 10px', fontSize: 12.5, margin: '0 0 12px' }}>
                            This message contains characters outside the GSM alphabet (an emoji, or a “smart” quote),
                            which cuts the single-SMS limit from 160 characters to 70.
                        </p>
                    )}

                    <p style={{ color: '#6b7280', fontSize: 12, margin: 0, lineHeight: 1.5 }}>
                        {data.code_length_caveat}
                    </p>

                    {checking && (
                        <p style={{ color: '#6b7280', fontSize: 12, marginTop: 10, marginBottom: 0 }}>Checking…</p>
                    )}
                </div>
            </div>
        </div>
    );
}

function Stat({ label, value, tone }) {
    const colors = {
        good: { color: '#166534', background: '#dcfce7', border: '#bbf7d0' },
        bad: { color: '#b42318', background: '#fef3f2', border: '#fecdca' },
    }[tone] || { color: '#374151', background: '#f3f4f6', border: '#e5e7eb' };
    return (
        <div style={{
            background: colors.background, border: `1px solid ${colors.border}`,
            borderRadius: 8, padding: '8px 12px', minWidth: 96,
        }}>
            <div style={{ fontSize: 11, color: '#6b7280', marginBottom: 2 }}>{label}</div>
            <div style={{ fontSize: 17, fontWeight: 700, color: colors.color }}>{value}</div>
        </div>
    );
}
