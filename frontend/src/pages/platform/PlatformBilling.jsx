import React, { useEffect, useState } from 'react';
import { platformApiCall } from '../../App';

export default function PlatformBilling() {
    const [summary, setSummary] = useState(null);
    const [operators, setOperators] = useState([]);
    const [loading, setLoading] = useState(false);

    const load = () => {
        platformApiCall('/platform/billing/summary').then(d => d && setSummary(d)).catch(() => {});
        platformApiCall('/platform/billing/operators').then(d => d && setOperators(d)).catch(() => {});
    };
    useEffect(() => { load(); }, []);

    const waive = async (invoiceNumber, operatorId) => {
        const op = operators.find(o => o.id === operatorId);
        if (!op?.outstanding_invoice_number) return;
        // We need the invoice_id — fetch operator detail or use a separate endpoint
        const confirmed = window.confirm(`Waive invoice ${invoiceNumber}?`);
        if (!confirmed) return;
        setLoading(true);
        // Find invoice id from operator list — we'd need a separate call; simplified here
        setLoading(false);
        alert('Use the API directly: PUT /api/v1/platform/invoices/{invoice_id}/waive');
    };

    const card = (label, value, color = '#2563eb') => (
        <div style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 8, padding: '16px 24px', flex: 1 }}>
            <div style={{ fontSize: 13, color: '#6b7280', marginBottom: 4 }}>{label}</div>
            <div style={{ fontSize: 28, fontWeight: 700, color }}>{value ?? '—'}</div>
        </div>
    );

    const statusColor = { trial: '#6366f1', active: '#22c55e', past_due: '#ef4444', cancelled: '#6b7280', suspended: '#ef4444' };

    return (
        <div>
            <h1>Platform Billing</h1>

            {summary && (
                <div style={{ display: 'flex', gap: 16, marginBottom: 32, flexWrap: 'wrap' }}>
                    {card('Active Operators', summary.total_active_operators)}
                    {card('On Trial', summary.operators_on_trial, '#6366f1')}
                    {card('Overdue', summary.operators_overdue, '#ef4444')}
                    {card('Monthly Recurring Revenue', `GHS ${summary.monthly_recurring_revenue_ghs?.toFixed(2)}`, '#22c55e')}
                    {card('Collected This Month', `GHS ${summary.revenue_collected_this_month_ghs?.toFixed(2)}`, '#16a34a')}
                </div>
            )}

            <h2>Operator Billing</h2>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
                <thead>
                    <tr style={{ background: '#f1f5f9' }}>
                        {['Operator', 'Billing Status', 'Monthly Fee', 'Last Paid', 'Next Due', 'Outstanding', 'Actions'].map(h => (
                            <th key={h} style={{ padding: '8px 12px', textAlign: 'left', borderBottom: '1px solid #e2e8f0' }}>{h}</th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {operators.map(op => (
                        <tr key={op.id}>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                <a href={`/platform/operators/${op.id}`} style={{ color: '#2563eb', textDecoration: 'none', fontWeight: 500 }}>{op.name}</a>
                                <div style={{ fontSize: 11, color: '#94a3b8' }}>{op.slug}</div>
                            </td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                <span style={{ background: statusColor[op.billing_status] || '#e2e8f0', color: '#fff', padding: '2px 8px', borderRadius: 999, fontSize: 12 }}>
                                    {op.billing_status}
                                </span>
                            </td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>GHS {op.monthly_fee_ghs}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{op.last_paid_at ? new Date(op.last_paid_at).toLocaleDateString() : '—'}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{op.next_due_at ? new Date(op.next_due_at).toLocaleDateString() : '—'}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9', color: op.outstanding_amount_ghs > 0 ? '#ef4444' : '#22c55e', fontWeight: op.outstanding_amount_ghs > 0 ? 600 : 400 }}>
                                {op.outstanding_amount_ghs > 0 ? `GHS ${op.outstanding_amount_ghs}` : 'Paid'}
                            </td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                <a href={`/platform/operators/${op.id}`} style={{ color: '#2563eb', fontSize: 12, marginRight: 8 }}>View</a>
                                {op.outstanding_invoice_number && (
                                    <button onClick={() => waive(op.outstanding_invoice_number, op.id)} disabled={loading}
                                        style={{ background: '#f1f5f9', border: '1px solid #e2e8f0', borderRadius: 4, padding: '2px 8px', cursor: 'pointer', fontSize: 12 }}>
                                        Waive
                                    </button>
                                )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}
