import React from 'react';
import { usePermissions } from '../App';
import ProviderCredentials from '../components/ProviderCredentials';
import VoucherSmsTemplate from '../components/VoucherSmsTemplate';

// The gateway settings and the message that goes through them, on one page:
// the wording is only meaningful in the context of the account paying for it,
// and a separate sidebar entry for a single textarea would not earn its place.
//
// The two halves need different permissions: the gateway credentials are
// 'secrets' (superadmin), the voucher message is 'config' (admin and up).
export default function SMSCredentials() {
    const { can } = usePermissions();
    return (
        <>
            {can('secrets') && <ProviderCredentials
                category="sms"
                apiPrefix="/sms-credentials"
                title="SMS Provider Settings"
                blurb="Choose the SMS gateway your voucher codes are delivered through, and enter your own credentials."
                activeNoun="SMS provider"
            />}
            {can('config') && <VoucherSmsTemplate />}
        </>
    );
}
