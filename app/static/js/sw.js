// Minimal service worker: exists only to make the app installable as a PWA
// (older Chromium versions require a fetch handler for the install prompt).
// It deliberately caches NOTHING — the dashboard is tiny, always served from
// the local network, and stale invoice data would be worse than a spinner.
// Served from the app root (see /sw.js in app/server.py) so its scope covers
// the whole app.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {
    // No respondWith(): the browser performs the request as if no service
    // worker existed (normal network behavior, including Basic Auth).
});
