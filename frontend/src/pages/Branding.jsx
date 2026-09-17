import React, { useEffect, useRef, useState } from 'react';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';

// Mirrors the platform defaults in backend src/modules/branding/service.py.
const DEFAULTS = {
    primary_color: '#2563eb',
    accent_color: '#764ba2',
    background_gradient_start: '#667eea',
    welcome_message: 'Enter your voucher code to get online',
};

// Shared by the real save (PUT /admin/branding) and the debounced draft push
// (POST /admin/branding/preview-draft) — one field list, not two, so the
// preview can never drift from what a save actually sends.
const buildBrandingPayload = (form) => ({
    portal_display_name: form.portal_display_name || null,
    portal_welcome_message: form.portal_welcome_message || null,
    primary_color: form.primary_color,
    accent_color: form.accent_color,
    background_gradient_start: form.background_gradient_start,
    portal_contact_phone: form.portal_contact_phone || null,
    portal_contact_email: form.portal_contact_email || null,
    portal_template: form.portal_template,
});

export default function Branding() {
    const [form, setForm] = useState(null);
    const [logoUrl, setLogoUrl] = useState(null);
    const [logoPreviewUrl, setLogoPreviewUrl] = useState(null); // local blob, pre-upload
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [message, setMessage] = useState('');
    const [error, setError] = useState('');
    const [previewUrl, setPreviewUrl] = useState(null);
    const [previewReloadKey, setPreviewReloadKey] = useState(0);
    const fileRef = useRef(null);
    const objectUrlRef = useRef(null); // tracks the current blob: URL for revocation
    const skipNextDraftPush = useRef(true); // don't push a draft for the initial load

    const applyBranding = (data) => {
        setForm({
            portal_display_name: data.portal_display_name || '',
            portal_welcome_message: data.welcome_message || '',
            primary_color: data.primary_color || DEFAULTS.primary_color,
            accent_color: data.accent_color || DEFAULTS.accent_color,
            background_gradient_start: data.background_gradient_start || DEFAULTS.background_gradient_start,
            portal_contact_phone: data.contact_phone || '',
            portal_contact_email: data.contact_email || '',
            portal_template: data.template || 'card_centered',
        });
        setLogoUrl(data.logo_url || null);
    };

    const load = () => {
        setLoading(true);
        skipNextDraftPush.current = true;
        apiCall('/admin/branding')
            .then(applyBranding)
            .catch((e) => setError(e.message))
            .finally(() => setLoading(false));
    };

    useEffect(load, []);

    // Mint the preview token once. Its payload is just {operator_id, purpose,
    // exp} — no branding values ride in it; draft values live server-side in
    // Redis, looked up by operator_id (see preview-draft below).
    useEffect(() => {
        apiCall('/admin/branding/preview-token')
            .then((data) => setPreviewUrl(data.preview_url))
            .catch(() => { /* preview is a nice-to-have; the form still works without it */ });
    }, []);

    // Debounced draft push: 500ms after the operator stops changing a field,
    // send the current (unsaved) form state to the transient preview store.
    // The iframe picks it up on its own poll cycle (~1.5s) — nothing here
    // talks to the iframe directly.
    useEffect(() => {
        if (!form) return;
        if (skipNextDraftPush.current) {
            skipNextDraftPush.current = false;
            return;
        }
        const t = setTimeout(() => {
            apiCall('/admin/branding/preview-draft', {
                method: 'POST',
                body: JSON.stringify(buildBrandingPayload(form)),
            }).catch(() => { /* preview draft is best-effort */ });
        }, 500);
        return () => clearTimeout(t);
    }, [form]);

    useEffect(() => () => {
        if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    }, []);

    const update = (field, value) => setForm((cur) => ({ ...cur, [field]: value }));

    const handleSave = async (event) => {
        event.preventDefault();
        setSaving(true);
        setMessage('');
        setError('');
        try {
            const data = await apiCall('/admin/branding', {
                method: 'PUT',
                body: JSON.stringify(buildBrandingPayload(form)),
            });
            skipNextDraftPush.current = true;
            applyBranding(data);
            setMessage('Branding saved.');
            setPreviewReloadKey((k) => k + 1); // verified fallback: fresh SSR from the now-persisted row
        } catch (err) {
            setError(err.message);
        } finally {
            setSaving(false);
        }
    };

    const handleLogo = async (event) => {
        const file = event.target.files && event.target.files[0];
        if (!file) return;

        // Instant local feedback — same document as the form, so no cross-frame
        // object-URL risk (see the iframe note below for why that matters).
        if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
        const blobUrl = URL.createObjectURL(file);
        objectUrlRef.current = blobUrl;
        setLogoPreviewUrl(blobUrl);

        setUploading(true);
        setMessage('');
        setError('');
        try {
            const fd = new FormData();
            fd.append('file', file);
            const data = await apiCall('/admin/branding/logo', { method: 'POST', body: fd });
            skipNextDraftPush.current = true;
            applyBranding(data);
            setMessage('Logo updated.');
            // Logo upload persists immediately (not part of the draft/Save flow),
            // so the iframe's next poll would pick it up regardless — this just
            // closes the gap to "as fast as the upload itself" instead of
            // waiting up to one poll interval.
            setPreviewReloadKey((k) => k + 1);
        } catch (err) {
            setError(err.message);
        } finally {
            if (objectUrlRef.current) {
                URL.revokeObjectURL(objectUrlRef.current);
                objectUrlRef.current = null;
            }
            setLogoPreviewUrl(null);
            setUploading(false);
            if (fileRef.current) fileRef.current.value = '';
        }
    };

    if (loading || !form) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <div>
                    <PageHeader title="Portal Branding" />
                    <p style={{ color: '#6b7280', fontSize: 14, marginTop: 4 }}>
                        Customise how your captive portal looks to customers. Unset fields use the platform defaults.
                    </p>
                </div>
            </div>

            {message && <div className="badge badge-green" style={{ marginBottom: 16 }}>{message}</div>}
            {error && <div className="badge badge-red" style={{ marginBottom: 16 }}>{error}</div>}

            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'flex-start' }}>
                {/* ── Settings form ─────────────────────────────────────────── */}
                <div className="card" style={{ flex: '1 1 420px', maxWidth: 560 }}>
                    <form onSubmit={handleSave}>
                        <div className="form-group">
                            <label>Display name</label>
                            <input
                                type="text"
                                value={form.portal_display_name}
                                onChange={(e) => update('portal_display_name', e.target.value)}
                                placeholder="e.g. Acme Wi-Fi"
                                maxLength={120}
                            />
                        </div>

                        <div className="form-group">
                            <label>Welcome message</label>
                            <textarea
                                value={form.portal_welcome_message}
                                onChange={(e) => update('portal_welcome_message', e.target.value)}
                                placeholder={DEFAULTS.welcome_message}
                                maxLength={500}
                                rows={3}
                                style={{ width: '100%', padding: 12, border: '2px solid #e5e7eb', borderRadius: 8, fontSize: 14, fontFamily: 'inherit', resize: 'vertical' }}
                            />
                        </div>

                        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                            <ColorField label="Primary" value={form.primary_color} onChange={(v) => update('primary_color', v)} />
                            <ColorField label="Accent" value={form.accent_color} onChange={(v) => update('accent_color', v)} />
                            <ColorField label="Gradient start" value={form.background_gradient_start} onChange={(v) => update('background_gradient_start', v)} />
                        </div>

                        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginTop: 4 }}>
                            <div className="form-group" style={{ flex: '1 1 200px' }}>
                                <label>Contact phone</label>
                                <input
                                    type="tel"
                                    value={form.portal_contact_phone}
                                    onChange={(e) => update('portal_contact_phone', e.target.value)}
                                    placeholder="e.g. 0244123456"
                                    maxLength={64}
                                />
                            </div>
                            <div className="form-group" style={{ flex: '1 1 200px' }}>
                                <label>Contact email</label>
                                <input
                                    type="email"
                                    value={form.portal_contact_email}
                                    onChange={(e) => update('portal_contact_email', e.target.value)}
                                    placeholder="e.g. support@acmewifi.com"
                                    maxLength={255}
                                />
                            </div>
                        </div>
                        <p style={{ color: '#6b7280', fontSize: 12, marginTop: -12, marginBottom: 8 }}>
                            Shown in the portal footer. Leave blank to keep the generic "Contact your local ISP office" text.
                        </p>

                        <div className="form-group" style={{ marginTop: 20 }}>
                            <label>Template</label>
                            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                                <TemplateOption
                                    value="card_centered"
                                    label="Card centered"
                                    description="Boxed, rounded card over the gradient — today's default."
                                    selected={form.portal_template === 'card_centered'}
                                    onSelect={() => update('portal_template', 'card_centered')}
                                />
                                <TemplateOption
                                    value="full_bleed"
                                    label="Full bleed"
                                    description="Flush edge-to-edge panel, no card chrome — a mobile-app feel."
                                    selected={form.portal_template === 'full_bleed'}
                                    onSelect={() => update('portal_template', 'full_bleed')}
                                />
                            </div>
                        </div>

                        <div className="form-group" style={{ marginTop: 20 }}>
                            <label>Logo</label>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                                <LogoThumb url={logoPreviewUrl || logoUrl} />
                                <div>
                                    <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" onChange={handleLogo} disabled={uploading} />
                                    <p style={{ color: '#6b7280', fontSize: 12, marginTop: 6 }}>
                                        PNG, JPG or WebP, up to 2 MB.{uploading ? ' Uploading…' : ''}
                                    </p>
                                </div>
                            </div>
                        </div>

                        <button type="submit" className="btn btn-primary" disabled={saving} style={{ marginTop: 12, width: 'auto', padding: '10px 20px' }}>
                            {saving ? 'Saving...' : 'Save changes'}
                        </button>
                    </form>
                </div>

                {/* ── Mobile preview: the real portal page, in an iframe ──────── */}
                <div style={{ flex: '0 0 auto' }}>
                    <p style={{ fontSize: 12, color: '#6b7280', marginBottom: 8, fontWeight: 600 }}>MOBILE PREVIEW</p>
                    <PhonePreview src={previewUrl} reloadKey={previewReloadKey} />
                    <p style={{ fontSize: 11, color: '#9ca3af', marginTop: 8, maxWidth: 220 }}>
                        Live — reflects edits a moment after you stop typing. Logo changes appear as soon as the upload finishes.
                    </p>
                </div>
            </div>
        </div>
    );
}

