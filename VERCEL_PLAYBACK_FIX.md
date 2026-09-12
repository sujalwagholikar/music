# SujalConnect — Vercel playback fix (v3)

## Root cause addressed

The production logs showed repeated `/api/stream/{video_id}` 404 responses. Those 404s were emitted by SujalConnect itself because `yt-dlp` could not resolve a direct media URL on the Vercel Python runtime.

Current yt-dlp YouTube extraction requires a supported external JavaScript runtime. Local development already had Node.js, while the Vercel Python runtime did not reliably provide one on PATH. The app therefore failed inside `yt-dlp` before a stream URL could ever be returned.

This version ships a Node.js v22 runtime in `vendor/node` and explicitly configures yt-dlp to use it. The deployed function also reports whether that runtime is present at `/api/runtime` and `/api/stream-debug/{id}`.

## Playback changes

- Bundled Node.js v22 runtime for yt-dlp YouTube EJS challenge solving.
- Automatic fallback to a writable `/tmp` copy if executable mode bits are lost during upload.
- Removed the default dependency on downloading EJS code from GitHub during cold starts.
- Full `yt-dlp` exception logging so future extraction failures are visible in Vercel logs.
- `/api/stream/{id}` returns the freshly resolved media URL directly to the browser first.
- `/api/proxy-audio/{id}` remains a fallback for sources that need server-side replay headers.
- Frontend waits for `canplay`/metadata or an explicit media error before deciding a source failed.
- Frontend tries direct media -> proxy -> fresh re-resolution -> direct/proxy again before skipping.
- Existing Range/206/416 proxy handling retained.

## Deployment notes

- Python Vercel functions include the `vendor/node` binary via `includeFiles`.
- The Node binary is x86_64; Vercel's Python function architecture defaults to x86_64 unless explicitly changed.
- Keep the default Vercel function architecture for this package.
- The Python bundle remains below Vercel's current standard Python uncompressed bundle size limit in normal deployments; large-functions is not required just for the bundled Node runtime.

## Diagnostics

After deployment check:

- `/api/health`
- `/api/runtime`
- `/api/stream-debug/<11-char-video-id>`

A healthy runtime report should show `bundled_node_exists: true` and either `bundled_node_executable: true` or a prepared `/tmp/sujalconnect/node` copy.
