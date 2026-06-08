import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

export default function Dashboard() {
    const [stats, setStats] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        apiCall('/dashboard').then((data) => {
            setStats(data);
            setLoading(false);
        });
    }, []);

    if (loading) return <p>Loading...</p>;
    if (!stats) return <p>Failed to load dashboard data.</p>;

    const cards = [
        { label: 'Total Vouchers', value: stats.total_vouchers, color: '#2563eb' },
        { label: 'Active Vouchers', value: stats.active_vouchers, color: '#16a34a' },
        { label: 'Active Sessions', value: stats.active_sessions, color: '#9333ea' },
        { label: 'Active Sites', value: stats.active_sites, color: '#ea580c' },
        { label: 'Expired Vouchers', value: stats.expired_vouchers, color: '#dc2626' },
        { label: 'Exhausted Vouchers', value: stats.exhausted_vouchers, color: '#ca8a04' },
    ];

    return (
        <div>
            <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 24 }}>Dashboard</h1>
            <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
                gap: 16,
            }}>
                {cards.map((card) => (
                    <div key={card.label} className="card stat-card">
                        <div className="number" style={{ color: card.color }}>{card.value}</div>
                        <div className="label">{card.label}</div>
                    </div>
                ))}
            </div>
        </div>
    );
}
