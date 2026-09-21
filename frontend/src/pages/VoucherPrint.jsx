import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { apiCall } from '../App';
import PageHeader from '../components/PageHeader';

// Print cards for a plan's unsold, unprinted voucher stock on A4.
//
// Two steps, matching the API. Loading a batch marks nothing; the operator
// prints, then says whether it came out, and only "Confirm printed" stamps
// printed_at — on exactly the vouchers shown. Controls lock between Print and
// that answer so the batch being confirmed is always the one that was printed.
//
// The printed output is a portal on <body> (.print-root): print CSS in
// index.css hides #root entirely and prints only that, so none of the
// dashboard chrome can leak onto the paper. On screen the same sheets render
// in-page as a preview.

// cols x rows on the usable A4 area (index.css: @page margin 8mm).
// `scale` sizes the card text; `code` sizes the voucher code separately (x10pt),
// chosen so a default 16-character code (19 with dashes, ~13.4em in monospace
// with its padding) stays on ONE line at each layout's card width: ~89mm at 2
// columns, ~58mm at 3, ~45mm at 4. Longer codes may wrap at a dash, never clip.
const LAYOUTS = {
    8: { cols: 2, rows: 4, scale: 1.25, code: 1.6 },
    12: { cols: 3, rows: 4, scale: 1.05, code: 1.15 },
    15: { cols: 3, rows: 5, scale: 1, code: 1.15 },
    21: { cols: 3, rows: 7, scale: 0.85, code: 1.1 },
    24: { cols: 3, rows: 8, scale: 0.8, code: 1.05 },
    40: { cols: 4, rows: 10, scale: 0.62, code: 0.85 },
};
const DEFAULT_PER_PAGE = 12;

function validity(plan) {
    const parts = [];
    const m = plan.duration_minutes;
    if (m) {
        if (m % 1440 === 0) parts.push(`${m / 1440} day${m === 1440 ? '' : 's'}`);
        else if (m % 60 === 0) parts.push(`${m / 60} hour${m === 60 ? '' : 's'}`);
        else parts.push(`${m} min`);
    }
    const mb = plan.data_limit_mb;
    if (mb) parts.push(mb >= 1024 && mb % 1024 === 0 ? `${mb / 1024} GB` : `${mb} MB`);
    return parts.join(' · ');
}

function Card({ voucher, plan, business }) {
    return (
        <div className="voucher-card">
            {business && <div className="voucher-card-business">{business}</div>}
            <div className="voucher-card-plan">{plan.name}</div>
            <div className="voucher-card-meta">
                {validity(plan) && <span>{validity(plan)}</span>}
                <span>GHS {Number(plan.price_ghs).toFixed(2)}</span>
            </div>
            <div className="voucher-card-code">{voucher.code}</div>
            <div className="voucher-card-help">Connect to the Wi-Fi and enter this code</div>
        </div>
    );
}

function Sheets({ vouchers, plan, business, perPage }) {
    const { cols, rows, scale, code } = LAYOUTS[perPage];
    const pages = [];
    for (let i = 0; i < vouchers.length; i += perPage) pages.push(vouchers.slice(i, i + perPage));
    return pages.map((page, i) => (
        <div
            key={i}
            className="voucher-sheet"
            style={{
                gridTemplateColumns: `repeat(${cols}, 1fr)`,
                gridTemplateRows: `repeat(${rows}, 1fr)`,
                '--card-scale': scale,
                '--code-scale': code,
            }}
        >
            {page.map((v) => <Card key={v.id} voucher={v} plan={plan} business={business} />)}
        </div>
    ));
}

