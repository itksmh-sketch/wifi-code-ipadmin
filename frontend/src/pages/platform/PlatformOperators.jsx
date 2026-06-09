import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { platformApiCall } from '../../App';
import PlatformLayout from './PlatformLayout';

export default function PlatformOperators() {
    const [operators, setOperators] = useState([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        platformApiCall('/platform/operators').then((data) => {
            setOperators(Array.isArray(data) ? data : []);
            setLoading(false);
        });
    }, []);

    return (
        <PlatformLayout>
            <div className="flex-between">
                <h1 style={{ fontSize: 24, fontWeight: 700 }}>Operators</h1>
                <Link className="btn btn-primary" to="/platform/operators/new">Create Operator</Link>
            </div>
            <div className="card table-wrap">
                {loading ? <p>Loading...</p> : (
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Slug</th>
                                <th>Status</th>
                                <th>Billing</th>
                                <th>Admins</th>
                                <th>Vouchers This Month</th>
                                <th>Revenue This Month</th>
                            </tr>
                        </thead>
                        <tbody>
                            {operators.map((operator) => (
                                <tr key={operator.id}>
                                    <td><Link style={{ color: '#2563eb', fontWeight: 600 }} to={`/platform/operators/${operator.id}`}>{operator.name}</Link></td>
                                    <td>{operator.slug}</td>
                                    <td><span className="badge badge-blue">{operator.status}</span></td>
                                    <td>{operator.billing_status}</td>
                                    <td>{operator.admin_count}</td>
                                    <td>{operator.voucher_count_this_month}</td>
                                    <td>GHS {Number(operator.monthly_revenue_this_month || 0).toFixed(2)}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}
            </div>
        </PlatformLayout>
    );
}
