# SujalConnect — Vercel crash fix

## Root cause fixed

The FastAPI module was trying to create `/static` beside the deployed source at import time:

```python
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
```

Vercel function source is read-only at runtime, so importing the app could fail before the first request and produce `500 FUNCTION_INVOCATION_FAILED`.

The deployment version now:

- never writes into the bundled project directory at runtime;
- uses `/tmp/sujalconnect` for ephemeral cache/user data on Vercel;
- only mounts `/static` if that directory is actually packaged;
- creates runtime directories with `parents=True` under `/tmp`.

## Deploy

1. Replace the old project files with this package.
2. Commit/push the changed files to the Git repository connected to Vercel, or deploy the folder with Vercel CLI.
3. Redeploy.
4. Open `/api/health` first. It should return JSON with `status: "ok"`.
5. Then open `/`.

## Important architecture note

This removes the startup crash. User data and cache still live in ephemeral `/tmp` storage on Vercel, so they are not durable across function instances. For production, move accounts/history/likes to a managed database and shared cache.
