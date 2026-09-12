# SujalConnect V6 — GitHub → Vercel runtime fix

## Root cause proven by production logs

The Vercel function had `yt-dlp` installed, but no real JavaScript runtime was present:

- `deno_package_installed: true`
- `deno_binary_exists: false`
- `path_runtimes.deno: null`
- `path_runtimes.node: null`

`yt-dlp` now requires a supported external JavaScript runtime for full YouTube extraction.

## V6 fix

V6 does **not** commit a large Deno binary to GitHub and does **not** rely on the `deno` Python package to expose an executable in the Vercel runtime.

Instead, Vercel runs `build_vercel.sh` during the build. The script:

1. detects Linux x86_64/ARM64;
2. downloads a pinned Deno release from the official Deno GitHub release;
3. extracts the executable to `vendor/deno`;
4. marks it executable;
5. verifies `deno --version`;
6. leaves the executable in the build workspace so Vercel's Python function bundling can include it.

`vendor/deno` is Git-ignored, so GitHub remains lightweight. Vercel's Python runtime includes project files available at build time in the function bundle.

## Runtime verification

`/api/runtime` now reports:

- bundled Deno path;
- whether it exists;
- whether it is executable;
- selected runtime path and version.

A healthy V6 deployment should show:

```json
{
  "music_runtime": {
    "bundled_deno_exists": true,
    "bundled_deno_executable": true,
    "selected_runtime": "deno"
  }
}
```

## YouTube bot checks

Some YouTube videos may still reject cloud/server-side extraction with `Sign in to confirm you're not a bot`. SujalConnect does not attempt to bypass that restriction. The API returns the official YouTube embed fallback for those cases.
