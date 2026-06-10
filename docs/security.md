# Security model

Design constraint: security must not tax the hot paths. Every mechanism
below costs at most one constant-time comparison per request or per
connection — fusion, polling, and rendering are untouched.

## Threat model

The tracker knows where your keys, wallet, phone, and you (presence) are,
live and historically. Treat the API as sensitive. Assumed attacker: a
device on the same LAN (guest Wi-Fi, compromised IoT gadget). Out of
scope: a compromised tracker host itself, physical attackers, and
malicious code in the optional CDN-loaded 3D renderer (vendor the modules
locally if that matters to you).

## HTTP API

- **Bearer tokens, two roles** (`api.auth.tokens`): `viewer` reads,
  `admin` reads and writes (tagging, splat upload, transform). Token
  lookup checks every configured token with `hmac.compare_digest` and no
  early exit — no timing signal for token discovery.
- **Secrets never live in config literals**: each token spec is `env:` or
  `file:` (or `token:` for throwaway setups). Token files should be 0600.
- **Secure-by-default fallback**: with no tokens configured, reads stay
  open but writes are refused from non-loopback peers — an unconfigured
  tracker on a LAN cannot have its scan or tags overwritten remotely.
- `/` (static dashboard shell) and `/health` are always served; the
  dashboard prompts for a token on its first 401 and stores it in the
  browser.
- GET also accepts `?token=` for clients that cannot set headers (the
  splat viewer's internal fetch). Use a *viewer* token there: query
  strings end up in access logs.
- Upload hardening: splat uploads are size-capped (2 GiB), streamed in
  1 MiB chunks, written to a temp file, and atomically renamed — a
  truncated or oversized upload never corrupts the live asset.
- **TLS**: deliberately not in-process. Bind to 127.0.0.1 (default) and
  put caddy/nginx with TLS + HTTP auth in front for any remote access.
  A reverse proxy also adds rate limiting and access logs for free.

## MCU network bridge

- **Connection-level shared secret** (`auth_token` on `network_bridge`):
  the first line of a connection must be `{"auth": "<token>"}` or the
  socket is closed. One constant-time check per connection, zero
  per-message cost — an ESP32 pays nothing after connect. Rejected
  connections are counted (`rejected_connections`).
- **Input bounds**: lines are capped at 64 KiB; oversized pre-auth input
  hangs up immediately. Malformed lines are dropped and counted, never
  fatal.
- **MCU clocks are never trusted** — timestamps are assigned on receipt,
  so a node cannot backdate or future-date observations to poison
  history/replay.
- The transport is plain TCP by design (MCU-friendly). Run nodes on a
  segmented IoT VLAN; for genuinely hostile networks, tunnel the bridge
  port over WireGuard instead of inventing per-message crypto the AVR
  nodes can't afford.
- Firmware reference: set `BRIDGE_TOKEN` via the environment at build
  time (`firmware/esp32-ble-scanner`); credentials are never committed.

## Camera streams

`stream_url` (MJPEG) is served by the cameras themselves, outside this
system. Prefer camera firmware with auth, or proxy the streams through
the same reverse proxy that fronts the API.

## Operational checklist

1. Generate tokens: `openssl rand -hex 32` (one admin, one viewer).
2. Export them via env/units (`TRACKER_ADMIN_TOKEN=...`) or 0600 files.
3. Set `network_bridge.auth_token`; rebuild nodes with `BRIDGE_TOKEN`.
4. Keep `api.host: 127.0.0.1`; add a TLS reverse proxy for remote access.
5. Put cameras and MCU nodes on an IoT VLAN that can reach only the
   tracker's bridge port.
6. Rotate: tokens are read at startup — update env/file and restart;
   restore from `state_path` makes restarts lossless.
