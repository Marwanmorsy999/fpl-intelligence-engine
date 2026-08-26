# Cloudflare Setup (free tier)

The FPL Intelligence Engine is served by **Vercel** (the app + its API).
Cloudflare sits in front as a **free reverse proxy + CDN** for the static
dashboard and to extend the edge-cache lifetime of read-only API responses.
No Workers KV, no paid rules — just DNS + the free WAF/CDN.

## 1. Point your domain at Vercel via Cloudflare

1. Add your domain in the [Cloudflare dashboard](https://dash.cloudflare.com).
2. Change the domain's nameservers to the two Cloudflare-assigned NS records
   (provided in the dashboard). This can take up to 24h but usually ~5 min.
3. In **Cloudflare DNS**, add a CNAME for the root/APEX and `www` pointing at
   your Vercel alias, with the **orange cloud toggled ON** (proxied):
   - `yourdomain.com` → CNAME → `your-vercel-deployment.vercel.app` (Proxied)
   - `www.yourdomain.com` → CNAME → `your-vercel-deployment.vercel.app` (Proxied)

   Proxied = traffic terminates at Cloudflare first, so the free CDN + WAF apply.

## 2. What Cloudflare caches

The app already emits the right `Cache-Control` headers via
`src/fpl_intelligence/api/cache.py` (`EdgeCachePolicyMiddleware`):

| Response type | Header | Cloudflare behaviour |
|----------------|--------|-----------------------|
| Public read-only API (health, players, decisions for a *public* squad, news) | `public, max-age=60, stale-while-revalidate=300` | Served from Cloudflare edge for 60s, stale served up to 300s |
| Squad/league/decisions that are session-scoped | `private, no-store` | Hard pass-through, never cached at the edge |
| Static dashboard assets | `public, max-age=31536000, immutable` | Cached at the edge for a year |

So **no Cloudflare Page Rules are required** — the origin sets the policy and
Cloudflare respects it. The free WAF ("managed challenge" rule) can be enabled
on `/` → `/` without cost.

## 3. TLS (free)

Cloudflare terminates TLS for free. After pointing DNS at Cloudflare, the
"Always Use HTTPS" and "Automatic HTTPS Rewrites" options (both free) ensure
every request is upgraded to HTTPS and never served over plain HTTP.

## 4. Orange-to-Orange (O2O) — Vercel + Cloudflare

Vercel is itself behind Cloudflare-compatible infra, so the standard CNAME
setup just works — no special proxying of the Vercel domain is needed. Keep
**`proxy status = Proxied`** (orange cloud) on the DNS records you want the
free CDN/WAF in front of, and toggle to **DNS-only** (grey cloud) if you ever
need a raw WebSocket/bypass for debugging.

## 5. Verify

After DNS + TLS are live:

```bash
curl -I https://yourdomain.com/api/v1/health
# expect: Cache-Control: public, max-age=60, stale-while-revalidate=300
#         via: cloudflare
curl -I https://yourdomain.com/dashboard
# expect: Cache-Control: public, max-age=31536000, immutable
```

If you see `server: cloudflare` in the headers, the free edge is active and
serving the current codebase.
