/* Service worker for the Owner Portal PWA.

   Deliberately conservative: the app shell (CSS, JS, icons, offline page) is
   cached so the portal opens on a weak signal, but every navigation and every
   API call goes to the network first. Water readings must never be served
   from a stale cache - showing yesterday's pressure as if it were live would
   be worse than showing nothing.
*/

const CACHE = 'shl-shell-v1';
const SHELL = [
  '/static/css/app.css',
  '/static/js/app.js',
  '/static/js/chart.js',
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
