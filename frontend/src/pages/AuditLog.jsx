import React, { useEffect, useRef, useState } from 'react';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';

// The operator's own audit trail: superadmin only (the "audit" permission;
// GET /admin/audit enforces it and the tenant). People's actions by default;
// the toggle adds what the system did on its own. Platform actions show as
// "Platform": the server never sends the owner's email, IP or typed reason.

const PAGE_SIZES = [25, 50, 100, 200];
const FIRST_PAGE = { index: 0, cursors: [null] };
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

// Keyset paging: the server hands back a cursor for the next (older) page
// only, so the cursors of the pages already visited are kept here, which is
// what lets "Newer" go back. cursors[i] fetches page i; cursors[0] is null.
// The total arrives with the first page alone and is capped server-side.
function Pager({ page, rows, total, pageSize, loading, onPageSize, onGo }) {
    const hasOlder = page.cursors.length > page.index + 1;
    const start = page.index * pageSize + 1;
    const pages = total && !total.capped ? Math.max(1, Math.ceil(total.count / pageSize)) : null;
    return (
        <nav className="audit-pager" aria-label="Audit log pages">
            <label className="audit-page-size">Rows per page
                <select value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))}>
                    {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
            </label>
            <span className="audit-range">
                {rows.length > 0 && (
                    <>
                        {start}–{start + rows.length - 1}
                        {total && ` of ${total.count.toLocaleString()}${total.capped ? '+' : ''}`}
                        {' · '}Page {page.index + 1}{pages && ` of ${pages}`}
                    </>
                )}
                {loading && <span> Loading…</span>}
            </span>
            <span className="audit-page-buttons">
                <button type="button" className="btn btn-sm" disabled={loading || page.index === 0} onClick={() => onGo(0)}>Newest</button>
                <button type="button" className="btn btn-sm" disabled={loading || page.index === 0} onClick={() => onGo(page.index - 1)}>← Newer</button>
                <button type="button" className="btn btn-sm" disabled={loading || !hasOlder} onClick={() => onGo(page.index + 1)}>Older →</button>
            </span>
        </nav>
    );
}

export default function AuditLog() {
    const [filters, setFilters] = useState(EMPTY_FILTERS);
    const [users, setUsers] = useState([]);
    const [rows, setRows] = useState([]);
    const [page, setPage] = useState(FIRST_PAGE);
    const [pageSize, setPageSize] = useState(50);
    const [total, setTotal] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    // Only the newest request may update the table (see Vouchers.jsx).
    const latest = useRef(0);
    const top = useRef(null);

    useEffect(() => {
        apiCall('/admin/users').then((list) => setUsers(Array.isArray(list) ? list : [])).catch(() => setUsers([]));
    }, []);

    const load = async (current, size, index, cursors) => {
        const mine = ++latest.current;
        const after = cursors[index];
        setLoading(true);
        setError('');
        const params = new URLSearchParams({ limit: String(size) });
        if (current.user) params.set('user', current.user);
        if (current.action.trim()) params.set('action', current.action.trim());
        if (current.from) params.set('date_from', current.from);
        if (current.to) params.set('date_to', current.to);
        if (current.system) params.set('include_system', 'true');
        if (after) params.set('cursor', after);
        try {
            const data = await apiCall(`/admin/audit?${params}`);
            if (mine !== latest.current) return false;
            setRows(data?.rows || []);
            if (index === 0) setTotal({ count: data?.total ?? 0, capped: Boolean(data?.total_capped) });
            const next = data?.next_cursor || null;
            setPage({ index, cursors: [...cursors.slice(0, index + 1), ...(next ? [next] : [])] });
            return true;
        } catch (err) {
            if (mine !== latest.current) return;
            setError(err instanceof Error ? err.message : 'Could not load the audit log.');
        } finally {
            if (mine === latest.current) setLoading(false);
        }
    };

    // A short pause after typing in the action box; every other filter reloads
    // at once. A filter or page-size change starts again from the newest page.
    useEffect(() => {
        const timer = setTimeout(() => load(filters, pageSize, 0, FIRST_PAGE.cursors), 300);
        return () => clearTimeout(timer);
    }, [filters, pageSize]);

    const go = async (index) => {
        const cursors = index === 0 ? FIRST_PAGE.cursors : page.cursors;
        if (await load(filters, pageSize, index, cursors)) top.current?.scrollIntoView({ block: 'start' });
    };

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

            <div className="card" ref={top}>
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
                <Pager page={page} rows={rows} total={total} pageSize={pageSize} loading={loading}
                    onPageSize={setPageSize} onGo={go} />
            </div>
        </div>
    );
}
