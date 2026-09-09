import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { apiCall } from '../App';

// One card per available payment provider. The field list for each card is
// whatever that provider's catalog credential_schema says — no hardcoded form.

function ProviderCard({ provider, configured, activeProvider, onChanged, setBanner }) {
    const fields = provider.credential_schema?.fields || [];
    const managedByPlatform = provider.credential_schema?.configured_by === 'platform_admin';

    const [values, setValues] = useState({});
    const [busy, setBusy] = useState('');

    const isConfigured = Boolean(configured);
    const isActive = activeProvider === provider.provider_key;

    const run = async (label, fn) => {
        setBusy(label);
        setBanner(null);
        try {
            await fn();
            onChanged();
        } catch (err) {
            setBanner({ type: 'error', text: err.message });
        } finally {
            setBusy('');
        }
    };

    const save = (e) => {
        e.preventDefault();
        return run('save', async () => {
            await apiCall(`/payment-credentials/${provider.provider_key}`, {
                method: 'PUT',
                body: JSON.stringify({ values, activate: isConfigured ? undefined : true }),
            });
            setValues({});
            setBanner({ type: 'ok', text: `${provider.display_name} credentials saved.` });
        });
    };

    const activate = () => run('activate', async () => {
        await apiCall(`/payment-credentials/${provider.provider_key}/activate`, { method: 'POST' });
        setBanner({ type: 'ok', text: `${provider.display_name} is now the active payment provider.` });
    });

    const test = () => run('test', async () => {
        await apiCall(`/payment-credentials/${provider.provider_key}/test`, { method: 'POST' });
        setBanner({ type: 'ok', text: 'Connection verified.' });
    });

    const remove = () => run('delete', async () => {
        await apiCall(`/payment-credentials/${provider.provider_key}`, { method: 'DELETE' });
        setBanner({ type: 'ok', text: `${provider.display_name} removed.` });
    });

    return (
        <div className="card" style={{ maxWidth: 720, marginBottom: 20 }}>
            <div className="flex-between" style={{ marginBottom: 8 }}>
                <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>{provider.display_name}</h2>
                <span className={`badge ${isActive ? 'badge-green' : isConfigured ? 'badge-blue' : 'badge-gray'}`}>
                    {isActive ? 'Active' : isConfigured ? 'Configured' : 'Not configured'}
                </span>
            </div>
            {provider.description && (
                <p style={{ color: '#6b7280', fontSize: 13, marginTop: 0 }}>{provider.description}</p>
            )}

            {managedByPlatform ? (
                <p style={{ color: '#4b5563', fontSize: 14 }}>
                    Credentials for this provider are managed by the platform — nothing to configure here.
                </p>
            ) : (
                <>
                    <form onSubmit={save}>
                        {fields.map((f) => (
                            <div className="form-group" key={f.name}>
                                <label>
                                    {f.label}
                                    {!f.required && <span style={{ color: '#9ca3af' }}> (optional)</span>}
                                </label>
                                <input
                                    type={f.secret ? 'password' : 'text'}
                                    value={values[f.name] || ''}
                                    onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                                    required={f.required}
                                    placeholder={
                                        isConfigured && configured.field_hints?.[f.name]
                                            ? `stored: ${configured.field_hints[f.name]}`
                                            : ''
                                    }
                                />
                            </div>
                        ))}
                        <div className="gap-2" style={{ flexWrap: 'wrap' }}>
                            <button type="submit" className="btn btn-primary" disabled={Boolean(busy)}>
                                {busy === 'save' ? 'Saving…' : isConfigured ? 'Update credentials' : 'Save & activate'}
                            </button>
                            {isConfigured && !isActive && (
                                <button type="button" className="btn" onClick={activate} disabled={Boolean(busy)}>
                                    {busy === 'activate' ? 'Activating…' : 'Make active'}
                                </button>
                            )}
                            {isConfigured && (
                                <button type="button" className="btn" onClick={test} disabled={Boolean(busy)}>
                                    {busy === 'test' ? 'Testing…' : 'Test connection'}
                                </button>
                            )}
                            {isConfigured && !isActive && (
                                <button type="button" className="btn btn-danger" onClick={remove} disabled={Boolean(busy)}>
                                    {busy === 'delete' ? 'Removing…' : 'Remove'}
                                </button>
                            )}
                        </div>
                    </form>

                    {isConfigured && (
                        <div style={{ marginTop: 16, color: '#4b5563', fontSize: 13 }}>
                            <p style={{ margin: '4px 0' }}>
                                Last verified:{' '}
                                {configured.last_validated_at
                                    ? new Date(configured.last_validated_at).toLocaleString()
                                    : 'Never'}
                            </p>
                            {configured.last_validation_error && (
                                <p style={{ color: '#991b1b', margin: '4px 0' }}>
                                    Last error: {configured.last_validation_error}
                                </p>
                            )}
                        </div>
                    )}
                </>
            )}
        </div>
    );
}

export default function PaymentCredentials() {
    const [providers, setProviders] = useState([]);
    const [creds, setCreds] = useState({ active_provider: null, configured: [] });
    const [loading, setLoading] = useState(true);
    const [banner, setBanner] = useState(null);
    const [loadError, setLoadError] = useState('');

    const load = useCallback(() => {
        return Promise.all([
            apiCall('/providers?category=payment'),
            apiCall('/payment-credentials'),
        ])
            .then(([p, c]) => {
                setProviders(p);
                setCreds(c);
                setLoadError('');
            })
            .catch((e) => setLoadError(e.message))
            .finally(() => setLoading(false));
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const configuredByKey = useMemo(() => {
        const map = {};
        for (const row of creds.configured) map[row.provider] = row;
        return map;
    }, [creds]);

    if (loading) return <p>Loading…</p>;

    return (
        <div>
            <h1 style={{ fontSize: 24, fontWeight: 700 }}>Payment Provider Settings</h1>
            <p style={{ color: '#6b7280', fontSize: 14, marginTop: 4 }}>
                Choose the payment provider your customers pay through, and enter your own credentials.
            </p>

            {loadError && <div className="badge badge-red" style={{ marginBottom: 16 }}>{loadError}</div>}
            {banner && (
                <div className={`badge ${banner.type === 'ok' ? 'badge-green' : 'badge-red'}`} style={{ marginBottom: 16 }}>
                    {banner.text}
                </div>
            )}

            {providers.length === 0 ? (
                <div className="card" style={{ maxWidth: 720 }}>
                    <p style={{ margin: 0, color: '#4b5563' }}>
                        No payment providers are available yet. Contact the platform if you expected to see one.
                    </p>
                </div>
            ) : (
                providers.map((p) => (
                    <ProviderCard
                        key={p.provider_key}
                        provider={p}
                        configured={configuredByKey[p.provider_key]}
                        activeProvider={creds.active_provider}
                        onChanged={load}
                        setBanner={setBanner}
                    />
                ))
            )}
        </div>
    );
}
