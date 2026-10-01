/* Enhance the static release snapshot without delaying the page. */
(function () {
  'use strict';

  var api = 'https://api.github.com/repos/RISE-X-Lab/OpenCollab/releases/latest';
  var releaseBase = 'https://github.com/RISE-X-Lab/OpenCollab/releases/tag/';
  var cacheKey = 'opencollab-latest-release';
  var cacheLifetime = 60 * 60 * 1000;
  var versionNodes = document.querySelectorAll('[data-release-version]');
  var dateNode = document.getElementById('release-date');
  if (!versionNodes.length || !dateNode) return;

  var fallbackVersion = versionNodes[0].textContent.trim();

  function releaseData(value) {
    if (!value || typeof value.tag_name !== 'string' ||
        !/^v?\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(value.tag_name) ||
        typeof value.published_at !== 'string' ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value.published_at) ||
        !Number.isFinite(Date.parse(value.published_at))) return null;
    return {tag_name: value.tag_name, published_at: value.published_at};
  }

  function predatesSnapshot(tag) {
    var current = tag.replace(/^v/, '').split('.').map(Number);
    var snapshot = fallbackVersion.replace(/^v/, '').split('.').map(Number);
    for (var i = 0; i < 3; i++) {
      if (current[i] !== snapshot[i]) return current[i] < snapshot[i];
    }
    return false;
  }

  function render(release) {
    var date = new Intl.DateTimeFormat('en-US', {
      month: 'short', day: 'numeric', year: 'numeric', timeZone: 'Asia/Shanghai'
    }).format(new Date(release.published_at));
    var url = releaseBase + encodeURIComponent(release.tag_name);
    versionNodes.forEach(function (node) { node.textContent = release.tag_name; });
    document.querySelectorAll('[data-release-link]').forEach(function (node) {
      node.href = url;
    });
    dateNode.textContent = date;
    dateNode.dateTime = release.published_at;
  }

  var cached;
  try {
    cached = JSON.parse(localStorage.getItem(cacheKey));
    var known = releaseData(cached);
    if (known && !predatesSnapshot(known.tag_name)) {
      render(known);
      var age = Date.now() - cached.checked_at;
      if (Number.isFinite(cached.checked_at) && age >= 0 && age < cacheLifetime) return;
    }
  } catch (error) { /* Storage is optional, including in private browsing. */ }

  if (typeof fetch !== 'function' || typeof AbortController !== 'function') return;
  var controller = new AbortController();
  var timeout = setTimeout(function () { controller.abort(); }, 4000);
  fetch(api, {
    headers: {'Accept': 'application/vnd.github+json'},
    credentials: 'omit',
    signal: controller.signal
  }).then(function (response) {
    if (!response.ok) throw new Error('Release request unavailable');
    return response.json();
  }).then(function (value) {
    var release = releaseData(value);
    if (!release || value.draft !== false || value.prerelease !== false ||
        predatesSnapshot(release.tag_name)) return;
    render(release);
    try {
      localStorage.setItem(cacheKey, JSON.stringify({
        tag_name: release.tag_name,
        published_at: release.published_at,
        checked_at: Date.now()
      }));
    } catch (error) { /* The refreshed page works without persistent storage. */ }
  }).catch(function () {
    /* Keep the static snapshot or last known release when the request fails. */
  }).finally(function () {
    clearTimeout(timeout);
  });
})();
