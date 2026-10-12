/* HomeMesh AI service worker.
 * Caches only the static application shell so the app opens quickly and shows a clear
 * "coordinator unreachable" state when offline. API responses (conversations, memories,
 * documents) are NEVER cached: personal data stays on the coordinator.
 */
const CACHE = "homemesh-shell-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest", "/icons/icon.svg", "/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // never intercept API traffic
  if (url.pathname.startsWith("/assets/")) {
    // Hashed, immutable build assets: cache-first.
    event.respondWith(
      caches.match(event.request).then((hit) => hit || fetch(event.request).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(event.request, copy)); }
        return res;
      })),
    );
    return;
  }
  if (event.request.mode === "navigate") {
    // Network-first for the HTML shell so updates apply immediately; fall back to cache offline.
    event.respondWith(fetch(event.request).catch(() => caches.match("/index.html")));
  }
});
