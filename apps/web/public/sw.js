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
    // Network-first for the HTML shell so updates apply immediately; fall back to cache offline. The cached
    // copy follows the shell the network served last (its hashed assets were just loaded and cached), so a
    // page opened while the coordinator is down, e.g. the hub restarting for its own update, is the current
    // app with its recovery help, not the one from when this worker was first installed.
    event.respondWith(fetch(event.request).then((res) => {
      const type = res.headers.get("content-type") || "";
      if (res.ok && (url.pathname === "/" || url.pathname === "/index.html") && type.startsWith("text/html")) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put("/index.html", copy));
      }
      return res;
    }).catch(() => caches.match("/index.html")));
  }
});
