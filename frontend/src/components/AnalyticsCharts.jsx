import React from 'react';
import {
    ResponsiveContainer, LineChart, Line, BarChart, Bar,
    CartesianGrid, XAxis, YAxis, Tooltip,
} from 'recharts';

export const ACCENT = '#2a78d6';
const GRID = '#e1e0d9';
const AXIS_TEXT = '#898781';

function fmtMoney(n) {
    if (n == null || isNaN(Number(n))) return '—';
    return 'GHS ' + Number(n).toLocaleString('en-GH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtNum(n) {
    return (n == null || isNaN(Number(n))) ? '—' : Number(n).toLocaleString('en-GH');
}

function fmtChartDate(iso) {
    const d = new Date(iso + 'T00:00:00Z');
    return d.toLocaleDateString('en-GH', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

function RevenueTooltip({ active, payload, label }) {
    if (!active || !payload || !payload.length) return null;
    return (
        <div style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 6, padding: '8px 12px', boxShadow: '0 1px 3px rgba(0,0,0,0.1)' }}>
            <div style={{ fontSize: 12, color: '#6b7280' }}>{fmtChartDate(label)}</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: '#1f2937' }}>{fmtMoney(payload[0].value)}</div>
        </div>
    );
}

function RedemptionsTooltip({ active, payload, label }) {
    if (!active || !payload || !payload.length) return null;
    return (
        <div style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 6, padding: '8px 12px', boxShadow: '0 1px 3px rgba(0,0,0,0.1)' }}>
            <div style={{ fontSize: 12, color: '#6b7280' }}>{fmtChartDate(label)}</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: '#1f2937' }}>{fmtNum(payload[0].value)} redeemed</div>
        </div>
    );
}

export function RevenueChart({ data, height = 240 }) {
    return (
        <div className="card" style={{ padding: '20px 20px 8px' }}>
            <ResponsiveContainer width="100%" height={height}>
                <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                    <CartesianGrid stroke={GRID} vertical={false} />
                    <XAxis
                        dataKey="date"
                        tickFormatter={fmtChartDate}
                        tick={{ fontSize: 11, fill: AXIS_TEXT }}
                        axisLine={{ stroke: GRID }}
                        tickLine={false}
                        interval={4}
                    />
                    <YAxis
                        tick={{ fontSize: 11, fill: AXIS_TEXT }}
                        axisLine={false}
                        tickLine={false}
                        width={70}
                        tickFormatter={(v) => 'GHS ' + Number(v).toLocaleString('en-GH')}
                    />
                    <Tooltip content={<RevenueTooltip />} />
                    <Line
                        type="monotone"
                        dataKey="amount_ghs"
                        stroke={ACCENT}
                        strokeWidth={2}
                        dot={false}
                        activeDot={{ r: 4, stroke: '#fff', strokeWidth: 2, fill: ACCENT }}
                    />
                </LineChart>
            </ResponsiveContainer>
        </div>
    );
}

export function RedemptionsChart({ data, height = 240 }) {
    return (
        <div className="card" style={{ padding: '20px 20px 8px' }}>
            <ResponsiveContainer width="100%" height={height}>
                <BarChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                    <CartesianGrid stroke={GRID} vertical={false} />
                    <XAxis
                        dataKey="date"
                        tickFormatter={fmtChartDate}
                        tick={{ fontSize: 11, fill: AXIS_TEXT }}
                        axisLine={{ stroke: GRID }}
                        tickLine={false}
                        interval={4}
                    />
                    <YAxis
                        tick={{ fontSize: 11, fill: AXIS_TEXT }}
                        axisLine={false}
                        tickLine={false}
                        width={40}
                        allowDecimals={false}
                    />
                    <Tooltip content={<RedemptionsTooltip />} cursor={{ fill: 'rgba(42,120,214,0.06)' }} />
                    <Bar dataKey="count" fill={ACCENT} radius={[4, 4, 0, 0]} maxBarSize={18} />
                </BarChart>
            </ResponsiveContainer>
        </div>
    );
}
