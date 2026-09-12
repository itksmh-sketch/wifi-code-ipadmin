import React, { useState } from 'react';
import { usePaginated, Pager, th, td, tableStyle } from './Pagination';

// Colour is the only status concern left to the client — the human-readable
// label comes from the server (status_label), so it has one definition.
const STATUS_COLOR = {
    success: '#dcfce7',
    pending: '#fef9c3',
    failed: '#fee2e2',
    reversed: '#fee2e2',
    refunded: '#f1f5f9',
};

const filterInput = {
    padding: '6px 10px',
    fontSize: 13,
    border: '1px solid #e2e8f0',
    borderRadius: 6,
};

export default function TransactionsTab() {
    const [start, setStart] = useState('');
    const [end, setEnd] = useState('');

    const state = usePaginated('/billing/transactions', 'transactions', {
        // "To" covers the whole day, so picking one date for both ends returns
        // that day rather than nothing.
        start_date: start ? `${start}T00:00:00Z` : '',
        end_date: end ? `${end}T23:59:59Z` : '',
    });

    const filters = (
        <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: 16 }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, color: '#64748b' }}>
                From
                <input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={filterInput} />
            </label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, color: '#64748b' }}>
                To
                <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={filterInput} />
            </label>
            {(start || end) && (
                <button
                    onClick={() => { setStart(''); setEnd(''); }}
                    style={{ ...filterInput, background: '#fff', cursor: 'pointer' }}
                >
                    Clear
                </button>
            )}
        </div>
    );

    if (state.error) return <div>{filters}<p style={{ color: '#ef4444' }}>{state.error}</p></div>;
    if (state.loading && !state.items.length) return <div>{filters}<p style={{ color: '#64748b' }}>Loading…</p></div>;
    if (!state.items.length) {
        return (
            <div>
                {filters}
                <p style={{ color: '#64748b' }}>
                    {start || end ? 'No transactions in this date range.' : 'No transactions yet.'}
                </p>
            </div>
        );
    }

    return (
        <div>
            {filters}
            <table style={tableStyle}>
                <thead>
                    <tr style={{ background: '#f1f5f9' }}>
                        {['Date', 'Amount (GHS)', 'Method', 'Status', 'Voucher'].map((h) => (
                            <th key={h} style={th}>{h}</th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {state.items.map((tx) => (
                        <tr key={tx.id}>
                            <td style={td}>{tx.initiated_at ? new Date(tx.initiated_at).toLocaleString() : '—'}</td>
                            <td style={td}>{tx.amount_ghs.toFixed(2)}</td>
                            <td style={td}>{tx.payment_method}</td>
                            <td style={td}>
                                <span style={{ background: STATUS_COLOR[tx.status] || '#f1f5f9', padding: '2px 8px', borderRadius: 999, fontSize: 12 }}>
                                    {tx.status_label}
                                </span>
                            </td>
                            <td style={{ ...td, fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 13 }}>
                                {tx.voucher_code || '—'}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
            <Pager state={state} />
        </div>
    );
}
