import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

const emptyForm = {
    provider: 'paystack',
    public_key: '',
    secret_key: '',
    webhook_secret: '',
    is_active: true,
};

export default function PaymentCredentials() {
    const [credentials, setCredentials] = useState(null);
    const [form, setForm] = useState(emptyForm);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [message, setMessage] = useState('');
    const [error, setError] = useState('');

    const loadCredentials = () => {
        setLoading(true);
        apiCall('/payment-credentials').then((data) => {
            setCredentials(data);
            setLoading(false);
        });
    };

    useEffect(() => {
        loadCredentials();
    }, []);

    const updateField = (field, value) => {
        setForm((current) => ({ ...current, [field]: value }));
    };

    const handleSave = async (event) => {
        event.preventDefault();
        setSaving(true);
        setMessage('');
        setError('');
        const data = await apiCall('/payment-credentials', {
            method: 'PUT',
            body: JSON.stringify({
                ...form,
                webhook_secret: form.webhook_secret || null,
            }),
        });
        setSaving(false);
        if (data?.detail) {
            setError(data.detail);
            return;
        }
        setCredentials(data);
        setForm(emptyForm);
        setMessage('Payment credentials saved.');
    };

    const handleTest = async () => {
        setTesting(true);
        setMessage('');
        setError('');
        const data = await apiCall('/payment-credentials/test', { method: 'POST' });
        setTesting(false);
        if (data?.detail) {
            setError(data.detail);
            loadCredentials();
            return;
        }
        setCredentials(data);
        setMessage('Connection verified.');
    };

    if (loading) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <div>
                    <h1 style={{ fontSize: 24, fontWeight: 700 }}>Payment Provider Settings</h1>
                    <p style={{ color: '#6b7280', fontSize: 14, marginTop: 4 }}>
                        Configure the Paystack account used by this network.
                    </p>
                </div>
                <span className={`badge ${credentials?.is_active ? 'badge-green' : 'badge-gray'}`}>
                    {credentials?.is_active ? 'Active' : 'Inactive'}
                </span>
            </div>

            {message && <div className="badge badge-green" style={{ marginBottom: 16 }}>{message}</div>}
            {error && <div className="badge badge-red" style={{ marginBottom: 16 }}>{error}</div>}

            <div className="card" style={{ maxWidth: 720 }}>
                <div style={{ marginBottom: 20, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                    <span className={`badge ${credentials?.is_configured ? 'badge-blue' : 'badge-yellow'}`}>
                        {credentials?.is_configured ? 'Configured' : 'Not configured'}
                    </span>
                    {credentials?.public_key_last4 && <span className="badge badge-gray">Public key ****{credentials.public_key_last4}</span>}
                    {credentials?.secret_key_last4 && <span className="badge badge-gray">Secret key ****{credentials.secret_key_last4}</span>}
                    {credentials?.webhook_secret_last4 && <span className="badge badge-gray">Webhook ****{credentials.webhook_secret_last4}</span>}
                </div>

                <form onSubmit={handleSave}>
                    <div className="form-group">
                        <label>Provider</label>
                        <select value={form.provider} onChange={(e) => updateField('provider', e.target.value)}>
                            <option value="paystack">Paystack</option>
                        </select>
                    </div>
                    <div className="form-group">
                        <label>Paystack Public Key</label>
                        <input type="text" value={form.public_key} onChange={(e) => updateField('public_key', e.target.value)} placeholder="pk_test_..." required />
                    </div>
                    <div className="form-group">
                        <label>Paystack Secret Key</label>
                        <input type="password" value={form.secret_key} onChange={(e) => updateField('secret_key', e.target.value)} placeholder="sk_test_..." required />
                    </div>
                    <div className="form-group">
                        <label>Paystack Webhook Secret</label>
                        <input type="password" value={form.webhook_secret} onChange={(e) => updateField('webhook_secret', e.target.value)} placeholder="Optional" />
                    </div>
                    <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <input id="payment-active" type="checkbox" checked={form.is_active} onChange={(e) => updateField('is_active', e.target.checked)} style={{ width: 'auto' }} />
                        <label htmlFor="payment-active" style={{ margin: 0 }}>Active</label>
                    </div>
                    <div className="gap-2">
                        <button type="submit" className="btn btn-primary" disabled={saving}>{saving ? 'Saving...' : 'Save'}</button>
                        <button type="button" className="btn" onClick={handleTest} disabled={testing || !credentials?.is_configured}>{testing ? 'Testing...' : 'Test Connection'}</button>
                    </div>
                </form>

                <div style={{ marginTop: 24, color: '#4b5563', fontSize: 14 }}>
                    <p>Last verified: {credentials?.last_validated_at ? new Date(credentials.last_validated_at).toLocaleString() : 'Never'}</p>
                    {credentials?.last_validation_error && <p style={{ color: '#991b1b', marginTop: 6 }}>Last error: {credentials.last_validation_error}</p>}
                </div>
            </div>
        </div>
    );
}
