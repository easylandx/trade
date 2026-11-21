// Service worker mínimo: nunca guarda /api/ (los datos siempre en vivo); solo la "carcasa" de la página.
const C = 'radar-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(k => Promise.all(k.filter(x => x !== C).map(x => caches.delete(x)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || u.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const c = r.clone(); caches.open(C).then(x => x.put(e.request, c)); }
    return r;
  }).catch(() => caches.match(e.request)));
});
