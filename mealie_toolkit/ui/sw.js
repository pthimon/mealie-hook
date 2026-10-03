// Minimal service worker: it makes the Toolkit installable as its own app. Requests always
// go to the network, so the page and its data are never served stale.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => e.respondWith(fetch(e.request)));
