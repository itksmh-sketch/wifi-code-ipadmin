// Platform audit log (audit.html). Loaded after setup-guard.js.
//
// Every value from a row is placed with textContent or an attribute set by
// setAttribute, never parsed as HTML: the summaries and details carry text
// operators and their users typed.
(function () {
  'use strict';

  var token = localStorage.getItem('platform_access_token');
  if (!token) { window.location.replace('/platform/login'); return; }

  var ACTOR_NAMES = { operator_user: 'Operator user', platform_owner: 'Platform owner', system: 'System' };
  var PREFIXES = [
    'admin_user', 'application', 'audit', 'auth', 'account', 'branding', 'commission_rule', 'config_template',
    'invoice', 'notification_template', 'operator', 'payment', 'payment_credentials', 'plan', 'platform',
    'platform_auth', 'platform_owner', 'platform_payment_credentials', 'platform_sms_credentials',
    'platform_notification_sms_credentials', 'provider_catalog', 'reseller', 'router', 'security', 'site',
    'sms_credentials', 'sms_template', 'sms_usage', 'town', 'voucher',
  ];

  var $ = function (id) { return document.getElementById(id); };
  // Keyset paging: the server returns a cursor for the next (older) page
  // only, so the cursors of pages already visited are kept, which is what
  // lets "Newer" go back. cursors[i] fetches page i; cursors[0] is null. The
  // total comes with the first page alone and is capped by the server.
  var cursors = [null];
  var index = 0;
  var total = null;
  var shown = 0;       // rows on the current page
  var generation = 0;  // drops a slow response that a newer request overtook

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function api(path) {
    return fetch(path, { headers: { 'Authorization': 'Bearer ' + localStorage.getItem('platform_access_token') } })
      .then(function (res) {
        if (res.status === 401) {
          localStorage.removeItem('platform_access_token');
          window.location.replace('/platform/login');
          throw new Error('Signed out');
        }
        return res;
      });
  }

  function showError(text) { $('msg').textContent = text; $('msg').hidden = false; }
  function hideError() { $('msg').hidden = true; }

  // A value as one readable line. "[redacted]" and "changed" are shown as
  // they are; null as an em dash.
  function show(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'string') return value;
    return JSON.stringify(value);
  }

  function changesList(changes) {
    var dl = el('dl', 'audit-kv');
    Object.keys(changes).sort().forEach(function (field) {
      var value = changes[field];
      dl.appendChild(el('dt', null, field));
      var dd = el('dd');
      if (value && typeof value === 'object' && !Array.isArray(value) && ('before' in value || 'after' in value)) {
        dd.appendChild(el('span', 'audit-before', show(value.before)));
        dd.appendChild(el('span', 'audit-arrow', '→'));
        dd.appendChild(el('span', null, show(value.after)));
      } else {
        dd.textContent = show(value);
      }
      dl.appendChild(dd);
    });
    return dl;
  }

  function detailList(row) {
    var dl = el('dl', 'audit-kv');
    function add(key, value) {
      dl.appendChild(el('dt', null, key));
      dl.appendChild(el('dd', null, show(value)));
    }
    Object.keys(row.detail || {}).sort().forEach(function (key) { add(key, row.detail[key]); });
    if (row.target_type) add('target', row.target_type + (row.target_id ? ' ' + row.target_id : ''));
    if (row.ip) add('ip', row.ip);
    if (row.user_agent) add('user agent', row.user_agent);
    return dl;
  }

  function rowEl(row) {
    var tr = el('tr');
    tr.appendChild(el('td', 'audit-when', row.created_at.replace('T', ' ').slice(0, 19)));

    var op = el('td');
    if (row.operator) {
      var a = el('a', null, row.operator.name || row.operator.id);
      a.setAttribute('href', '/platform/operators/' + encodeURIComponent(row.operator.id));
      op.appendChild(a);
    } else {
      op.appendChild(el('span', 'text-muted', 'Platform'));
    }
    tr.appendChild(op);

    var actor = el('td');
    actor.appendChild(el('span', null, ACTOR_NAMES[row.actor.type] || row.actor.type));
    if (row.actor.label) actor.appendChild(el('span', 'audit-actor-label', row.actor.label));
    tr.appendChild(actor);

    tr.appendChild(el('td', 'audit-action', row.action));

    var what = el('td');
    what.appendChild(el('p', 'audit-summary', row.summary || row.action));
    var hasChanges = row.changes && Object.keys(row.changes).length;
    var more = el('details', 'audit-more');
    more.appendChild(el('summary', null, hasChanges ? 'Changes and details' : 'Details'));
    if (hasChanges) {
      more.appendChild(el('p', 'audit-subhead', 'Changes'));
      more.appendChild(changesList(row.changes));
    }
    more.appendChild(el('p', 'audit-subhead', 'Details'));
    more.appendChild(detailList(row));
    what.appendChild(more);
    tr.appendChild(what);
    return tr;
  }

  function pageSize() { return Number($('page-size').value) || 50; }

  function query(after) {
    var params = new URLSearchParams();
    var operator = $('f-operator').value;
    var actor = $('f-actor').value;
    var action = $('f-action').value.trim();
    if (operator) params.set('operator', operator);
    if (actor) params.set('actor_type', actor);
    if (action) params.set('action', action);
    if ($('f-from').value) params.set('date_from', $('f-from').value);
    if ($('f-to').value) params.set('date_to', $('f-to').value);
    if ($('f-system').checked) params.set('include_system', 'true');
    params.set('limit', String(pageSize()));
    if (after) params.set('cursor', after);
    return params.toString();
  }

  function renderPager(busy) {
    var start = index * pageSize() + 1;
    var pages = total && !total.capped ? Math.max(1, Math.ceil(total.count / pageSize())) : null;
    $('pager-summary').textContent = shown
      ? start + '–' + (start + shown - 1)
        + (total ? ' of ' + total.count.toLocaleString() + (total.capped ? '+' : '') : '')
        + ' · Page ' + (index + 1) + (pages ? ' of ' + pages : '')
      : '';
    $('newest-btn').disabled = busy || index === 0;
    $('newer-btn').disabled = busy || index === 0;
    $('older-btn').disabled = busy || cursors.length <= index + 1;
  }

  // Page `target`, from the newest (0) or one already reached. A filter or
  // page-size change always starts again from the newest page.
  function load(target, scroll) {
    var mine = ++generation;
    var list = target === 0 ? [null] : cursors;
    hideError();
    renderPager(true);
    return api('/api/v1/platform/audit?' + query(list[target]))
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Could not load the audit log.');
          return body;
        });
      })
      .then(function (body) {
        if (mine !== generation) return;
        var rows = body.rows || [];
        var tbody = $('rows');
        tbody.replaceChildren();
        rows.forEach(function (row) { tbody.appendChild(rowEl(row)); });
        shown = rows.length;
        if (!shown) {
          var empty = el('tr');
          var td = el('td', 'empty text-muted', 'No audit rows match these filters.');
          td.setAttribute('colspan', '5');
          empty.appendChild(td);
          tbody.appendChild(empty);
        }
        if (target === 0) total = { count: body.total || 0, capped: !!body.total_capped };
        index = target;
        cursors = list.slice(0, target + 1);
        if (body.next_cursor) cursors.push(body.next_cursor);
        renderPager(false);
        $('loading').hidden = true;
        $('main').hidden = false;
        if (scroll) $('results').scrollIntoView({ block: 'start' });
      })
      .catch(function (err) {
        if (mine !== generation) return;
        $('loading').hidden = true;
        $('main').hidden = false;
        renderPager(false);
        showError(err.message);
      });
  }

  function loadOperators() {
    return api('/api/v1/platform/operators')
      .then(function (res) { return res.ok ? res.json() : []; })
      .then(function (operators) {
        var select = $('f-operator');
        (operators || []).forEach(function (o) {
          var option = el('option', null, o.name + ' (' + o.slug + ')');
          option.value = o.id;
          select.appendChild(option);
        });
      })
      .catch(function () { /* the list just stays at its two fixed choices */ });
  }

  var list = $('action-prefixes');
  PREFIXES.forEach(function (p) { var o = el('option'); o.value = p; list.appendChild(o); });

  ['f-operator', 'f-actor', 'f-from', 'f-to', 'f-system', 'page-size'].forEach(function (id) {
    $(id).addEventListener('change', function () { load(0); });
  });
  var typing = null;
  $('f-action').addEventListener('input', function () {
    clearTimeout(typing);
    typing = setTimeout(function () { load(0); }, 350);
  });
  $('filters').addEventListener('submit', function (e) { e.preventDefault(); load(0); });
  $('f-clear').addEventListener('click', function () {
    $('filters').reset();
    load(0);
  });
  $('newest-btn').addEventListener('click', function () { load(0, true); });
  $('newer-btn').addEventListener('click', function () { load(index - 1, true); });
  $('older-btn').addEventListener('click', function () { load(index + 1, true); });

  loadOperators();
  load(0);
})();
