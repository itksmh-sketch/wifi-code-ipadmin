import React from 'react';
import { usePaginated, Pager, th, td, tableStyle } from './Pagination';

export default function SmsUsageTab() {
    const state = usePaginated('/billing/sms-usage', 'records');

    if (state.loading && !state.items.length) return <p style={{ color: '#64748b' }}>Loading…</p>;
    if (state.error) return <p style={{ color: '#ef4444' }}>{state.error}</p>;
    if (!state.items.length) {
        return (
            <p style={{ color: '#64748b' }}>
                No SMS usage yet. Charges appear here only if you send voucher codes through the
                platform SMS gateway — using your own SMS provider costs you nothing here.
            </p>
        );
    }

    return (
        <div>
            <table style={tableStyle}>
                <thead>
                    <tr style={{ background: '#f1f5f9' }}>
                        {['Date', 'Segments', 'Rate (GHS)', 'Amount (GHS)', 'Invoice'].map((h) => (
                            <th key={h} style={th}>{h}</th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {state.items.map((rec) => (
                        <tr key={rec.id}>
                            <td style={td}>{rec.sent_at ? new Date(rec.sent_at).toLocaleString() : '—'}</td>
                            <td style={td}>{rec.segment_count}</td>
                            <td style={td}>{rec.rate_ghs_per_segment}</td>
                            <td style={td}>{rec.amount_ghs.toFixed(2)}</td>
                            <td style={td}>
                                {rec.invoice_number ? (
                                    <span>
                                        {rec.invoice_number}
                                        {rec.invoice_issued_at && (
                                            <span style={{ color: '#94a3b8', fontSize: 12 }}>
                                                {' '}({new Date(rec.invoice_issued_at).toLocaleDateString()})
                                            </span>
                                        )}
                                    </span>
                                ) : (
                                    <span style={{ background: '#fef9c3', padding: '2px 8px', borderRadius: 999, fontSize: 12 }}>
                                        Not yet invoiced
                                    </span>
                                )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
            <Pager state={state} />
        </div>
    );
}
