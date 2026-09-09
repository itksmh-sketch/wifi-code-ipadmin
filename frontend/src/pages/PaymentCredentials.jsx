import React from 'react';
import ProviderCredentials from '../components/ProviderCredentials';

// Thin wrapper over the shared, catalog-driven credentials page.
export default function PaymentCredentials() {
    return (
        <ProviderCredentials
            category="payment"
            apiPrefix="/payment-credentials"
            title="Payment Provider Settings"
            blurb="Choose the payment provider your customers pay through, and enter your own credentials."
            activeNoun="payment provider"
        />
    );
}
