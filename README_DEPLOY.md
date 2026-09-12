# SujalConnect — Vercel deployment

## What changed
- Added `app.py` as the Vercel FastAPI entrypoint.
- Added `pyproject.toml` with Python 3.12+ dependencies.
- Added `vercel.json` security headers.
- Moved runtime-writable cache/user data to `/tmp/sujalconnect` on Vercel.
- Kept local development behavior unchanged.

## Deploy
1. Install Vercel CLI: `npm i -g vercel`
2. From this directory run: `vercel`
3. For production: `vercel --prod`

## Important production note
The app currently stores users, likes, history, and metadata caches in process-local memory plus `/tmp`. Vercel function instances are ephemeral and can scale horizontally, so this is suitable for a demo/prototype but **not durable multi-user production storage**. Replace `UserStore` and the metadata/album cache with a managed database/cache before treating the app as production-grade.

The audio proxy and yt-dlp work are also compute/network intensive. Test your Vercel plan's function duration and traffic limits before relying on it for high-volume playback.
