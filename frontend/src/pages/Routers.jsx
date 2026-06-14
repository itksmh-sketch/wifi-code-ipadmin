import React, { useEffect, useRef, useState, useCallback } from 'react';
import { apiCall } from '../App';

// ─── helpers ────────────────────────────────────────────────────────────────

function slug(value) {
    return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}
function randomSecret() {
    return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
}
function fmt(value) {
    return value ? new Date(value).toLocaleString() : 'Never';
}
function fmtBytes(value) {
    if (!value) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0, n = value;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + ' ' + units[i];
}
function drawLineChart(canvas, series, color, mapValue) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!series || !series.length) {
        ctx.fillStyle = '#6b7280';
        ctx.font = '14px sans-serif';
        ctx.fillText('No metrics collected yet.', 20, 30);
        return;
    }
    const values = series.map(mapValue);
    const max = Math.max(...values, 1);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((v, i) => {
        const x = 40 + (i * (canvas.width - 80) / Math.max(values.length - 1, 1));
        const y = canvas.height - 30 - ((v / max) * (canvas.height - 60));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
}

// ─── shared modal wrapper ────────────────────────────────────────────────────

function Modal({ open, onClose, title, children, wide }) {
    if (!open) return null;
    return (
        <div
            style={{ position: 'fixed', inset: 0, background: 'rgba(17,33,59,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16, zIndex: 1000 }}
            onClick={onClose}
        >
            <div
                style={{ background: '#fff', border: '1px solid #d7deea', borderRadius: 18, padding: 24, width: wide ? 'min(780px,100%)' : 'min(560px,100%)', maxHeight: '90vh', overflowY: 'auto' }}
                onClick={e => e.stopPropagation()}
            >
                <h3 style={{ margin: '0 0 16px', fontSize: 20 }}>{title}</h3>
                {children}
            </div>
        </div>
    );
}

// ─── ROUTER LIST ─────────────────────────────────────────────────────────────

function RouterList({ onAdd, onSelect }) {
    const [routers, setRouters] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    const load = useCallback(async () => {
        setLoading(true);
        const data = await apiCall('/admin/routers');
        if (data) setRouters(data);
        else setError('Failed to load routers.');
        setLoading(false);
    }, []);

    useEffect(() => { load(); }, [load]);

    if (loading) return <p>Loading routers…</p>;

    return (
        <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                <div>
                    <h1 style={{ margin: 0, fontSize: 28, fontWeight: 700 }}>Router Fleet</h1>
                    <p style={{ margin: '4px 0 0', color: '#5c677d' }}>Live reachability, last contact time, and provisioning.</p>
                </div>
                <button className="btn btn-primary" onClick={onAdd}>+ Add Router</button>
            </div>

            {error && <p style={{ color: '#b42318' }}>{error}</p>}

            <div className="card">
                <div className="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Site</th>
                                <th>IP Address</th>
                                <th>NAS Identifier</th>
                                <th>Online</th>
                                <th>Connection</th>
                                <th>Last Seen</th>
                            </tr>
                        </thead>
                        <tbody>
                            {routers.length === 0 && (
                                <tr><td colSpan="7" style={{ color: '#9ca3af', padding: 24 }}>No routers found.</td></tr>
                            )}
                            {routers.map(r => (
                                <tr key={r.id} style={{ cursor: 'pointer' }} onClick={() => onSelect(r.id)}>
                                    <td>
                                        <span style={{ color: '#0d6e5f', fontWeight: 600, textDecoration: 'underline' }}>{r.name}</span>
                                    </td>
                                    <td>{r.site_name}</td>
                                    <td><code>{r.ip_address || 'VPN only'}</code></td>
                                    <td><code>{r.nas_identifier}</code></td>
                                    <td>
                                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                                            <span style={{ width: 10, height: 10, borderRadius: '50%', background: r.is_online ? '#12b76a' : '#f04438', display: 'inline-block' }} />
                                            {r.is_online ? 'Online' : 'Offline'}
                                        </span>
                                    </td>
                                    <td>
                                        <span className={`badge ${r.connection_status === 'connected' ? 'badge-green' : 'badge-gray'}`}>
                                            {r.connection_status || 'unknown'}
                                        </span>
                                    </td>
                                    <td style={{ color: '#5c677d', fontSize: 13 }}>{fmt(r.last_connected_at || r.last_seen_at)}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    );
}

// ─── PROVISIONING WIZARD ─────────────────────────────────────────────────────

const WIZARD_STEPS = ['1. Basic info', '2. Connect via VPN', '3. Credentials & hotspot', '4. Provision'];

function RouterWizard({ onBack, onDone }) {
    const [step, setStep] = useState(1);
    const [sites, setSites] = useState([]);
    const [templates, setTemplates] = useState([]);
    const [interfaces, setInterfaces] = useState([]);
    // The router record is created at the end of Step 1 so the VPN tunnel and
    // credentials configured in later steps have a router to attach to.
    const [routerId, setRouterId] = useState(null);
    const [busy, setBusy] = useState(false);
    // Step 2 — WireGuard tunnel (the only supported connection method).
    const [wgStatus, setWgStatus] = useState(null);   // { enabled, connected, tunnel_ip }
    const [wgConfig, setWgConfig] = useState(null);   // { mikrotik_commands, tunnel_ip }
    const [wgMsg, setWgMsg] = useState('');
    const [copied, setCopied] = useState(false);
    // Step 3 — credentials + hotspot config, reached over the tunnel.
    const [credSaved, setCredSaved] = useState(false);
    const [connResult, setConnResult] = useState('Save the RouterOS credentials to load interfaces over the tunnel.');
    // Step 4 — provisioning.
    const [provisionLog, setProvisionLog] = useState([]);
    const [provisionDone, setProvisionDone] = useState(null);
    const [state, setState] = useState({
        name: '', site_id: '', nas_identifier: '', nas_secret: randomSecret(),
        ip_address: '', api_port: 8728, api_username: 'admin', api_password: '', use_ssl: false,
        hotspot_interface: '', dns_name: '', template_id: '',
    });
    const pollRef = useRef(null);

    useEffect(() => {
        apiCall('/admin/sites').then(data => { if (data) setSites(data); });
    }, []);

    function set(key, value) {
        setState(prev => {
            const next = { ...prev, [key]: value };
            if (key === 'name') next.nas_identifier = slug(value);
            return next;
        });
    }

    const tunnelConnected = !!(wgStatus && wgStatus.connected);

    // ── Step 1 → create the router record ────────────────────────────────────
    async function createRouter() {
        if (routerId) return routerId; // already created (e.g. navigating back & forth)
        const result = await apiCall('/admin/routers/onboard', {
            method: 'POST',
            body: JSON.stringify({ name: state.name, site_id: state.site_id, nas_identifier: state.nas_identifier, nas_secret: state.nas_secret }),
        });
        if (!result || !result.router_id) throw new Error(result?.detail || 'Failed to create the router.');
        setRouterId(result.router_id);
        return result.router_id;
    }

    // ── Step 2 — WireGuard tunnel (only supported connection method) ──────────
    const loadWgStatus = useCallback(async () => {
        if (!routerId) return null;
        const data = await apiCall(`/admin/routers/${routerId}/wireguard/status`);
        if (data) setWgStatus(data);
        return data;
    }, [routerId]);

    async function generateVpnConfig() {
        setBusy(true); setWgMsg('');
        try {
            const result = await apiCall(`/admin/routers/${routerId}/wireguard/setup`, { method: 'POST', body: '{}' });
            if (!result || !result.mikrotik_commands) { setWgMsg(result?.detail || 'Could not generate the VPN config.'); return; }
            setWgConfig(result);
            await loadWgStatus();
        } finally { setBusy(false); }
    }

    async function verifyConnection() {
        setWgMsg('Checking for a tunnel handshake…');
        const data = await loadWgStatus();
        setWgMsg(data && data.connected
            ? `VPN connected — tunnel IP ${data.tunnel_ip}.`
            : 'No handshake yet — paste the config into the router, then try again in a moment.');
    }

    function copyCommands() {
        if (!wgConfig?.mikrotik_commands) return;
        navigator.clipboard.writeText(wgConfig.mikrotik_commands).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); });
    }

    // While on Step 2, poll the tunnel status so "Continue" unlocks on its own
    // once the router completes its first handshake.
    useEffect(() => {
        if (step !== 2 || !routerId) return undefined;
        loadWgStatus();
        const timer = setInterval(() => loadWgStatus().catch(() => {}), 5000);
        return () => clearInterval(timer);
    }, [step, routerId, loadWgStatus]);

    // ── Step 3 — credentials + hotspot, all over the tunnel ──────────────────
    async function saveCredsAndLoadInterfaces() {
        setConnResult('Saving credentials and connecting over the tunnel…');
        setCredSaved(false);
        await apiCall(`/admin/routers/${routerId}/credentials`, {
            method: 'PUT',
            body: JSON.stringify({ api_username: state.api_username, api_password: state.api_password, api_port: Number(state.api_port), use_ssl: state.use_ssl }),
        });
        const ifaces = await apiCall(`/admin/routers/${routerId}/interfaces`);
        if (!Array.isArray(ifaces)) {
            setConnResult('Saved credentials, but could not read interfaces over the tunnel: ' + (ifaces?.detail || 'check the RouterOS username/password.'));
            return;
        }
        setInterfaces(ifaces);
        const tpls = await apiCall('/admin/config-templates');
        setTemplates(tpls || []);
        setConnResult(`Credentials saved — loaded ${ifaces.length} interface(s) over the tunnel.`);
        setCredSaved(true);
    }

    // ── Step 4 — provision over the tunnel ───────────────────────────────────
    async function startProvision() {
        setProvisionLog([]);
        setProvisionDone(null);
        const result = await apiCall(`/admin/routers/${routerId}/provision`, {
            method: 'POST',
            body: JSON.stringify({ hotspot_interface: state.hotspot_interface, dns_name: state.dns_name, template_id: state.template_id || null }),
        });
        if (!result || !result.log_id) {
            setProvisionDone({ ok: false, message: result?.detail || 'Provisioning failed to start.' });
            return;
        }
        const logId = result.log_id;
        function poll() {
            apiCall(`/admin/routers/${routerId}/provision-status/${logId}`).then(body => {
                if (!body) return;
                setProvisionLog(body.commands_executed || []);
                if (body.status === 'success') {
                    setProvisionDone({ ok: true, router_id: routerId, message: 'Provisioning complete.' });
                } else if (body.status === 'failed') {
                    setProvisionDone({ ok: false, message: 'Provisioning failed: ' + (body.error_message || 'Unknown error') });
                } else {
                    pollRef.current = setTimeout(poll, 2000);
                }
            });
        }
        poll();
    }

    async function next() {
        try {
            if (step === 1) {
                if (!state.name || !state.site_id || !state.nas_identifier || !state.nas_secret)
                    throw new Error('Fill in router name, site, NAS identifier, and NAS secret.');
                setBusy(true);
                try { await createRouter(); } finally { setBusy(false); }
                setStep(2);
            } else if (step === 2) {
                if (!tunnelConnected) throw new Error('The VPN tunnel must be connected before continuing.');
                setStep(3);
            } else if (step === 3) {
                if (!credSaved) throw new Error('Save the RouterOS credentials and load interfaces first.');
                if (!state.hotspot_interface || !state.dns_name) throw new Error('Choose the hotspot interface and DNS name.');
                setStep(4);
            }
        } catch (e) { alert(e.message); }
    }

    useEffect(() => () => clearTimeout(pollRef.current), []);

    const summary = `Router: ${state.name || '-'} | NAS: ${state.nas_identifier || '-'} | Tunnel: ${wgStatus?.tunnel_ip || '-'} | Interface: ${state.hotspot_interface || '-'} | DNS: ${state.dns_name || '-'}`;

    return (
        <div>
            <button onClick={onBack} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#0d6e5f', fontWeight: 600, marginBottom: 16, padding: 0 }}>← Back to Routers</button>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 10, marginBottom: 18 }}>
                {WIZARD_STEPS.map((label, i) => (
                    <div key={i} style={{ padding: '10px 14px', borderRadius: 14, border: `1px solid ${i + 1 === step ? '#0d6e5f' : '#d7deea'}`, background: '#fff', color: i + 1 === step ? '#14213d' : '#5c677d', fontSize: 13, fontWeight: i + 1 === step ? 700 : 400, boxShadow: i + 1 === step ? '0 4px 16px rgba(13,110,95,.12)' : 'none' }}>
                        {label}
                    </div>
                ))}
            </div>

            <div className="card" style={{ padding: 24 }}>
                {/* Step 1 */}
                {step === 1 && (
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                        <div className="form-group">
                            <label>Router name</label>
                            <input value={state.name} onChange={e => set('name', e.target.value)} />
                        </div>
                        <div className="form-group">
                            <label>Site</label>
                            <select value={state.site_id} onChange={e => set('site_id', e.target.value)}>
                                <option value="">Choose site</option>
                                {sites.map(s => <option key={s.id} value={s.id}>{s.town_name} - {s.name}</option>)}
                            </select>
                        </div>
                        <div className="form-group">
                            <label>NAS identifier</label>
                            <input value={state.nas_identifier} onChange={e => set('nas_identifier', e.target.value)} />
                        </div>
                        <div className="form-group">
                            <label>NAS shared secret</label>
                            <input value={state.nas_secret} onChange={e => set('nas_secret', e.target.value)} />
                        </div>
                    </div>
                )}

                {/* Step 2 — Connect via VPN (the only supported method) */}
                {step === 2 && (
                    <>
                        <p style={{ color: '#5c677d', marginTop: 0, marginBottom: 14 }}>
                            Connect this router to the platform over a WireGuard VPN tunnel. This is the only supported
                            connection method — the platform reaches the router through the tunnel, even behind NAT.
                        </p>
                        {tunnelConnected ? (
                            <div style={{ padding: '16px', background: '#ecfdf3', border: '1px solid #abefc6', borderRadius: 12 }}>
                                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontWeight: 700, color: '#067647' }}>
                                    <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#12b76a', display: 'inline-block' }} />
                                    VPN connected — {wgStatus.tunnel_ip}
                                </span>
                                <p style={{ margin: '8px 0 0', color: '#5c677d', fontSize: 14 }}>The tunnel has a recent handshake. Click Next to continue.</p>
                            </div>
                        ) : (
                            <>
                                <button className="btn btn-primary" onClick={() => generateVpnConfig().catch(e => setWgMsg(e.message))} disabled={busy}>
                                    {busy ? 'Generating secure keypair…' : (wgConfig || (wgStatus && wgStatus.enabled)) ? 'Regenerate VPN config' : 'Generate VPN config'}
                                </button>
                                {wgConfig && (
                                    <div style={{ marginTop: 16 }}>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                                            <strong style={{ color: '#14213d' }}>MikroTik commands — paste into the router</strong>
                                            <button className="btn btn-primary" style={{ padding: '6px 12px', fontSize: 13 }} onClick={copyCommands}>{copied ? '✓ Copied' : 'Copy all commands'}</button>
                                        </div>
                                        <pre style={{ background: '#0f172a', color: '#e5efff', padding: 14, borderRadius: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13, lineHeight: 1.5 }}>{wgConfig.mikrotik_commands}</pre>
                                        <p style={{ color: '#5c677d', fontSize: 14, margin: '4px 0 0' }}>Tunnel IP: <code>{wgConfig.tunnel_ip}</code>. After pasting, the handshake appears within ~2 minutes (this page re-checks every 5 seconds).</p>
                                    </div>
                                )}
                                {(wgConfig || (wgStatus && wgStatus.enabled)) && (
                                    <div style={{ marginTop: 12 }}>
                                        <button className="btn" style={{ background: '#f59e0b', color: '#1f2937' }} onClick={() => verifyConnection().catch(e => setWgMsg(e.message))}>Verify connection</button>
                                    </div>
                                )}
                                {wgMsg && <div style={{ marginTop: 12, padding: '10px 14px', background: '#f8fafc', borderRadius: 10, color: '#5c677d', fontSize: 14 }}>{wgMsg}</div>}
                            </>
                        )}
                    </>
                )}

                {/* Step 3 — Credentials + hotspot config, over the tunnel */}
                {step === 3 && (
                    <>
                        <p style={{ color: '#5c677d', marginTop: 0, marginBottom: 14 }}>
                            These RouterOS API credentials are used to reach the router over the VPN tunnel{wgStatus?.tunnel_ip ? ` (${wgStatus.tunnel_ip})` : ''}.
                        </p>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                            <div className="form-group">
                                <label>RouterOS username</label>
                                <input value={state.api_username} onChange={e => set('api_username', e.target.value)} />
                            </div>
                            <div className="form-group">
                                <label>RouterOS password</label>
                                <input type="password" value={state.api_password} onChange={e => set('api_password', e.target.value)} />
                            </div>
                            <div className="form-group">
                                <label>API port</label>
                                <input type="number" value={state.api_port} onChange={e => set('api_port', e.target.value)} />
                            </div>
                            <div className="form-group">
                                <label>Use SSL</label>
                                <select value={state.use_ssl ? 'true' : 'false'} onChange={e => set('use_ssl', e.target.value === 'true')}>
                                    <option value="false">No</option>
                                    <option value="true">Yes</option>
                                </select>
                            </div>
                        </div>
                        <div style={{ marginTop: 12 }}>
                            <button className="btn" style={{ background: '#f59e0b', color: '#1f2937' }} onClick={() => saveCredsAndLoadInterfaces().catch(e => setConnResult(e.message))}>
                                Save credentials &amp; load interfaces
                            </button>
                            <div style={{ marginTop: 12, padding: '10px 14px', background: '#f8fafc', borderRadius: 10, color: '#5c677d', fontSize: 14 }}>{connResult}</div>
                        </div>
                        {credSaved && (
                            <>
                                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 16 }}>
                                    <div className="form-group">
                                        <label>Hotspot interface</label>
                                        <select value={state.hotspot_interface} onChange={e => set('hotspot_interface', e.target.value)}>
                                            <option value="">Select interface</option>
                                            {interfaces.map(i => <option key={i.name} value={i.name}>{i.name}</option>)}
                                        </select>
                                    </div>
                                    <div className="form-group">
                                        <label>Captive portal DNS name</label>
                                        <input value={state.dns_name} onChange={e => set('dns_name', e.target.value)} placeholder="hotspot.yourisp.com" />
                                    </div>
                                    <div className="form-group">
                                        <label>Config template</label>
                                        <select value={state.template_id} onChange={e => set('template_id', e.target.value)}>
                                            <option value="">No template</option>
                                            {templates.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                                        </select>
                                    </div>
                                </div>
                                <div style={{ marginTop: 14, padding: '12px 14px', background: '#f8fafc', border: '1px dashed #d7deea', borderRadius: 12, fontSize: 14, color: '#5c677d' }}>
                                    <strong style={{ color: '#14213d' }}>Provision summary</strong><br />{summary}
                                </div>
                            </>
                        )}
                    </>
                )}

                {/* Step 4 */}
                {step === 4 && (
                    <div>
                        <p style={{ color: '#5c677d' }}>Provisioning starts immediately. The log updates every 2 seconds.</p>
                        {!provisionDone && provisionLog.length === 0 && (
                            <button className="btn btn-primary" onClick={() => startProvision().catch(e => setProvisionDone({ ok: false, message: e.message }))}>
                                Provision router
                            </button>
                        )}
                        {provisionLog.length > 0 && (
                            <ul style={{ paddingLeft: 20, marginTop: 16 }}>
                                {provisionLog.map((entry, i) => (
                                    <li key={i} style={{ margin: '6px 0', fontSize: 14, color: entry.status === 'failed' ? '#b42318' : entry.status === 'success' ? '#067647' : '#5c677d' }}>
                                        {entry.status === 'failed' ? '✗' : entry.status === 'success' ? '✓' : '…'}{' '}
                                        {entry.message || entry.command || entry.step || entry.path || 'update'}
                                        {entry.error ? ' — ' + entry.error : ''}
                                    </li>
                                ))}
                            </ul>
                        )}
                        {provisionDone && (
                            <div style={{ marginTop: 16, padding: '12px 14px', borderRadius: 12, background: provisionDone.ok ? '#ecfdf3' : '#fef3f2', color: provisionDone.ok ? '#067647' : '#b42318' }}>
                                {provisionDone.message}
                                {provisionDone.ok && (
                                    <> <button className="btn btn-primary" style={{ marginLeft: 12 }} onClick={() => onDone(provisionDone.router_id)}>View router</button></>
                                )}
                            </div>
                        )}
                    </div>
                )}

                {/* Navigation */}
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 20 }}>
                    <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setStep(s => Math.max(1, s - 1))} disabled={step === 1}>Back</button>
                    {step < 4 && <button className="btn btn-primary" onClick={next}>Next</button>}
                </div>
            </div>
        </div>
    );
}

