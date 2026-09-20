// War_ning – Service Worker (red primero, con tiempo límite y respaldo en caché)
const CACHE_NAME = 'warning-pwa-v2';
const NETWORK_TIMEOUT_MS = 4000; // con red mala, tras 4 s se sirve lo cacheado
const ASSETS_TO_CACHE = [
  './',
  './index.html',
  './manifest.json',
  './data/levels.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // Cada recurso por separado: si uno falta (p. ej. un icono), la instalación no se cae
      Promise.all(ASSETS_TO_CACHE.map((url) => cache.add(url).catch(() => {})))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

function fromCache(request) {
  return caches.match(request).then((hit) => {
    if (hit) return hit;
    if (request.mode === 'navigate') return caches.match('./index.html');
    return undefined;
  });
}

function networkFirst(request) {
  return new Promise((resolve) => {
    let settled = false;

    const timer = setTimeout(() => {
      fromCache(request).then((hit) => {
        if (hit && !settled) { settled = true; resolve(hit); }
      });
    }, NETWORK_TIMEOUT_MS);

    fetch(request)
      .then((res) => {
        clearTimeout(timer);
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(request, copy)).catch(() => {});
        }
        if (!settled) { settled = true; resolve(res); }
      })
      .catch(() => {
        clearTimeout(timer);
        if (settled) return;
        fromCache(request).then((hit) => {
          settled = true;
          resolve(hit || new Response('Sin conexión y sin copia en caché.', {
            status: 503,
            headers: { 'Content-Type': 'text/plain; charset=utf-8' }
          }));
        });
      });
  });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                       // no cachear POST, etc.
  const url = new URL(req.url);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
  if (url.origin !== self.location.origin) return;        // solo recursos propios
  event.respondWith(networkFirst(req));
});
