// Shared nav for every signed-in platform portal page.
//
// Each page keeps a small static shell, and loads this file with a plain
// <script src> straight after it:
//
//   <nav class="nav" id="platform-nav" aria-label="Platform">
//     <a class="brand" href="/platform/operators">Platform <span>Admin</span></a>
//     <button class="nav-link" type="button" id="logout-btn">Sign out</button>
//   </nav>
//   <script src="/platform/statics/nav.js"></script>
//
// The script is synchronous, so the links are in place before the rest of the
// page is parsed: no flash, and no layout shift because the shell already
// gives the row its height. The shell keeps #logout-btn in the static markup
// on purpose: each page's own script does
// getElementById('logout-btn').addEventListener(...) at top level, and if that
// button were missing (say this file failed to load) the whole page script
// would throw. Sign-out is handled here, and only here.
//
// Sign-out ends the session on the server (POST /api/v1/platform/auth/logout,
// which signs the owner out on every device) but never waits on it: the token
// is cleared first, and the redirect happens when the call settles or after
// SIGN_OUT_TIMEOUT_MS, whichever comes first. keepalive lets the call finish
// after the page has gone.
(function () {
  var nav = document.getElementById('platform-nav');
  if (!nav || nav.getAttribute('data-ready')) return;
  nav.setAttribute('data-ready', '1');

  // The one link list. `match` lists path prefixes that mark the link active,
  // so /platform/operators/new and /platform/operators/<id> light up Operators.
  var LINKS = [
    { href: '/platform/operators', label: 'Operators' },
    { href: '/platform/analytics', label: 'Analytics' },
    { href: '/platform/billing', label: 'Billing' },
    { href: '/platform/transactions', label: 'Transactions' },
    { href: '/platform/providers', label: 'Providers' },
    { href: '/platform/notification-templates', label: 'Templates' },
    { href: '/platform/health', label: 'Service Health' },
  ];
  var ACCOUNT = [
    { href: '/platform/settings', label: 'Settings' },
  ];

  var path = window.location.pathname.replace(/\/+$/, '') || '/';
  function isActive(link) {
    return path === link.href || path.indexOf(link.href + '/') === 0;
  }

  function linkEl(link) {
    var a = document.createElement('a');
    a.className = 'nav-link';
    a.href = link.href;
    a.textContent = link.label;
    if (isActive(link)) a.setAttribute('aria-current', 'page');
    return a;
  }

  var menu = document.createElement('div');
  menu.className = 'nav-menu';
  menu.id = 'platform-nav-menu';

  var main = document.createElement('div');
  main.className = 'nav-links';
  LINKS.forEach(function (l) { main.appendChild(linkEl(l)); });

  var account = document.createElement('div');
  account.className = 'nav-account';
  ACCOUNT.forEach(function (l) { account.appendChild(linkEl(l)); });

  var logout = document.getElementById('logout-btn');
  if (!logout) {
    logout = document.createElement('button');
    logout.type = 'button';
    logout.id = 'logout-btn';
    logout.textContent = 'Sign out';
  }
  logout.className = 'nav-link';
  account.appendChild(logout);  // moves it out of the shell if it was there
  var SIGN_OUT_TIMEOUT_MS = 3000;
  var signingOut = false;
  logout.addEventListener('click', function () {
    if (signingOut) return;
    signingOut = true;
    var token = localStorage.getItem('platform_access_token');
    localStorage.removeItem('platform_access_token');
    localStorage.removeItem('platform_refresh_token');
    var left = false;
    function leave() {
      if (left) return;
      left = true;
      window.location.replace('/platform/login');
    }
    if (!token) return leave();
    setTimeout(leave, SIGN_OUT_TIMEOUT_MS);
    try {
      fetch('/api/v1/platform/auth/logout', {
        method: 'POST',
        keepalive: true,
        headers: { 'Authorization': 'Bearer ' + token },
      }).then(leave, leave);
    } catch (e) {
      leave();
    }
  });

  menu.appendChild(main);
  menu.appendChild(account);

  // Narrow-screen menu toggle; hidden by CSS above 960px.
  var toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'btn nav-toggle';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', menu.id);
  toggle.textContent = 'Menu';

  function setOpen(open) {
    nav.classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  toggle.addEventListener('click', function () { setOpen(!nav.classList.contains('open')); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && nav.classList.contains('open')) { setOpen(false); toggle.focus(); }
  });
  document.addEventListener('click', function (e) {
    if (nav.classList.contains('open') && !nav.contains(e.target)) setOpen(false);
  });

  nav.appendChild(toggle);
  nav.appendChild(menu);
})();