function ColorField({ label, value, onChange }) {
    return (
        <div className="form-group" style={{ flex: '1 1 140px' }}>
            <label>{label}</label>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <input type="color" value={value} onChange={(e) => onChange(e.target.value)} style={{ width: 40, height: 38, padding: 2, border: '2px solid #e5e7eb', borderRadius: 8, background: '#fff', cursor: 'pointer' }} />
                <input type="text" value={value} onChange={(e) => onChange(e.target.value)} placeholder="#2563eb" style={{ flex: 1, fontFamily: 'monospace' }} />
            </div>
        </div>
    );
}

// Swatch mirrors the actual [data-portal-template] rules in style.css (radius
// + shadow on/off) so the picker itself previews the structural difference —
// a static schematic icon for the control, not a live reflection of the
// operator's actual branding (that's what the iframe to the right is for).
function TemplateOption({ value, label, description, selected, onSelect }) {
    const isFullBleed = value === 'full_bleed';
    return (
        <button
            type="button"
            onClick={onSelect}
            style={{
                flex: '1 1 180px',
                textAlign: 'left',
                border: selected ? '2px solid #2563eb' : '2px solid #e5e7eb',
                borderRadius: 10,
                padding: 12,
                background: selected ? '#eff6ff' : '#fff',
                cursor: 'pointer',
            }}
        >
            <div style={{ background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', borderRadius: 8, padding: 10, marginBottom: 8 }}>
                <div style={{
                    background: '#fff',
                    height: 28,
                    borderRadius: isFullBleed ? 0 : 6,
                    boxShadow: isFullBleed ? 'none' : '0 4px 10px rgba(0,0,0,0.25)',
                    margin: isFullBleed ? 0 : '0 8px',
                }} />
            </div>
            <div style={{ fontWeight: 600, fontSize: 13, color: '#111827' }}>{label}</div>
            <div style={{ fontSize: 11, color: '#6b7280', marginTop: 2 }}>{description}</div>
        </button>
    );
}

function LogoThumb({ url }) {
    const box = { width: 56, height: 56, borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 };
    if (url) {
        return <div style={{ ...box, border: '1px solid #e5e7eb', background: '#fff' }}><img src={url} alt="Logo" style={{ maxWidth: '100%', maxHeight: '100%', borderRadius: 8 }} /></div>;
    }
    return <div style={{ ...box, border: '1px dashed #d1d5db', background: '#f9fafb', color: '#9ca3af', fontSize: 11 }}>No logo</div>;
}

// The real /portal/login page, in a phone-sized frame. No mock to keep in
// sync: colours/logo/template/footer are the same server-rendered page a
// customer gets, driven by the same branding.js that page always ships with.
// `reloadKey` forces a remount (fresh request) after a save or logo upload;
// live edits before that arrive via branding.js's own poll of
// /portal/branding/{rt} while is_preview is true — nothing here talks to the
// iframe directly.
function PhonePreview({ src, reloadKey }) {
    const frameOuter = {
        width: 300,
        height: 620,
        borderRadius: 32,
        background: '#111827',
        padding: 12,
        boxShadow: '0 12px 30px rgba(0,0,0,0.25)',
    };
    const frameInner = {
        width: '100%',
        height: '100%',
        borderRadius: 20,
        overflow: 'hidden',
        background: '#fff',
    };
    if (!src) {
        return (
            <div style={frameOuter}>
                <div style={{ ...frameInner, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#9ca3af', fontSize: 12, textAlign: 'center', padding: 16 }}>
                    Loading preview…
                </div>
            </div>
        );
    }
    return (
        <div style={frameOuter}>
            <div style={frameInner}>
                <iframe
                    key={reloadKey}
                    src={src}
                    title="Portal preview"
                    style={{ width: '100%', height: '100%', border: 'none' }}
                />
            </div>
        </div>
    );
}
