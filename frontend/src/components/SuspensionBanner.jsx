import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiCall } from '../App';

/**
 * Persistent banner shown on every admin page while the operator is suspended.
 *
 * Login deliberately stays open for a suspended operator (see
 * middleware/auth.py) so they can sign in, understand why writes are failing,
 * and reach the billing page to pay. That decision only works if something
 * actually tells them — without this banner the first sign of suspension is a
 * write 403 from a button that looks like it should work.
 *
 * Driven off `account_status`, NOT `billing_status`: an operator can be
 * billing_status='active' while status='suspended' (a manual suspension), and
 * billing_status='past_due' while still fully able to trade. Only `status`
 * decides whether the write guards bite.
 *
 * Fails silent. A billing-status fetch that errors must never take the
 * dashboard down with it.
 */
export default function SuspensionBanner() {
    const [status, setStatus] = useState(null);

    const load = useCallback(async () => {
        try {
            setStatus(await apiCall('/billing/status'));
        } catch {
            // Silent by design — see the note above.
        }
    }, []);

    useEffect(() => {
        load();
        // Refetch when the tab regains focus, so the banner clears on its own
        // after the operator pays in the Paystack tab rather than stranding a
        // stale "you are suspended" over a reactivated account.
        const onFocus = () => load();
        window.addEventListener('focus', onFocus);
        return () => window.removeEventListener('focus', onFocus);
    }, [load]);

    if (!status?.is_suspended) return null;

    // A manual suspension is not reversed by paying, so pointing that operator
    // at the invoice would be a dead end. reactivate_operator() only lifts
    // suspension_reason === 'billing'.
    const isBilling = status.suspension_reason === 'billing';

    return (
        <div
            role="alert"
            style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                flexWrap: 'wrap',
                background: '#fef3f2',
                border: '1px solid #fda29b',
                borderLeft: '4px solid #d92d20',
                borderRadius: 8,
                padding: '12px 16px',
                marginBottom: 20,
                color: '#7a271a',
            }}
        >
            <strong style={{ fontSize: 15 }}>
                Account suspended — pay your invoice to restore access
            </strong>
            <span style={{ fontSize: 14, flex: 1, minWidth: 260 }}>
                {isBilling ? (
                    <>
                        Customers cannot buy new vouchers and your resellers cannot buy stock.
                        Anyone already online stays connected, and vouchers already sold keep working.
                    </>
                ) : (
                    <>
                        This suspension was applied manually, so paying an invoice will not lift it.
                        Contact support to restore access.
                    </>
                )}
            </span>
            {isBilling && (
                <Link
                    to="/billing"
                    style={{
                        background: '#d92d20',
                        color: '#fff',
                        borderRadius: 6,
                        padding: '8px 16px',
                        fontWeight: 600,
                        fontSize: 14,
                        textDecoration: 'none',
                        whiteSpace: 'nowrap',
                    }}
                >
                    Pay now
                </Link>
            )}
        </div>
    );
}
