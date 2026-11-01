import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link, useParams } from 'react-router-dom';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';

// One invoice on A4, to print or save as PDF; once it is paid, the same page
// carries the receipt (how and when it was paid).
//
// As on VoucherPrint, the printed copy is a portal on <body> (.print-root):
// print CSS in index.css hides #root, so no dashboard chrome reaches the paper.
// On screen the same document renders in-page.

const LINE_KIND = { subscription: 'Subscription', sms_usage: 'SMS usage', adjustment: 'Adjustment' };
const STATUS_LABEL = { issued: 'Unpaid', overdue: 'Overdue', paid: 'Paid', waived: 'Waived', draft: 'Draft' };

const day = (s) => (s ? new Date(s).toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' }) : '—');
// A date-only value ("2026-10-30") is a calendar day, not a UTC midnight.
const calendarDay = (s) => (s ? day(`${s}T00:00:00`) : '—');
const ghs = (n) => `GHS ${Number(n).toLocaleString('en-GH', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

function InvoiceDocument({ invoice }) {
    const paid = invoice.status === 'paid' && invoice.receipt;
    return (
        <div className="invoice-doc">
            <div className="invoice-doc-head">
                <div>
                    <div className="invoice-doc-issuer">{invoice.issuer.name}</div>
                    {invoice.issuer.email && <div className="invoice-doc-muted">{invoice.issuer.email}</div>}
                </div>
                <div className="invoice-doc-title">
                    <div>{paid ? 'Invoice and receipt' : 'Invoice'}</div>
                    <div className="invoice-doc-number">{invoice.invoice_number}</div>
                    <div className={`invoice-doc-status invoice-doc-status-${invoice.status}`}>
                        {STATUS_LABEL[invoice.status] || invoice.status}
                    </div>
                </div>
            </div>

            <div className="invoice-doc-meta">
                <div>
                    <div className="invoice-doc-label">Billed to</div>
                    <div className="invoice-doc-strong">{invoice.bill_to.name}</div>
                    {invoice.bill_to.email && <div>{invoice.bill_to.email}</div>}
                    {invoice.bill_to.phone && <div>{invoice.bill_to.phone}</div>}
                </div>
                <dl className="invoice-doc-dates">
                    <dt>Issued</dt><dd>{day(invoice.issued_at)}</dd>
                    <dt>Period</dt><dd>{day(invoice.period_start)} – {day(invoice.period_end)}</dd>
                    <dt>Due</dt><dd>{day(invoice.due_at)}</dd>
                </dl>
            </div>

            <table className="invoice-doc-lines">
                <thead>
                    <tr>
                        <th>Item</th>
                        <th>Description</th>
                        <th className="num">Qty</th>
                        <th className="num">Unit price (GHS)</th>
                        <th className="num">Amount (GHS)</th>
                    </tr>
                </thead>
                <tbody>
                    {(invoice.line_items || []).map((line, i) => (
                        <tr key={i}>
                            <td>{LINE_KIND[line.kind] || line.kind}</td>
                            <td>{line.description}</td>
                            <td className="num">{Number(line.quantity)}</td>
                            <td className="num">{Number(line.unit_price_ghs)}</td>
                            <td className="num">{Number(line.amount_ghs).toFixed(2)}</td>
                        </tr>
                    ))}
                </tbody>
                <tfoot>
                    <tr>
                        <td colSpan={4} className="num">Total</td>
                        <td className="num">{ghs(invoice.amount_ghs)}</td>
                    </tr>
                </tfoot>
            </table>

            {paid ? (
                <div className="invoice-doc-receipt">
                    <div className="invoice-doc-label">Receipt — payment received</div>
                    <dl className="invoice-doc-dates">
                        <dt>Amount</dt><dd>{ghs(invoice.receipt.amount_ghs)}</dd>
                        <dt>Paid on</dt><dd>{calendarDay(invoice.receipt.paid_on)}</dd>
                        <dt>Method</dt><dd>{invoice.receipt.method}</dd>
                        {invoice.receipt.reference && <><dt>Reference</dt><dd>{invoice.receipt.reference}</dd></>}
                    </dl>
                    <p className="invoice-doc-muted">Thank you. This invoice is paid in full.</p>
                </div>
            ) : invoice.status === 'waived' ? (
                <p className="invoice-doc-muted">This invoice was waived: nothing is owed on it.</p>
            ) : (
                <p className="invoice-doc-muted">
                    Please pay by {day(invoice.due_at)} from the Billing page of your dashboard.
                </p>
            )}
        </div>
    );
}

export default function InvoicePrint() {
    const { invoiceId } = useParams();
    const [invoice, setInvoice] = useState(null);
    const [error, setError] = useState('');

    useEffect(() => {
        apiCall(`/billing/invoices/${invoiceId}`)
            .then((d) => d && setInvoice(d))
            .catch((e) => setError(e.message || 'Could not load the invoice.'));
    }, [invoiceId]);

    const doc = invoice && <InvoiceDocument invoice={invoice} />;
    return (
        <div>
            <div className="flex-between">
                <PageHeader title={invoice ? `Invoice ${invoice.invoice_number}` : 'Invoice'} />
                <div style={{ display: 'flex', gap: 8 }}>
                    <Link to="/billing" className="btn">Back to billing</Link>
                    <button type="button" className="btn btn-primary" onClick={() => window.print()} disabled={!invoice}>
                        Print / save as PDF
                    </button>
                </div>
            </div>
            {error && <div style={{ color: '#ef4444', marginBottom: 16 }}>{error}</div>}
            {!invoice && !error && <p style={{ color: '#64748b' }}>Loading…</p>}
            {doc && <div className="invoice-preview">{doc}</div>}
            {doc && createPortal(<div className="print-root">{doc}</div>, document.body)}
        </div>
    );
}