export default function VoucherPrint() {
    const [plans, setPlans] = useState([]);
    const [business, setBusiness] = useState('');
    const [planId, setPlanId] = useState('');
    const [quantity, setQuantity] = useState(DEFAULT_PER_PAGE);
    const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);
    const [batch, setBatch] = useState(null); // { plan, vouchers, available }
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    // 'idle' -> 'awaiting' (print dialog was opened) -> result shown, back to idle
    const [stage, setStage] = useState('idle');
    const [result, setResult] = useState(null);
    const [confirming, setConfirming] = useState(false);
    const latestRequest = useRef(0);

    useEffect(() => {
        apiCall('/plans').then((p) => setPlans(p || [])).catch((e) => setError(e.message));
        apiCall('/admin/branding').then((b) => setBusiness(b?.portal_display_name || '')).catch(() => {});
    }, []);

    const loadBatch = (pid = planId, qty = quantity) => {
        if (!pid) { setBatch(null); return; }
        const n = parseInt(qty, 10);
        if (!n || n < 1) { setBatch(null); return; }
        const requestId = ++latestRequest.current;
        setLoading(true);
        setError('');
        apiCall(`/vouchers/print-batch?plan_id=${pid}&limit=${n}`)
            .then((data) => { if (requestId === latestRequest.current) setBatch(data); })
            .catch((e) => { if (requestId === latestRequest.current) { setBatch(null); setError(e.message); } })
            .finally(() => { if (requestId === latestRequest.current) setLoading(false); });
    };

    // Plan or quantity changed -> fetch the batch that would print (debounced,
    // so typing "120" doesn't fire three requests). Nothing is marked here.
    useEffect(() => {
        if (stage !== 'idle') return undefined;
        const t = setTimeout(() => loadBatch(planId, quantity), 300);
        return () => clearTimeout(t);
    }, [planId, quantity]); // eslint-disable-line react-hooks/exhaustive-deps

    const print = () => {
        setResult(null);
        setStage('awaiting');
        // Let React commit the locked state before the (blocking) dialog opens.
        setTimeout(() => window.print(), 50);
    };

    const discard = () => { setStage('idle'); setResult(null); };

    const confirmPrinted = async () => {
        if (!batch) return;
        setConfirming(true);
        setError('');
        try {
            const res = await apiCall('/vouchers/print-batch/confirm', {
                method: 'POST',
                body: JSON.stringify({ voucher_ids: batch.vouchers.map((v) => v.id) }),
            });
            setResult(res);
            setStage('idle');
            loadBatch(); // the confirmed vouchers drop out; show what's next
        } catch (e) {
            setError(e.message);
        }
        setConfirming(false);
    };

    const vouchers = batch?.vouchers || [];
    const locked = stage === 'awaiting';
    const sheets = useMemo(
        () => (batch && vouchers.length ? <Sheets vouchers={vouchers} plan={batch.plan} business={business} perPage={perPage} /> : null),
        [batch, business, perPage], // eslint-disable-line react-hooks/exhaustive-deps
    );
    const pageCount = Math.ceil(vouchers.length / perPage);
    const input = { padding: '6px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 14 };

    return (
        <div>
            <div className="flex-between">
                <PageHeader title="Print vouchers" />
                <Link to="/vouchers" className="btn">Back to vouchers</Link>
            </div>

            <div className="card" style={{ marginBottom: 16 }}>
                <p style={{ marginTop: 0, color: '#6b7280', fontSize: 14 }}>
                    Only your own unused stock that has never been printed is shown — vouchers bought online
                    or allocated to resellers are never included.
                </p>
                <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-end' }}>
                    <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                        Plan
                        <select value={planId} disabled={locked} onChange={(e) => { setPlanId(e.target.value); setResult(null); }} style={input}>
                            <option value="">Choose a plan…</option>
                            {plans.map((p) => <option key={p.id} value={p.id}>{p.name}{p.is_active ? '' : ' (inactive)'}</option>)}
                        </select>
                    </label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                        Quantity
                        <input type="number" min="1" value={quantity} disabled={locked}
                            onChange={(e) => { setQuantity(e.target.value); setResult(null); }} style={{ ...input, width: 100 }} />
                    </label>
                    <label style={{ display: 'grid', gap: 4, fontSize: 13 }}>
                        Per A4 page
                        <select value={perPage} disabled={locked} onChange={(e) => setPerPage(Number(e.target.value))} style={input}>
                            {Object.keys(LAYOUTS).map((n) => <option key={n} value={n}>{n} per page</option>)}
                        </select>
                    </label>
                    {!locked && (
                        <button type="button" className="btn btn-primary" onClick={print} disabled={!vouchers.length || loading}>
                            Print {vouchers.length || ''} voucher{vouchers.length === 1 ? '' : 's'}
                        </button>
                    )}
                </div>

                {batch && (
                    <p style={{ margin: '12px 0 0', fontSize: 13.5, color: '#374151' }}>
                        {batch.available === 0
                            ? 'No unprinted stock left for this plan. Generate a batch on the Vouchers page first.'
                            : `${vouchers.length} of ${batch.available} unprinted voucher${batch.available === 1 ? '' : 's'} · ${pageCount} A4 page${pageCount === 1 ? '' : 's'}`}
                        {batch.available > 0 && parseInt(quantity, 10) > batch.available && ' (quantity capped to what is available)'}
                    </p>
                )}
                {error && <p style={{ margin: '12px 0 0', color: '#b91c1c', fontSize: 14 }}>{error}</p>}
            </div>

            {locked && (
                <div className="card" style={{ marginBottom: 16, borderColor: '#bfdbfe', background: '#eff6ff' }}>
                    <h3 style={{ marginTop: 0 }}>Did all {vouchers.length} vouchers print correctly?</h3>
                    <p style={{ fontSize: 14, color: '#374151' }}>
                        Confirming marks exactly these vouchers as printed, so they won't be offered again.
                        If the print failed or you cancelled it, choose "Not printed" — nothing is marked.
                    </p>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <button type="button" className="btn btn-primary" onClick={confirmPrinted} disabled={confirming}>
                            {confirming ? 'Confirming…' : 'Yes — confirm printed'}
                        </button>
                        <button type="button" className="btn" onClick={() => window.print()} disabled={confirming}>Print again</button>
                        <button type="button" className="btn" onClick={discard} disabled={confirming}>Not printed</button>
                    </div>
                </div>
            )}

            {result && (
                <div className="card" style={{ marginBottom: 16 }}>
                    <p style={{ margin: 0, color: '#166534', fontWeight: 600 }}>
                        {result.confirmed} voucher{result.confirmed === 1 ? '' : 's'} marked as printed.
                    </p>
                    {result.already_printed.length > 0 && (
                        <p style={{ margin: '8px 0 0', color: '#92400e' }}>
                            {result.already_printed.length} had already been marked printed — possibly by another print run of
                            the same stock. Those cards may exist twice on paper; check before selling them.
                        </p>
                    )}
                    {result.not_printable.length > 0 && (
                        <p style={{ margin: '8px 0 0', color: '#92400e' }}>
                            {result.not_printable.length} were no longer printable stock (used or changed since the batch
                            was loaded) and were not marked.
                        </p>
                    )}
                </div>
            )}

            {sheets && <div className="voucher-preview">{sheets}</div>}
            {sheets && createPortal(<div className="print-root">{sheets}</div>, document.body)}
        </div>
    );
}