// ─── VPN TUNNEL TAB ───────────────────────────────────────────────────────────

function VpnTunnelTab({ routerId, onChange }) {
    const [status, setStatus] = useState(null);
    const [config, setConfig] = useState(null);      // { mikrotik_commands, tunnel_ip, ... }
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [copied, setCopied] = useState(false);
    const pollRef = useRef(null);

    const loadStatus = useCallback(async () => {
        const data = await apiCall(`/admin/routers/${routerId}/wireguard/status`);
        if (data) setStatus(data);
    }, [routerId]);

    useEffect(() => {
        loadStatus();
        pollRef.current = setInterval(() => loadStatus().catch(() => {}), 15000);
        return () => clearInterval(pollRef.current);
    }, [loadStatus]);

    async function setup() {
        setBusy(true); setError('');
        const result = await apiCall(`/admin/routers/${routerId}/wireguard/setup`, { method: 'POST', body: '{}' });
        setBusy(false);
        if (!result || !result.mikrotik_commands) {
            setError(result?.detail || 'Setup failed.');
            return;
        }
        setConfig(result);
        await loadStatus();
        if (onChange) onChange();
    }

    async function removeTunnel() {
        if (!window.confirm('Remove the VPN tunnel for this router? Its peer will be removed from the server.')) return;
        setBusy(true); setError('');
        const result = await apiCall(`/admin/routers/${routerId}/wireguard`, { method: 'DELETE' });
        setBusy(false);
        if (result && result.detail) { setError(result.detail); return; }
        setConfig(null);
        await loadStatus();
        if (onChange) onChange();
    }

    function copyAll() {
        if (!config?.mikrotik_commands) return;
        navigator.clipboard.writeText(config.mikrotik_commands).then(() => {
            setCopied(true); setTimeout(() => setCopied(false), 2000);
        });
    }

    if (!status) return <div className="card" style={{ padding: 18 }}>Loading tunnel status…</div>;

    const badge = (color, text) => (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, fontWeight: 700, fontSize: 16 }}>
            <span style={{ width: 12, height: 12, borderRadius: '50%', background: color, display: 'inline-block' }} />
            {text}
        </span>
    );

    const codeBlock = config && (
        <div style={{ marginTop: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <strong style={{ color: '#14213d' }}>MikroTik commands</strong>
                <button className="btn btn-primary" style={{ padding: '6px 12px', fontSize: 13 }} onClick={copyAll}>
                    {copied ? '✓ Copied' : 'Copy all commands'}
                </button>
            </div>
            <pre style={{ background: '#0f172a', color: '#e5efff', padding: 14, borderRadius: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13, lineHeight: 1.5 }}>{config.mikrotik_commands}</pre>
            <p style={{ color: '#5c677d', fontSize: 14 }}>
                After pasting into MikroTik, the connection status updates within ~2 minutes (this page also re-checks every 15 seconds).
            </p>
        </div>
    );

    return (
        <div className="card" style={{ padding: 18 }}>
            {error && <div style={{ marginBottom: 12, padding: '10px 14px', borderRadius: 12, background: '#fef3f2', color: '#b42318' }}>{error}</div>}

            {/* Not configured */}
            {!status.enabled && (
                <div>
                    <h4 style={{ margin: '0 0 6px' }}>VPN Tunnel</h4>
                    <p style={{ color: '#5c677d', marginTop: 0 }}>
                        Set up a WireGuard tunnel so the platform can reach this router even when it's behind NAT
                        with a dynamic IP. The router dials the server — no public IP required on the router side.
                    </p>
                    <button className="btn btn-primary" onClick={() => setup().catch(e => setError(e.message))} disabled={busy}>
                        {busy ? 'Generating secure keypair…' : 'Setup VPN Tunnel'}
                    </button>
                    {codeBlock}
                </div>
            )}

            {/* Configured */}
            {status.enabled && (
                <div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
                        {status.connected
                            ? badge('#12b76a', 'Tunnel Active')
                            : badge('#f04438', 'Tunnel Offline')}
                        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                            {config
                                ? <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setConfig(null)}>Hide config</button>
                                : <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setup().catch(e => setError(e.message))} disabled={busy}>View MikroTik config</button>}
                            <button className="btn" style={{ background: '#fef3f2', color: '#b42318' }} onClick={() => removeTunnel().catch(e => setError(e.message))} disabled={busy}>Remove Tunnel</button>
                        </div>
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 14, marginTop: 16 }}>
                        {[
                            ['Tunnel IP', status.tunnel_ip || '—'],
                            ['Last handshake', fmt(status.last_handshake_at)],
                            ['Data transferred', `↑ ${fmtBytes(status.transfer_tx)}  ↓ ${fmtBytes(status.transfer_rx)}`],
                        ].map(([label, value]) => (
                            <div key={label} style={{ padding: 14, border: '1px solid #d7deea', borderRadius: 12, background: '#fafcff' }}>
                                <div style={{ color: '#5c677d', fontSize: 12, textTransform: 'uppercase', letterSpacing: '.05em' }}>{label}</div>
                                <div style={{ fontSize: 17, fontWeight: 700, marginTop: 6 }}>{value}</div>
                            </div>
                        ))}
                    </div>
                    {!status.connected && (
                        <p style={{ color: '#b42318', fontSize: 14, marginTop: 12 }}>
                            Tunnel is configured but the router hasn't connected yet. Paste the MikroTik config below into the router, or check that it can reach the VPN server on UDP 51820.
                        </p>
                    )}
                    {codeBlock}
                </div>
            )}
        </div>
    );
}

