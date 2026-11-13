// Offline support: the app shell is cached; data and page are fetched fresh when online.
const V = 'energy-v2';
const SHELL = ['./', 'index.html', 'manifest.json', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.method !== 'GET') return;
  const u = new URL(r.url);
  const live = u.origin === location.origin &&
    (u.pathname.endsWith('data.json') || u.pathname.endsWith('/') || u.pathname.endsWith('index.html'));

  if (live) {
    // network first, so new data shows up; fall back to the cached copy offline
    e.respondWith(
      fetch(r)
        .then(res => { const copy = res.clone(); caches.open(V).then(c => c.put(r, copy)); return res; })
        .catch(() => caches.match(r))
    );
  } else {
    // cache first for icons and the Chart.js library
    e.respondWith(
      caches.match(r).then(hit => hit || fetch(r).then(res => {
        if (res.ok || res.type === 'opaque') { const copy = res.clone(); caches.open(V).then(c => c.put(r, copy)); }
        return res;
      }))
    );
  }
});
