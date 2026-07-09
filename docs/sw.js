/* PWA service worker：一律網路優先，快取僅作離線備援（避免更新後拿到舊版頁面） */
const VER = "btc-trader-v5";
const SHELL = ["./", "./index.html", "./styles.css", "./app.js", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VER).then(c => c.addAll(SHELL)).catch(() => null).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== VER).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin || e.request.method !== "GET") return; // 外部請求（交易所報價等）不攔
  e.respondWith(
    fetch(e.request).then(r => {
      if (r && r.ok) {
        const cp = r.clone();
        caches.open(VER).then(c => c.put(e.request, cp));
      }
      return r;
    }).catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
