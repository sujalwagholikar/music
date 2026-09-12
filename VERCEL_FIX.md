# SujalConnect — Vercel hardening summary

This version includes the original startup fix plus playback and serverless hardening.

## Deployment/runtime fixes

- No runtime directory creation beside the deployed source bundle.
- Cache and JSON user data use `/tmp/sujalconnect` on Vercel.
- Root-level `app.py` is the FastAPI entrypoint.
- Vercel function duration is configured to 300 seconds.
- Blocking Python work is exposed through synchronous FastAPI handlers so FastAPI can run it in its worker pool.
- CORS is disabled by default for same-origin deployment and can be enabled with `CORS_ORIGINS` when a split frontend is used.

## Playback fixes

- Current `yt-dlp[default]` with YouTube EJS support.
- No hard-coded obsolete YouTube player client list.
- Fresh media URL resolution with a short TTL.
- Per-song resolution locking to prevent duplicate extractor calls.
- Safe `User-Agent` / `Referer` / `Origin` headers from yt-dlp are retained server-side.
- Audio proxy retries a fresh URL on stale/rejected upstream responses.
- Proper HTTP 206 and 416 handling.
- Bounded 2 MiB range responses for long serverless playback sessions.
- Correct content type propagation instead of always claiming `audio/mp4`.
- Frontend retries a failed track once before skipping.

## Persistence caveat

Vercel `/tmp` storage is ephemeral. Likes, history, users and caches are therefore runtime-local. For durable multi-instance production persistence, move the `UserStore` and shared cache to a managed database/cache.
