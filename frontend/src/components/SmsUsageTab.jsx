import React from 'react';
import { usePaginated, Pager, th, td, tableStyle } from './Pagination';

// Delivery status per message (backend: /billing/sms-usage `delivery`, from
// sms_messages). Read back from Arkesel; null = sent before tracking began.
const DELIVERY_BADGES = {
    delivered: { label: 'Delivered', bg: '#dcfce7', fg: '#166534' },
    not_delivered: { label: 'Not delivered', bg: '#fee2e2', fg: '#991b1b' },
    sent_to_network: { label: 'Sent to network', bg: '#e0f2fe', fg: '#075985' },
    waiting: { label: 'Waiting at gateway', bg: '#fef9c3', fg: '#854d0e' },
};

function fmtDelay(seconds) {
    if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
}

function DeliveryCell({ delivery }) {
    if (!delivery) {
        return <span style={{ color: '#94a3b8' }} title="Sent before delivery tracking started">—</span>;
    }
    const badge = DELIVERY_BADGES[delivery.state] || DELIVERY_BADGES.waiting;
    const delay = delivery.network_delay_seconds;
    return (
        <span>
            <span
                title={delivery.provider_status || ''}
                style={{ background: badge.bg, color: badge.fg, padding: '2px 8px', borderRadius: 999, fontSize: 12 }}
            >
                {badge.label}
            </span>
            {delay != null && delay >= 60 && (
                <div style={{ color: delay >= 300 ? '#b45309' : '#94a3b8', fontSize: 12, marginTop: 4 }}>
                    reached the network after {fmtDelay(delay)}
                </div>
            )}
        </span>
    );
}

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
                        {['Date', 'Delivery', 'Segments', 'Rate (GHS)', 'Amount (GHS)', 'Invoice'].map((h) => (
                            <th key={h} style={th}>{h}</th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {state.items.map((rec) => (
                        <tr key={rec.id}>
                            <td style={td}>{rec.sent_at ? new Date(rec.sent_at).toLocaleString() : '—'}</td>
                            <td style={td}><DeliveryCell delivery={rec.delivery} /></td>
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
