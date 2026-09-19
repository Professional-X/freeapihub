/* FreeAPIHub theme toggle - persists choice, respects system preference. */
(function () {
  'use strict';

  var root = document.documentElement;
  var toggle = document.getElementById('theme-toggle');
  if (!toggle) return;

  function apply(theme) {
    root.setAttribute('data-theme', theme);
    try { localStorage.setItem('fah-theme', theme); } catch (e) { /* private mode */ }
  }

  toggle.addEventListener('click', function () {
    var current = root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    apply(current === 'dark' ? 'light' : 'dark');
  });

  // Follow OS preference only while the user has not chosen manually.
  var stored = null;
  try { stored = localStorage.getItem('fah-theme'); } catch (e) { /* ignore */ }
  if (!stored && window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) {
      mq.addEventListener('change', function (e) { apply(e.matches ? 'dark' : 'light'); });
    }
  }
})();
