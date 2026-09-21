import React, { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '../App';

// Platform identity assets (tokens.css, auth.css, icons.svg) are served by the
// backend at /platform-ui/ and linked in frontend/index.html. The vanilla apply
// page (backend/static/apply.html) uses the same files — keep the two in step.
const ICONS = '/platform-ui/icons.svg';
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const NOTICES = {
    'setup-complete': 'Your account is set up. Sign in with your new password.',
    'password-reset': 'Your password has been reset. Sign in with your new password.',
    'password-changed': 'Your new password is saved. Sign in with it.',
};

function Icon({ name, className = '' }) {
    return (
        <svg className={`auth-icon ${className}`} aria-hidden="true" focusable="false">
            <use href={`${ICONS}#${name}`} />
        </svg>
    );
}

// Keeps the last text while collapsing so the exit animation has content to hide.
function FieldMessage({ id, text }) {
    const last = useRef(text);
    if (text) last.current = text;
    return (
        <div id={id} className={`auth-msg auth-msg--error${text ? ' is-shown' : ''}`}>
            <div className="auth-msg-inner">
                <div className="auth-msg-body">
                    <Icon name="alert-circle" />
                    <span>{last.current}</span>
                </div>
            </div>
        </div>
    );
}

export default function Login() {
    const { login } = useAuth();
    const navigate = useNavigate();
    const emailRef = useRef(null);
    const passwordRef = useRef(null);
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const [fieldErrors, setFieldErrors] = useState({});
    const [shaking, setShaking] = useState(false);
    // { text, id } — a fresh id re-keys the alert so it animates in on every failure
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(false);
    const [searchParams] = useSearchParams();
    const notice = NOTICES[searchParams.get('notice')] || null;
    // Platform support address from the public endpoint; the line below stays
    // hidden until (and unless) a valid address arrives.
    const [supportEmail, setSupportEmail] = useState('');

    useEffect(() => {
        let cancelled = false;
        fetch('/api/v1/public/support-contact')
            .then((res) => (res.ok ? res.json() : null))
            .then((data) => {
                const address = data && data.support_email;
                if (!cancelled && EMAIL_RE.test(address || '')) setSupportEmail(address);
            })
            .catch(() => {});
        return () => { cancelled = true; };
    }, []);

    // index.html sets the default document title to "IpAdmin" for the whole
    // dashboard; this page-specific title applies only while Login is mounted.
    useEffect(() => {
        document.title = 'Sign in — IpAdmin';
        return () => { document.title = 'IpAdmin'; };
    }, []);

    const clearFieldError = (name) =>
        setFieldErrors((prev) => (prev[name] ? { ...prev, [name]: '' } : prev));

    const handleSubmit = async (e) => {
        e.preventDefault();
        if (loading) return;

        const errors = {};
        if (!EMAIL_RE.test(email.trim())) errors.email = 'Enter the email address for your account.';
        if (!password) errors.password = 'Enter your password.';
        setFieldErrors(errors);
        if (errors.email || errors.password) {
            setShaking(true);
            (errors.email ? emailRef : passwordRef).current?.focus();
            return;
        }

        setLoading(true);
        setError(null);
        try {
            const res = await fetch('/api/v1/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email: email.trim(), password }),
            });
            const data = await res.json().catch(() => ({}));
            if (res.ok) {
                login(data.access_token);
                // Temp-password accounts must finish setup (or, after a platform
                // reset, choose a new password) before anything else.
                navigate(data.must_complete_onboarding || data.must_change_password ? '/onboarding' : '/');
                return;
            }
            setError({ text: typeof data.detail === 'string' ? data.detail : 'Sign-in failed. Please try again.', id: Date.now() });
        } catch (err) {
            setError({ text: 'Network error — check your connection and try again.', id: Date.now() });
        }
        setLoading(false);
    };

    const fieldClass = (name) =>
        `auth-field${fieldErrors[name] ? ' is-invalid' : ''}${fieldErrors[name] && shaking ? ' is-shaking' : ''}`;
    const stopShake = (e) => {
        if (e.animationName === 'auth-shake') setShaking(false);
    };

    return (
        <div className="auth-shell">
            <aside className="auth-brand">
                <span className="auth-glow auth-glow--a" aria-hidden="true" />
                <span className="auth-glow auth-glow--b" aria-hidden="true" />
                <div className="auth-signal" aria-hidden="true">
                    <span className="auth-ring" />
                    <span className="auth-ring" />
                    <span className="auth-ring" />
                    <span className="auth-signal-core"><Icon name="router" /></span>
                </div>

                <div className="auth-wordmark auth-reveal" style={{ '--i': 0 }}>
                    <span className="auth-wordmark-mark" aria-hidden="true" />
                    <span className="auth-wordmark-name">IpAdmin</span>
                </div>

                <div className="auth-brand-body">
                    <span className="auth-eyebrow auth-reveal" style={{ '--i': 1 }}>
                        <span className="auth-eyebrow-dot" aria-hidden="true" />
                        Operator console
                    </span>
                    <h1 className="auth-headline auth-reveal" style={{ '--i': 2 }}>
                        Welcome back.
                        <span className="auth-headline-extra"><br /><em>Your network is waiting.</em></span>
                    </h1>
                    <p className="auth-lede auth-reveal" style={{ '--i': 3 }}>
                        Routers, vouchers, live sessions and revenue — right where you left them.
                    </p>
                </div>

                <div className="auth-brand-foot auth-reveal" style={{ '--i': 4 }}>
                    <Icon name="shield-check" />
                    Encrypted sign-in · routers connected over VPN
                </div>
            </aside>

            <main className="auth-main">
                <div className="auth-stack">
                    <section className="auth-card auth-reveal" style={{ '--i': 1 }} aria-labelledby="login-title">
                        <header className="auth-card-head">
                            <h2 className="auth-title" id="login-title">Sign in</h2>
                            <p className="auth-subtitle">Use the email and password for your operator account.</p>
                        </header>

                        {notice && !error && (
                            <div className="auth-msg auth-msg--success is-shown" role="status">
                                <div className="auth-msg-inner">
                                    <div className="auth-msg-body" style={{ paddingTop: 0, marginBottom: 16 }}>
                                        <Icon name="check" />
                                        <span>{notice}</span>
                                    </div>
                                </div>
                            </div>
                        )}

                        {error && (
                            <div className="auth-alert auth-alert--error" role="alert" key={error.id}>
                                <Icon name="alert-circle" />
                                <span>{error.text}</span>
                            </div>
                        )}

                        <form onSubmit={handleSubmit} noValidate>
                            <div className={fieldClass('email')} onAnimationEnd={stopShake}>
                                <label className="auth-label" htmlFor="login-email">Email</label>
                                <div className="auth-control">
                                    <Icon name="mail" className="auth-lead" />
                                    <input
                                        ref={emailRef}
                                        id="login-email"
                                        className="auth-input"
                                        type="email"
                                        inputMode="email"
                                        autoComplete="username"
                                        autoCapitalize="off"
                                        spellCheck={false}
                                        placeholder="you@yourbusiness.com"
                                        value={email}
                                        onChange={(e) => { setEmail(e.target.value); clearFieldError('email'); }}
                                        aria-invalid={fieldErrors.email ? true : undefined}
                                        aria-describedby="login-email-msg"
                                    />
                                </div>
                                <FieldMessage id="login-email-msg" text={fieldErrors.email} />
                            </div>

                            <div className={fieldClass('password')} onAnimationEnd={stopShake}>
                                <label className="auth-label" htmlFor="login-password">
                                    Password
                                    <Link className="auth-label-aside" to="/forgot-password">Forgot password?</Link>
                                </label>
                                <div className="auth-control">
                                    <Icon name="lock" className="auth-lead" />
                                    <input
                                        ref={passwordRef}
                                        id="login-password"
                                        className="auth-input"
                                        type={showPassword ? 'text' : 'password'}
                                        autoComplete="current-password"
                                        value={password}
                                        onChange={(e) => { setPassword(e.target.value); clearFieldError('password'); }}
                                        aria-invalid={fieldErrors.password ? true : undefined}
                                        aria-describedby="login-password-msg"
                                    />
                                    <button
                                        type="button"
                                        className="auth-trail"
                                        onClick={() => setShowPassword((s) => !s)}
                                        aria-label={showPassword ? 'Hide password' : 'Show password'}
                                        aria-pressed={showPassword}
                                        aria-controls="login-password"
                                    >
                                        <Icon name={showPassword ? 'eye-off' : 'eye'} />
                                    </button>
                                </div>
                                <FieldMessage id="login-password-msg" text={fieldErrors.password} />
                            </div>

                            <button
                                type="submit"
                                className={`auth-btn${loading ? ' is-loading' : ''}`}
                                disabled={loading}
                                aria-busy={loading}
                            >
                                <span className="auth-btn-face auth-btn-face--idle" aria-hidden={loading}>
                                    Sign in <Icon name="arrow-right" />
                                </span>
                                <span className="auth-btn-face auth-btn-face--busy" aria-hidden={!loading}>
                                    <Icon name="loader" className="auth-spin" /> Signing in…
                                </span>
                            </button>
                        </form>
                    </section>

                    <a className="auth-callout auth-reveal" style={{ '--i': 2 }} href="/apply">
                        <span className="auth-callout-icon"><Icon name="rocket" /></span>
                        <span className="auth-callout-text">
                            <strong>New operator?</strong>
                            Launch your hotspot business on the platform.
                        </span>
                        <span className="auth-callout-go"><span>Apply</span><Icon name="arrow-right" /></span>
                    </a>

                    {supportEmail && (
                        <p className="auth-fine-print">
                            Need help? Email <a href={`mailto:${supportEmail}`}>{supportEmail}</a>
                        </p>
                    )}
                </div>
            </main>
        </div>
    );
}
