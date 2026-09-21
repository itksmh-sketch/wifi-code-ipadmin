import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { apiCall } from '../App';
import PageHeader from './PageHeader';
import PinPrompt, { usePinGate } from './PinPrompt';

// Shared bring-your-own-credentials page, driven entirely by the provider
// catalog. Used for both payment (`/payment-credentials`) and SMS
// (`/sms-credentials`) — the only differences are the category, the API prefix,
// and the page copy. One card per available provider; the field list for each
// card is whatever that provider's catalog credential_schema says — no
// hardcoded form.

function ProviderCard({ provider, category, apiPrefix, configured, activeProvider, activeNoun, onChanged, setBanner, gate }) {
    const schema = provider.credential_schema || {};
    const fields = schema.fields || [];
    const managedByPlatform = schema.configured_by === 'platform_admin';
    const supportsTest = Boolean(schema.supports_test);
    const isPlatformProvided = Boolean(provider.is_platform_provided);

    const [values, setValues] = useState({});
    const [busy, setBusy] = useState('');
    const [copied, setCopied] = useState(false);

    const copyWebhookUrl = () => {
        if (!configured?.webhook_url) return;
        navigator.clipboard.writeText(configured.webhook_url).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        });
    };

    const isConfigured = Boolean(configured);
    const isActive = activeProvider === provider.provider_key;

    const run = async (label, fn) => {
        setBusy(label);
        setBanner(null);
        try {
            // Save/activate/delete are PIN-gated; gate.run parks the call on a
            // 403, shows the prompt, and replays it once the PIN clears, so a
            // half-filled credentials form survives the interruption.
            await gate.run(fn);
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
            await apiCall(`${apiPrefix}/${provider.provider_key}`, {
                method: 'PUT',
                body: JSON.stringify({ values, activate: isConfigured ? undefined : true }),
            });
            setValues({});
            setBanner({ type: 'ok', text: `${provider.display_name} credentials saved.` });
        });
    };

    const activate = () => run('activate', async () => {
        await apiCall(`${apiPrefix}/${provider.provider_key}/activate`, { method: 'POST' });
        setBanner({ type: 'ok', text: `${provider.display_name} is now the active ${activeNoun}.` });
    });

    const activatePlatform = () => run('activate', async () => {
        await apiCall(`${apiPrefix}/activate-platform`, { method: 'POST' });
        setBanner({ type: 'ok', text: `${provider.display_name} is now the active ${activeNoun}.` });
    });

    const test = () => run('test', async () => {
        const res = await apiCall(`${apiPrefix}/${provider.provider_key}/test`, { method: 'POST' });
        const detail = res && typeof res.test_detail === 'string' ? res.test_detail.trim() : '';
        setBanner({ type: 'ok', text: detail ? `Connection verified — ${detail}.` : 'Connection verified.' });
    });

    const remove = () => run('delete', async () => {
        await apiCall(`${apiPrefix}/${provider.provider_key}`, { method: 'DELETE' });
        setBanner({ type: 'ok', text: `${provider.display_name} removed.` });
    });

    return (
        <div className="card" style={{ maxWidth: 720, marginBottom: 20 }}>
            <div className="flex-between" style={{ marginBottom: 8 }}>
                <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>
                    {provider.display_name}
                    {isPlatformProvided && (
                        <span className="badge badge-yellow" style={{ marginLeft: 8, fontSize: 11, fontWeight: 600, verticalAlign: 'middle' }}>
                            Platform-provided
                        </span>
                    )}
                </h2>
                <span className={`badge ${isActive ? 'badge-green' : isConfigured ? 'badge-blue' : 'badge-gray'}`}>
                    {isActive ? 'Active' : isConfigured ? 'Configured' : 'Not configured'}
                </span>
            </div>
            {provider.description && (
                <p style={{ color: '#6b7280', fontSize: 13, marginTop: 0 }}>{provider.description}</p>
            )}

            {managedByPlatform ? (
                <>
                    {provider.platform_rate_per_segment != null && (
                        <p style={{ color: '#4b5563', fontSize: 14 }}>
                            Billed at <strong>GHS {provider.platform_rate_per_segment}</strong> per SMS segment on
                            your monthly invoice. No credentials to configure — the platform sends on your behalf.
                        </p>
                    )}
                    {isActive ? (
                        <p style={{ color: '#065f46', fontSize: 13, fontWeight: 600 }}>
                            This is your active {activeNoun}.
                        </p>
                    ) : (
                        <button type="button" className="btn btn-primary" onClick={activatePlatform} disabled={Boolean(busy)}>
                            {busy === 'activate' ? 'Activating…' : `Use this ${activeNoun}`}
                        </button>
                    )}
                </>
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
                            {isConfigured && supportsTest && (
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

                    {isConfigured && supportsTest && (
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

                    {isConfigured && category === 'payment' && configured.webhook_url && (
                        <div style={{ marginTop: 16 }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                                <strong style={{ color: '#14213d', fontSize: 13 }}>Webhook URL — give this to {provider.display_name}</strong>
                                <button type="button" className="btn" style={{ padding: '6px 12px', fontSize: 13 }} onClick={copyWebhookUrl}>
                                    {copied ? '✓ Copied' : 'Copy'}
                                </button>
                            </div>
                            <pre style={{ background: '#0f172a', color: '#e5efff', padding: 10, borderRadius: 8, whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontSize: 13, margin: 0 }}>
                                {configured.webhook_url}
                            </pre>
                        </div>
                    )}
                </>
            )}
        </div>
    );
}

export default function ProviderCredentials({ category, apiPrefix, title, blurb, activeNoun }) {
    const noun = activeNoun || `${category} provider`;
    const [providers, setProviders] = useState([]);
    const [creds, setCreds] = useState({ active_provider: null, configured: [] });
    const [loading, setLoading] = useState(true);
    const [banner, setBanner] = useState(null);
    const [loadError, setLoadError] = useState('');
    // One prompt for the whole page, shared by every provider card.
    const gate = usePinGate();

    const load = useCallback(() => {
        return Promise.all([
            apiCall(`/providers?category=${category}`),
            apiCall(apiPrefix),
        ])
            .then(([p, c]) => {
                setProviders(p);
                setCreds(c);
                setLoadError('');
            })
            .catch((e) => setLoadError(e.message))
            .finally(() => setLoading(false));
    }, [category, apiPrefix]);

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
            <PageHeader title={title} />
            <p style={{ color: '#6b7280', fontSize: 14, marginTop: 4 }}>{blurb}</p>

            {loadError && <div className="badge badge-red" style={{ marginBottom: 16 }}>{loadError}</div>}
            {banner && (
                <div className={`badge ${banner.type === 'ok' ? 'badge-green' : 'badge-red'}`} style={{ marginBottom: 16 }}>
                    {banner.text}
                </div>
            )}

            {providers.length === 0 ? (
                <div className="card" style={{ maxWidth: 720 }}>
                    <p style={{ margin: 0, color: '#4b5563' }}>
                        No {category} providers are available yet. Contact the platform if you expected to see one.
                    </p>
                </div>
            ) : (
                providers.map((p) => (
                    <ProviderCard
                        key={p.provider_key}
                        provider={p}
                        category={category}
                        apiPrefix={apiPrefix}
                        activeNoun={noun}
                        configured={configuredByKey[p.provider_key]}
                        activeProvider={creds.active_provider}
                        onChanged={load}
                        setBanner={setBanner}
                        gate={gate}
                    />
                ))
            )}

            {gate.reason && (
                <PinPrompt reason={gate.reason} onCancel={gate.cancel} onUnlocked={gate.unlocked} />
            )}
        </div>
    );
}
