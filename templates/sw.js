/* Service worker for the Owner Portal PWA.

   Deliberately conservative: the app shell (CSS, JS, icons, offline page) is
   cached so the portal opens on a weak signal, but every navigation and every
   API call goes to the network first. Water readings must never be served
   from a stale cache - showing yesterday's pressure as if it were live would
   be worse than showing nothing.
*/

/* The cache name carries a fingerprint of the cached files (see
   core.context_processors.asset_version). Editing app.css or app.js changes
   the name, so this worker installs a fresh cache and activate() deletes the
   stale one - without it, cache-first would keep serving the old stylesheet
   indefinitely. */
const CACHE = 'shl-shell-{{ ASSET_VERSION }}';
/* Precached with the same ?v= the pages request, so these entries are the
   ones the fetch handler actually matches. */
const SHELL = [
  '/static/css/app.css?v={{ ASSET_VERSION }}',
  '/static/js/app.js?v={{ ASSET_VERSION }}',
  '/static/js/chart.js?v={{ ASSET_VERSION }}',
  '/static/img/icon.svg',
  '/offline/'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Live data is never cached.
  if (url.pathname.startsWith('/api/')) return;

  // Navigations: network first, offline page as the fallback.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/offline/'))
    );
    return;
  }

  // Static assets: cache first, refreshed in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        const network = fetch(request).then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        }).catch(() => cached);
        return cached || network;
      })
    );
  }
});