// ─── ROUTER DETAIL ────────────────────────────────────────────────────────────

// ─── ROUTER SETUP WIZARD ──────────────────────────────────────────────────────

const SUBNET_OPTIONS = [
    { prefix: 24, label: '/24 — 254 hosts (most common)' },
    { prefix: 25, label: '/25 — 126 hosts' },
    { prefix: 26, label: '/26 — 62 hosts' },
    { prefix: 27, label: '/27 — 30 hosts' },
    { prefix: 28, label: '/28 — 14 hosts' },
    { prefix: 23, label: '/23 — 510 hosts' },
    { prefix: 22, label: '/22 — 1022 hosts' },
];
const LEASE_OPTIONS = ['1h', '4h', '8h', '24h'];

function hostsInSubnet(prefix) {
    return Math.pow(2, 32 - prefix) - 2;
}
function ipToInt(ip) {
    const parts = String(ip).split('.');
    if (parts.length !== 4) return null;
    let n = 0;
    for (const p of parts) {
        const v = Number(p);
        if (!Number.isInteger(v) || v < 0 || v > 255) return null;
        n = (n * 256) + v;
    }
    return n >>> 0;
}
function intToIp(n) {
    return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.');
}
// Mirrors the server's plan_subnet: pool runs gateway+1 .. broadcast-1.
function subnetPlan(gateway, prefix) {
    const gw = ipToInt(gateway);
    if (gw === null) return null;
    const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
    const network = (gw & mask) >>> 0;
    const broadcast = (network | (~mask >>> 0)) >>> 0;
    if (gw === network || gw === broadcast) return null;
    return {
        network: intToIp(network),
        broadcast: intToIp(broadcast),
        poolStart: intToIp(Math.min(gw + 1, broadcast - 1)),
        poolEnd: intToIp(broadcast - 1),
        hotspotNetwork: `${intToIp(network)}/${prefix}`,
    };
}

