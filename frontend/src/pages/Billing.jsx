import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiCall, usePermissions } from '../App';
import TransactionsTab from '../components/TransactionsTab';
import SmsUsageTab from '../components/SmsUsageTab';
import PageHeader from '../components/PageHeader';
import PinPrompt, { usePinGate } from '../components/PinPrompt';

const LINE_KIND = { subscription: 'Subscription', sms_usage: 'SMS usage', adjustment: 'Adjustment' };

const fmtDate = (s) => (s ? new Date(s).toLocaleDateString() : '—');

// How far through the trial the operator is, 0-100, from the trial as they
// actually have it (start and end), not an assumed length.
function trialProgress(status) {
    const length = status.trial_length_days;
    if (!length) return 0;
    const used = length - (status.trial_days_remaining ?? 0);
    return Math.min(100, Math.max(0, (used / length) * 100));
}

function LineItems({ invoice }) {
    const lines = invoice.line_items || [];
    if (!lines.length) return <p style={{ margin: 0, color: '#64748b' }}>No line items.</p>;
    return (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
                <tr style={{ color: '#64748b', textAlign: 'left' }}>
                    <th style={{ padding: '4px 8px', fontWeight: 500 }}>Item</th>
                    <th style={{ padding: '4px 8px', fontWeight: 500 }}>Description</th>
                    <th style={{ padding: '4px 8px', fontWeight: 500, textAlign: 'right' }}>Qty</th>
                    <th style={{ padding: '4px 8px', fontWeight: 500, textAlign: 'right' }}>Unit (GHS)</th>
                    <th style={{ padding: '4px 8px', fontWeight: 500, textAlign: 'right' }}>Amount (GHS)</th>
                </tr>
            </thead>
            <tbody>
                {lines.map((line, i) => (
                    <tr key={i}>
                        <td style={{ padding: '4px 8px' }}>{LINE_KIND[line.kind] || line.kind}</td>
                        <td style={{ padding: '4px 8px' }}>{line.description}</td>
                        <td style={{ padding: '4px 8px', textAlign: 'right' }}>{Number(line.quantity)}</td>
                        <td style={{ padding: '4px 8px', textAlign: 'right' }}>{Number(line.unit_price_ghs)}</td>
                        <td style={{ padding: '4px 8px', textAlign: 'right' }}>{Number(line.amount_ghs).toFixed(2)}</td>
                    </tr>
                ))}
                <tr style={{ fontWeight: 600 }}>
                    <td colSpan={4} style={{ padding: '4px 8px', textAlign: 'right', borderTop: '1px solid #e2e8f0' }}>Total</td>
                    <td style={{ padding: '4px 8px', textAlign: 'right', borderTop: '1px solid #e2e8f0' }}>{Number(invoice.amount_ghs).toFixed(2)}</td>
                </tr>
            </tbody>
        </table>
    );
}

const TABS = [
    ['invoices', 'Invoices'],
    ['transactions', 'Transactions'],
    ['sms-usage', 'SMS Usage'],
];

