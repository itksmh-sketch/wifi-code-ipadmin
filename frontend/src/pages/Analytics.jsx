import React, { useCallback, useEffect, useState } from 'react';
import { apiCall } from '../App';
import { RevenueChart, RedemptionsChart } from '../components/AnalyticsCharts';
import PageHeader from '../components/PageHeader';

function fmtMoney(n) {
    if (n == null || isNaN(Number(n))) return '—';
    return 'GHS ' + Number(n).toLocaleString('en-GH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtNum(n) {
    return (n == null || isNaN(Number(n))) ? '—' : Number(n).toLocaleString('en-GH');
}

function fmtMb(n) {
    if (n == null || isNaN(Number(n))) return '—';
    const mb = Number(n);
    if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
    return mb.toLocaleString('en-GH', { maximumFractionDigits: 1 }) + ' MB';
}

function StatCard({ label, value, sub }) {
    return (
        <div className="card stat-card">
            <div className="number">{value}</div>
            <div className="label">{label}</div>
            {sub && <div style={{ fontSize: 12, color: '#9ca3af', marginTop: 4 }}>{sub}</div>}
        </div>
    );
}

function SectionTitle({ children }) {
    return <h2 style={{ fontSize: 15, fontWeight: 600, color: '#374151', margin: '28px 0 12px' }}>{children}</h2>;
}

// auto-fit (not auto-fill) so cards stretch to fill the row instead of leaving
// empty phantom columns when there are fewer cards than would fit at minwidth.
function statGrid(children) {
    return (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 16 }}>
            {children}
        </div>
    );
}

export default function Analytics() {
    const [snapshot, setSnapshot] = useState(null);
    const [trends, setTrends] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const [snap, tr] = await Promise.all([
                apiCall('/analytics/snapshot'),
                apiCall('/analytics/trends'),
            ]);
            setSnapshot(snap);
            setTrends(tr);
        } catch (e) {
            setError(e.message || 'Could not load analytics');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    if (loading && !snapshot) return <p>Loading...</p>;

    return (
        <div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <PageHeader title="Analytics" />
                <button className="btn btn-sm" onClick={load} disabled={loading}>
                    {loading ? 'Refreshing…' : 'Refresh'}
                </button>
            </div>

            {error && <div style={{ color: '#ef4444', marginBottom: 16 }}>{error}</div>}

            {snapshot && (
                <>
                    <SectionTitle>Revenue</SectionTitle>
                    {statGrid(<>
                        <StatCard label="Today" value={fmtMoney(snapshot.revenue_ghs.today)} />
                        <StatCard label="This week" value={fmtMoney(snapshot.revenue_ghs.this_week)} />
                        <StatCard label="This month" value={fmtMoney(snapshot.revenue_ghs.this_month)} />
                    </>)}

                    <SectionTitle>Right now</SectionTitle>
                    {statGrid(<>
                        <StatCard label="Active sessions" value={fmtNum(snapshot.active_sessions)} />
                        <StatCard label="Routers online" value={fmtNum(snapshot.routers.online)} sub={`of ${fmtNum(snapshot.routers.total)} total`} />
                        <StatCard label="Routers offline" value={fmtNum(snapshot.routers.offline)} />
                    </>)}

                    <SectionTitle>Vouchers</SectionTitle>
                    {statGrid(<>
                        <StatCard
                            label="Sold online / via reseller"
                            value={fmtNum(snapshot.vouchers.sold_tracked_channels)}
                            sub="Excludes vouchers you generate and hand out manually — those aren't tracked as sales"
                        />
                        <StatCard label="Redeemed" value={fmtNum(snapshot.vouchers.redeemed)} sub="All channels" />
                        <StatCard label="Unredeemed inventory" value={fmtNum(snapshot.vouchers.unredeemed_inventory)} sub="All channels, not yet activated" />
                    </>)}

                    <SectionTitle>Top package this month</SectionTitle>
                    {snapshot.top_package ? (
                        <div className="card" style={{ padding: 20 }}>
                            <div style={{ fontSize: 18, fontWeight: 700, color: '#1f2937' }}>{snapshot.top_package.name}</div>
                            <div style={{ fontSize: 13, color: '#6b7280', marginTop: 4 }}>
                                {fmtNum(snapshot.top_package.voucher_count)} vouchers sold · {fmtMoney(snapshot.top_package.revenue_ghs)} revenue
                            </div>
                        </div>
                    ) : (
                        <p style={{ color: '#64748b' }}>No package sales yet this month.</p>
                    )}

                    <SectionTitle>Data used</SectionTitle>
                    {statGrid(<>
                        <StatCard label="Today" value={fmtMb(snapshot.data_used_mb.today)} />
                        <StatCard label="This week" value={fmtMb(snapshot.data_used_mb.this_week)} />
                        <StatCard label="This month" value={fmtMb(snapshot.data_used_mb.this_month)} />
                    </>)}
                </>
            )}

            {trends && (
                <>
                    <SectionTitle>Revenue — last 30 days</SectionTitle>
                    <RevenueChart data={trends.revenue_by_day} />

                    <SectionTitle>Voucher redemptions — last 30 days</SectionTitle>
                    <RedemptionsChart data={trends.redemptions_by_day} />
                </>
            )}
        </div>
    );
}
