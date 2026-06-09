import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

export default function Billing() {
    const [status, setStatus] = useState(null);
    const [invoices, setInvoices] = useState([]);
    const [checklist, setChecklist] = useState({});
    const [payLoading, setPayLoading] = useState(false);
    const [error, setError] = useState('');

    useEffect(() => {
        apiCall('/billing/status').then(d => d && setStatus(d));
        apiCall('/billing/invoices').then(d => d && setInvoices(d));
        apiCall('/billing/onboarding-checklist').then(d => d && setChecklist(d));
    }, []);

    const handlePay = async (invoiceId) => {
        setPayLoading(true);
        setError('');
        const res = await apiCall(`/billing/invoices/${invoiceId}/pay`, { method: 'POST' });
        setPayLoading(false);
        if (res?.redirect_url) {
            window.location.href = res.redirect_url;
        } else {
            setError(res?.detail || 'Could not initiate payment');
        }
    };

    const badgeColor = { trial: '#6366f1', active: '#22c55e', past_due: '#ef4444', suspended: '#ef4444', cancelled: '#6b7280' };
    const allChecked = Object.values(checklist).length > 0 && Object.values(checklist).every(Boolean);

    return (
        <div>
            <h1 style={{ marginBottom: 24 }}>Billing</h1>

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
                                {status.trial_ends_at && ` (${new Date(status.trial_ends_at).toLocaleDateString()})`}
                            </div>
                            <div style={{ background: '#e2e8f0', borderRadius: 4, height: 8, width: '100%' }}>
                                <div style={{ background: '#6366f1', borderRadius: 4, height: 8, width: `${Math.max(0, 100 - (status.trial_days_remaining / 14) * 100)}%` }} />
                            </div>
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

            <h2>Invoices</h2>
            {invoices.length === 0 ? (
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
                            <tr key={inv.id}>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{inv.invoice_number}</td>
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
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{inv.paid_at ? new Date(inv.paid_at).toLocaleDateString() : '—'}</td>
                                <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                    {['issued', 'overdue'].includes(inv.status) && (
                                        <button onClick={() => handlePay(inv.id)} disabled={payLoading}
                                            style={{ background: '#2563eb', color: '#fff', border: 'none', borderRadius: 4, padding: '4px 12px', cursor: 'pointer', fontSize: 13 }}>
                                            {payLoading ? '…' : 'Pay Now'}
                                        </button>
                                    )}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </div>
    );
}
