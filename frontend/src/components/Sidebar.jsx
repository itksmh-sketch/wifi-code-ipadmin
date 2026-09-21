import React, { useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { useAuth } from '../App';

// Platform identity icon sprite (Lucide, ISC) — served by the backend, same
// asset the login/apply pages use. See backend/static/platform-ui/icons.svg.
const ICONS = '/platform-ui/icons.svg';

function Icon({ name }) {
    return (
        <svg className="nav-icon" aria-hidden="true">
            <use href={`${ICONS}#${name}`} />
        </svg>
    );
}

// Icon choices are constrained to the symbols already in icons.svg (shared
// with the login/apply identity pass) — picked for closest available fit,
// not a literal 1:1 match for every label.
const links = [
    { to: '/', label: 'Dashboard', icon: 'rocket' },
    { to: '/analytics', label: 'Analytics', icon: 'trending-up' },
    { to: '/towns-sites', label: 'Towns & Sites', icon: 'map-pin' },
    { to: '/routers', label: 'Routers', icon: 'router' },
    { to: '/plans', label: 'Plans', icon: 'check' },
    { to: '/vouchers', label: 'Vouchers', icon: 'wifi' },
    { to: '/sessions', label: 'Sessions', icon: 'clock' },
    { to: '/payment-credentials', label: 'Payments', icon: 'smartphone' },
    { to: '/sms-credentials', label: 'SMS', icon: 'message' },
    { to: '/branding', label: 'Branding', icon: 'building' },
    { to: '/billing', label: 'Billing', icon: 'mail' },
    { to: '/security', label: 'Security', icon: 'lock' },
];

export default function Sidebar() {
    const { user, logout } = useAuth();
    const [open, setOpen] = useState(false);

    // Off-canvas nav only exists below the mobile breakpoint; Escape closes it
    // same as clicking the backdrop.
    useEffect(() => {
        if (!open) return;
        const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [open]);

    return (
        <>
            {/* Hidden once open — the panel already covers this corner, and
                closing happens via the backdrop, a nav click, or Escape. */}
            {!open && (
                <button
                    className="sidebar-toggle"
                    onClick={() => setOpen(true)}
                    aria-label="Open navigation"
                    aria-expanded={open}
                >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="4" y1="6" x2="20" y2="6" />
                        <line x1="4" y1="12" x2="20" y2="12" />
                        <line x1="4" y1="18" x2="20" y2="18" />
                    </svg>
                </button>
            )}
            <div className={`sidebar-backdrop${open ? ' is-open' : ''}`} onClick={() => setOpen(false)} aria-hidden="true" />
            <div className={`sidebar${open ? ' is-open' : ''}`}>
                <div className="sidebar-brand">
                    <span className="sidebar-mark" aria-hidden="true" />
                    <div>
                        <span className="sidebar-wordmark">IpAdmin</span>
                        <span className="sidebar-tagline">Admin Panel</span>
                    </div>
                </div>
                <nav className="sidebar-nav">
                    {links.map((link) => (
                        <NavLink
                            key={link.to}
                            to={link.to}
                            end={link.to === '/'}
                            onClick={() => setOpen(false)}
                            className={({ isActive }) => `sidebar-link${isActive ? ' active' : ''}`}
                        >
                            <span className="sidebar-link-icon"><Icon name={link.icon} /></span>
                            {link.label}
                        </NavLink>
                    ))}
                </nav>
                <div className="sidebar-foot">
                    <p className="sidebar-user">{user?.email || 'Admin'}</p>
                    <button className="sidebar-logout" onClick={logout}>Logout</button>
                </div>
            </div>
        </>
    );
}
