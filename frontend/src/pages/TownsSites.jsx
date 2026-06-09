import React, { useEffect, useState } from 'react';
import { apiCall } from '../App';

export default function TownsSites() {
    const [towns, setTowns] = useState([]);
    const [sites, setSites] = useState({});
    const [showTownForm, setShowTownForm] = useState(false);
    const [showSiteForm, setShowSiteForm] = useState(null);
    const [newTown, setNewTown] = useState({ name: '', region: '' });
    const [newSite, setNewSite] = useState({ name: '', address: '' });
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState('');

    const normalizeList = (value) => {
        if (Array.isArray(value)) return value;
        if (Array.isArray(value?.data)) return value.data;
        if (Array.isArray(value?.items)) return value.items;
        return [];
    };

    const fetchData = async () => {
        setLoading(true);
        setLoadError('');
        const t = await apiCall('/towns');
        const normalizedTowns = normalizeList(t);

        if (!Array.isArray(t)) {
            setLoadError(t?.detail || t?.message || 'Loading towns or service temporarily unavailable...');
        }

        setTowns(normalizedTowns);
        if (normalizedTowns.length > 0) {
            const siteMap = {};
            for (const town of normalizedTowns) {
                const s = await apiCall(`/towns/${town.id}/sites`);
                siteMap[town.id] = normalizeList(s);
            }
            setSites(siteMap);
        } else {
            setSites({});
        }
        setLoading(false);
    };

    useEffect(() => { fetchData(); }, []);

    const createTown = async () => {
        await apiCall('/towns', { method: 'POST', body: JSON.stringify(newTown) });
        setNewTown({ name: '', region: '' });
        setShowTownForm(false);
        fetchData();
    };

    const createSite = async (townId) => {
        await apiCall(`/towns/${townId}/sites`, { method: 'POST', body: JSON.stringify(newSite) });
        setNewSite({ name: '', address: '' });
        setShowSiteForm(null);
        fetchData();
    };

    if (loading) return <p>Loading...</p>;

    return (
        <div>
            <div className="flex-between">
                <h1 style={{ fontSize: 24, fontWeight: 700 }}>Towns & Sites</h1>
                <button className="btn btn-primary" onClick={() => setShowTownForm(true)}>+ New Town</button>
            </div>

            {showTownForm && (
                <div className="modal-overlay" onClick={() => setShowTownForm(false)}>
                    <div className="modal" onClick={e => e.stopPropagation()}>
                        <h2>Add Town</h2>
                        <div className="form-group">
                            <label>Name</label>
                            <input value={newTown.name} onChange={e => setNewTown({ ...newTown, name: e.target.value })} />
                        </div>
                        <div className="form-group">
                            <label>Region</label>
                            <input value={newTown.region} onChange={e => setNewTown({ ...newTown, region: e.target.value })} />
                        </div>
                        <div className="gap-2">
                            <button className="btn btn-primary" onClick={createTown}>Create</button>
                            <button className="btn" style={{ background: '#e5e7eb' }} onClick={() => setShowTownForm(false)}>Cancel</button>
                        </div>
                    </div>
                </div>
            )}

            {loadError && <p style={{ color: '#b45309' }}>{loadError}</p>}

            {Array.isArray(towns) && towns.length === 0 && !loadError && (
                <p style={{ color: '#6b7280' }}>No towns yet. Click "New Town" to add one.</p>
            )}

            {Array.isArray(towns) ? towns.map(town => (
                <div key={town.id} className="card" style={{ marginBottom: 16 }}>
                    <div className="flex-between">
                        <h3 style={{ fontSize: 16 }}>{town.name} <span style={{ color: '#9ca3af', fontWeight: 400 }}>({town.region})</span></h3>
                        <button className="btn btn-primary btn-sm" onClick={() => setShowSiteForm(town.id)}>+ Add Site</button>
                    </div>

                    {showSiteForm === town.id && (
                        <div style={{ marginTop: 16, padding: 16, background: '#f9fafb', borderRadius: 8 }}>
                            <div className="form-group">
                                <label>Site Name</label>
                                <input value={newSite.name} onChange={e => setNewSite({ ...newSite, name: e.target.value })} />
                            </div>
                            <div className="form-group">
                                <label>Address</label>
                                <input value={newSite.address} onChange={e => setNewSite({ ...newSite, address: e.target.value })} />
                            </div>
                            <div className="gap-2">
                                <button className="btn btn-primary btn-sm" onClick={() => createSite(town.id)}>Create Site</button>
                                <button className="btn btn-sm" style={{ background: '#e5e7eb' }} onClick={() => setShowSiteForm(null)}>Cancel</button>
                            </div>
                        </div>
                    )}

                    <div className="table-wrap" style={{ marginTop: 12 }}>
                        <table>
                            <thead>
                                <tr><th>Site Name</th><th>Address</th><th>Created</th></tr>
                            </thead>
                            <tbody>
                                {(sites[town.id] || []).map(site => (
                                    <tr key={site.id}>
                                        <td>{site.name}</td>
                                        <td>{site.address}</td>
                                        <td>{new Date(site.created_at).toLocaleDateString()}</td>
                                    </tr>
                                ))}
                                {(!sites[town.id] || sites[town.id].length === 0) && (
                                    <tr><td colSpan="3" style={{ color: '#9ca3af' }}>No sites in this town yet</td></tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                </div>
            )) : <p style={{ color: '#6b7280' }}>Loading towns or service temporarily unavailable...</p>}
        </div>
    );
}
