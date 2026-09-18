import React, { useEffect, useState } from 'react';
import {
    AuthCard,
    AuthFrame,
    PasswordFields,
    SelectField,
    SubmitButton,
    TextField,
    passwordMeetsPolicy,
    readError,
} from '../components/AuthFrame';

// First sign-in on a temporary password. The API refuses everything else until
// this is finished: verify a phone by SMS code, then choose a password and a
// security question. Finishing invalidates the temp-password session, so the
// admin is signed out and signs back in with the new password.
//
// After a platform-owner password reset of an admin whose phone is already
// verified (mode "change_password"), only the new-password step is shown.
//
// Uses fetch directly (not apiCall): apiCall bounces any "onboarding required"
// response back to this page, which is exactly where we already are.
const token = () => localStorage.getItem('access_token');

async function call(path, { method = 'GET', body } = {}) {
    const res = await fetch(`/api/v1/auth/onboarding${path}`, {
        method,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token()}` },
        body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) {
        localStorage.removeItem('access_token');
        window.location.href = '/admin/login';
        throw new Error('Session expired');
    }
    return res;
}

export default function Onboarding() {
    const [status, setStatus] = useState(null);
    const changeOnly = status?.mode === 'change_password';
    const [step, setStep] = useState('loading'); // loading | phone | code | password
    const [phone, setPhone] = useState('');
    const [sentTo, setSentTo] = useState('');
    const [code, setCode] = useState('');
    const [password, setPassword] = useState('');
    const [confirm, setConfirm] = useState('');
    const [question, setQuestion] = useState('');
    const [answer, setAnswer] = useState('');
    const [error, setError] = useState(null);
    const [notice, setNotice] = useState('');
    const [loading, setLoading] = useState(false);

    const fail = (text) => setError({ text, id: Date.now() });

    useEffect(() => {
        document.title = 'Set up your account — IpAdmin';
        if (!token()) {
            window.location.href = '/admin/login';
            return undefined;
        }
        call('/status')
            .then(async (res) => {
                if (!res.ok) throw new Error(await readError(res, 'Could not load your account.'));
                const data = await res.json();
                if (!data.mode) {
                    window.location.href = '/admin/';
                    return;
                }
                setStatus(data);
                setStep(data.mode === 'change_password' || data.phone_verified ? 'password' : 'phone');
            })
            .catch((e) => {
                setStep('phone');
                fail(e.message);
            });
        return () => { document.title = 'IpAdmin'; };
    }, []);

    const run = async (fn) => {
        if (loading) return;
        setLoading(true);
        setError(null);
        try {
            await fn();
        } catch (e) {
            if (e.message !== 'Session expired') fail(e.message || 'Network error — try again.');
        }
        setLoading(false);
    };

    const sendCode = (e) => {
        e.preventDefault();
        run(async () => {
            if (!phone.trim()) throw new Error('Enter your mobile number.');
            const res = await call('/phone', { method: 'POST', body: { phone } });
            if (!res.ok) throw new Error(await readError(res, 'Could not send the code.'));
            const data = await res.json();
            setSentTo(data.phone);
            setCode('');
            setNotice(`We sent a 6-digit code to ${data.phone}.`);
            setStep('code');
        });
    };

    const verifyCode = (e) => {
        e.preventDefault();
        run(async () => {
            if (!/^\d{6}$/.test(code.trim())) throw new Error('Enter the 6-digit code from the SMS.');
            const res = await call('/otp/verify', { method: 'POST', body: { code: code.trim() } });
            if (!res.ok) throw new Error(await readError(res, 'That code was not accepted.'));
            const data = await res.json();
            setNotice(`Phone ${data.phone} verified.`);
            setStep('password');
        });
    };

    const finish = (e) => {
        e.preventDefault();
        run(async () => {
            if (!passwordMeetsPolicy(password)) throw new Error('Your new password does not meet all the rules yet.');
            if (password !== confirm) throw new Error("The two passwords don't match.");
            if (!changeOnly) {
                if (!question) throw new Error('Choose a security question.');
                if (answer.trim().length < 2) throw new Error('Enter an answer to your security question.');
            }
            const res = await call('/password', {
                method: 'POST',
                body: changeOnly
                    ? { new_password: password, confirm_password: confirm }
                    : {
                        new_password: password,
                        confirm_password: confirm,
                        security_question: question,
                        security_answer: answer,
                    },
            });
            if (!res.ok) throw new Error(await readError(res, 'Could not save your new password.'));
            // The temp-password session is now invalid on the server: sign out
            // and send the admin to the normal sign-in page.
            localStorage.removeItem('access_token');
            window.location.href = `/admin/login?notice=${changeOnly ? 'password-changed' : 'setup-complete'}`;
        });
    };

    const questions = status?.security_questions || [];

    return (
        <AuthFrame
            eyebrow="Account setup"
            headline={<>One last step.<span className="auth-headline-extra"><br /><em>Secure your account.</em></span></>}
            lede={changeOnly
                ? 'Your password was reset. Choose a new one before you continue managing your network.'
                : 'Verify your mobile number and replace your temporary password before you start managing your network.'}
        >
            {step === 'loading' && <AuthCard title="Loading…" />}

            {step === 'phone' && (
                <AuthCard
                    title="Verify your phone"
                    subtitle="Step 1 of 3 · We'll text a 6-digit code to this number. It's also where password-reset codes will go."
                    error={error}
                >
                    <form onSubmit={sendCode} noValidate>
                        <TextField
                            id="onboarding-phone"
                            label="Mobile number"
                            icon="phone"
                            type="tel"
                            inputMode="tel"
                            autoComplete="tel"
                            placeholder={status?.phone_on_file ? `On file: ${status.phone_on_file}` : '0244123456'}
                            value={phone}
                            onChange={(e) => setPhone(e.target.value)}
                        />
                        <SubmitButton loading={loading} idle="Send code" busy="Sending…" />
                    </form>
                </AuthCard>
            )}

            {step === 'code' && (
                <AuthCard title="Enter the code" subtitle="Step 2 of 3" error={error} notice={notice}>
                    <form onSubmit={verifyCode} noValidate>
                        <TextField
                            id="onboarding-code"
                            label="6-digit code"
                            icon="message"
                            inputMode="numeric"
                            autoComplete="one-time-code"
                            maxLength={6}
                            value={code}
                            onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                        />
                        <SubmitButton loading={loading} idle="Verify" busy="Checking…" />
                    </form>
                    <p className="auth-fine-print" style={{ marginTop: 14 }}>
                        Didn't get it, or wrong number {sentTo}?{' '}
                        <button type="button" className="auth-label-aside" style={{ background: 'none', border: 0, cursor: 'pointer', textDecoration: 'underline' }}
                            onClick={() => { setError(null); setNotice(''); setStep('phone'); }}>
                            Change number or resend
                        </button>
                    </p>
                </AuthCard>
            )}

            {step === 'password' && (
                <AuthCard
                    title={changeOnly ? 'Choose a new password' : 'Choose your password'}
                    subtitle={changeOnly
                        ? 'Your password was reset by platform support. Replace the temporary password to continue.'
                        : 'Step 3 of 3 · Replace your temporary password and set a security question for account recovery.'}
                    error={error}
                    notice={notice}
                >
                    <form onSubmit={finish} noValidate>
                        <PasswordFields password={password} confirm={confirm} onPassword={setPassword} onConfirm={setConfirm} />
                        {!changeOnly && (<>
                        <SelectField
                            id="security-question"
                            label="Security question"
                            value={question}
                            onChange={setQuestion}
                            options={questions}
                            placeholder="Choose a question…"
                        />
                        <TextField
                            id="security-answer"
                            label="Answer"
                            icon="shield-check"
                            autoComplete="off"
                            value={answer}
                            onChange={(e) => setAnswer(e.target.value)}
                            aside={<span className="auth-label-aside">Not case-sensitive</span>}
                        />
                        </>)}
                        <SubmitButton loading={loading} idle={changeOnly ? 'Save new password' : 'Finish setup'} busy="Saving…" />
                    </form>
                </AuthCard>
            )}

        </AuthFrame>
    );
}
