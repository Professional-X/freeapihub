/* FreeAPIHub client-side search.
   Loads a lightweight JSON index and filters it locally - no server needed.
   Covers name, description, category, tags and features (spec #16). */
(function () {
  'use strict';

  var params = new URLSearchParams(window.location.search);
  var initialQuery = (params.get('q') || '').trim();
  var initialCategory = (params.get('category') || '').trim();

  var searchInput = document.getElementById('search-input');
  var meta = document.getElementById('search-meta');
  var resultsBox = document.getElementById('search-results');
  var filterRow = document.getElementById('filter-row');
  if (!searchInput || !resultsBox) return;

  var INDEX_URL = searchInput.getAttribute('data-index') || '../data/search.json';
  var index = null;
  var activeCategory = initialCategory;
  var debounceTimer = null;

  var categoryNames = {};
  if (filterRow) {
    filterRow.querySelectorAll('.filter-chip').forEach(function (chip) {
      categoryNames[chip.getAttribute('data-category')] = chip.textContent.trim();
    });
  }

  function fetchIndex() {
    if (index) return Promise.resolve(index);
    return fetch(INDEX_URL)
      .then(function (r) { if (!r.ok) throw new Error('index ' + r.status); return r.json(); })
      .then(function (data) { index = data.apis || []; return index; })
      .catch(function () { index = []; return index; });
  }

  function esc(s) {
    return String(s || '').replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function scoreEntry(entry, terms) {
    var score = 0;
    var name = (entry.name || '').toLowerCase();
    var desc = (entry.description || '').toLowerCase();
    var tags = (entry.tags || []).join(' ').toLowerCase();
    var features = (entry.features || []).join(' ').toLowerCase();
    var category = (entry.category || '').toLowerCase();

    terms.forEach(function (t) {
      if (name === t) score += 60;
      else if (name.indexOf(t) === 0) score += 40;
      else if (name.indexOf(t) !== -1) score += 25;
      if (category.indexOf(t) !== -1) score += 12;
      if (tags.indexOf(t) !== -1) score += 10;
      if (features.indexOf(t) !== -1) score += 8;
      if (desc.indexOf(t) !== -1) score += 5;
    });
    return score;
  }

  function search(query) {
    var terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    var pool = index || [];
    var out = [];
    for (var i = 0; i < pool.length; i++) {
      var entry = pool[i];
      if (activeCategory && entry.category !== activeCategory) continue;
      if (terms.length) {
        var s = scoreEntry(entry, terms);
        if (s <= 0) continue;
        out.push({ entry: entry, score: s });
      } else {
        out.push({ entry: entry, score: 0 });
      }
    }
    out.sort(function (a, b) {
      return b.score - a.score || a.entry.name.localeCompare(b.entry.name);
    });
    return out.map(function (x) { return x.entry; });
  }

  function cardHtml(entry) {
    var desc = esc(entry.description || '');
    if (desc.length > 170) desc = desc.slice(0, 169) + '\u2026';
    var catName = esc(categoryNames[entry.category] || entry.category || '');
    var badges = '';
    if (entry.status && entry.status !== 'verified') {
      badges += '<span class="badge status-badge status-' + esc(entry.status) + '">' +
        esc(entry.status).replace('_', ' ') + '</span>';
    }
    if (entry.https) badges += '<span class="badge">HTTPS</span>';
    var auth = entry.auth && entry.auth !== 'unknown'
      ? '<span class="badge badge-auth">' + esc(entry.auth === 'none' ? 'No auth' : entry.auth) + '</span>' : '';
    return '<article class="api-card">' +
      '<div class="card-head"><h3 class="card-title"><a href="' + esc(entry.url) + '">' +
      esc(entry.name) + '</a></h3>' + auth + '</div>' +
      '<p class="card-desc">' + desc + '</p>' +
      '<div class="card-meta"><a class="chip" href="' + esc(entry.category_url) + '">' +
      catName + '</a>' + badges + '</div></article>';
  }

  function render(query) {
    var results = search(query);
    var q = query ? ' for \u201c' + query + '\u201d' : '';
    var cat = activeCategory ? ' in ' + (categoryNames[activeCategory] || activeCategory) : '';
    if (!index) {
      meta.textContent = 'Search index unavailable.';
      resultsBox.innerHTML = '';
      return;
    }
    if (results.length === 0) {
      meta.textContent = 'No results' + q + cat + '.';
      resultsBox.innerHTML = '<div class="search-empty">Try fewer words, a broader term, ' +
        'or <a href="../submit.html">submit the API</a> if it is missing.</div>';
      return;
    }
    meta.textContent = results.length + ' result' + (results.length === 1 ? '' : 's') + q + cat +
      (query ? '' : ' \u00b7 newest first');
    var html = '';
    results.slice(0, 60).forEach(function (entry) { html += cardHtml(entry); });
    resultsBox.innerHTML = html;
  }

  function updateUrl(query) {
    if (!window.history || !window.history.replaceState) return;
    var url = new URL(window.location.href);
    if (query) url.searchParams.set('q', query); else url.searchParams.delete('q');
    if (activeCategory) url.searchParams.set('category', activeCategory);
    else url.searchParams.delete('category');
    window.history.replaceState({}, '', url);
  }

  function run(query) {
    fetchIndex().then(function () { render(query); });
  }

  searchInput.addEventListener('input', function () {
    clearTimeout(debounceTimer);
    var value = searchInput.value.trim();
    debounceTimer = setTimeout(function () {
      run(value);
      updateUrl(value);
    }, 140);
  });

  if (filterRow) {
    filterRow.addEventListener('click', function (event) {
      var chip = event.target.closest('.filter-chip');
      if (!chip) return;
      var category = chip.getAttribute('data-category');
      activeCategory = (category === activeCategory) ? '' : category;
      filterRow.querySelectorAll('.filter-chip').forEach(function (c) {
        c.setAttribute('aria-pressed', c.getAttribute('data-category') === activeCategory ? 'true' : 'false');
      });
      run(searchInput.value.trim());
      updateUrl(searchInput.value.trim());
    });
  }

  searchInput.value = initialQuery;
  if (activeCategory && filterRow) {
    filterRow.querySelectorAll('.filter-chip').forEach(function (c) {
      c.setAttribute('aria-pressed', c.getAttribute('data-category') === activeCategory ? 'true' : 'false');
    });
  }
  run(initialQuery);
})();
