# SujalConnect — GitHub → Vercel deployment

## Commit these files
- `app.py`
- `server.py`
- `music.py`
- `index.html`
- `album.html`
- `playlist.html`
- `requirements.txt`
- `pyproject.toml`
- `vercel.json`
- `.gitignore`
- `.vercelignore`
- README/docs

Do not commit `vendor/`, `cache/`, `data/`, `.env*`, `__pycache__/`, or `.vercel/`.

## Why there is no `vendor/` folder
The app now depends on the official `deno` Python package. It installs a platform-appropriate Deno binary during the Vercel Python dependency install and exposes it through `deno.find_deno_bin()`. This avoids GitHub's single-file limits while satisfying yt-dlp's current JavaScript runtime requirement.

## Vercel settings
- Framework preset: Other
- Build command: empty/default
- Output directory: empty/default
- Install command: default
- Python: 3.12

## Playback behavior
SujalConnect first attempts direct audio extraction. If the server cannot resolve a track, the API returns an official YouTube embed fallback instead of producing a 404 and skipping. This is important because a shared cloud IP can receive an anti-bot response even when the same video works in a normal browser.

## Diagnostics
- `/api/health`
- `/api/runtime`
- `/api/stream-debug/<VIDEO_ID>`
