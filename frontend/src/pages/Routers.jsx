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
                                    <td><code>{r.ip_address}</code></td>
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

const WIZARD_STEPS = ['1. Basic info', '2. API connection', '3. Hotspot config', '4. Provision'];

function RouterWizard({ onBack, onDone }) {
    const [step, setStep] = useState(1);
    const [sites, setSites] = useState([]);
    const [templates, setTemplates] = useState([]);
    const [interfaces, setInterfaces] = useState([]);
    const [connTested, setConnTested] = useState(false);
    const [connResult, setConnResult] = useState('Test the connection before moving on.');
    const [provisionLog, setProvisionLog] = useState([]);
    const [provisionDone, setProvisionDone] = useState(null);
    const [state, setState] = useState({
        name: '', site_id: '', nas_identifier: '', nas_secret: randomSecret(),
        ip_address: '', api_port: 8728, api_username: 'admin', api_password: '',
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

    function validate() {
        if (step === 1 && (!state.name || !state.site_id || !state.nas_identifier || !state.nas_secret))
            throw new Error('Fill in router name, site, NAS identifier, and NAS secret.');
        if (step === 2 && (!state.ip_address || !state.api_username || !state.api_password))
            throw new Error('Fill in the API connection fields.');
        if (step === 3 && (!connTested || !state.hotspot_interface || !state.dns_name))
            throw new Error('Test the connection successfully, then choose the hotspot interface and DNS name.');
    }

    function next() {
        try { validate(); setStep(s => Math.min(4, s + 1)); } catch (e) { alert(e.message); }
    }

    async function testConnection() {
        setConnResult('Testing…');
        setConnTested(false);
        const result = await apiCall('/admin/routers/test-connection', {
            method: 'POST',
            body: JSON.stringify({ host: state.ip_address, port: state.api_port, username: state.api_username, password: state.api_password, use_ssl: false }),
        });
        if (!result || !result.success) {
            setConnResult('Connection failed: ' + (result?.error || 'Unknown error'));
            return;
        }
        setConnResult(`Connected: ${result.board_name || 'MikroTik'} running RouterOS ${result.ros_version || 'unknown'}`);
        const ifaces = await apiCall('/admin/routers/temp-interfaces', {
            method: 'POST',
            body: JSON.stringify({ host: state.ip_address, port: state.api_port, username: state.api_username, password: state.api_password, use_ssl: false }),
        });
        setInterfaces(ifaces || []);
        const tpls = await apiCall('/admin/config-templates');
        setTemplates(tpls || []);
        setConnTested(true);
    }

    async function startProvision() {
        setProvisionLog([]);
        setProvisionDone(null);
        const result = await apiCall('/admin/routers/onboard', {
            method: 'POST',
            body: JSON.stringify({ ...state, api_port: Number(state.api_port), template_id: state.template_id || null }),
        });
        if (!result || !result.router_id) {
            setProvisionDone({ ok: false, message: result?.detail || 'Onboard failed.' });
            return;
        }
        const { router_id, log_id } = result;
        function poll() {
            apiCall(`/admin/routers/${router_id}/provision-status/${log_id}`).then(body => {
                if (!body) return;
                setProvisionLog(body.commands_executed || []);
                if (body.status === 'success') {
                    setProvisionDone({ ok: true, router_id, message: 'Provisioning complete.' });
                } else if (body.status === 'failed') {
                    setProvisionDone({ ok: false, message: 'Provisioning failed: ' + (body.error_message || 'Unknown error') });
                } else {
                    pollRef.current = setTimeout(poll, 2000);
                }
            });
        }
        poll();
    }

    useEffect(() => () => clearTimeout(pollRef.current), []);

    const summary = `Router: ${state.name || '-'} | NAS: ${state.nas_identifier || '-'} | IP: ${state.ip_address || '-'} | Interface: ${state.hotspot_interface || '-'} | DNS: ${state.dns_name || '-'}`;

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

                {/* Step 2 */}
                {step === 2 && (
                    <>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                            <div className="form-group">
                                <label>Router IP address</label>
                                <input value={state.ip_address} onChange={e => set('ip_address', e.target.value)} placeholder="192.168.99.1" />
                            </div>
                            <div className="form-group">
                                <label>API port</label>
                                <input type="number" value={state.api_port} onChange={e => set('api_port', e.target.value)} />
                            </div>
                            <div className="form-group">
                                <label>RouterOS username</label>
                                <input value={state.api_username} onChange={e => set('api_username', e.target.value)} />
                            </div>
                            <div className="form-group">
                                <label>RouterOS password</label>
                                <input type="password" value={state.api_password} onChange={e => set('api_password', e.target.value)} />
                            </div>
                        </div>
                        <div style={{ marginTop: 12 }}>
                            <button className="btn" style={{ background: '#f59e0b', color: '#1f2937' }} onClick={() => testConnection().catch(e => setConnResult(e.message))}>
                                Test connection
                            </button>
                            <div style={{ marginTop: 12, padding: '10px 14px', background: '#f8fafc', borderRadius: 10, color: '#5c677d', fontSize: 14 }}>{connResult}</div>
                        </div>
                    </>
                )}

                {/* Step 3 */}
                {step === 3 && (
                    <>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
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

// ─── ROUTER DETAIL ────────────────────────────────────────────────────────────

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

    useEffect(() => {
        Promise.all([loadOverview(), loadMetrics(), loadSessions(), loadLogs()]);
        refreshRef.current = setInterval(() => loadSessions().catch(() => {}), 30000);
        return () => clearInterval(refreshRef.current);
    }, [loadOverview, loadMetrics, loadSessions, loadLogs]);

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
    const TABS = ['overview', 'metrics', 'sessions', 'logs'];

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
                    <p style={{ margin: '4px 0 0', color: '#5c677d' }}>{router.site_name} — {router.ip_address} — {router.nas_identifier}</p>
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
                    <button key={t} onClick={() => setTab(t)} style={{ border: `1px solid ${t === tab ? '#0d6e5f' : '#d7deea'}`, background: t === tab ? '#0d6e5f' : '#fff', color: t === tab ? '#fff' : '#14213d', borderRadius: 999, padding: '8px 16px', cursor: 'pointer', fontWeight: 600, textTransform: 'capitalize' }}>{t === 'logs' ? 'Provision log' : t === 'sessions' ? 'Active sessions' : t.charAt(0).toUpperCase() + t.slice(1)}</button>
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
