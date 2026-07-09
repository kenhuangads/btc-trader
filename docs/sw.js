/* PWA service worker：殼層快取＋資料網路優先（離線時退回快取） */
const VER = "btc-trader-v3";
const SHELL = ["./", "./index.html", "./styles.css", "./app.js", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VER).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== VER).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return; // 交易所即時價等外部請求不攔
  if (url.pathname.includes("/data/")) {
    e.respondWith(
      fetch(e.request).then(r => {
        const cp = r.clone();
        caches.open(VER).then(c => c.put(e.request, cp));
        return r;
      }).catch(() => caches.match(e.request, { ignoreSearch: true }))
    );
  } else {
    e.respondWith(
      caches.match(e.request, { ignoreSearch: true }).then(hit => hit ||
        fetch(e.request).then(r => {
          const cp = r.clone();
          caches.open(VER).then(c => c.put(e.request, cp));
          return r;
        }))
    );
  }
});