const BADGE = {
    configured: { bg: '#ecfdf3', fg: '#067647', dot: '🟢', label: 'Configured' },
    partial: { bg: '#fffaeb', fg: '#b54708', dot: '🟡', label: 'Partial' },
    unconfigured: { bg: '#f2f4f7', fg: '#5c677d', dot: '⚪', label: 'Not configured' },
    error: { bg: '#fef3f2', fg: '#b42318', dot: '🔴', label: 'Error' },
    applying: { bg: '#eff8ff', fg: '#175cd3', dot: '🔄', label: 'Applying…' },
};
function StatusBadge({ status }) {
    const b = BADGE[status] || BADGE.unconfigured;
    return (
        <span style={{ background: b.bg, color: b.fg, borderRadius: 999, padding: '4px 12px', fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap' }}>
            {b.dot} {b.label}
        </span>
    );
}

function TerminalPanel({ lines }) {
    const text = (lines || []).join('\n');
    return (
        <details style={{ marginTop: 12 }}>
            <summary style={{ cursor: 'pointer', fontSize: 13, color: '#5c677d', fontWeight: 600 }}>Terminal commands</summary>
            <div style={{ marginTop: 8 }}>
                <pre style={{ background: '#0f172a', color: '#e5efff', padding: 12, borderRadius: 10, fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>{text || '# Fill in the form above to generate commands'}</pre>
                <button className="btn" style={{ background: '#e5e7eb', marginTop: 8, fontSize: 13, padding: '6px 12px' }} onClick={() => navigator.clipboard?.writeText(text)} disabled={!text}>Copy all commands</button>
            </div>
        </details>
    );
}

function ApplyLog({ commands, error }) {
    if (error) {
        return <div style={{ marginTop: 12, padding: '10px 14px', borderRadius: 10, background: '#fef3f2', color: '#b42318', fontSize: 13 }}>{error}</div>;
    }
    if (!commands || commands.length === 0) return null;
    return (
        <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: '#5c677d', marginBottom: 6 }}>Command log</div>
            <div style={{ background: '#0f172a', borderRadius: 10, padding: 10, maxHeight: 220, overflowY: 'auto' }}>
                {commands.map((c, i) => {
                    const st = c.status || 'success';
                    const color = st === 'failed' ? '#fda4af' : st === 'success' ? '#86efac' : '#fcd34d';
                    return (
                        <div key={i} style={{ fontSize: 12, color: '#e5efff', fontFamily: 'monospace', padding: '2px 0' }}>
                            <span style={{ color }}>{st === 'failed' ? '✗' : st === 'success' ? '✓' : '…'}</span>{' '}
                            {c.command || ''} {c.path || ''}{c.error ? ` — ${c.error}` : ''}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

function SectionCard({ index, title, status, expanded, onToggle, children }) {
    return (
        <div className="card" style={{ marginBottom: 14, overflow: 'hidden' }}>
            <div onClick={onToggle} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 18px', cursor: 'pointer', background: '#fafcff' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontWeight: 700, fontSize: 16 }}>
                    <span style={{ color: '#5c677d' }}>{expanded ? '▼' : '▶'}</span>
                    {index}. {title}
                </div>
                <StatusBadge status={status} />
            </div>
            {expanded && <div style={{ padding: '4px 18px 18px' }}>{children}</div>}
        </div>
    );
}

function Field({ label, children, hint }) {
    return (
        <div className="form-group">
            <label>{label}</label>
            {children}
            {hint && <p style={{ color: '#5c677d', fontSize: 13, margin: '4px 0 0' }}>{hint}</p>}
        </div>
    );
}

function OfflineNote({ online }) {
    if (online) return null;
    return (
        <div style={{ marginBottom: 12, padding: '10px 14px', borderRadius: 10, background: '#fffaeb', border: '1px solid #fedf89', color: '#b54708', fontSize: 13 }}>
            Router must be connected via VPN tunnel or direct IP to apply settings.
        </div>
    );
}

function ActionRow({ online, detecting, applying, onDetect, onApply, applyLabel, extra }) {
    return (
        <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
            <button className="btn" style={{ background: '#e5e7eb' }} onClick={onDetect} disabled={detecting || applying}>
                {detecting ? 'Detecting…' : 'Detect current config'}
            </button>
            {extra}
            <button className="btn btn-primary" style={{ marginLeft: 'auto' }} onClick={onApply} disabled={!online || applying || detecting} title={online ? '' : 'Router must be connected to apply'}>
                {applying ? 'Applying…' : (applyLabel || 'Apply')}
            </button>
        </div>
    );
}

// Generic per-section state hook for detect/apply lifecycle.
function useSection(routerId, section, onApplied) {
    const [detecting, setDetecting] = useState(false);
    const [applying, setApplying] = useState(false);
    const [log, setLog] = useState(null);
    const [error, setError] = useState('');
    const [detected, setDetected] = useState(null);

    const detect = useCallback(async (onData) => {
        setDetecting(true); setError('');
        try {
            const res = await apiCall(`/admin/routers/${routerId}/setup/${section}/detect`);
            if (res?.detail) { setError(res.detail); }
            else { setDetected(res); onData?.(res); }
        } catch (e) { setError(e.message); }
        finally { setDetecting(false); }
    }, [routerId, section]);

    const apply = useCallback(async (body) => {
        setApplying(true); setError(''); setLog(null);
        try {
            const res = await apiCall(`/admin/routers/${routerId}/setup/${section}/apply`, { method: 'POST', body: JSON.stringify(body) });
            if (res?.success) { setLog(res.commands_executed || []); onApplied?.(); }
            else { setError(res?.detail || 'Apply failed.'); }
        } catch (e) { setError(e.message); }
        finally { setApplying(false); }
    }, [routerId, section, onApplied]);

    return { detecting, applying, log, error, detected, detect, apply };
}

function NetworkSection({ routerId, online, status, cfg, ifaces, expanded, onToggle, onApplied }) {
    const s = useSection(routerId, 'network', onApplied);
    const [form, setForm] = useState({
        bridge_name: cfg?.bridge_name || 'bridge-hotspot',
        interfaces: cfg?.interfaces || [],
        gateway_ip: cfg?.gateway_ip || '192.168.10.1',
        prefix: cfg?.prefix || 24,
        pool_start: cfg?.pool_start || '',
        pool_end: cfg?.pool_end || '',
        dns: cfg?.dns || '8.8.8.8',
        lease_time: cfg?.lease_time || '1h',
    });
    const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
    const plan = subnetPlan(form.gateway_ip, Number(form.prefix));
    const poolStart = form.pool_start || plan?.poolStart || '';
    const poolEnd = form.pool_end || plan?.poolEnd || '';
    const interfaceList = ifaces;

    const terminal = plan ? [
        `/interface/bridge/add name=${form.bridge_name} comment="hotspot-bridge"`,
        ...form.interfaces.map(i => `/interface/bridge/port/add bridge=${form.bridge_name} interface=${i}`),
        `/ip/address/add address=${form.gateway_ip}/${form.prefix} interface=${form.bridge_name}`,
        `/ip/pool/add name=hs-pool ranges=${poolStart}-${poolEnd}`,
        `/ip/dhcp-server/add name=dhcp-hotspot interface=${form.bridge_name} address-pool=hs-pool disabled=no`,
        `/ip/dhcp-server/network/add address=${plan.network}/${form.prefix} gateway=${form.gateway_ip} dns-server=${form.dns}`,
    ] : [];

    const toggleIface = (name) => set('interfaces', form.interfaces.includes(name) ? form.interfaces.filter(i => i !== name) : [...form.interfaces, name]);

    return (
        <SectionCard index={1} title="Network" status={s.applying ? 'applying' : status} expanded={expanded} onToggle={onToggle}>
            <OfflineNote online={online} />
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                <Field label="Bridge name"><input value={form.bridge_name} onChange={e => set('bridge_name', e.target.value)} /></Field>
                <Field label="Gateway IP"><input value={form.gateway_ip} onChange={e => set('gateway_ip', e.target.value)} placeholder="192.168.10.1" /></Field>
                <Field label="Subnet size" hint={`This subnet supports up to ${hostsInSubnet(Number(form.prefix))} hotspot clients`}>
                    <select value={form.prefix} onChange={e => set('prefix', Number(e.target.value))}>
                        {SUBNET_OPTIONS.map(o => <option key={o.prefix} value={o.prefix}>{o.label}</option>)}
                    </select>
                </Field>
                <Field label="DNS server"><input value={form.dns} onChange={e => set('dns', e.target.value)} /></Field>
                <Field label="Pool start" hint="Auto-calculated; editable"><input value={poolStart} onChange={e => set('pool_start', e.target.value)} /></Field>
                <Field label="Pool end" hint="Auto-calculated; editable"><input value={poolEnd} onChange={e => set('pool_end', e.target.value)} /></Field>
                <Field label="Lease time">
                    <select value={form.lease_time} onChange={e => set('lease_time', e.target.value)}>
                        {LEASE_OPTIONS.map(l => <option key={l} value={l}>{l}</option>)}
                    </select>
                </Field>
            </div>
            <Field label="LAN interfaces to bridge" hint="Do not include the WAN interface (the one with your internet uplink).">
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                    {interfaceList.length === 0 && <span style={{ color: '#9ca3af', fontSize: 13 }}>Click “Detect current config” to load interfaces, or type names below.</span>}
                    {interfaceList.map(i => {
                        const name = i.name || i;
                        return (
                            <label key={name} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, border: '1px solid #d7deea', borderRadius: 8, padding: '6px 10px', fontSize: 13 }}>
                                <input type="checkbox" checked={form.interfaces.includes(name)} onChange={() => toggleIface(name)} />
                                {name}{i.type ? ` (${i.type})` : ''}
                            </label>
                        );
                    })}
                </div>
                <input style={{ marginTop: 8 }} placeholder="Add interface names comma-separated, e.g. ether2,ether3" onBlur={e => { const v = e.target.value.trim(); if (v) { set('interfaces', Array.from(new Set([...form.interfaces, ...v.split(',').map(x => x.trim()).filter(Boolean)]))); e.target.value = ''; } }} />
            </Field>
            <ActionRow online={online} detecting={s.detecting} applying={s.applying}
                onDetect={() => s.detect(res => {
                    const d = res?.detected || {};
                    const addr = (d.addresses || []).find(a => a.interface === form.bridge_name);
                    if (addr?.address) {
                        const [ip, pfx] = addr.address.split('/');
                        setForm(f => ({ ...f, gateway_ip: ip || f.gateway_ip, prefix: Number(pfx) || f.prefix }));
                    }
                    const members = (d.bridge_ports || []).filter(p => p.bridge === form.bridge_name).map(p => p.interface);
                    if (members.length) set('interfaces', members);
                })}
                onApply={() => s.apply({ bridge_name: form.bridge_name, interfaces: form.interfaces, gateway_ip: form.gateway_ip, prefix: Number(form.prefix), pool_start: poolStart, pool_end: poolEnd, dns: form.dns, lease_time: form.lease_time })}
                applyLabel="Apply network" />
            <ApplyLog commands={s.log} error={s.error} />
            <TerminalPanel lines={terminal} />
        </SectionCard>
    );
}

function HotspotSection({ routerId, online, status, cfg, networkReady, bridges, expanded, onToggle, onApplied }) {
    const s = useSection(routerId, 'hotspot', onApplied);
    const [form, setForm] = useState({
        bridge_name: cfg?.bridge_name || 'bridge-hotspot',
        dns_name: cfg?.dns_name || '',
        cookie: cfg?.login_by ? cfg.login_by.includes('cookie') : true,
        session_timeout: cfg?.session_timeout ?? 0,
        idle_timeout: cfg?.idle_timeout ?? 0,
        addresses_per_mac: cfg?.addresses_per_mac ?? 2,
    });
    const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
    const login_by = ['http-pap', ...(form.cookie ? ['cookie'] : [])];
    const bridgeList = bridges.length ? bridges : (s.detected?.detected?.bridges || []);
    const terminal = [
        `/ip/hotspot/add name=hotspot1 interface=${form.bridge_name} address-pool=hs-pool disabled=no`,
        `/ip/hotspot/profile/set [find name=hsprof1] login-by=${login_by.join(',')} use-radius=yes nas-port-type=wireless-802.11 dns-name=${form.dns_name}`,
        `/ip/hotspot/user/profile/set [find name=default] session-timeout=${form.session_timeout} idle-timeout=${form.idle_timeout || 'none'} shared-users=${form.addresses_per_mac}`,
    ];
    return (
        <SectionCard index={2} title="Hotspot" status={s.applying ? 'applying' : status} expanded={expanded} onToggle={onToggle}>
            <OfflineNote online={online} />
            {!networkReady && (
                <div style={{ marginBottom: 12, padding: '10px 14px', borderRadius: 10, background: '#fffaeb', border: '1px solid #fedf89', color: '#b54708', fontSize: 13 }}>
                    Network setup must be completed before configuring the hotspot.
                </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                <Field label="Hotspot interface (bridge)">
                    <select value={form.bridge_name} onChange={e => set('bridge_name', e.target.value)}>
                        <option value="bridge-hotspot">bridge-hotspot</option>
                        {bridgeList.map(b => { const n = b.name || b; return n !== 'bridge-hotspot' ? <option key={n} value={n}>{n}</option> : null; })}
                    </select>
                </Field>
                <Field label="DNS name" hint="Shown in the browser when clients are redirected to login">
                    <input value={form.dns_name} onChange={e => set('dns_name', e.target.value)} placeholder="hotspot.youroperator.local" />
                </Field>
                <Field label="Session timeout (minutes)" hint="0 = use the RADIUS value (recommended)"><input type="number" min="0" value={form.session_timeout} onChange={e => set('session_timeout', Number(e.target.value))} /></Field>
                <Field label="Idle timeout (minutes)" hint="0 = disabled"><input type="number" min="0" value={form.idle_timeout} onChange={e => set('idle_timeout', Number(e.target.value))} /></Field>
                <Field label="Addresses per MAC"><input type="number" min="1" value={form.addresses_per_mac} onChange={e => set('addresses_per_mac', Number(e.target.value))} /></Field>
            </div>
            <Field label="Login methods">
                <div style={{ display: 'flex', gap: 16 }}>
                    <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked readOnly /> HTTP PAP (required)</label>
                    <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={form.cookie} onChange={e => set('cookie', e.target.checked)} /> Cookie</label>
                </div>
            </Field>
            <ActionRow online={online && networkReady} detecting={s.detecting} applying={s.applying}
                onDetect={() => s.detect(res => {
                    const prof = (res?.detected?.profiles || [])[0];
                    if (prof?.dns_name) set('dns_name', prof.dns_name);
                })}
                onApply={() => s.apply({ bridge_name: form.bridge_name, dns_name: form.dns_name, login_by, session_timeout: Number(form.session_timeout), idle_timeout: Number(form.idle_timeout), addresses_per_mac: Number(form.addresses_per_mac) })}
                applyLabel="Apply hotspot" />
            <ApplyLog commands={s.log} error={s.error} />
            <TerminalPanel lines={terminal} />
        </SectionCard>
    );
}

function RadiusSection({ routerId, online, status, cfg, expanded, onToggle, onApplied }) {
    const s = useSection(routerId, 'radius', onApplied);
    const [timeout, setTimeoutMs] = useState(cfg?.timeout || 3000);
    const [secret, setSecret] = useState({ masked: '●●●●●●●●', hint: '' });
    const [showHint, setShowHint] = useState(false);
    const [radiusHost, setRadiusHost] = useState(cfg?.radius_host || '');

    useEffect(() => {
        apiCall(`/admin/routers/${routerId}/nas-secret`).then(d => { if (d && !d.detail) setSecret(d); }).catch(() => {});
    }, [routerId]);

    const terminal = [
        `/radius/add service=hotspot address=${radiusHost || '<platform-radius-host>'} secret=<your-router-secret> authentication-port=1812 accounting-port=1813 timeout=${Math.floor(timeout / 1000)}s`,
        `/ip/hotspot/profile/set [find name=hsprof1] use-radius=yes`,
    ];
    return (
        <SectionCard index={3} title="RADIUS" status={s.applying ? 'applying' : status} expanded={expanded} onToggle={onToggle}>
            <OfflineNote online={online} />
            <div style={{ marginBottom: 12, padding: '10px 14px', borderRadius: 10, background: '#eff8ff', color: '#175cd3', fontSize: 13 }}>
                These settings are pre-configured for your platform. The shared secret is unique to this router — do not share it.
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                <Field label="Server address" hint="Platform RADIUS host (from platform settings)"><input value={radiusHost} onChange={e => setRadiusHost(e.target.value)} placeholder="set by platform / detect" readOnly /></Field>
                <Field label="Authentication port"><input value="1812" readOnly /></Field>
                <Field label="Accounting port"><input value="1813" readOnly /></Field>
                <Field label="Service"><input value="hotspot" readOnly /></Field>
                <Field label="Shared secret">
                    <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                        <input value={showHint ? (secret.hint || secret.masked) : secret.masked} readOnly style={{ flex: 1 }} />
                        <button className="btn" style={{ background: '#e5e7eb', fontSize: 13, padding: '6px 12px' }} onClick={() => setShowHint(v => !v)}>{showHint ? 'Hide' : 'Show'}</button>
                    </div>
                </Field>
                <Field label="Timeout (ms)"><input type="number" min="100" value={timeout} onChange={e => setTimeoutMs(Number(e.target.value))} /></Field>
            </div>
            <ActionRow online={online} detecting={s.detecting} applying={s.applying}
                onDetect={() => s.detect(res => {
                    if (res?.radius_host) setRadiusHost(res.radius_host);
                })}
                onApply={() => s.apply({ timeout: Number(timeout) })}
                applyLabel="Apply RADIUS" />
            {s.detected?.other_addresses?.length > 0 && (
                <div style={{ marginTop: 10, padding: '8px 12px', borderRadius: 8, background: '#fffaeb', color: '#b54708', fontSize: 13 }}>
                    A RADIUS server already points to {s.detected.other_addresses.join(', ')}. Applying adds a new entry alongside it.
                </div>
            )}
            <ApplyLog commands={s.log} error={s.error} />
            <TerminalPanel lines={terminal} />
        </SectionCard>
    );
}

function NatSection({ routerId, online, status, cfg, hotspotNetwork, ifaces, expanded, onToggle, onApplied }) {
    const s = useSection(routerId, 'nat', onApplied);
    const [form, setForm] = useState({
        wan_interface: cfg?.wan_interface || '',
        enable_nat: cfg?.enable_nat ?? true,
        established: (cfg?.firewall_options || ['established', 'invalid', 'icmp']).includes('established'),
        invalid: (cfg?.firewall_options || ['established', 'invalid', 'icmp']).includes('invalid'),
        icmp: (cfg?.firewall_options || ['established', 'invalid', 'icmp']).includes('icmp'),
    });
    const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
    const network = cfg?.hotspot_network || hotspotNetwork || '';
    const wanList = ifaces.length ? ifaces : (s.detected?.detected?.interfaces || []);
    const firewall_options = [form.established && 'established', form.invalid && 'invalid', form.icmp && 'icmp'].filter(Boolean);
    const terminal = [
        form.enable_nat && `/ip/firewall/nat/add chain=srcnat src-address=${network || '<hotspot-network>'} out-interface=${form.wan_interface || '<wan>'} action=masquerade comment="hotspot-nat"`,
        form.established && '/ip/firewall/filter/add chain=forward connection-state=established,related action=accept comment="allow-established"',
        form.invalid && '/ip/firewall/filter/add chain=forward connection-state=invalid action=drop comment="drop-invalid"',
        form.icmp && '/ip/firewall/filter/add chain=input protocol=icmp action=accept comment="allow-icmp"',
    ].filter(Boolean);
    const duplicate = s.detected?.duplicate_masquerade;
    return (
        <SectionCard index={4} title="NAT & Firewall" status={s.applying ? 'applying' : status} expanded={expanded} onToggle={onToggle}>
            <OfflineNote online={online} />
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                <Field label="WAN interface" hint="The interface connected to your internet provider">
                    <select value={form.wan_interface} onChange={e => set('wan_interface', e.target.value)}>
                        <option value="">Select interface</option>
                        {wanList.map(i => { const n = i.name || i; return <option key={n} value={n}>{n}{i.type ? ` (${i.type})` : ''}</option>; })}
                    </select>
                </Field>
                <Field label="Hotspot source network"><input value={network} readOnly placeholder="from Network setup" /></Field>
            </div>
            <Field label="Internet access">
                <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={form.enable_nat} onChange={e => set('enable_nat', e.target.checked)} /> Enable internet access for hotspot clients (NAT masquerade)</label>
            </Field>
            <details>
                <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600, color: '#5c677d' }}>Basic firewall protection</summary>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
                    <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={form.established} onChange={e => set('established', e.target.checked)} /> Allow established/related connections</label>
                    <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={form.invalid} onChange={e => set('invalid', e.target.checked)} /> Drop invalid packets</label>
                    <label style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={form.icmp} onChange={e => set('icmp', e.target.checked)} /> Allow ICMP (ping)</label>
                </div>
            </details>
            {duplicate && (
                <div style={{ marginTop: 10, padding: '8px 12px', borderRadius: 8, background: '#fffaeb', color: '#b54708', fontSize: 13 }}>
                    A duplicate masquerade rule was detected. Use “Remove duplicates” to clean up.
                </div>
            )}
            <ActionRow online={online} detecting={s.detecting} applying={s.applying}
                onDetect={() => s.detect(res => { const w = res?.detected?.suggested_wan; if (w) set('wan_interface', w); })}
                onApply={() => s.apply({ wan_interface: form.wan_interface, hotspot_network: network || null, enable_nat: form.enable_nat, firewall_options })}
                applyLabel="Apply NAT"
                extra={duplicate ? <button className="btn" style={{ background: '#fef3f2', color: '#b42318' }} onClick={() => s.apply({ wan_interface: form.wan_interface, hotspot_network: network || null, enable_nat: form.enable_nat, firewall_options, remove_duplicates: true })} disabled={!online || s.applying}>Remove duplicates</button> : null} />
            <ApplyLog commands={s.log} error={s.error} />
            <TerminalPanel lines={terminal} />
        </SectionCard>
    );
}

function SetupProgress({ complete, total }) {
    const filled = Math.round((complete / total) * 10);
    return (
        <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 13, color: '#5c677d', marginBottom: 6, fontWeight: 600 }}>Setup progress: {complete}/{total} sections complete</div>
            <div style={{ fontFamily: 'monospace', fontSize: 18, color: '#0d6e5f', letterSpacing: 2 }}>{'█'.repeat(filled)}{'░'.repeat(10 - filled)}</div>
        </div>
    );
}

function RouterSetupTab({ routerId }) {
    const [status, setStatus] = useState(null);
    const [ifaces, setIfaces] = useState([]);
    const [expanded, setExpanded] = useState({ network: true, hotspot: false, radius: false, nat: false });

    const loadStatus = useCallback(async () => {
        const data = await apiCall(`/admin/routers/${routerId}/setup/status`);
        if (data && !data.detail) setStatus(data);
    }, [routerId]);

    useEffect(() => {
        loadStatus();
        apiCall(`/admin/routers/${routerId}/interfaces`).then(d => { if (Array.isArray(d)) setIfaces(d); }).catch(() => {});
    }, [routerId, loadStatus]);

    if (!status) return <p>Loading setup status…</p>;
    const toggle = (k) => setExpanded(e => ({ ...e, [k]: !e[k] }));
    const online = status.online;
    const networkReady = status.network?.status === 'configured';
    const hotspotNetwork = status.network?.config?.hotspot_network;

    return (
        <div>
            <SetupProgress complete={status.sections_complete} total={status.total_sections || 4} />
            {!online && (
                <div style={{ marginBottom: 16, padding: '12px 16px', borderRadius: 12, background: '#fef3f2', border: '1px solid #fda4af', color: '#b42318', fontSize: 14 }}>
                    This router is offline. Detect and Apply require a live connection (VPN tunnel or direct IP). Forms remain editable.
                </div>
            )}
            <NetworkSection routerId={routerId} online={online} status={status.network?.status} cfg={status.network?.config} ifaces={ifaces} expanded={expanded.network} onToggle={() => toggle('network')} onApplied={loadStatus} />
            <HotspotSection routerId={routerId} online={online} status={status.hotspot?.status} cfg={status.hotspot?.config} networkReady={networkReady} bridges={[]} expanded={expanded.hotspot} onToggle={() => toggle('hotspot')} onApplied={loadStatus} />
            <RadiusSection routerId={routerId} online={online} status={status.radius?.status} cfg={status.radius?.config} expanded={expanded.radius} onToggle={() => toggle('radius')} onApplied={loadStatus} />
            <NatSection routerId={routerId} online={online} status={status.nat?.status} cfg={status.nat?.config} hotspotNetwork={hotspotNetwork} ifaces={ifaces} expanded={expanded.nat} onToggle={() => toggle('nat')} onApplied={loadStatus} />
        </div>
    );
}

function RouterDetail({ routerId, onBack }) {
    const [router, setRouter] = useState(null);
    const [tab, setTab] = useState('overview');
    const [metrics, setMetrics] = useState([]);
    const [sessions, setSessions] = useState([]);
    const [logs, setLogs] = useState([]);
    const [diagnostics, setDiagnostics] = useState(null);
    const [diagLoading, setDiagLoading] = useState(false);
    const [banner, setBanner] = useState('');
    const [editOpen, setEditOpen] = useState(false);
    const [reprovisionOpen, setReprovisionOpen] = useState(false);
    const [rebootOpen, setRebootOpen] = useState(false);
    const [rebootConfirm, setRebootConfirm] = useState('');
    const [editForm, setEditForm] = useState({});
    const [rpIfaces, setRpIfaces] = useState([]);
    const [rpTemplates, setRpTemplates] = useState([]);
    const [rpDns, setRpDns] = useState('');
    const [rpIface, setRpIface] = useState('');
    const [rpTemplate, setRpTemplate] = useState('');
    const [setupSummary, setSetupSummary] = useState(null);
    const cpuRef = useRef(null);
    const sessRef = useRef(null);
    const txRef = useRef(null);
    const refreshRef = useRef(null);

    const loadOverview = useCallback(async () => {
        const data = await apiCall(`/admin/routers/${routerId}`);
        if (data) {
            setRouter(data);
            setEditForm({
                name: data.name || '',
                ip_address: data.ip_address || '',
                nas_identifier: data.nas_identifier || '',
                nas_secret: '',
                api_username: data.credentials?.api_username || '',
                api_port: data.credentials?.api_port || 8728,
                api_password: '',
                use_ssl: data.credentials?.use_ssl ? 'true' : 'false',
            });
        }
    }, [routerId]);

    const loadMetrics = useCallback(async () => {
        const data = await apiCall(`/admin/routers/${routerId}/metrics?hours=24`);
        if (data) setMetrics(data);
    }, [routerId]);

    const loadSessions = useCallback(async () => {
        const data = await apiCall(`/admin/routers/${routerId}/active-sessions`);
        if (data) setSessions(data);
    }, [routerId]);

    const loadLogs = useCallback(async () => {
        const data = await apiCall(`/admin/routers/${routerId}/provision-logs`);
        if (data) setLogs(data);
    }, [routerId]);

    const loadSetupSummary = useCallback(async () => {
        const data = await apiCall(`/admin/routers/${routerId}/setup/status`);
        if (data && !data.detail) setSetupSummary(data);
    }, [routerId]);

    useEffect(() => {
        Promise.all([loadOverview(), loadMetrics(), loadSessions(), loadLogs(), loadSetupSummary()]);
        refreshRef.current = setInterval(() => loadSessions().catch(() => {}), 30000);
        return () => clearInterval(refreshRef.current);
    }, [loadOverview, loadMetrics, loadSessions, loadLogs, loadSetupSummary]);

    useEffect(() => {
        if (tab === 'metrics' && metrics.length > 0) {
            drawLineChart(cpuRef.current, metrics, '#0b6e4f', r => r.cpu_load_percent || 0);
            drawLineChart(sessRef.current, metrics, '#175cd3', r => r.active_sessions || 0);
            drawLineChart(txRef.current, metrics, '#f97316', r => (r.total_tx_bytes || 0) + (r.total_rx_bytes || 0));
        }
    }, [tab, metrics]);

    async function saveEdit() {
        const payload = { name: editForm.name, ip_address: editForm.ip_address, nas_identifier: editForm.nas_identifier };
        if (editForm.nas_secret) payload.nas_secret = editForm.nas_secret;
        await apiCall(`/sites/routers/${routerId}`, { method: 'PUT', body: JSON.stringify(payload) });
        if (editForm.api_password) {
            await apiCall(`/admin/routers/${routerId}/credentials`, {
                method: 'PUT',
                body: JSON.stringify({ api_username: editForm.api_username, api_password: editForm.api_password, api_port: Number(editForm.api_port), use_ssl: editForm.use_ssl === 'true' }),
            });
        }
        setEditOpen(false);
        await Promise.all([loadOverview(), loadLogs()]);
        setBanner(editForm.api_password ? 'Router record and credentials updated.' : 'Router record updated.');
    }

    async function loadReprovisionData() {
        const [ifaces, tpls] = await Promise.all([
            apiCall(`/admin/routers/${routerId}/interfaces`).catch(() => []),
            apiCall('/admin/config-templates').catch(() => []),
        ]);
        setRpIfaces(ifaces || []);
        setRpTemplates(tpls || []);
    }

    async function startReprovision() {
        if (!rpDns) throw new Error('Enter a DNS name.');
        if (!rpIface) throw new Error('Choose a hotspot interface.');
        await apiCall(`/admin/routers/${routerId}/provision`, {
            method: 'POST',
            body: JSON.stringify({ dns_name: rpDns, hotspot_interface: rpIface, template_id: rpTemplate || null }),
        });
        setReprovisionOpen(false);
        await Promise.all([loadOverview(), loadLogs()]);
        setTab('logs');
        setBanner('Reprovision started. Watch the Provision log tab.');
    }

    async function doReboot() {
        if (rebootConfirm !== 'REBOOT') { alert('Type REBOOT to confirm.'); return; }
        await apiCall(`/admin/routers/${routerId}/reboot`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
        setRebootOpen(false);
        setRebootConfirm('');
        setBanner('Reboot command queued.');
    }

    async function disconnectUser(activeId, username) {
        await apiCall(`/admin/routers/${routerId}/disconnect-user`, {
            method: 'POST',
            body: JSON.stringify({ active_id: activeId, username }),
        });
        await loadSessions();
    }

    async function runDiagnostics() {
        setDiagLoading(true);
        setDiagnostics(null);
        const data = await apiCall(`/admin/routers/${routerId}/diagnostics`);
        setDiagnostics(data);
        setDiagLoading(false);
    }

    if (!router) return <p>Loading router…</p>;

    const info = router.system_info || {};
    const TABS = ['overview', 'setup', 'metrics', 'sessions', 'logs', 'vpn'];

    return (
        <div>
            {banner && (
                <div style={{ marginBottom: 16, padding: '10px 14px', borderRadius: 12, background: '#ecfdf3', color: '#067647' }}>
                    {banner} <button onClick={() => setBanner('')} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#067647', float: 'right' }}>✕</button>
                </div>
            )}

            <button onClick={onBack} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#0d6e5f', fontWeight: 600, marginBottom: 12, padding: 0 }}>← Back to Routers</button>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12, marginBottom: 16 }}>
                <div>
                    <h1 style={{ margin: 0, fontSize: 28, fontWeight: 700 }}>{router.name}</h1>
                    <p style={{ margin: '4px 0 0', color: '#5c677d' }}>{router.site_name} — {router.ip_address || 'VPN only'} — {router.nas_identifier}</p>
                    {setupSummary && (
                        <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 10, fontSize: 13, color: '#5c677d' }}>
                            <span style={{ fontWeight: 600 }}>Setup progress: {setupSummary.sections_complete}/{setupSummary.total_sections || 4}</span>
                            <span style={{ fontFamily: 'monospace', color: '#0d6e5f', letterSpacing: 1 }}>
                                {'█'.repeat(Math.round((setupSummary.sections_complete / (setupSummary.total_sections || 4)) * 10))}
                                {'░'.repeat(10 - Math.round((setupSummary.sections_complete / (setupSummary.total_sections || 4)) * 10))}
                            </span>
                            <button onClick={() => setTab('setup')} style={{ background: 'none', border: 'none', color: '#0d6e5f', cursor: 'pointer', fontWeight: 600, padding: 0 }}>Open setup →</button>
                        </div>
                    )}
                </div>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setEditOpen(true)}>Edit</button>
                    <button className="btn btn-primary" onClick={() => { setReprovisionOpen(true); loadReprovisionData(); }}>Reprovision</button>
                    <button className="btn" style={{ background: '#e5e7eb' }} onClick={runDiagnostics} disabled={diagLoading}>{diagLoading ? 'Running…' : 'Diagnostics'}</button>
                    <button className="btn" style={{ background: '#fef3f2', color: '#b42318' }} onClick={() => setRebootOpen(true)}>Reboot</button>
                </div>
            </div>

            {/* Tabs */}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 16 }}>
                {TABS.map(t => (
                    <button key={t} onClick={() => setTab(t)} style={{ border: `1px solid ${t === tab ? '#0d6e5f' : '#d7deea'}`, background: t === tab ? '#0d6e5f' : '#fff', color: t === tab ? '#fff' : '#14213d', borderRadius: 999, padding: '8px 16px', cursor: 'pointer', fontWeight: 600, textTransform: 'capitalize' }}>{t === 'logs' ? 'Provision log' : t === 'sessions' ? 'Active sessions' : t === 'vpn' ? 'VPN Tunnel' : t.charAt(0).toUpperCase() + t.slice(1)}</button>
                ))}
            </div>

            {/* Overview */}
            {tab === 'overview' && (
                <div className="card" style={{ padding: 18 }}>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 14 }}>
                        {[
                            ['Connection status', router.connection_status || 'unknown'],
                            ['Last connected', fmt(router.last_connected_at)],
                            ['Last seen', fmt(router.last_seen_at)],
                            ['Board name', info.board_name || '—'],
                            ['RouterOS version', info.ros_version || '—'],
                            ['CPU / Memory', `${info.cpu_load_percent ?? '—'}% / ${info.memory_used_percent ?? '—'}%`],
                        ].map(([label, value]) => (
                            <div key={label} style={{ padding: 14, border: '1px solid #d7deea', borderRadius: 12, background: '#fafcff' }}>
                                <div style={{ color: '#5c677d', fontSize: 12, textTransform: 'uppercase', letterSpacing: '.05em' }}>{label}</div>
                                <div style={{ fontSize: 18, fontWeight: 700, marginTop: 6 }}>{value}</div>
                            </div>
                        ))}
                    </div>
                    {diagnostics && (
                        <div style={{ marginTop: 18 }}>
                            <h4 style={{ margin: '0 0 8px' }}>Diagnostics</h4>
                            <pre style={{ background: '#0f172a', color: '#e5efff', padding: 12, borderRadius: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13 }}>{JSON.stringify(diagnostics, null, 2)}</pre>
                        </div>
                    )}
                </div>
            )}

            {/* Metrics */}
            {tab === 'metrics' && (
                <div className="card" style={{ padding: 18 }}>
                    <p style={{ color: '#5c677d', marginTop: 0 }}>Last 24 hours — CPU load, active sessions, and traffic.</p>
                    <canvas ref={cpuRef} width="900" height="200" style={{ width: '100%', border: '1px solid #d7deea', borderRadius: 12 }} />
                    <canvas ref={sessRef} width="900" height="200" style={{ width: '100%', border: '1px solid #d7deea', borderRadius: 12, marginTop: 14 }} />
                    <canvas ref={txRef} width="900" height="200" style={{ width: '100%', border: '1px solid #d7deea', borderRadius: 12, marginTop: 14 }} />
                </div>
            )}

            {/* Sessions */}
            {tab === 'sessions' && (
                <div className="card">
                    <div className="table-wrap">
                        <table>
                            <thead><tr><th>User</th><th>IP</th><th>MAC</th><th>Uptime</th><th>Data In</th><th>Data Out</th><th>Time Left</th><th></th></tr></thead>
                            <tbody>
                                {sessions.length === 0
                                    ? <tr><td colSpan="8" style={{ color: '#9ca3af', padding: 16 }}>No active sessions.</td></tr>
                                    : sessions.map((s, i) => (
                                        <tr key={i}>
                                            <td>{s.user || '—'}</td>
                                            <td>{s.address || '—'}</td>
                                            <td><code>{s.mac_address || '—'}</code></td>
                                            <td>{s.uptime || '—'}</td>
                                            <td>{fmtBytes(s.bytes_in)}</td>
                                            <td>{fmtBytes(s.bytes_out)}</td>
                                            <td>{s.session_time_left || '—'}</td>
                                            <td>
                                                <button className="btn" style={{ background: '#fef3f2', color: '#b42318', padding: '6px 10px', fontSize: 13 }} onClick={() => disconnectUser(s.id, s.user).catch(e => alert(e.message))}>
                                                    Disconnect
                                                </button>
                                            </td>
                                        </tr>
                                    ))
                                }
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Provision logs */}
            {tab === 'logs' && (
                <div className="card">
                    <div className="table-wrap">
                        <table>
                            <thead><tr><th>Date</th><th>Action</th><th>Triggered by</th><th>Status</th><th>Commands</th></tr></thead>
                            <tbody>
                                {logs.length === 0
                                    ? <tr><td colSpan="5" style={{ color: '#9ca3af', padding: 16 }}>No provision logs.</td></tr>
                                    : logs.map(l => (
                                        <tr key={l.id}>
                                            <td style={{ fontSize: 13, color: '#5c677d' }}>{fmt(l.started_at)}</td>
                                            <td>{l.action}</td>
                                            <td style={{ fontSize: 13 }}>{l.triggered_by}</td>
                                            <td><span className={`badge ${l.status === 'success' ? 'badge-green' : l.status === 'failed' ? 'badge-red' : 'badge-gray'}`}>{l.status}</span></td>
                                            <td>
                                                <details>
                                                    <summary style={{ cursor: 'pointer', fontSize: 13 }}>View commands</summary>
                                                    <pre style={{ background: '#0f172a', color: '#e5efff', padding: 10, borderRadius: 10, fontSize: 12, marginTop: 6, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{JSON.stringify(l.commands_executed, null, 2)}</pre>
                                                </details>
                                            </td>
                                        </tr>
                                    ))
                                }
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Setup wizard */}
            {tab === 'setup' && (
                <RouterSetupTab routerId={routerId} />
            )}

            {/* VPN Tunnel */}
            {tab === 'vpn' && (
                <VpnTunnelTab routerId={routerId} onChange={loadOverview} />
            )}

            {/* Edit modal */}
            <Modal open={editOpen} onClose={() => setEditOpen(false)} title="Edit router" wide>
                <p style={{ color: '#5c677d', marginTop: 0 }}>Leave RouterOS password blank to keep the current credentials.</p>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                    {[
                        { label: 'Router name', key: 'name' },
                        { label: 'Router IP address', key: 'ip_address' },
                        { label: 'NAS identifier', key: 'nas_identifier' },
                        { label: 'NAS shared secret', key: 'nas_secret', placeholder: 'Leave blank to keep current' },
                        { label: 'RouterOS username', key: 'api_username' },
                        { label: 'API port', key: 'api_port', type: 'number' },
                        { label: 'RouterOS password', key: 'api_password', type: 'password', placeholder: 'Leave blank to keep current' },
                    ].map(({ label, key, type, placeholder }) => (
                        <div key={key} className="form-group">
                            <label>{label}</label>
                            <input type={type || 'text'} value={editForm[key] || ''} placeholder={placeholder} onChange={e => setEditForm(f => ({ ...f, [key]: e.target.value }))} />
                        </div>
                    ))}
                    <div className="form-group">
                        <label>Use SSL</label>
                        <select value={editForm.use_ssl || 'false'} onChange={e => setEditForm(f => ({ ...f, use_ssl: e.target.value }))}>
                            <option value="false">No</option>
                            <option value="true">Yes</option>
                        </select>
                    </div>
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
                    <button className="btn btn-primary" onClick={() => saveEdit().catch(e => alert(e.message))}>Save changes</button>
                    <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setEditOpen(false)}>Cancel</button>
                </div>
            </Modal>

            {/* Reprovision modal */}
            <Modal open={reprovisionOpen} onClose={() => setReprovisionOpen(false)} title="Reprovision router" wide>
                <p style={{ color: '#5c677d', marginTop: 0 }}>Use this after editing credentials or changing RouterOS settings. Progress appears in the Provision log tab.</p>
                {router.wireguard?.enabled && router.wireguard?.connected && (
                    <div style={{ marginBottom: 14, padding: '12px 16px', background: '#ecfdf3', border: '1px solid #abefc6', borderRadius: 12, color: '#067647', fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#12b76a', display: 'inline-block' }} />
                        VPN tunnel active — provisioning will use tunnel IP {router.wireguard.tunnel_ip}
                    </div>
                )}
                {router.wireguard?.enabled && !router.wireguard?.connected && (
                    <div style={{ marginBottom: 14, padding: '12px 16px', background: '#fffaeb', border: '1px solid #fedf89', borderRadius: 12, color: '#b54708', fontSize: 14 }}>
                        VPN tunnel configured but not connected — provisioning may fail.
                    </div>
                )}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                    <div className="form-group">
                        <label>Captive portal DNS name</label>
                        <input value={rpDns} onChange={e => setRpDns(e.target.value)} placeholder="hotspot.yourisp.com" />
                    </div>
                    <div className="form-group">
                        <label>Config template</label>
                        <select value={rpTemplate} onChange={e => setRpTemplate(e.target.value)}>
                            <option value="">No template</option>
                            {rpTemplates.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                        </select>
                    </div>
                    <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                        <label>Hotspot interface</label>
                        <select value={rpIface} onChange={e => setRpIface(e.target.value)}>
                            <option value="">Select interface</option>
                            {rpIfaces.map(i => <option key={i.name} value={i.name}>{i.name}</option>)}
                        </select>
                        {rpIfaces.length === 0 && <p style={{ color: '#5c677d', fontSize: 13, margin: '4px 0 0' }}>Load interfaces from the router before starting if this list is empty.</p>}
                    </div>
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 16, flexWrap: 'wrap' }}>
                    <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => loadReprovisionData().catch(e => alert(e.message))}>Load interfaces</button>
                    <button className="btn btn-primary" onClick={() => startReprovision().catch(e => alert(e.message))}>Start reprovision</button>
                    <button className="btn" style={{ background: '#e5e7eb', marginLeft: 'auto' }} onClick={() => setReprovisionOpen(false)}>Cancel</button>
                </div>
            </Modal>

            {/* Reboot modal */}
            <Modal open={rebootOpen} onClose={() => setRebootOpen(false)} title="Confirm reboot">
                <p>Type <strong>REBOOT</strong> to confirm sending the reboot command.</p>
                <input value={rebootConfirm} onChange={e => setRebootConfirm(e.target.value)} placeholder="REBOOT" />
                <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                    <button className="btn" style={{ background: '#fef3f2', color: '#b42318' }} onClick={doReboot}>Send reboot</button>
                    <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => { setRebootOpen(false); setRebootConfirm(''); }}>Cancel</button>
                </div>
            </Modal>
        </div>
    );
}

// ─── ROOT COMPONENT ───────────────────────────────────────────────────────────

export default function Routers() {
    const [view, setView] = useState('list');
    const [selectedId, setSelectedId] = useState(null);

    if (view === 'wizard') {
        return (
            <RouterWizard
                onBack={() => setView('list')}
                onDone={id => { setSelectedId(id); setView('detail'); }}
            />
        );
    }
    if (view === 'detail' && selectedId) {
        return (
            <RouterDetail
                routerId={selectedId}
                onBack={() => { setSelectedId(null); setView('list'); }}
            />
        );
    }
    return (
        <RouterList
            onAdd={() => setView('wizard')}
            onSelect={id => { setSelectedId(id); setView('detail'); }}
        />
    );
}
