import React from 'react';
import ProviderCredentials from '../components/ProviderCredentials';
import VoucherSmsTemplate from '../components/VoucherSmsTemplate';

// The gateway settings and the message that goes through them, on one page:
// the wording is only meaningful in the context of the account paying for it,
// and a separate sidebar entry for a single textarea would not earn its place.
export default function SMSCredentials() {
    return (
        <>
            <ProviderCredentials
                category="sms"
                apiPrefix="/sms-credentials"
                title="SMS Provider Settings"
                blurb="Choose the SMS gateway your voucher codes are delivered through, and enter your own credentials."
                activeNoun="SMS provider"
            />
            <VoucherSmsTemplate />
        </>
    );
}
