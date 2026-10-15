// Service worker: permite instalar la web como app y abrirla aunque no haya conexión
// (mostrando los últimos datos que se cargaron). Siempre intenta primero la red.
const CACHE = "stock-tcg-v9";
const BASE = ["./", "index.html", "manifest.webmanifest", "icono-30-192.png", "icono-30-512.png", "logo-30.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((claves) => Promise.all(claves.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const propio = url.origin === location.origin;
  const datos = url.pathname.endsWith("/datos/stock.json");
  if (e.request.method !== "GET" || !(propio || datos)) return; // fotos de las tiendas: sin tocar

  const clave = datos ? "datos" : e.request;
  // La página y sus archivos se piden siempre al servidor (sin la caché del navegador) para no ver versiones viejas
  const peticion = e.request.mode === "navigate"
    ? fetch(e.request.url, { cache: "no-cache", credentials: "same-origin" })
    : fetch(propio ? new Request(e.request, { cache: "no-cache" }) : e.request);
  e.respondWith(
    peticion
      .then((r) => {
        if (r.ok) { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(clave, copia)); }
        return r;
      })
      .catch(() => caches.match(clave).then((r) => r || caches.match("index.html")))
  );
});
