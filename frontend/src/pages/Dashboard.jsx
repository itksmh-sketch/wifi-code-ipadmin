import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiCall } from '../App';
import { RevenueChart, RedemptionsChart } from '../components/AnalyticsCharts';
import PageHeader from '../components/PageHeader';

function fmtMoney(n) {
    if (n == null || isNaN(Number(n))) return '—';
    return 'GHS ' + Number(n).toLocaleString('en-GH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function Dashboard() {
    const [stats, setStats] = useState(null);
    const [snapshot, setSnapshot] = useState(null);
    const [trends, setTrends] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        Promise.all([
            apiCall('/dashboard').catch(() => null),
            apiCall('/analytics/snapshot').catch(() => null),
            apiCall('/analytics/trends').catch(() => null),
        ])
            .then(([dash, snap, tr]) => {
                setStats(dash);
                setSnapshot(snap);
                setTrends(tr);
            })
            .finally(() => setLoading(false));
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

    if (snapshot) {
        cards.push({ label: 'Revenue Today', value: fmtMoney(snapshot.revenue_ghs.today), color: '#0d9488' });
        cards.push({
            label: 'Top Package This Month',
            value: snapshot.top_package ? snapshot.top_package.name : '—',
            sub: snapshot.top_package ? `${snapshot.top_package.voucher_count} sold` : null,
            color: '#7c3aed',
        });
    }

    return (
        <div>
            <PageHeader title="Dashboard" style={{ marginBottom: 24 }} />
            <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
                gap: 16,
            }}>
                {cards.map((card) => (
                    <div key={card.label} className="card stat-card">
                        <div
                            className="number"
                            style={{ color: card.color, fontSize: typeof card.value === 'string' && card.value.length > 10 ? 20 : undefined }}
                        >
                            {card.value}
                        </div>
                        <div className="label">{card.label}</div>
                        {card.sub && <div style={{ fontSize: 12, color: '#9ca3af', marginTop: 4 }}>{card.sub}</div>}
                    </div>
                ))}
            </div>

            {trends && (
                <>
                    <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', margin: '32px 0 12px' }}>
                        <h2 style={{ fontSize: 15, fontWeight: 600, color: '#374151' }}>Trends — last 30 days</h2>
                        <Link to="/analytics" style={{ fontSize: 13, color: '#2563eb', textDecoration: 'none' }}>
                            View full analytics →
                        </Link>
                    </div>
                    <div style={{
                        display: 'grid',
                        gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
                        gap: 16,
                    }}>
                        <div>
                            <div style={{ fontSize: 13, color: '#6b7280', marginBottom: 8 }}>Revenue</div>
                            <RevenueChart data={trends.revenue_by_day} height={180} />
                        </div>
                        <div>
                            <div style={{ fontSize: 13, color: '#6b7280', marginBottom: 8 }}>Voucher redemptions</div>
                            <RedemptionsChart data={trends.redemptions_by_day} height={180} />
                        </div>
                    </div>
                </>
            )}
        </div>
    );
}
