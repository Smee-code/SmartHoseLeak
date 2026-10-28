/* Service worker for the app - every role installs the same one.

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

  // Static assets: network first so code updates take effect immediately;
  // fall back to the cached shell when offline.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      fetch(request).then((response) => {
        if (response && response.status === 200) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      }).catch(() => caches.match(request))
    );
  }
});


/* ---------------------------------------------------------------- push
   A notification arrives here even when no tab is open - that is the whole
   point of it, and it is why this lives in the worker rather than in app.js.
   -------------------------------------------------------------------------- */

self.addEventListener('push', (event) => {
  /* Never throw. A push handler that fails leaves the browser to show its own
     "This site has been updated in the background" notice, which tells a
     household nothing and looks like a fault. A bad payload falls back to a
     message that is at least true. */
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = {};
  }

  const title = data.title || 'Smart Hose Leak';
  const options = {
    body: data.body || 'There is news about your water supply.',
    icon: '/static/img/icon-192.png',
    badge: '/static/img/icon-192.png',
    /* Phones sleep. A board that flaps between leak and normal would
       otherwise stack a column of notifications nobody reads; one tag means
       the latest replaces the last. renotify so a genuinely new alert still
       buzzes rather than swapping silently. */
    tag: 'shl-water',
    renotify: true,
    data: { url: data.url || '/portal/' }
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/post-login/';

  /* Focus the portal if it is already open rather than stacking another
     window on top of it. Somebody who left the app open and got an alert
     should be taken to the page, not given a second copy of the app. */
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if ('focus' in client) {
          client.navigate(target).catch(() => {});
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});

/* The push service can rotate a subscription on its own. When it does the old
   endpoint stops working, so the new one has to reach the server or that
   handset goes quiet without anybody noticing. */
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(
    self.registration.pushManager
      .subscribe({ userVisibleOnly: true, applicationServerKey: event.oldSubscription?.options?.applicationServerKey })
      .then((subscription) => fetch('/api/push/subscribe/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(subscription)
      }))
      .catch(() => {})
  );
});
