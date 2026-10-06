// Sign-in access controls on the operator detail page: disable or re-enable
// sign-in for the whole operator (admins and resellers) or for one admin.
//
// An external file so the page gains no inline script: everything is bound
// here with addEventListener, nothing uses an inline handler or a style
// attribute (the dialog opens with the .show class, notices with `hidden`).
// Self-contained: it does not call into the page's inline script, apart from
// asking it to refresh the Admins table after a change, if it can.
(function () {
  'use strict';

  const operatorId = window.location.pathname.split('/').filter(Boolean).pop();
  const base = '/api/v1/platform/operators/' + encodeURIComponent(operatorId);
  const DISABLED_BY = { platform_owner: 'the platform', operator_user: 'the operator', system: 'the system' };

  let operator = null;
  let admins = [];
  let pending = null; // { kind: 'operator' | 'admin', action: 'disable' | 'enable', admin? }

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function when(s) {
    if (!s) return '';
    const d = new Date(s);
    return isNaN(d) ? '' : d.toLocaleString();
  }

  async function api(path, opts) {
    const token = localStorage.getItem('platform_access_token');
    if (!token) { window.location.replace('/platform/login'); throw new Error('Signed out'); }
    const res = await fetch(path, Object.assign({}, opts, {
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    }));
    if (res.status === 401) {
      localStorage.removeItem('platform_access_token');
      window.location.replace('/platform/login');
      throw new Error('Unauthorized');
    }
    return res;
  }

  async function errorText(res, fallback) {
    try {
      const body = await res.json();
      if (typeof body.detail === 'string') return body.detail;
      if (Array.isArray(body.detail)) return body.detail.map(d => d.msg).join('; ');
    } catch (_) { /* not JSON */ }
    return fallback + ' (' + res.status + ').';
  }

  function notice(el, text) {
    el.textContent = text || '';
    el.hidden = !text;
  }

  // ── render ────────────────────────────────────────────────────────────

  function otherActiveSuperadmins(admin) {
    return admins.filter(a => a.id !== admin.id && a.role === 'superadmin' && a.is_active).length;
  }

  function render() {
    const off = !!operator.admin_logins_disabled_at;
    let html = '<p class="detail-line">'
      + (off
        ? '<span class="badge suspended">All sign-in disabled</span> '
          + '<span class="text-muted">since ' + esc(when(operator.admin_logins_disabled_at)) + '</span>'
        : '<span class="badge approved">Sign-in enabled</span>')
      + '</p>';
    if (off && operator.admin_logins_disabled_reason) {
      html += '<p class="detail-line dim">Reason: ' + esc(operator.admin_logins_disabled_reason) + '</p>';
    }
    html += '<p class="detail-line dim">'
      + (off
        ? 'No admin or reseller of this operator can sign in or use the API.'
        : 'Disabling signs every admin out at once and blocks every admin and reseller until re-enabled.')
      + '</p>'
      + '<div class="actions">'
      + (off
        ? '<button class="btn" type="button" data-access-action="operator-enable">Re-enable sign-in</button>'
        : '<button class="btn danger" type="button" data-access-action="operator-disable">Disable all sign-in</button>')
      + '</div>';

    if (admins.length) {
      html += '<div class="table-wrap"><table class="table"><thead><tr>'
        + '<th>Admin</th><th>Role</th><th>Sign-in</th><th></th></tr></thead><tbody>'
        + admins.map(a => {
          const state = a.is_active
            ? '<span class="badge approved">enabled</span>'
            : '<span class="badge suspended">disabled</span> <span class="text-muted">by '
              + esc(DISABLED_BY[a.disabled_by_type] || 'unknown') + ' · ' + esc(when(a.disabled_at)) + '</span>'
              + (a.disabled_reason ? '<br><span class="text-muted">' + esc(a.disabled_reason) + '</span>' : '');
          const button = a.is_active
            ? '<button class="btn sm danger" type="button" data-access-action="admin-disable" data-admin-id="' + esc(a.id) + '">Disable</button>'
            : '<button class="btn sm" type="button" data-access-action="admin-enable" data-admin-id="' + esc(a.id) + '">Re-enable</button>';
          return '<tr><td class="strong">' + esc(a.email) + '</td><td class="text-muted">' + esc(a.role)
            + '</td><td>' + state + '</td><td>' + button + '</td></tr>';
        }).join('')
        + '</tbody></table></div>';
    }
    $('access-body').innerHTML = html;
  }

  async function load() {
    try {
      const [opRes, adminRes] = await Promise.all([api(base), api(base + '/admins')]);
      if (!opRes.ok) throw new Error(await errorText(opRes, 'Could not load sign-in access'));
      if (!adminRes.ok) throw new Error(await errorText(adminRes, 'Could not load admins'));
      operator = await opRes.json();
      const list = await adminRes.json();
      admins = Array.isArray(list) ? list : [];
      notice($('access-err'), '');
      render();
    } catch (e) {
      $('access-body').innerHTML = '';
      notice($('access-err'), e.message || 'Could not load sign-in access.');
    }
  }

  // ── dialog ────────────────────────────────────────────────────────────

  function open(next) {
    pending = next;
    const disabling = next.action === 'disable';
    const who = next.kind === 'operator' ? 'every admin and reseller of ' + operator.name : next.admin.email;
    $('access-title').textContent = (disabling ? 'Disable sign-in: ' : 'Re-enable sign-in: ')
      + (next.kind === 'operator' ? operator.name : next.admin.email);
    $('access-explain').textContent = disabling
      ? 'This signs ' + who + ' out immediately and blocks sign-in until you re-enable it. '
        + 'Hotspot service is not affected. They are notified by email and SMS; the reason is not sent.'
      : 'This lets ' + who + ' sign in again. They are notified by email and SMS. '
        + (next.kind === 'operator' ? 'Admins disabled one by one stay disabled.' : '');
    $('access-reason-label').textContent = disabling ? 'Reason (required, kept internal)' : 'Note (optional, kept internal)';
    $('access-reason-help').textContent = 'Recorded in the audit trail. Never shown to the operator.';
    $('access-reason').value = '';
    $('access-confirm').checked = false;
    $('access-confirm-wrap').hidden = !(disabling && next.kind === 'admin'
      && next.admin.role === 'superadmin' && otherActiveSuperadmins(next.admin) === 0);
    $('access-submit').textContent = disabling ? 'Disable' : 'Re-enable';
    $('access-submit').className = disabling ? 'btn danger' : 'btn primary';
    notice($('access-msg'), '');
    $('access-overlay').classList.add('show');
    $('access-reason').focus();
  }

  function close() {
    pending = null;
    $('access-overlay').classList.remove('show');
  }

  async function submit() {
    if (!pending) return;
    const disabling = pending.action === 'disable';
    const reason = $('access-reason').value.trim();
    if (disabling && reason.length < 3) { notice($('access-msg'), 'Give a reason (at least 3 characters).'); return; }

    const path = pending.kind === 'operator'
      ? base + '/admin-logins/' + pending.action
      : base + '/admins/' + encodeURIComponent(pending.admin.id) + '/' + pending.action;
    const body = disabling ? { reason } : (reason ? { reason } : {});
    if (disabling && pending.kind === 'admin' && !$('access-confirm-wrap').hidden) {
      if (!$('access-confirm').checked) { notice($('access-msg'), 'Tick the box to confirm.'); return; }
      body.confirm_last_superadmin = true;
    }

    const btn = $('access-submit');
    btn.disabled = true;
    try {
      const res = await api(path, { method: 'POST', body: JSON.stringify(body) });
      if (!res.ok) { notice($('access-msg'), await errorText(res, 'Could not save')); return; }
      close();
      await load();
      // Keep the page's own Admins table in step, when it exposes a loader.
      if (typeof window.loadAdmins === 'function') window.loadAdmins();
    } catch (e) {
      notice($('access-msg'), e.message || 'Could not save.');
    } finally {
      btn.disabled = false;
    }
  }

  // ── wiring ────────────────────────────────────────────────────────────

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-access-action]');
    if (!el || !operator) return;
    const action = el.dataset.accessAction;
    if (action === 'cancel') { close(); return; }
    if (action === 'operator-disable') open({ kind: 'operator', action: 'disable' });
    else if (action === 'operator-enable') open({ kind: 'operator', action: 'enable' });
    else if (action === 'admin-disable' || action === 'admin-enable') {
      const admin = admins.find(a => a.id === el.dataset.adminId);
      if (admin) open({ kind: 'admin', action: action === 'admin-disable' ? 'disable' : 'enable', admin });
    }
  });
  $('access-submit').addEventListener('click', submit);

  load();
})();
