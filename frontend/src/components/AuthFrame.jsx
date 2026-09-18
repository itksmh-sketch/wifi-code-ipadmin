import React, { useState } from 'react';

// Shared pieces for the signed-out / account-setup screens (onboarding, forgot
// password). They use the same platform identity stylesheet as Login
// (/platform-ui/auth.css) so the three screens read as one flow.
const ICONS = '/platform-ui/icons.svg';

export function Icon({ name, className = '' }) {
    return (
        <svg className={`auth-icon ${className}`} aria-hidden="true" focusable="false">
            <use href={`${ICONS}#${name}`} />
        </svg>
    );
}

export function AuthFrame({ eyebrow, headline, lede, children }) {
    return (
        <div className="auth-shell">
            <aside className="auth-brand">
                <span className="auth-glow auth-glow--a" aria-hidden="true" />
                <span className="auth-glow auth-glow--b" aria-hidden="true" />
                <div className="auth-signal" aria-hidden="true">
                    <span className="auth-ring" />
                    <span className="auth-ring" />
                    <span className="auth-ring" />
                    <span className="auth-signal-core"><Icon name="shield-check" /></span>
                </div>
                <div className="auth-wordmark auth-reveal" style={{ '--i': 0 }}>
                    <span className="auth-wordmark-mark" aria-hidden="true" />
                    <span className="auth-wordmark-name">IpAdmin</span>
                </div>
                <div className="auth-brand-body">
                    <span className="auth-eyebrow auth-reveal" style={{ '--i': 1 }}>
                        <span className="auth-eyebrow-dot" aria-hidden="true" />
                        {eyebrow}
                    </span>
                    <h1 className="auth-headline auth-reveal" style={{ '--i': 2 }}>{headline}</h1>
                    <p className="auth-lede auth-reveal" style={{ '--i': 3 }}>{lede}</p>
                </div>
                <div className="auth-brand-foot auth-reveal" style={{ '--i': 4 }}>
                    <Icon name="lock" />
                    Codes are sent by SMS and expire after 10 minutes
                </div>
            </aside>
            <main className="auth-main">
                <div className="auth-stack">{children}</div>
            </main>
        </div>
    );
}

export function AuthCard({ title, subtitle, error, notice, children }) {
    return (
        <section className="auth-card auth-reveal" style={{ '--i': 1 }} aria-labelledby="auth-card-title">
            <header className="auth-card-head">
                <h2 className="auth-title" id="auth-card-title">{title}</h2>
                {subtitle && <p className="auth-subtitle">{subtitle}</p>}
            </header>
            {error && (
                <div className="auth-alert auth-alert--error" role="alert" key={error.id}>
                    <Icon name="alert-circle" />
                    <span>{error.text}</span>
                </div>
            )}
            {notice && (
                <div className="auth-msg auth-msg--success is-shown" role="status">
                    <div className="auth-msg-inner">
                        <div className="auth-msg-body" style={{ paddingTop: 0, marginBottom: 16 }}>
                            <Icon name="check" />
                            <span>{notice}</span>
                        </div>
                    </div>
                </div>
            )}
            {children}
        </section>
    );
}

export function TextField({ id, label, icon, aside, ...inputProps }) {
    return (
        <div className="auth-field">
            <label className="auth-label" htmlFor={id}>
                {label}
                {aside}
            </label>
            <div className="auth-control">
                {icon && <Icon name={icon} className="auth-lead" />}
                <input id={id} className="auth-input" {...inputProps} />
            </div>
        </div>
    );
}

export function SubmitButton({ loading, idle, busy }) {
    return (
        <button type="submit" className={`auth-btn${loading ? ' is-loading' : ''}`} disabled={loading} aria-busy={loading}>
            <span className="auth-btn-face auth-btn-face--idle" aria-hidden={loading}>
                {idle} <Icon name="arrow-right" />
            </span>
            <span className="auth-btn-face auth-btn-face--busy" aria-hidden={!loading}>
                <Icon name="loader" className="auth-spin" /> {busy}
            </span>
        </button>
    );
}

// Mirrors backend src/modules/admin_accounts/passwords.py — the server is the
// authority; this only gives live feedback while typing.
export const PASSWORD_RULES = [
    { key: 'length', label: '8+ characters', test: (p) => p.length >= 8 },
    { key: 'upper', label: 'uppercase', test: (p) => /[A-Z]/.test(p) },
    { key: 'lower', label: 'lowercase', test: (p) => /[a-z]/.test(p) },
    { key: 'digit', label: 'number', test: (p) => /[0-9]/.test(p) },
];

export function passwordMeetsPolicy(password) {
    return PASSWORD_RULES.every((rule) => rule.test(password));
}

export function PasswordFields({ password, confirm, onPassword, onConfirm }) {
    const [show, setShow] = useState(false);
    const met = PASSWORD_RULES.filter((rule) => rule.test(password)).length;
    return (
        <>
            <div className="auth-field">
                <label className="auth-label" htmlFor="new-password">
                    New password
                    <span className={`auth-label-aside${met === PASSWORD_RULES.length ? ' is-met' : ''}`}>
                        {met}/{PASSWORD_RULES.length} rules met
                    </span>
                </label>
                <div className="auth-control">
                    <Icon name="lock" className="auth-lead" />
                    <input
                        id="new-password"
                        className="auth-input"
                        type={show ? 'text' : 'password'}
                        autoComplete="new-password"
                        value={password}
                        onChange={(e) => onPassword(e.target.value)}
                        aria-describedby="password-rules"
                    />
                    <button
                        type="button"
                        className="auth-trail"
                        onClick={() => setShow((s) => !s)}
                        aria-label={show ? 'Hide password' : 'Show password'}
                        aria-pressed={show}
                    >
                        <Icon name={show ? 'eye-off' : 'eye'} />
                    </button>
                </div>
                <p id="password-rules" className="auth-fine-print" style={{ textAlign: 'left', marginTop: 6 }}>
                    {PASSWORD_RULES.map((rule, i) => (
                        <span key={rule.key} style={{ color: rule.test(password) ? 'var(--platform-success)' : undefined }}>
                            {i ? ' · ' : ''}{rule.label}
                        </span>
                    ))}
                </p>
            </div>
            <TextField
                id="confirm-password"
                label="Confirm new password"
                icon="lock"
                type={show ? 'text' : 'password'}
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => onConfirm(e.target.value)}
                aside={confirm && confirm === password ? <span className="auth-label-aside is-met">Matches</span> : null}
            />
        </>
    );
}

export function SelectField({ id, label, value, onChange, options, placeholder }) {
    return (
        <div className="auth-field">
            <label className="auth-label" htmlFor={id}>{label}</label>
            <div className="auth-control">
                <select id={id} className="auth-input" value={value} onChange={(e) => onChange(e.target.value)}>
                    <option value="">{placeholder}</option>
                    {options.map((opt) => (
                        <option key={opt.key} value={opt.key}>{opt.text}</option>
                    ))}
                </select>
            </div>
        </div>
    );
}

// Reads a FastAPI error body into a display string.
export async function readError(res, fallback) {
    const data = await res.json().catch(() => ({}));
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) {
        const parts = data.detail.map((d) => d?.msg).filter(Boolean);
        if (parts.length) return parts.join('; ');
    }
    if (res.status === 429) return 'Too many attempts. Please wait a few minutes and try again.';
    return fallback;
}
