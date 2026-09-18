import React, { useEffect, useState } from 'react';
import { AuthCard, AuthFrame, PasswordFields, SubmitButton, TextField, passwordMeetsPolicy, readError } from '../components/AuthFrame';

// Signed-out password reset for operator admins. Two ways to prove identity:
// a 6-digit SMS code to the verified phone on file, or the security question
// chosen during account setup. Either yields a short-lived reset grant that is
// exchanged once for a new password; that also signs the admin out everywhere.
//
// The server never says whether an email has an account, so this page doesn't
// either: every step reads the same for unknown emails.
async function post(path, body) {
    const res = await fetch(`/api/v1/auth/reset${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await readError(res, 'Something went wrong. Please try again.'));
    return res.json();
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function ForgotPassword() {
    const [step, setStep] = useState('email'); // email | code | question | password
    const [email, setEmail] = useState('');
    const [code, setCode] = useState('');
    const [question, setQuestion] = useState('');
    const [answer, setAnswer] = useState('');
    const [grant, setGrant] = useState('');
    const [password, setPassword] = useState('');
    const [confirm, setConfirm] = useState('');
    const [error, setError] = useState(null);
    const [notice, setNotice] = useState('');
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        document.title = 'Reset password — IpAdmin';
        return () => { document.title = 'IpAdmin'; };
    }, []);

    const run = async (fn) => {
        if (loading) return;
        setLoading(true);
        setError(null);
        try {
            await fn();
        } catch (e) {
            setError({ text: e.message || 'Network error — try again.', id: Date.now() });
        }
        setLoading(false);
    };

    const requireEmail = () => {
        if (!EMAIL_RE.test(email.trim())) throw new Error('Enter the email address for your account.');
    };

    const sendCode = (e) => {
        e?.preventDefault();
        run(async () => {
            requireEmail();
            const data = await post('/request', { email: email.trim() });
            setCode('');
            setNotice(data.message);
            setStep('code');
        });
    };

    const useQuestion = () => {
        run(async () => {
            requireEmail();
            const data = await post('/security-question', { email: email.trim() });
            setQuestion(data.question);
            setAnswer('');
            setNotice('');
            setStep('question');
        });
    };

    const verifyCode = (e) => {
        e.preventDefault();
        run(async () => {
            if (!/^\d{6}$/.test(code.trim())) throw new Error('Enter the 6-digit code from the SMS.');
            const data = await post('/verify-otp', { email: email.trim(), code: code.trim() });
            setGrant(data.reset_token);
            setNotice('Code accepted. Choose your new password.');
            setStep('password');
        });
    };

    const verifyAnswer = (e) => {
        e.preventDefault();
        run(async () => {
            if (answer.trim().length < 2) throw new Error('Enter your answer.');
            const data = await post('/security-question', { email: email.trim(), answer });
            setGrant(data.reset_token);
            setNotice('Answer accepted. Choose your new password.');
            setStep('password');
        });
    };

    const setNewPassword = (e) => {
        e.preventDefault();
        run(async () => {
            if (!passwordMeetsPolicy(password)) throw new Error('Your new password does not meet all the rules yet.');
            if (password !== confirm) throw new Error("The two passwords don't match.");
            await post('/set-password', { reset_token: grant, new_password: password, confirm_password: confirm });
            localStorage.removeItem('access_token');
            window.location.href = '/admin/login?notice=password-reset';
        });
    };

    const backToEmail = () => { setError(null); setNotice(''); setStep('email'); };
    const linkButton = { background: 'none', border: 0, cursor: 'pointer', textDecoration: 'underline', padding: 0 };

    return (
        <AuthFrame
            eyebrow="Account recovery"
            headline={<>Locked out?<span className="auth-headline-extra"><br /><em>Let's get you back in.</em></span></>}
            lede="Confirm it's you with a code sent to your verified phone, or with your security question."
        >
            {step === 'email' && (
                <AuthCard title="Reset your password" subtitle="Enter the email you use to sign in." error={error}>
                    <form onSubmit={sendCode} noValidate>
                        <TextField
                            id="reset-email"
                            label="Email"
                            icon="mail"
                            type="email"
                            inputMode="email"
                            autoComplete="username"
                            autoCapitalize="off"
                            spellCheck={false}
                            placeholder="you@yourbusiness.com"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                        />
                        <SubmitButton loading={loading} idle="Text me a code" busy="Sending…" />
                    </form>
                    <p className="auth-fine-print" style={{ marginTop: 14 }}>
                        No access to your phone?{' '}
                        <button type="button" className="auth-label-aside" style={linkButton} onClick={useQuestion} disabled={loading}>
                            Answer your security question instead
                        </button>
                    </p>
                </AuthCard>
            )}

            {step === 'code' && (
                <AuthCard title="Enter the code" subtitle={email.trim()} error={error} notice={notice}>
                    <form onSubmit={verifyCode} noValidate>
                        <TextField
                            id="reset-code"
                            label="6-digit code"
                            icon="message"
                            inputMode="numeric"
                            autoComplete="one-time-code"
                            maxLength={6}
                            value={code}
                            onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                        />
                        <SubmitButton loading={loading} idle="Verify code" busy="Checking…" />
                    </form>
                    <p className="auth-fine-print" style={{ marginTop: 14 }}>
                        <button type="button" className="auth-label-aside" style={linkButton} onClick={() => sendCode()} disabled={loading}>
                            Send a new code
                        </button>
                        {' · '}
                        <button type="button" className="auth-label-aside" style={linkButton} onClick={useQuestion} disabled={loading}>
                            Use security question
                        </button>
                        {' · '}
                        <button type="button" className="auth-label-aside" style={linkButton} onClick={backToEmail}>
                            Different email
                        </button>
                    </p>
                </AuthCard>
            )}

            {step === 'question' && (
                <AuthCard title="Security question" subtitle={question} error={error}>
                    <form onSubmit={verifyAnswer} noValidate>
                        <TextField
                            id="reset-answer"
                            label="Your answer"
                            icon="shield-check"
                            autoComplete="off"
                            value={answer}
                            onChange={(e) => setAnswer(e.target.value)}
                            aside={<span className="auth-label-aside">Not case-sensitive</span>}
                        />
                        <SubmitButton loading={loading} idle="Continue" busy="Checking…" />
                    </form>
                    <p className="auth-fine-print" style={{ marginTop: 14 }}>
                        <button type="button" className="auth-label-aside" style={linkButton} onClick={() => sendCode()} disabled={loading}>
                            Text me a code instead
                        </button>
                        {' · '}
                        <button type="button" className="auth-label-aside" style={linkButton} onClick={backToEmail}>
                            Different email
                        </button>
                    </p>
                </AuthCard>
            )}

            {step === 'password' && (
                <AuthCard title="Choose a new password" subtitle="This signs you out on every device." error={error} notice={notice}>
                    <form onSubmit={setNewPassword} noValidate>
                        <PasswordFields password={password} confirm={confirm} onPassword={setPassword} onConfirm={setConfirm} />
                        <SubmitButton loading={loading} idle="Save password" busy="Saving…" />
                    </form>
                </AuthCard>
            )}

            <a className="auth-callout auth-reveal" style={{ '--i': 2 }} href="/admin/login">
                <span className="auth-callout-icon"><span aria-hidden="true">←</span></span>
                <span className="auth-callout-text">
                    <strong>Remembered it?</strong>
                    Back to sign in.
                </span>
            </a>
        </AuthFrame>
    );
}
