// Security-setup guard for every signed-in platform portal page.
//
// While the owner's security setup is pending, the API answers every
// platform-owner route with 403 + X-Security-Setup-Required. This wraps
// window.fetch once, so each page's own api() helper AND its direct fetch()
// calls are covered without editing ten copies of the same logic.
//
// On that header it sends the browser to /platform/setup and returns a promise
// that never settles. That is deliberate: several pages treat any 403 (or a
// failed request) as "sign out" and would otherwise clear the token and
// redirect to /platform/login in a race with this redirect. Leaving the
// caller waiting means none of its error handling runs.
//
// Load it with a plain <script src> BEFORE the page's inline script.
(function () {
  if (window.__platformSetupGuard) return;
  window.__platformSetupGuard = true;
  var originalFetch = window.fetch.bind(window);
  var redirecting = false;
  window.fetch = function () {
    return originalFetch.apply(null, arguments).then(function (res) {
      if (res.status === 403 && res.headers.get('X-Security-Setup-Required')) {
        if (!redirecting) {
          redirecting = true;
          window.location.replace('/platform/setup');
        }
        return new Promise(function () {});
      }
      return res;
    });
  };
})();