export default function Billing() {
    const [status, setStatus] = useState(null);
    const [invoices, setInvoices] = useState([]);
    const [checklist, setChecklist] = useState({});
    const [payLoading, setPayLoading] = useState(false);
    const [error, setError] = useState('');
    const [tab, setTab] = useState('invoices');
    const [open, setOpen] = useState({});
    const gate = usePinGate();
    // Paying an invoice is a superadmin action ('secrets'); admins see the bills.
    const { can } = usePermissions();

    useEffect(() => {
        apiCall('/billing/status').then(d => d && setStatus(d)).catch(() => {});
        apiCall('/billing/invoices').then(d => d && setInvoices(d)).catch(() => {});
        apiCall('/billing/onboarding-checklist').then(d => d && setChecklist(d)).catch(() => {});
    }, []);

    const handlePay = async (invoiceId) => {
        setPayLoading(true);
        setError('');
        try {
            // PIN-gated: on a 403 gate.run waits for the PIN, replays the call
            // and resolves with the replay's answer, so the redirect below
            // happens either way. undefined means the PIN prompt was cancelled.
            const res = await gate.run(() =>
                apiCall(`/billing/invoices/${invoiceId}/pay`, { method: 'POST' }));
            if (res === undefined) return;
            if (res?.redirect_url) {
                window.location.href = res.redirect_url;
            } else {
                setError('Could not initiate payment');
            }
        } catch (e) {
            setError(e.message || 'Could not initiate payment');
        } finally {
            setPayLoading(false);
        }
    };

    const badgeColor = { trial: '#6366f1', active: '#22c55e', past_due: '#ef4444', suspended: '#ef4444', cancelled: '#6b7280' };
    const allChecked = Object.values(checklist).length > 0 && Object.values(checklist).every(Boolean);

    return (
        <div>
            <PageHeader title="Billing" style={{ marginBottom: 24 }} />

            {status && (
                <div style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: 20, marginBottom: 24 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                        <span style={{ fontWeight: 600, fontSize: 18 }}>Billing Status</span>
                        <span style={{ background: badgeColor[status.billing_status] || '#6b7280', color: '#fff', padding: '2px 10px', borderRadius: 999, fontSize: 13 }}>
                            {status.billing_status}
                        </span>
                    </div>
                    {status.billing_status === 'trial' && status.trial_days_remaining !== null && (
                        <div style={{ marginBottom: 8 }}>
                            <div style={{ fontSize: 14, color: '#475569', marginBottom: 4 }}>
                                Trial ends in <strong>{status.trial_days_remaining} day{status.trial_days_remaining !== 1 ? 's' : ''}</strong>
                                {status.trial_ends_at && ` (${fmtDate(status.trial_ends_at)})`}
                                {status.trial_length_days && ` of ${status.trial_length_days}`}
                            </div>
                            <div style={{ background: '#e2e8f0', borderRadius: 4, height: 8, width: '100%' }}>
                                <div style={{ background: '#6366f1', borderRadius: 4, height: 8, width: `${trialProgress(status)}%` }} />
                            </div>
                            {status.first_invoice_date && (
                                <div style={{ fontSize: 13, color: '#64748b', marginTop: 6 }}>
                                    Your account stays fully active when the trial ends. Monthly billing starts on{' '}
                                    <strong>{new Date(`${status.first_invoice_date}T00:00:00`).toLocaleDateString()}</strong>, with your first invoice.
                                </div>
                            )}
                        </div>
                    )}
                    {status.has_outstanding_invoice && (
                        <div style={{ color: '#b91c1c', fontWeight: 500 }}>
                            Outstanding: GHS {status.outstanding_amount_ghs}
                        </div>
                    )}
                </div>
            )}

            {!allChecked && Object.keys(checklist).length > 0 && (
                <div style={{ background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 8, padding: 20, marginBottom: 24 }}>
                    <h3 style={{ marginTop: 0 }}>Getting Started</h3>
                    {[
                        ['town_added', 'Add your first town and site'],
                        ['router_added', 'Register your MikroTik router'],
                        ['payment_configured', 'Configure your Paystack credentials'],
                        ['portal_tested', 'Test your captive portal'],
                        ['voucher_generated', 'Generate your first batch of vouchers'],
                        ['first_sale_made', 'Make a test sale'],
                    ].map(([key, label]) => (
                        <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0' }}>
                            <span style={{ fontSize: 18 }}>{checklist[key] ? '✅' : '⬜'}</span>
                            <span style={{ color: checklist[key] ? '#6b7280' : '#1e293b', textDecoration: checklist[key] ? 'line-through' : 'none' }}>{label}</span>
                        </div>
                    ))}
                </div>
            )}

            {error && <div style={{ color: '#ef4444', marginBottom: 16 }}>{error}</div>}

            <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid #e2e8f0', marginBottom: 20 }}>
                {TABS.map(([key, label]) => (
                    <button
                        key={key}
                        onClick={() => setTab(key)}
                        style={{
                            background: 'none',
                            border: 'none',
                            borderBottom: tab === key ? '2px solid #2563eb' : '2px solid transparent',
                            color: tab === key ? '#1e293b' : '#64748b',
                            fontWeight: tab === key ? 600 : 400,
                            fontSize: 14,
                            padding: '8px 14px',
                            cursor: 'pointer',
                            marginBottom: -1,
                        }}
                    >
                        {label}
                    </button>
                ))}
            </div>

            {tab === 'transactions' && <TransactionsTab />}
            {tab === 'sms-usage' && <SmsUsageTab />}

            {tab === 'invoices' && (invoices.length === 0 ? (
                <p style={{ color: '#64748b' }}>No invoices yet.</p>
            ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
                    <thead>
                        <tr style={{ background: '#f1f5f9' }}>
                            {['Invoice #', 'Period', 'Amount (GHS)', 'Status', 'Due', 'Paid', 'Action'].map(h => (
                                <th key={h} style={{ padding: '8px 12px', textAlign: 'left', borderBottom: '1px solid #e2e8f0' }}>{h}</th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {invoices.map(inv => (
                            <React.Fragment key={inv.id}>
                            <tr>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                    <button type="button" onClick={() => setOpen(o => ({ ...o, [inv.id]: !o[inv.id] }))}
                                        aria-expanded={!!open[inv.id]} title="Show line items"
                                        style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: '#2563eb', fontSize: 14 }}>
                                        {open[inv.id] ? '▾' : '▸'} {inv.invoice_number}
                                    </button>
                                </td>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                    {inv.period_start ? new Date(inv.period_start).toLocaleDateString() : '—'} – {inv.period_end ? new Date(inv.period_end).toLocaleDateString() : '—'}
                                </td>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{inv.amount_ghs}</td>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                    <span style={{ background: { issued: '#fef9c3', paid: '#dcfce7', overdue: '#fee2e2', waived: '#f1f5f9', draft: '#f1f5f9' }[inv.status] || '#f1f5f9', padding: '2px 8px', borderRadius: 999, fontSize: 12 }}>
                                        {inv.status}
                                    </span>
                                </td>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{inv.due_at ? new Date(inv.due_at).toLocaleDateString() : '—'}</td>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                    {fmtDate(inv.paid_at)}
                                    {inv.receipt && <div style={{ fontSize: 12, color: '#64748b' }}>{inv.receipt.method}</div>}
                                </td>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9', whiteSpace: 'nowrap' }}>
                                    <Link to={`/billing/invoices/${inv.id}`} style={{ fontSize: 13, marginRight: 10 }}>
                                        {inv.status === 'paid' ? 'Receipt' : 'View / print'}
                                    </Link>
                                    {can('secrets') && ['issued', 'overdue'].includes(inv.status) && (
                                        <button onClick={() => handlePay(inv.id)} disabled={payLoading}
                                            style={{ background: '#2563eb', color: '#fff', border: 'none', borderRadius: 4, padding: '4px 12px', cursor: 'pointer', fontSize: 13 }}>
                                            {payLoading ? '…' : 'Pay Now'}
                                        </button>
                                    )}
                                </td>
                            </tr>
                            {open[inv.id] && (
                                <tr>
                                    <td colSpan={7} style={{ padding: '8px 12px 14px 28px', background: '#f8fafc', borderBottom: '1px solid #e2e8f0' }}>
                                        <LineItems invoice={inv} />
                                    </td>
                                </tr>
                            )}
                            </React.Fragment>
                        ))}
                    </tbody>
                </table>
            ))}
            {gate.reason && (
                <PinPrompt reason={gate.reason} onCancel={gate.cancel} onUnlocked={gate.unlocked} />
            )}
        </div>
    );
}
