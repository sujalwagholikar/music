# SujalConnect — Vercel deployment notes

## What this package fixes

- Vercel-safe runtime/cache paths under `/tmp`
- No runtime writes beside the deployed source
- Modern zero-config FastAPI entrypoint (`app.py`)
- `yt-dlp[default]` with current YouTube EJS support
- Fresh stream URL resolution with per-video locking
- Forwarding of safe `yt-dlp` media headers
- Correct 200/206/416/4xx handling in the audio proxy
- Automatic retry/re-resolution after stale upstream media URLs
- Bounded byte-range proxy responses to reduce long-lived serverless requests
- Frontend playback retry before skipping a track
- Configurable CORS only when `CORS_ORIGINS` is set
- Vercel function max duration set to 300 seconds
- Runtime diagnostics at `/api/runtime`

## Deploy

1. Upload this folder to a Git repository and import it into Vercel, or run `vercel` from the project root.
2. Use the default Python runtime detected from `app.py` and `pyproject.toml`.
3. Deploy.

## Smoke test after deployment

- `/api/health`
- `/api/runtime`
- `/api/search?q=Chinnamma`
- Search a song and press play.
- Verify the browser Network tab shows `/api/proxy-audio/...` returning `206 Partial Content` for range requests.

## Important persistence note

Guest accounts, likes, history and JSON caches are intentionally stored under `/tmp` on Vercel. This avoids deployment crashes, but `/tmp` is ephemeral. A production multi-user app should move `UserStore` and shared cache data to a managed database/Redis later.
