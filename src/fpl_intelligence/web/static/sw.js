/* Phase 24 — PWA service worker: cache shell + offline fallback */
"use strict";
var CACHE = "fpl-intel-v24-1";
var SHELL = [
  "/",
  "/dashboard",
  "/static/app.css",
  "/static/app.js",
  "/static/manifest.json",
  "/static/offline.html",
  "/static/icon-192.png",
  "/static/icon-512.png"
];
self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); })
  );
});
self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});
self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  // never cache API or health – always network-first
  if (req.url.indexOf("/api/") !== -1 || req.url.indexOf("/health") !== -1) {
    e.respondWith(fetch(req).catch(function () { return caches.match("/static/offline.html"); }));
    return;
  }
  e.respondWith(
    caches.match(req).then(function (cached) {
      var fetchPromise = fetch(req).then(function (resp) {
        // cache shell assets on success
        if (resp.ok && req.url.indexOf("/static/") !== -1) {
          var clone = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(req, clone); });
        }
        return resp;
      }).catch(function () {
        if (cached) return cached;
        // navigation fallback
        if (req.mode === "navigate") return caches.match("/static/offline.html");
        return new Response("", { status: 504 });
      });
      return cached || fetchPromise;
    })
  );
});
