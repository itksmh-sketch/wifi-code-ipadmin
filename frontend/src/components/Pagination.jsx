import React, { useCallback, useEffect, useState } from 'react';
import { apiCall } from '../App';

// Shared paging for the read-only history tables. Both the Transactions and
// SMS Usage tabs use BOTH pieces below — the hook and the Pager — so the
// paging contract (clamped page, server-supplied total_pages, "page past the
// end is empty, not an error") has exactly one implementation. The tabs differ
// only in their columns and row markup, which genuinely differ; everything
// about how a page is fetched and navigated is here.

// `params` is an optional flat object of extra query params (e.g. a date
// range). Serialised here rather than by each caller so filtering and paging
// can never disagree about how a request is built.
export function usePaginated(path, itemsKey, params = {}) {
    const [page, setPage] = useState(1);
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    // Stable across renders unless a value actually changes, so the effect
    // below doesn't refetch on every parent re-render.
    const query = new URLSearchParams(
        Object.entries(params).filter(([, v]) => v)
    ).toString();

    // A filter change must reset to page 1 — staying on page 4 of a narrower
    // result set shows an empty table for no visible reason.
    useEffect(() => {
        setPage(1);
    }, [query]);

    const load = useCallback(() => {
        setLoading(true);
        return apiCall(`${path}?page=${page}&page_size=50${query ? `&${query}` : ''}`)
            .then((d) => {
                setData(d);
                setError('');
            })
            .catch((e) => setError(e.message || 'Could not load'))
            .finally(() => setLoading(false));
    }, [path, page, query]);

    useEffect(() => {
        load();
    }, [load]);

    return {
        items: data?.[itemsKey] || [],
        page: data?.page ?? page,
        totalCount: data?.total_count ?? 0,
        totalPages: data?.total_pages ?? 1,
        pageSize: data?.page_size ?? 50,
        loading,
        error,
        next: () => setPage((p) => p + 1),
        prev: () => setPage((p) => Math.max(1, p - 1)),
    };
}

export function Pager({ state }) {
    const { page, totalPages, totalCount, pageSize, next, prev } = state;
    if (totalCount === 0) return null;

    const start = (page - 1) * pageSize + 1;
    const end = Math.min(page * pageSize, totalCount);
    const btn = (disabled) => ({
        background: disabled ? '#f1f5f9' : '#fff',
        color: disabled ? '#94a3b8' : '#1e293b',
        border: '1px solid #e2e8f0',
        borderRadius: 6,
        padding: '6px 12px',
        fontSize: 13,
        cursor: disabled ? 'not-allowed' : 'pointer',
    });

    return (
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'flex-end', marginTop: 12, fontSize: 13, color: '#64748b' }}>
            <span>{start}–{end} of {totalCount}</span>
            <button onClick={prev} disabled={page <= 1} style={btn(page <= 1)}>← Previous</button>
            <button onClick={next} disabled={page >= totalPages} style={btn(page >= totalPages)}>Next →</button>
        </div>
    );
}

// Shared table chrome, so the two tabs cannot drift on header/cell styling.
export const th = { padding: '8px 12px', textAlign: 'left', borderBottom: '1px solid #e2e8f0' };
export const td = { padding: '8px 12px', borderBottom: '1px solid #f1f5f9' };
export const tableStyle = { width: '100%', borderCollapse: 'collapse', fontSize: 14 };
