// Public pages (/ and /guides). The fee, trial and support email are rendered
// by the server; this only closes the pop-over menus and highlights the docs
// table of contents. Builds no HTML from strings and sets no inline styles
// (tests/test_landing_pages.py).
(function () {
  'use strict';

  // Close an open <details> pop-over on outside click or Escape.
  var pops = document.querySelectorAll('details.lp-pop');
  document.addEventListener('click', function (e) {
    pops.forEach(function (d) { if (d.open && !d.contains(e.target)) d.open = false; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    pops.forEach(function (d) {
      if (d.open) { d.open = false; d.querySelector('summary').focus(); }
    });
  });
  pops.forEach(function (d) {
    d.querySelectorAll('.lp-pop-panel a').forEach(function (a) {
      a.addEventListener('click', function () { d.open = false; });
    });
  });

  // Docs: mark the table-of-contents entry for the section being read.
  var tocLinks = document.querySelectorAll('.lp-toc a[href^="#"]');
  if (!tocLinks.length || !('IntersectionObserver' in window)) return;
  var byId = {};
  tocLinks.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });
  var visible = {};
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) { visible[entry.target.id] = entry.isIntersecting; });
    var current = null;
    document.querySelectorAll('.lp-doc[id]').forEach(function (sec) {
      if (!current && visible[sec.id]) current = sec.id;
    });
    if (!current) return;
    tocLinks.forEach(function (a) { a.classList.remove('is-active'); });
    if (byId[current]) byId[current].classList.add('is-active');
  }, { rootMargin: '-80px 0px -55% 0px' });
  document.querySelectorAll('.lp-doc[id]').forEach(function (sec) { observer.observe(sec); });
})();
