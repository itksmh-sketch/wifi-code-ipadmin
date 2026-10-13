import React, { useEffect, useRef, useState } from 'react';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';

// The operator's own audit trail: superadmin only (the "audit" permission;
// GET /admin/audit enforces it and the tenant). People's actions by default;
// the toggle adds what the system did on its own. Platform actions show as
// "Platform": the server never sends the owner's email, IP or typed reason.

const PAGE_SIZE = 50;
const ACTOR_NAMES = { operator_user: 'User', platform_owner: 'Platform', system: 'System' };
const PREFIXES = [
    'account', 'admin_user', 'auth', 'branding', 'commission_rule', 'config_template', 'invoice', 'operator',
    'payment', 'payment_credentials', 'plan', 'reseller', 'router', 'security', 'site', 'sms_credentials',
    'sms_template', 'sms_usage', 'town', 'voucher',
];
const EMPTY_FILTERS = { user: '', action: '', from: '', to: '', system: false };

// One readable line per value. "[redacted]" and "changed" read as they are.
function show(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'string') return value;
    return JSON.stringify(value);
}

function isBeforeAfter(value) {
    return value && typeof value === 'object' && !Array.isArray(value) && ('before' in value || 'after' in value);
}

function Changes({ changes }) {
    return (
        <dl className="audit-kv">
            {Object.keys(changes).sort().map((field) => (
                <React.Fragment key={field}>
                    <dt>{field}</dt>
                    <dd>
                        {isBeforeAfter(changes[field]) ? (
                            <>
                                <span className="audit-before">{show(changes[field].before)}</span>
                                <span className="audit-arrow">→</span>
                                <span>{show(changes[field].after)}</span>
                            </>
                        ) : show(changes[field])}
                    </dd>
                </React.Fragment>
            ))}
        </dl>
    );
}

function Details({ row }) {
    const items = Object.keys(row.detail || {}).sort().map((key) => [key, row.detail[key]]);
    if (row.target_type) items.push(['target', `${row.target_type}${row.target_id ? ` ${row.target_id}` : ''}`]);
    if (row.ip) items.push(['ip', row.ip]);
    if (row.user_agent) items.push(['user agent', row.user_agent]);
    return (
        <dl className="audit-kv">
            {items.map(([key, value]) => (
                <React.Fragment key={key}>
                    <dt>{key}</dt>
                    <dd>{show(value)}</dd>
                </React.Fragment>
            ))}
        </dl>
    );
}

function Row({ row }) {
    const hasChanges = row.changes && Object.keys(row.changes).length > 0;
    return (
        <tr>
            <td className="audit-when">{new Date(row.created_at).toLocaleString()}</td>
            <td>
                <span className={`badge ${row.actor.type === 'platform_owner' ? 'badge-blue' : row.actor.type === 'system' ? 'badge-gray' : 'badge-green'}`}>
                    {ACTOR_NAMES[row.actor.type] || row.actor.type}
                </span>
                {row.actor.type === 'operator_user' && row.actor.label && (
                    <span className="audit-actor-label">{row.actor.label}</span>
                )}
            </td>
            <td className="audit-action">{row.action}</td>
            <td>
                <p className="audit-summary">{row.summary || row.action}</p>
                <details className="audit-more">
                    <summary>{hasChanges ? 'Changes and details' : 'Details'}</summary>
                    {hasChanges && (<><p className="audit-subhead">Changes</p><Changes changes={row.changes} /></>)}
                    <p className="audit-subhead">Details</p>
                    <Details row={row} />
                </details>
            </td>
        </tr>
    );
}

export default function AuditLog() {
    const [filters, setFilters] = useState(EMPTY_FILTERS);
    const [users, setUsers] = useState([]);
    const [rows, setRows] = useState([]);
    const [cursor, setCursor] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    // Only the newest request may update the table (see Vouchers.jsx).
    const latest = useRef(0);

    useEffect(() => {
        apiCall('/admin/users').then((list) => setUsers(Array.isArray(list) ? list : [])).catch(() => setUsers([]));
    }, []);

    const load = async (current, after) => {
        const mine = ++latest.current;
        setLoading(true);
        setError('');
        const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
        if (current.user) params.set('user', current.user);
        if (current.action.trim()) params.set('action', current.action.trim());
        if (current.from) params.set('date_from', current.from);
        if (current.to) params.set('date_to', current.to);
        if (current.system) params.set('include_system', 'true');
        if (after) params.set('cursor', after);
        try {
            const data = await apiCall(`/admin/audit?${params}`);
            if (mine !== latest.current) return;
            setRows((prev) => (after ? [...prev, ...(data?.rows || [])] : data?.rows || []));
            setCursor(data?.next_cursor || null);
        } catch (err) {
            if (mine !== latest.current) return;
            setError(err instanceof Error ? err.message : 'Could not load the audit log.');
        } finally {
            if (mine === latest.current) setLoading(false);
        }
    };

    // A short pause after typing in the action box; every other filter reloads at once.
    useEffect(() => {
        const timer = setTimeout(() => load(filters, null), 300);
        return () => clearTimeout(timer);
    }, [filters]);

    const set = (key) => (e) => {
        const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
        setFilters((f) => {
            const next = { ...f, [key]: value };
            // "System" as the user only makes sense with system rows on.
            if (key === 'system' && !value && f.user === 'system') next.user = '';
            return next;
        });
    };

    return (
        <div>
            <PageHeader title="Audit log" />
            <p className="audit-intro">
                Who changed what in your account, newest first. Actions taken by the platform team show as
                "Platform". Turn on system rows to add what happened automatically: payments completing,
                vouchers expiring, invoices being issued.
            </p>

            <div className="card audit-filters">
                <label>User
                    <select value={filters.user} onChange={set('user')}>
                        <option value="">Everyone</option>
                        {users.map((u) => <option key={u.id} value={u.id}>{u.email}</option>)}
                        <option value="platform">Platform</option>
                        {filters.system && <option value="system">System</option>}
                    </select>
                </label>
                <label>Action starts with
                    <input type="text" list="audit-prefixes" maxLength={64} placeholder="e.g. voucher or plan.update"
                        value={filters.action} onChange={set('action')} />
                    <datalist id="audit-prefixes">
                        {PREFIXES.map((p) => <option key={p} value={p} />)}
                    </datalist>
                </label>
                <label>From
                    <input type="date" value={filters.from} onChange={set('from')} />
                </label>
                <label>To
                    <input type="date" value={filters.to} onChange={set('to')} />
                </label>
                <label className="audit-toggle">
                    <input type="checkbox" checked={filters.system} onChange={set('system')} />
                    Include system rows
                </label>
                <button type="button" className="btn btn-sm" onClick={() => setFilters(EMPTY_FILTERS)}>Clear</button>
            </div>

            {error && <div className="audit-error" role="alert">{error}</div>}

            <div className="card">
                <div className="table-wrap">
                    <table>
                        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>What happened</th></tr></thead>
                        <tbody>
                            {rows.map((row) => <Row key={row.id} row={row} />)}
                            {!loading && rows.length === 0 && (
                                <tr><td colSpan="4" className="audit-empty">No audit rows match these filters.</td></tr>
                            )}
                        </tbody>
                    </table>
                </div>
                <div className="audit-pager">
                    <span>{rows.length ? `Showing ${rows.length}${cursor ? ' (more available)' : ''}` : ''}</span>
                    {loading && <span>Loading…</span>}
                    {cursor && !loading && (
                        <button type="button" className="btn btn-sm" onClick={() => load(filters, cursor)}>Load older rows</button>
                    )}
                </div>
            </div>
        </div>
    );
}
