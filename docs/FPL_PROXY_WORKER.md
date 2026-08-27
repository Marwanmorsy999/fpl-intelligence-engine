# Free egress proxy — Cloudflare Worker (2-minute deploy)

The engine's egress mask chain ends with the user-supplied `FPL_PROXY_URL`
mask. Historically that meant a paid Apps-Script proxy; the Worker in
[`scripts/cloudflare_worker_fpl_proxy.js`](../scripts/cloudflare_worker_fpl_proxy.js)
is a **free** drop-in replacement for that final hop.

## What it is

* A **host-allowlisted** passthrough proxy — `fantasy.premierleague.com`,
  `understat.com`, `resources.premierleague.com` only (it is *not* a generic
  open proxy).
* `GET <worker>/...?url=<absolute https target>` → upstream body, unmodified.
* **60s edge cache** (`cf-cache-control: public, s-maxage=60` + Cache API) so
  repeated bootstrap/squad reads cost zero upstream hits within a minute.
* Zero bindings, zero secrets — no dashboard env vars needed.

## Deploy (Cloudflare Workers — free tier is plenty)

### Option A — wrangler CLI (fastest)

```bash
npm create cloudflare@latest fpl-proxy -- --type=hello-world   # or any scaffold
cd fpl-proxy
cp ../cloudflare_worker_fpl_proxy.js src/index.js              # your worker file
npx wrangler login
npx wrangler deploy
```

Copy the printed URL, e.g.
`https://fpl-proxy.<your-subdomain>.workers.dev`.

### Option B — Cloudflare dashboard

1. **Workers & Pages → Create application** → pick any starter.
2. Replace `src/index.js` with the contents of
   `scripts/cloudflare_worker_fpl_proxy.js`.
3. **Deploy**. Copy the Worker URL from the overview page.

## Point the engine at it

Set the Worker URL as the engine's proxy environment variable (Vercel
project env, `docker-compose`, or your `.env`):

```bash
FPL_PROXY_URL=https://fpl-proxy.<your-subdomain>.workers.dev
```

The engine strips any trailing `?url=` and appends the encoded target
itself, so no other configuration is required. The mask chain now runs:

```
direct → allorigins → corsproxy → codetabs → FPL_PROXY_URL (your free Worker)
```

## Verify

```bash
# Allowlisted host (must 200 with JSON):
curl "https://fpl-proxy.<you>.workers.dev/?url=https%3A%2F%2Ffantasy.premierleague.com%2Fapi%2Fbootstrap%2F"

# Non-allowlisted host (must 403 "host not allowed"):
curl "https://fpl-proxy.<you>.workers.dev/?url=https%3A%2F%2Fexample.com%2F"
```

In the app, **Sources → mask health** will then show `env_proxy` (your Worker)
as `ok` on a fresh probe.

## Notes & limits

* Free tier: 100k requests/day — far more than one engine instance needs
  (the 60s cache keeps hot paths near a handful of upstream calls).
* Only `GET` is served (405 otherwise) — the engine's egress chain is GET-only.
* To change the allowlist, edit `ALLOWED_HOSTS` at the top of the Worker and
  redeploy.
