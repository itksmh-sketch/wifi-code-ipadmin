import React, { useEffect, useRef, useState } from 'react';
import { apiCall } from '../App';

// Mirrors the platform defaults in backend src/modules/branding/service.py.
const DEFAULTS = {
    primary_color: '#2563eb',
    accent_color: '#764ba2',
    background_gradient_start: '#667eea',
    welcome_message: 'Enter your voucher code to get online',
};

export default function Branding() {
    const [form, setForm] = useState(null);
    const [logoUrl, setLogoUrl] = useState(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [message, setMessage] = useState('');
    const [error, setError] = useState('');
    const fileRef = useRef(null);

    const applyBranding = (data) => {
        setForm({
            portal_display_name: data.portal_display_name || '',
            portal_welcome_message: data.welcome_message || '',
            primary_color: data.primary_color || DEFAULTS.primary_color,
            accent_color: data.accent_color || DEFAULTS.accent_color,
            background_gradient_start: data.background_gradient_start || DEFAULTS.background_gradient_start,
        });
        setLogoUrl(data.logo_url || null);
    };

    const load = () => {
        setLoading(true);
        apiCall('/admin/branding')
            .then(applyBranding)
            .catch((e) => setError(e.message))
            .finally(() => setLoading(false));
    };

    useEffect(load, []);

    const update = (field, value) => setForm((cur) => ({ ...cur, [field]: value }));

    const handleSave = async (event) => {
        event.preventDefault();
        setSaving(true);
        setMessage('');
        setError('');
        try {
            const data = await apiCall('/admin/branding', {
                method: 'PUT',
                body: JSON.stringify({
                    portal_display_name: form.portal_display_name || null,
                    portal_welcome_message: form.portal_welcome_message || null,
                    primary_color: form.primary_color,
                    accent_color: form.accent_color,
                    background_gradient_start: form.background_gradient_start,
                }),
            });
            applyBranding(data);
            setMessage('Branding saved.');
        } catch (err) {
            setError(err.message);
        } finally {
            setSaving(false);
        }
    };

    const handleLogo = async (event) => {
        const file = event.target.files && event.target.files[0];
        if (!file) return;
        setUploading(true);
        setMessage('');
        setError('');
        try {
            const fd = new FormData();
            fd.append('file', file);
            const data = await apiCall('/admin/branding/logo', { method: 'POST', body: fd });
            applyBranding(data);
            setMessage('Logo updated.');
        } catch (err) {
            setError(err.message);
        } finally {
            setUploading(false);
            if (fileRef.current) fileRef.current.value = '';
        }
    };

    if (loading || !form) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <div>
                    <h1 style={{ fontSize: 24, fontWeight: 700 }}>Portal Branding</h1>
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

                        <div className="form-group" style={{ marginTop: 20 }}>
                            <label>Logo</label>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                                <LogoThumb url={logoUrl} />
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

                {/* ── Live preview ──────────────────────────────────────────── */}
                <div style={{ flex: '0 0 320px' }}>
                    <p style={{ fontSize: 12, color: '#6b7280', marginBottom: 8, fontWeight: 600 }}>LIVE PREVIEW</p>
                    <PortalPreview form={form} logoUrl={logoUrl} />
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

function LogoThumb({ url }) {
    const box = { width: 56, height: 56, borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 };
    if (url) {
        return <div style={{ ...box, border: '1px solid #e5e7eb', background: '#fff' }}><img src={url} alt="Logo" style={{ maxWidth: '100%', maxHeight: '100%', borderRadius: 8 }} /></div>;
    }
    return <div style={{ ...box, border: '1px dashed #d1d5db', background: '#f9fafb', color: '#9ca3af', fontSize: 11 }}>No logo</div>;
}

// Small mockup of the portal login card reflecting the chosen colours/logo,
// matching how the real portal applies them (gradient + primary accent).
function PortalPreview({ form, logoUrl }) {
    const displayName = form.portal_display_name || 'Connect to Wi-Fi';
    const welcome = form.portal_welcome_message || DEFAULTS.welcome_message;
    return (
        <div style={{ borderRadius: 16, padding: 20, background: `linear-gradient(135deg, ${form.background_gradient_start} 0%, ${form.accent_color} 100%)` }}>
            <div style={{ background: '#fff', borderRadius: 12, padding: '24px 20px', boxShadow: '0 10px 30px rgba(0,0,0,0.2)', textAlign: 'center' }}>
                <div style={{ marginBottom: 14 }}>
                    {logoUrl
                        ? <img src={logoUrl} alt="Logo" style={{ width: 44, height: 44, objectFit: 'contain' }} />
                        : <div style={{ width: 44, height: 44, borderRadius: '50%', background: form.primary_color, margin: '0 auto' }} />}
                </div>
                <div style={{ fontWeight: 700, fontSize: 17, color: '#1a1a2e', marginBottom: 4 }}>{displayName}</div>
                <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 16 }}>{welcome}</div>
                <div style={{ border: '2px solid #e5e7eb', borderRadius: 8, padding: '9px 12px', fontSize: 12, color: '#9ca3af', marginBottom: 12, fontFamily: 'monospace', letterSpacing: 2 }}>XXXX-XXXX-XXXX</div>
                <div style={{ background: form.primary_color, color: '#fff', borderRadius: 8, padding: '11px', fontSize: 14, fontWeight: 600 }}>Connect</div>
            </div>
        </div>
    );
}
