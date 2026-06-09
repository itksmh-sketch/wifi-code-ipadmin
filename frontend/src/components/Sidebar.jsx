import React from 'react';
import { NavLink } from 'react-router-dom';
import { useAuth } from '../App';

const links = [
    { to: '/', label: 'Dashboard', icon: 'DB' },
    { to: '/towns-sites', label: 'Towns & Sites', icon: 'TS' },
    { to: '/routers', label: 'Routers', icon: 'RT' },
    { to: '/plans', label: 'Plans', icon: 'PL' },
    { to: '/vouchers', label: 'Vouchers', icon: 'VC' },
    { to: '/sessions', label: 'Sessions', icon: 'SE' },
    { to: '/payment-credentials', label: 'Payments', icon: '$' },
    { to: '/billing', label: 'Billing', icon: '₵' },
];

export default function Sidebar() {
    const { user, logout } = useAuth();

    return (
        <div style={{
            width: 240,
            background: '#1e293b',
            color: 'white',
            padding: '24px 0',
            display: 'flex',
            flexDirection: 'column',
            minHeight: '100vh',
        }}>
            <div style={{ padding: '0 20px', marginBottom: 32 }}>
                <h2 style={{ fontSize: 18, fontWeight: 700 }}>ISP Hotspot</h2>
                <p style={{ fontSize: 12, color: '#94a3b8' }}>Admin Panel</p>
            </div>
            <nav style={{ flex: 1 }}>
                {links.map((link) => (
                    <NavLink
                        key={link.to}
                        to={link.to}
                        style={({ isActive }) => ({
                            display: 'flex',
                            alignItems: 'center',
                            gap: 10,
                            padding: '10px 20px',
                            color: isActive ? '#60a5fa' : '#cbd5e1',
                            background: isActive ? 'rgba(96,165,250,0.1)' : 'transparent',
                            borderLeft: isActive ? '3px solid #60a5fa' : '3px solid transparent',
                            fontSize: 14,
                            fontWeight: isActive ? 600 : 400,
                        })}
                    >
                        <span style={{ width: 22, fontSize: 12, fontWeight: 700 }}>{link.icon}</span>
                        {link.label}
                    </NavLink>
                ))}
            </nav>
            <div style={{ padding: '16px 20px', borderTop: '1px solid #334155' }}>
                <p style={{ fontSize: 12, color: '#94a3b8', marginBottom: 8 }}>
                    {user?.email || 'Admin'}
                </p>
                <button
                    onClick={logout}
                    style={{
                        background: '#dc2626',
                        color: 'white',
                        border: 'none',
                        borderRadius: 6,
                        padding: '6px 12px',
                        fontSize: 12,
                        cursor: 'pointer',
                        width: '100%',
                    }}
                >
                    Logout
                </button>
            </div>
        </div>
    );
}
