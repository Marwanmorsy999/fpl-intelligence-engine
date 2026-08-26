/* Phase 3.3 - IndexedDB cache w/ TTL + graceful degradation (no deps).
 * Fresh hit (<TTL) -> cached, zero network. Miss/expired/ANY storage error
 * -> bounded network fetch, stored best-effort; never crashes.
 * Browser: window.FPLCache. Node: CommonJS export. */
"use strict";
(function (root, factory) {
  var api = factory(root);
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.FPLCache = api;
  }
})(typeof self !== "undefined" ? self : this, function (root) {
  var DEFAULT_TTL_MS = 24 * 60 * 60 * 1000; /* spec: 24h */
  var DB_NAME = "fpl-cache";
  var DB_VERSION = 1;
  var STORE_NAME = "predictions";

  function createMemoryStore() {
    var map = new Map();
    return {
      kind: "memory",
      get: function (key) {
        return Promise.resolve(map.has(key) ? map.get(key) : undefined);
      },
      put: function (key, value) {
        map.set(key, value);
        return Promise.resolve(value);
      }
    };
  }

  function createIdbStore(idbFactory) {
    function openDb() {
      return new Promise(function (resolve, reject) {
        var req = idbFactory.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = function () {
          var db = req.result;
          if (!db.objectStoreNames.contains(STORE_NAME)) {
            db.createObjectStore(STORE_NAME);
          }
        };
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { reject(req.error || new Error("indexedDB open failed")); };
        req.onblocked = function () { reject(new Error("indexedDB open blocked")); };
      });
    }

    function run(mode, action) {
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          var tx = db.transaction(STORE_NAME, mode);
          var store = tx.objectStore(STORE_NAME);
          var req = action(store);
          req.onsuccess = function () { resolve(req.result); };
          req.onerror = function () { reject(req.error || new Error("indexedDB request failed")); };
        }).then(
          function (value) { db.close(); return value; },
          function (err) { try { db.close(); } catch (e) {} throw err; }
        );
      });
    }

    return {
      kind: "indexeddb",
      get: function (key) {
        return run("readonly", function (store) { return store.get(String(key)); });
      },
      put: function (key, value) {
        return run("readwrite", function (store) { return store.put(value, String(key)); });
      }
    };
  }

  function hasIdb() {
    return !!(root && root.indexedDB && typeof root.indexedDB.open === "function");
  }

  /* Resilient store: primary IndexedDB, any failure -> in-memory fallback. */
  function createResilientStore(options) {
    var opts = options || {};
    var primary = null;
    var secondary = createMemoryStore();

    function pickStore() {
      if (primary !== null) return Promise.resolve(primary);
      if (opts.store) {
        primary = opts.store;
        return Promise.resolve(primary);
      }
      if (!opts.forceMemory && hasIdb()) {
        try {
          primary = createIdbStore(root.indexedDB);
          return Promise.resolve(primary);
        } catch (e) {
          primary = null;
        }
      }
      primary = secondary;
      return Promise.resolve(primary);
    }

    return {
      kind: "resilient",
      get: function (key) {
        return pickStore()
          .then(function (store) {
            return store.get(key).catch(function () { return secondary.get(key); });
          })
          .catch(function () { return secondary.get(key); });
      },
      put: function (key, value) {
        return pickStore()
          .then(function (store) {
            return store.put(key, value).catch(function () { return secondary.put(key, value); });
          })
          .catch(function () { return secondary.put(key, value); });
      }
    };
  }

  function isFresh(envelope, ttlMs, now) {
    var clock = typeof now === "number" ? now : Date.now();
    return !!(envelope && typeof envelope === "object" &&
      typeof envelope.timestamp === "number" && clock - envelope.timestamp < ttlMs);
  }

  function createCache(options) {
    var opts = options || {};
    var store = createResilientStore(opts);
    var defaultTtl = opts.defaultTtlMs || DEFAULT_TTL_MS;

    function peek(key, ttlMs, now) {
      return store.get(key)
        .then(function (envelope) {
          return isFresh(envelope, ttlMs || defaultTtl, now) ? envelope.data : null;
        })
        .catch(function () { return null; });
    }

    function save(key, data, now) {
      return store.put(key, { data: data, timestamp: typeof now === "number" ? now : Date.now() })
        .then(function () { return true; })
        .catch(function () { return false; });
    }

    /* Fresh hit -> cached data; else fetcher (stored best-effort).
       Fetcher errors PROPAGATE - caching never invents data. */
    function getOrFetch(key, fetcher, ttlMs) {
      return peek(key, ttlMs).then(function (fresh) {
        if (fresh !== null) return fresh;
        return Promise.resolve()
          .then(fetcher)
          .then(function (data) {
            return save(key, data).then(function () { return data; });
          });
      });
    }

    return { peek: peek, save: save, getOrFetch: getOrFetch };
  }

  var shared = createCache();

  function getCachedPredictions(gameweek, fetchPredictionsFromAPI, ttlMs) {
    return shared.getOrFetch("predictions:" + gameweek,
      fetchPredictionsFromAPI, typeof ttlMs === "number" ? ttlMs : DEFAULT_TTL_MS);
  }

  return {
    DEFAULT_TTL_MS: DEFAULT_TTL_MS,
    DB_NAME: DB_NAME,
    STORE_NAME: STORE_NAME,
    createIdbStore: createIdbStore,
    createMemoryStore: createMemoryStore,
    createResilientStore: createResilientStore,
    isFresh: isFresh,
    createCache: createCache,
    shared: shared,
    getCachedPredictions: getCachedPredictions
  };
});