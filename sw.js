// sw.js — CTE service worker (rewritten 2026-09-14)
//
// ROOT CAUSE OF THE RECURRING "site shows old content after deploy" COMPLAINTS:
// index.html registers this file at root scope on every load (`navigator.serviceWorker.register
// ('sw.js')`, see the bottom of index.html). Whatever version of this file was live the FIRST time
// a visitor's browser installed it keeps running in that browser — a service worker's *installed*
// bytes, not the current file on the server, are what execute — until something makes it update.
// The live copy of this file had somehow ended up completely EMPTY (0 bytes, checked 2026-09-14),
// which meant it had no logic at all going forward, but every browser that had already installed an
// OLDER, non-empty version (almost certainly one that cached pages aggressively for the offline/
// "add to home screen" PWA behaviour) stayed stuck running that OLD cached-fetch logic forever —
// completely ignoring the Cache-Control header fix in firebase.json, because Service Worker Cache
// Storage is a separate layer from the HTTP cache. That is why the merged chart, the tile prices,
// the legend etc. kept not showing up live even after a correct deploy: returning visitors' browsers
// were serving a self-cached copy of index.html from days ago and never even asking the server.
//
// FIX: this file is intentionally a one-time "self-destruct" worker. Every browser still running an
// old version will detect these bytes differ (Chrome checks on every navigation) and install THIS
// version; skipWaiting() below makes it take over immediately instead of waiting for tabs to close;
// activate then wipes every Cache Storage bucket this origin owns and unregisters itself, handing
// control straight back to plain network requests (i.e. back to the Cache-Control header in
// firebase.json actually mattering again). It also force-reloads any open tab so the fix is visible
// without the user having to manually refresh. After this has rolled out to everyone once, a future
// deploy can replace this with real offline/PWA caching logic if that's wanted — just make sure
// whatever replaces it bumps a cache-name version string and prunes old caches on every activate, so
// this exact problem can't happen again.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.registration.unregister();
    const clientsList = await self.clients.matchAll({ type: 'window' });
    for (const client of clientsList) {
      client.navigate(client.url);
    }
  })());
});

// No 'fetch' handler on purpose — once activated, this worker gets out of the way entirely and
// every request goes straight to the network (and normal HTTP caching), rather than this worker
// intercepting and answering requests itself.
