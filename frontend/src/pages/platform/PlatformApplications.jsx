import React, { useEffect, useState } from 'react';
import { platformApiCall } from '../../App';

export default function PlatformApplications() {
    const [apps, setApps] = useState([]);
    const [selected, setSelected] = useState(null);
    const [rejectReason, setRejectReason] = useState('');
    const [feeInput, setFeeInput] = useState('200.00');
    const [result, setResult] = useState(null);
    const [loading, setLoading] = useState(false);

    const load = () => platformApiCall('/platform/applications').then(d => d && setApps(d)).catch(() => {});
    useEffect(() => { load(); }, []);

    const approve = async (id) => {
        setLoading(true); setResult(null);
        try {
            const res = await platformApiCall(`/platform/applications/${id}/approve`, {
                method: 'PUT',
                body: JSON.stringify({ monthly_fee_ghs: parseFloat(feeInput) }),
            });
            setResult(res); load(); setSelected(null);
        } catch (e) {
            alert(e.message);
        } finally {
            setLoading(false);
        }
    };

    const reject = async (id) => {
        if (!rejectReason.trim()) return alert('Please enter a rejection reason');
        setLoading(true); setResult(null);
        try {
            const res = await platformApiCall(`/platform/applications/${id}/reject`, {
                method: 'PUT',
                body: JSON.stringify({ rejection_reason: rejectReason }),
            });
            setResult(res); load(); setSelected(null); setRejectReason('');
        } catch (e) {
            alert(e.message);
        } finally {
            setLoading(false);
        }
    };

    const statusColor = { pending: '#fbbf24', approved: '#22c55e', rejected: '#ef4444' };

    return (
        <div>
            <h1>Operator Applications</h1>

            {result && (
                <div style={{ background: '#f0fdf4', border: '1px solid #86efac', borderRadius: 8, padding: 16, marginBottom: 20 }}>
                    {result.temp_password
                        ? <><strong>Approved!</strong> Share these details with the operator:<br/>
                            Email: <code>{result.admin_email}</code> &nbsp; Temp password: <code>{result.temp_password}</code>
                            <br/><small style={{color:'#6b7280'}}>This password is shown only once.</small></>
                        : <span>{result.message}</span>}
                </div>
            )}

            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
                <thead>
                    <tr style={{ background: '#f1f5f9' }}>
                        {['ISP Name', 'Contact', 'Email', 'Region', 'Status', 'Applied', 'Action'].map(h => (
                            <th key={h} style={{ padding: '8px 12px', textAlign: 'left', borderBottom: '1px solid #e2e8f0' }}>{h}</th>
                        ))}
                    </tr>
                </thead>
                <tbody>
                    {apps.map(app => (
                        <tr key={app.id}>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{app.isp_name}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{app.contact_name}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{app.email}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{app.region}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                <span style={{ background: statusColor[app.status] || '#e2e8f0', color: '#fff', padding: '2px 8px', borderRadius: 999, fontSize: 12 }}>
                                    {app.status}
                                </span>
                            </td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>{new Date(app.created_at).toLocaleDateString()}</td>
                            <td style={{ padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}>
                                {app.status === 'pending' && (
                                    <button onClick={() => setSelected(app)} style={{ background: '#2563eb', color: '#fff', border: 'none', borderRadius: 4, padding: '4px 10px', cursor: 'pointer', fontSize: 12 }}>
                                        Review
                                    </button>
                                )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>

            {selected && (
                <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}>
                    <div style={{ background: '#fff', borderRadius: 12, padding: 32, width: 480, maxWidth: '90vw' }}>
                        <h2 style={{ marginTop: 0 }}>Review: {selected.isp_name}</h2>
                        <p><strong>Contact:</strong> {selected.contact_name}<br/>
                        <strong>Email:</strong> {selected.email}<br/>
                        <strong>Phone:</strong> {selected.phone}<br/>
                        <strong>Region:</strong> {selected.region}<br/>
                        {selected.expected_sites && <><strong>Expected sites:</strong> {selected.expected_sites}<br/></>}
                        {selected.message && <><strong>Message:</strong> {selected.message}</>}</p>

                        <div style={{ marginBottom: 16 }}>
                            <label style={{ display: 'block', fontWeight: 600, marginBottom: 4 }}>Monthly fee (GHS)</label>
                            <input value={feeInput} onChange={e => setFeeInput(e.target.value)}
                                style={{ border: '1px solid #e2e8f0', borderRadius: 6, padding: '6px 10px', width: '100%' }} />
                        </div>

                        <div style={{ marginBottom: 16 }}>
                            <label style={{ display: 'block', fontWeight: 600, marginBottom: 4 }}>Rejection reason (if rejecting)</label>
                            <textarea value={rejectReason} onChange={e => setRejectReason(e.target.value)} rows={3}
                                style={{ border: '1px solid #e2e8f0', borderRadius: 6, padding: '6px 10px', width: '100%' }} />
                        </div>

                        <div style={{ display: 'flex', gap: 10 }}>
                            <button onClick={() => approve(selected.id)} disabled={loading}
                                style={{ flex: 1, background: '#22c55e', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 0', cursor: 'pointer', fontWeight: 600 }}>
                                ✓ Approve
                            </button>
                            <button onClick={() => reject(selected.id)} disabled={loading}
                                style={{ flex: 1, background: '#ef4444', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 0', cursor: 'pointer', fontWeight: 600 }}>
                                ✗ Reject
                            </button>
                            <button onClick={() => { setSelected(null); setResult(null); }}
                                style={{ background: '#f1f5f9', border: 'none', borderRadius: 6, padding: '8px 16px', cursor: 'pointer' }}>
                                Cancel
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
