const CACHE_NAME = 'nexus-v1';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Canlı trading verisi için network-first
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
