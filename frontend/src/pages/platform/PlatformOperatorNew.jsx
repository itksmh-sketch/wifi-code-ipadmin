import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { platformApiCall } from '../../App';
import PlatformLayout from './PlatformLayout';

export default function PlatformOperatorNew() {
    const navigate = useNavigate();
    const [form, setForm] = useState({
        name: '',
        slug: '',
        contact_email: '',
        contact_phone: '',
        initial_admin_email: '',
        initial_admin_password: '',
    });
    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);

    const update = (field, value) => setForm((current) => ({ ...current, [field]: value }));

    const submit = async (event) => {
        event.preventDefault();
        setSaving(true);
        setError('');
        const data = await platformApiCall('/platform/operators', {
            method: 'POST',
            body: JSON.stringify({ ...form, contact_phone: form.contact_phone || null }),
        });
        setSaving(false);
        if (data?.detail) {
            setError(data.detail);
            return;
        }
        navigate(`/platform/operators/${data.id}`);
    };

    return (
        <PlatformLayout>
            <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 16 }}>New Operator</h1>
            {error && <div className="badge badge-red" style={{ marginBottom: 16 }}>{error}</div>}
            <form className="card" style={{ maxWidth: 720 }} onSubmit={submit}>
                {[
                    ['name', 'Name'],
                    ['slug', 'Slug'],
                    ['contact_email', 'Contact Email'],
                    ['contact_phone', 'Contact Phone'],
                    ['initial_admin_email', 'Initial Admin Email'],
                    ['initial_admin_password', 'Initial Admin Password'],
                ].map(([field, label]) => (
                    <div className="form-group" key={field}>
                        <label>{label}</label>
                        <input
                            type={field.includes('password') ? 'password' : field.includes('email') ? 'email' : 'text'}
                            value={form[field]}
                            onChange={(event) => update(field, event.target.value)}
                            required={!field.includes('phone')}
                        />
                    </div>
                ))}
                <button className="btn btn-primary" disabled={saving}>{saving ? 'Creating...' : 'Create Operator'}</button>
            </form>
        </PlatformLayout>
    );
}
