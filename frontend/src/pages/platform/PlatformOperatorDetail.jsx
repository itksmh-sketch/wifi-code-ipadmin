import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { platformApiCall } from '../../App';
import PlatformLayout from './PlatformLayout';

export default function PlatformOperatorDetail() {
    const { id } = useParams();
    const [operator, setOperator] = useState(null);
    const [admins, setAdmins] = useState([]);
    const [adminForm, setAdminForm] = useState({ email: '', password: '', role: 'admin' });
    const [error, setError] = useState('');

    const load = () => {
        platformApiCall(`/platform/operators/${id}`).then(setOperator).catch(() => {});
        platformApiCall(`/platform/operators/${id}/admins`).then((data) => setAdmins(Array.isArray(data) ? data : [])).catch(() => setAdmins([]));
    };

    useEffect(load, [id]);

    const setStatus = async (status) => {
        setError('');
        try {
            const data = await platformApiCall(`/platform/operators/${id}/status`, {
                method: 'PUT',
                body: JSON.stringify({ status }),
            });
            setOperator(data);
        } catch (e) {
            setError(e.message);
        }
    };

    const createAdmin = async (event) => {
        event.preventDefault();
        setError('');
        try {
            await platformApiCall(`/platform/operators/${id}/admins`, {
                method: 'POST',
                body: JSON.stringify(adminForm),
            });
            setAdminForm({ email: '', password: '', role: 'admin' });
            load();
        } catch (e) {
            setError(e.message);
        }
    };

    if (!operator) {
        return <PlatformLayout><p>Loading...</p></PlatformLayout>;
    }

    return (
        <PlatformLayout>
            <div className="flex-between">
                <div>
                    <h1 style={{ fontSize: 24, fontWeight: 700 }}>{operator.name}</h1>
                    <p style={{ color: '#6b7280', fontSize: 14 }}>{operator.slug}</p>
                </div>
                <div className="gap-2">
                    <button className="btn btn-primary" onClick={() => setStatus('approved')}>Reactivate</button>
                    <button className="btn" onClick={() => setStatus('suspended')}>Suspend</button>
                    <button className="btn btn-danger" onClick={() => setStatus('cancelled')}>Cancel</button>
                </div>
            </div>
            {error && <div className="badge badge-red" style={{ marginBottom: 16 }}>{error}</div>}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 16, marginBottom: 16 }}>
                <div className="card">
                    <h2 style={{ fontSize: 16, marginBottom: 12 }}>Contact</h2>
                    <p>{operator.contact_email}</p>
                    <p style={{ color: '#6b7280' }}>{operator.contact_phone || 'No phone'}</p>
                    <p style={{ marginTop: 12 }}><span className="badge badge-blue">{operator.status}</span></p>
                </div>
                <div className="card">
                    <h2 style={{ fontSize: 16, marginBottom: 12 }}>Revenue</h2>
                    <p>GHS {Number(operator.monthly_revenue_this_month || 0).toFixed(2)} this month</p>
                    <p style={{ color: '#6b7280' }}>{operator.voucher_count_this_month || 0} vouchers this month</p>
                    <p style={{ color: '#6b7280' }}>{operator.total_sessions || 0} total sessions</p>
                </div>
                <div className="card">
                    <h2 style={{ fontSize: 16, marginBottom: 12 }}>Payment Credentials</h2>
                    <p>{operator.payment_credentials?.configured ? 'Configured' : 'Not configured'}</p>
                    <p style={{ color: '#6b7280' }}>Last validated: {operator.payment_credentials?.last_validated_at ? new Date(operator.payment_credentials.last_validated_at).toLocaleString() : 'Never'}</p>
                </div>
            </div>
            <div className="card" style={{ marginBottom: 16 }}>
                <h2 style={{ fontSize: 16, marginBottom: 12 }}>Admins</h2>
                <div className="table-wrap">
                    <table>
                        <thead><tr><th>Email</th><th>Role</th><th>Status</th><th>Last Login</th></tr></thead>
                        <tbody>
                            {admins.map((admin) => (
                                <tr key={admin.id}>
                                    <td>{admin.email}</td>
                                    <td>{admin.role}</td>
                                    <td>{admin.is_active ? 'Active' : 'Inactive'}</td>
                                    <td>{admin.last_login_at ? new Date(admin.last_login_at).toLocaleString() : 'Never'}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
            <form className="card" style={{ maxWidth: 720 }} onSubmit={createAdmin}>
                <h2 style={{ fontSize: 16, marginBottom: 12 }}>Create Admin</h2>
                <div className="form-group">
                    <label>Email</label>
                    <input type="email" value={adminForm.email} onChange={(event) => setAdminForm({ ...adminForm, email: event.target.value })} required />
                </div>
                <div className="form-group">
                    <label>Password</label>
                    <input type="password" value={adminForm.password} onChange={(event) => setAdminForm({ ...adminForm, password: event.target.value })} required />
                </div>
                <div className="form-group">
                    <label>Role</label>
                    <select value={adminForm.role} onChange={(event) => setAdminForm({ ...adminForm, role: event.target.value })}>
                        <option value="admin">Admin</option>
                        <option value="superadmin">Superadmin</option>
                        <option value="viewer">Viewer</option>
                    </select>
                </div>
                <button className="btn btn-primary">Create Admin</button>
            </form>
        </PlatformLayout>
    );
}
