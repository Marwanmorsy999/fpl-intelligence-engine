/**
 * FPL Intelligence Engine — free egress proxy (Cloudflare Worker).
 *
 * The engine's egress mask chain (direct → allorigins → corsproxy → codetabs →
 * env_proxy) previously ended at the user's paid Apps-Script proxy. This
 * Worker is a free replacement for that final hop: host-allowlisted,
 * stateless passthrough with a 60s edge cache.
 *
 * Deploy in ~2 minutes (see docs/FPL_PROXY_WORKER.md), then point the engine
 * at it:
 *
 *   FPL_PROXY_URL=https://<your-worker>.<your-subdomain>.workers.dev
 *
 * The engine appends ?url=<encoded target> itself — this Worker only needs to
 * answer GET with an absolute https target on the allowlist.
 */

const ALLOWED_HOSTS = new Set([
  "fantasy.premierleague.com",
  "understat.com",
  "resources.premierleague.com",
]);

/** Edge-cache lifetime for successful upstream responses (seconds). */
const S_MAXAGE_SECONDS = 60;

/** Browser-like identity — the official FPL API rejects bot-looking clients. */
const UPSTREAM_HEADERS = {
  "user-agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  accept: "*/*",
};

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/**
 * Fetch the target through the 60s edge cache. The Cache API keeps one
 * response per exact target URL per edge POP; cf-cache-control tells the
 * CDN to honour the same lifetime for anonymous GETs.
 */
async function serve(targetUrl) {
  const cached = await caches.default.match(targetUrl);
  if (cached) {
    const headers = new Headers(cached.headers);
    headers.set("cf-cache-status", "HIT");
    return new Response(cached.body, { status: cached.status, headers });
  }

  const upstream = await fetch(targetUrl, { headers: UPSTREAM_HEADERS });
  const body = await upstream.arrayBuffer();
  const headers = new Headers(upstream.headers);
  headers.set("cf-cache-status", "MISS");
  headers.set("cf-cache-control", `public, s-maxage=${S_MAXAGE_SECONDS}`);
  const response = new Response(body, { status: upstream.status, headers });
  if (upstream.status < 400) {
    try {
      await caches.default.put(targetUrl, response.clone());
    } catch {
      // Opaque / uncachable upstream — the passthrough itself still works.
    }
  }
  return response;
}

export default {
  async fetch(request) {
    if (request.method !== "GET") {
      return json({ error: "method not allowed — GET only" }, 405);
    }

    let target = "";
    try {
      target = new URL(request.url).searchParams.get("url") || "";
    } catch {
      return json({ error: "malformed request URL" }, 400);
    }
    if (!target) {
      return json({ error: "pass ?url=<absolute https url>" }, 400);
    }

    let parsed;
    try {
      parsed = new URL(target);
    } catch {
      return json({ error: "?url must be an absolute https URL" }, 400);
    }
    if (parsed.protocol !== "https:" || !ALLOWED_HOSTS.has(parsed.hostname)) {
      // Host allowlist: this Worker is NOT a generic open proxy.
      return json({ error: `host not allowed: ${parsed.hostname || "(missing)"}` }, 403);
    }

    return serve(parsed.toString());
  },
};
