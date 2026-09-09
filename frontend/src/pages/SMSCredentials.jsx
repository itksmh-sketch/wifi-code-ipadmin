import React from 'react';
import ProviderCredentials from '../components/ProviderCredentials';

// Thin wrapper over the shared, catalog-driven credentials page.
export default function SMSCredentials() {
    return (
        <ProviderCredentials
            category="sms"
            apiPrefix="/sms-credentials"
            title="SMS Provider Settings"
            blurb="Choose the SMS gateway your voucher codes are delivered through, and enter your own credentials."
            activeNoun="SMS provider"
        />
    );
}
