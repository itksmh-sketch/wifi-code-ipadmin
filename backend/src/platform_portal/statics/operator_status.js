// Account status controls on the operator detail page: approve, suspend,
// unsuspend, cancel and restore (PUT /operators/{id}/status). The moves offered
// are the ones modules/platform/operator_status.py allows.
//
// An external file so the page gains no inline script: everything is bound
// here with addEventListener, nothing uses an inline handler or a style
// attribute (the dialog opens with the .show class, notices with `hidden`).
// After a change the page reloads, so the header, the sign-in card and the
// admins table all show the new state.
(function () {
  'use strict';

  const operatorId = window.location.pathname.split('/').filter(Boolean).pop();
  const base = '/api/v1/platform/operators/' + encodeURIComponent(operatorId);
  const DEFAULT_DAYS = 30;

  // status -> the moves offered, in button order.
  const MOVES = {
    pending: ['approve', 'cancel'],
    approved: ['suspend', 'cancel'],
    suspended: ['unsuspend', 'cancel'],
    cancelled: ['restore'],
  };
  const MOVE = {
    approve: { status: 'approved', label: 'Approve', button: 'btn primary' },
    suspend: { status: 'suspended', label: 'Suspend', button: 'btn danger' },
    unsuspend: { status: 'approved', label: 'Unsuspend', button: 'btn primary' },
    cancel: { status: 'cancelled', label: 'Cancel operator', button: 'btn danger' },
    restore: { status: 'approved', label: 'Restore', button: 'btn primary' },
  };

  let operator = null;
  let pending = null; // the move being confirmed

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

  function money(n) {
    return 'GHS ' + Number(n || 0).toFixed(2);
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

  function stateLines() {
    const lines = [];
    if (operator.status === 'suspended') {
      lines.push(operator.suspension_reason === 'billing'
        ? 'Suspended for non-payment: paying the overdue invoice lifts it.'
        : 'Suspended by the platform: paying an invoice does not lift it. Invoicing is paused.');
      lines.push('They can sign in; new sales and setup changes are refused.');
    } else if (operator.status === 'cancelled') {
      lines.push('Cancelled ' + when(operator.cancelled_at) + '. No admin or reseller can sign in, '
        + 'new sales are refused and they are not invoiced.');
      lines.push('Hotspot service for vouchers already sold: until ' + when(operator.hotspot_service_ends_at)
        + ' (not enforced yet).');
    } else if (operator.status === 'pending') {
      lines.push('Not approved yet.');
    }
    return lines;
  }

  function render() {
    let html = '<p class="detail-line">'
      + '<span class="badge ' + esc(operator.status) + '">' + esc(operator.status) + '</span> '
      + '<span class="text-muted">billing</span> '
      + '<span class="badge ' + esc(operator.billing_status) + '">' + esc(operator.billing_status) + '</span>'
      + '</p>';
    html += stateLines().map(l => '<p class="detail-line dim">' + esc(l) + '</p>').join('');
    html += '<div class="actions">'
      + (MOVES[operator.status] || []).map(m => '<button class="' + (m === 'cancel' || m === 'suspend' ? 'btn danger' : 'btn')
        + '" type="button" data-status-action="' + m + '">' + esc(MOVE[m].label) + '</button>').join('')
      + '</div>';
    $('status-body').innerHTML = html;
  }

  // The operators list links here with #status-card, but the page shows its
  // main area only once its own data has loaded: wait for that, briefly.
  function reveal(tries) {
    const card = $('status-card');
    if (card.offsetParent !== null) { card.scrollIntoView(); return; }
    if (tries < 100) window.requestAnimationFrame(() => reveal(tries + 1));
  }

  async function load() {
    try {
      const res = await api(base);
      if (!res.ok) throw new Error(await errorText(res, 'Could not load the account status'));
      operator = await res.json();
      notice($('status-err'), '');
      render();
      if (window.location.hash === '#status-card') reveal(0);
    } catch (e) {
      $('status-body').innerHTML = '';
      notice($('status-err'), e.message || 'Could not load the account status.');
    }
  }

  // ── dialog ────────────────────────────────────────────────────────────

  function explain(move) {
    const told = ' They are notified by email and SMS; the reason is not sent.';
    if (move === 'suspend') {
      return 'They can still sign in, but new sales (captive portal and resellers) and setup changes stop. '
        + 'Vouchers already sold keep working. Invoicing pauses until you unsuspend, and paying an invoice '
        + 'does not lift this suspension.' + told;
    }
    if (move === 'unsuspend') {
      return 'Sales and setup changes open again and invoicing resumes.'
        + (operator.suspension_reason === 'billing'
          ? ' This was a non-payment suspension: if the overdue invoice is still unpaid, the daily billing '
            + 'check suspends them again. Waive it first if that is not what you want.'
          : '')
        + told;
    }
    if (move === 'cancel') {
      return 'Every admin and reseller is signed out now and cannot sign in again. New sales stop and they '
        + 'are no longer invoiced. Their data is kept, and you can restore them later.' + told;
    }
    if (move === 'restore') {
      return 'Admins and resellers can sign in again (sessions ended by the cancellation stay ended) and '
        + 'sales reopen. Invoicing resumes, on trial if the trial has not ended. The platform sign-in '
        + 'switch, if it is on, stays on.' + told;
    }
    return 'Approves ' + operator.name + '. No notice is sent.';
  }

  function owedHtml() {
    const invoices = Array.isArray(operator.unpaid_invoices) ? operator.unpaid_invoices : [];
    let html = '';
    if (invoices.length) {
      html += '<p class="detail-line">Unpaid invoices (they will not be able to sign in to pay; waive them '
        + 'or settle outside the platform):</p><ul>'
        + invoices.map(i => '<li>' + esc(i.invoice_number) + ' · ' + esc(money(i.amount_ghs)) + ' · '
          + esc(i.status) + '</li>').join('')
        + '</ul>';
    } else {
      html += '<p class="detail-line dim">No unpaid invoices.</p>';
    }
    if (Number(operator.reseller_wallet_total_ghs) > 0) {
      html += '<p class="detail-line">Resellers hold ' + esc(money(operator.reseller_wallet_total_ghs))
        + ' in prepaid wallets, which they can no longer spend.</p>';
    }
    return html;
  }

  function syncEndNow() {
    $('status-days').disabled = $('status-end-now').checked;
  }

  function open(move) {
    pending = move;
    $('status-title').textContent = MOVE[move].label + ': ' + operator.name;
    $('status-explain').textContent = explain(move);
    const cancelling = move === 'cancel';
    $('status-cancel-wrap').hidden = !cancelling;
    if (cancelling) {
      $('status-owed').innerHTML = owedHtml();
      $('status-days').value = String(DEFAULT_DAYS);
      $('status-end-now').checked = false;
      syncEndNow();
    }
    $('status-reason').value = '';
    $('status-submit').textContent = MOVE[move].label;
    $('status-submit').className = MOVE[move].button;
    notice($('status-msg'), '');
    $('status-overlay').classList.add('show');
    $('status-reason').focus();
  }

  function close() {
    pending = null;
    $('status-overlay').classList.remove('show');
  }

  async function submit() {
    if (!pending) return;
    const body = { status: MOVE[pending].status };
    const reason = $('status-reason').value.trim();
    if (reason) body.reason = reason;
    if (pending === 'cancel') {
      if ($('status-end-now').checked) {
        body.hotspot_service_days = 0;
      } else {
        const days = Number($('status-days').value);
        if (!Number.isInteger(days) || days < 0 || days > 365) {
          notice($('status-msg'), 'Days must be a whole number from 0 to 365.');
          return;
        }
        body.hotspot_service_days = days;
      }
    }

    const btn = $('status-submit');
    btn.disabled = true;
    try {
      const res = await api(base + '/status', { method: 'PUT', body: JSON.stringify(body) });
      if (!res.ok) { notice($('status-msg'), await errorText(res, 'Could not change the status')); return; }
      close();
      window.location.reload();
    } catch (e) {
      notice($('status-msg'), e.message || 'Could not change the status.');
    } finally {
      btn.disabled = false;
    }
  }

  // ── wiring ────────────────────────────────────────────────────────────

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-status-action]');
    if (!el || !operator) return;
    const action = el.dataset.statusAction;
    if (action === 'close') { close(); return; }
    if (MOVE[action] && (MOVES[operator.status] || []).includes(action)) open(action);
  });
  $('status-end-now').addEventListener('change', syncEndNow);
  $('status-submit').addEventListener('click', submit);

  load();
})();
