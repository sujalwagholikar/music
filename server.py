"""
SujalConnect - server.py
==========================
FastAPI backend that bridges the frontend (index.html) with the
Music Intelligence Engine (music.py).

Features:
    - Serves index.html + static assets (single-file deploy friendly)
    - /api/search           search songs by query
    - /api/stream/{id}      resolve + return a fresh playable stream URL
    - /api/proxy-audio/{id} proxies the actual audio bytes (avoids CORS /
                             hot-linking / expiry issues -- the <audio> tag
                             just points at us, we handle YouTube's URL)
    - /api/trending         home-feed / discovery feed
    - /api/related/{id}     "Up Next" queue for autoplay
    - /api/song/{id}        metadata only (no network re-resolve)
    - User handling:
        /api/users/register       simple username-based session (no password
                                   needed for a demo project, but structured
                                   so real auth can be swapped in later)
        /api/users/preferences    save onboarding genre preferences
        /api/users/me             fetch current profile + preferences
        /api/users/history        listen history (recently played)
        /api/users/like/{id}      like / unlike a song
        /api/users/liked          list liked songs

Run:
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import music  # our engine (music.py) -- module-level `engine` singleton

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [server.py] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sujalconnect.server")

BASE_DIR = Path(__file__).resolve().parent
# User data is ephemeral on Vercel unless replaced by a real database.
# /tmp is writable during a function instance lifetime; local development
# continues to use ./data.
RUNTIME_DIR = Path(os.environ.get("SUJALCONNECT_RUNTIME_DIR", "/tmp/sujalconnect" if os.environ.get("VERCEL") else str(BASE_DIR)))
DATA_DIR = RUNTIME_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_DB_FILE = DATA_DIR / "users.json"
SESSION_COOKIE_NAME = "sujal_session"
AUDIO_PROXY_CHUNK_BYTES = 2 * 1024 * 1024  # keep each serverless response small
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

AVAILABLE_GENRES = list(music.MusicEngine.GENRE_SEED_QUERIES.keys())

# --------------------------------------------------------------------------
# Tiny JSON "database" for users (keeps this a genuine zero-external-deps
# project -- swap for Postgres/Mongo later without touching the API shape)
# --------------------------------------------------------------------------
class UserStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("Failed loading users db: %s", e)
                self._data = {}

    def _save(self):
        try:
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Failed saving users db: %s", e)

    def get(self, session_id: str) -> Optional[dict]:
        return self._data.get(session_id)

    def get_by_username(self, username: str) -> Optional[tuple[str, dict]]:
        for sid, u in self._data.items():
            if u["username"].lower() == username.lower():
                return sid, u
        return None

    def create(self, username: str) -> tuple[str, dict]:
        session_id = str(uuid.uuid4())
        user = {
            "username": username,
            "created_at": time.time(),
            "onboarded": False,
            "preferences": {"genres": [], "language": "any"},
            "history": [],  # list of {id, played_at}
            "liked": [],    # list of song ids
        }
        self._data[session_id] = user
        self._save()
        return session_id, user

    def update(self, session_id: str, **fields):
        if session_id in self._data:
            self._data[session_id].update(fields)
            self._save()

    def add_history(self, session_id: str, song_id: str, max_len: int = 200):
        user = self._data.get(session_id)
        if not user:
            return
        user["history"] = [h for h in user["history"] if h["id"] != song_id]
        user["history"].insert(0, {"id": song_id, "played_at": time.time()})
        user["history"] = user["history"][:max_len]
        self._save()

    def toggle_like(self, session_id: str, song_id: str) -> bool:
        user = self._data.get(session_id)
        if not user:
            raise KeyError("no such session")
        liked = set(user.get("liked", []))
        if song_id in liked:
            liked.remove(song_id)
            is_liked = False
        else:
            liked.add(song_id)
            is_liked = True
        user["liked"] = list(liked)
        self._save()
        return is_liked


users = UserStore(USERS_DB_FILE)


# --------------------------------------------------------------------------
# Pydantic request/response models
# --------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=40)


class PreferencesRequest(BaseModel):
    genres: list[str] = Field(default_factory=list)
    language: str = "any"


class LikeRequest(BaseModel):
    song_id: str


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------
app = FastAPI(
    title="SujalConnect Music API",
    description="Backend powering SujalConnect -- search, stream, and discover music.",
    version="1.0.0",
)

cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Range"],
        expose_headers=["Accept-Ranges", "Content-Length", "Content-Range", "Content-Type"],
    )


# --------------------------------------------------------------------------
# Session helper
# --------------------------------------------------------------------------
def get_or_create_session(request: Request, response: Response) -> tuple[str, dict]:
    """
    Resolves the current user's session. If none exists, creates an
    anonymous "Guest" user transparently so the app works instantly without
    forcing a hard login wall -- registering later just renames the guest.

    NOTE: we read the cookie directly off `request.cookies` (rather than
    FastAPI's `Cookie(...)` parameter-injection) because this helper is
    called manually from inside route bodies rather than being used as a
    route parameter / Depends() itself -- `Cookie(...)` markers are only
    resolved by FastAPI's dependency-injection system when the function is
    invoked *by* FastAPI, not when we call it ourselves.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    user = users.get(session_id) if session_id else None

    if not user:
        session_id, user = users.create(username=f"Guest{str(uuid.uuid4())[:6]}")
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_id,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
        )
    return session_id, user


# --------------------------------------------------------------------------
# Root + static frontend
# --------------------------------------------------------------------------
INDEX_FILE = BASE_DIR / "index.html"
ALBUM_FILE = BASE_DIR / "album.html"
PLAYLIST_FILE = BASE_DIR / "playlist.html"


def _serve_html_file(path: Path, label: str) -> HTMLResponse:
    if path.exists():
        return HTMLResponse(path.read_text(encoding="utf-8"))
    return HTMLResponse(
        f"<h1>SujalConnect</h1><p>{label} not found next to server.py "
        f"(expected at {path}).</p>",
        status_code=404,
    )


@app.get("/", response_class=HTMLResponse)
def serve_index():
    return _serve_html_file(INDEX_FILE, "index.html")


# NOTE: these two routes are the actual fix for the "Not Found" JSON page
# you get when clicking an album card. Without an explicit route, FastAPI
# has nothing registered for GET /album.html (or /playlist.html), so it
# falls through to its default 404 JSON handler -- which is exactly what
# was being displayed instead of the album page. Serving the raw HTML file
# here (same pattern as "/") fixes it, and query params (?id=...&title=...)
# are untouched since they're read client-side via URLSearchParams anyway.
@app.get("/album.html", response_class=HTMLResponse)
def serve_album_page():
    return _serve_html_file(ALBUM_FILE, "album.html")


@app.get("/playlist.html", response_class=HTMLResponse)
def serve_playlist_page():
    return _serve_html_file(PLAYLIST_FILE, "playlist.html")


@app.get("/api/runtime")
def runtime_info():
    return {
        "vercel": bool(os.environ.get("VERCEL")),
        "python": os.sys.version.split()[0],
        "runtime_dir": str(music.RUNTIME_DIR),
        "cache_dir": str(music.CACHE_DIR),
        "cwd_writable": os.access(str(BASE_DIR), os.W_OK),
        "stream_chunk_bytes": AUDIO_PROXY_CHUNK_BYTES,
        "max_duration_hint_seconds": 300,
        "music_runtime": music.engine.runtime_info(),
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "engine_stats": music.engine.stats(), "time": time.time()}


@app.get("/api/genres")
def get_genres():
    """List of genres the onboarding portal can offer as preference chips."""
    return {"genres": AVAILABLE_GENRES}


# --------------------------------------------------------------------------
# Request validation / audio helpers
# --------------------------------------------------------------------------
def _validate_video_id(video_id: str) -> str:
    video_id = (video_id or "").strip()
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid video id")
    return video_id


def _parse_range_header(value: Optional[str]) -> Optional[tuple[int, Optional[int]]]:
    if not value:
        return None
    m = re.fullmatch(r"bytes=(\d+)-(\d*)", value.strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    if end is not None and end < start:
        return None
    return start, end


def _safe_upstream_headers(song: music.Song) -> dict[str, str]:
    headers = dict(song.stream_headers or {})
    # Requests sends its own Host/Connection/etc. Keep only headers yt-dlp
    # may have attached specifically for the media request.
    allowed = {"user-agent", "referer", "origin", "accept", "accept-language"}
    return {k: v for k, v in headers.items() if k.lower() in allowed}


def _audio_content_type(headers: dict) -> str:
    value = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if value.startswith("audio/"):
        return value
    if value in {"video/mp4", "application/octet-stream"}:
        return value
    return ""

# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
@app.get("/api/search")
def search_songs(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(24, ge=1, le=50)):
    results = music.engine.search(q, limit=limit)
    return {"query": q, "count": len(results), "results": [s.to_public_dict() for s in results]}


# --------------------------------------------------------------------------
# Metadata only (fast, no network hit if cached)
# --------------------------------------------------------------------------
@app.get("/api/song/{video_id}")
def get_song(video_id: str):
    video_id = _validate_video_id(video_id)
    song = music.engine.get_song_metadata(video_id)
    if not song:
        # not seen before -- do a full resolve as a fallback
        song = music.engine.get_stream_url(video_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    return song.to_public_dict()


# --------------------------------------------------------------------------
# Stream URL resolution (used by the player right before playback)
# --------------------------------------------------------------------------
@app.get("/api/stream/{video_id}")
def get_stream(video_id: str, request: Request, response: Response, refresh: bool = False):
    video_id = _validate_video_id(video_id)
    session_id, _user = get_or_create_session(request, response)
    song = music.engine.get_stream_url(video_id, force_refresh=refresh)
    if not song or not song.stream_url:
        # YouTube may refuse server-side extraction from a shared cloud IP even
        # when the same video plays normally in a user's browser. Do not turn
        # this into an endless 404/skip loop; return a first-party YouTube
        # embed fallback instead. This uses YouTube's supported playback path
        # rather than trying to evade its anti-bot controls.
        cached = music.engine.get_song_metadata(video_id)
        error_text = music.engine.last_resolve_error(video_id) or "server-side stream unavailable"
        if cached:
            data = cached.to_public_dict(include_stream=False)
            data["playback_mode"] = "youtube_embed"
        else:
            data = {"id": video_id, "title": "YouTube playback", "artist": "", "duration": 0, "duration_str": "0:00", "poster": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg", "audio_format": "", "source": "youtube", "album_id": None, "album_name": None, "track_number": None, "playback_mode": "youtube_embed"}
        data["embed_url"] = f"https://www.youtube.com/embed/{video_id}?autoplay=1&playsinline=1&rel=0"
        data["stream_error"] = error_text[:300]
        users.add_history(session_id, video_id)
        return data

    users.add_history(session_id, video_id)

    data = song.to_public_dict(include_stream=False)
    # Prefer the freshly resolved media URL directly in the browser. This
    # keeps Vercel out of the hot audio path and avoids Vercel->YouTube egress
    # failures for media requests. The server proxy remains available as a
    # fallback for sources that require replay headers.
    data["playback_url"] = song.stream_url
    data["proxy_url"] = f"/api/proxy-audio/{video_id}"
    data["expires_at"] = song.stream_fetched_at + music.STREAM_URL_TTL_SECONDS
    return data


# --------------------------------------------------------------------------
# Stream diagnostics (never exposes the direct media URL)
# --------------------------------------------------------------------------
@app.get("/api/stream-debug/{video_id}")
def stream_debug(video_id: str, refresh: bool = False):
    video_id = _validate_video_id(video_id)
    song = music.engine.get_stream_url(video_id, force_refresh=refresh)
    if not song or not song.stream_url:
        return {
            "ok": False,
            "video_id": video_id,
            "detail": "yt-dlp could not resolve a playable stream",
            "reason": music.engine.last_resolve_error(video_id),
            "fallback": "youtube_embed",
            "engine": music.engine.runtime_info(),
        }
    return {
        "ok": True,
        "video_id": video_id,
        "title": song.title,
        "audio_format": song.audio_format,
        "resolved_at": song.stream_fetched_at,
        "expires_at": song.stream_fetched_at + music.STREAM_URL_TTL_SECONDS,
        "has_replay_headers": bool(song.stream_headers),
        "engine": music.engine.runtime_info(),
    }


# --------------------------------------------------------------------------
# Audio proxy -- streams actual bytes with Range support for seeking
# --------------------------------------------------------------------------
@app.get("/api/proxy-audio/{video_id}")
def proxy_audio(video_id: str, request: Request):
    """
    Vercel-safe byte-range audio proxy.

    Key fixes versus the old implementation:
      * validates video ids;
      * forwards the safe headers yt-dlp extracted with the media URL;
      * retries on upstream 403/404/410/429/5xx by forcing a fresh URL;
      * never lies about an upstream failure by converting it to HTTP 200;
      * caps each response to ~2 MiB so long tracks do not require one
        serverless invocation to stay open for the whole song;
      * supports standard browser Range requests and returns proper 206/416
        semantics.
    """
    video_id = _validate_video_id(video_id)
    client_range = request.headers.get("range")
    parsed_range = _parse_range_header(client_range)

    if client_range and parsed_range is None:
        return Response(status_code=416, headers={"Content-Range": "bytes */*"})

    range_start = parsed_range[0] if parsed_range else 0
    requested_end = parsed_range[1] if parsed_range else None
    range_was_explicit = parsed_range is not None

    def _upstream_request(song: music.Song):
        headers = _safe_upstream_headers(song)
        # Always request a bounded byte range. This makes initial playback
        # chunked too, rather than letting a serverless request try to relay
        # an entire multi-megabyte track in one invocation.
        if requested_end is None:
            upstream_end = range_start + AUDIO_PROXY_CHUNK_BYTES - 1
        else:
            upstream_end = min(requested_end, range_start + AUDIO_PROXY_CHUNK_BYTES - 1)
        headers["Range"] = f"bytes={range_start}-{upstream_end}"
        return requests.get(
            song.stream_url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=(10, 30),
        ), upstream_end

    song = music.engine.get_stream_url(video_id)
    if not song or not song.stream_url:
        raise HTTPException(status_code=404, detail="Stream unavailable")

    upstream = None
    upstream_end = None
    last_status = None
    for attempt in range(2):
        try:
            upstream, upstream_end = _upstream_request(song)
            last_status = upstream.status_code
            content_type = _audio_content_type(upstream.headers)

            # These statuses strongly indicate that the short-lived media URL
            # is stale or temporarily rejected. Resolve a brand-new one once.
            if upstream.status_code in {401, 403, 404, 410, 429} or upstream.status_code >= 500:
                upstream.close()
                upstream = None
                if attempt == 0:
                    music.engine.invalidate_stream(video_id)
                    song = music.engine.get_stream_url(video_id, force_refresh=True)
                    if not song or not song.stream_url:
                        break
                    continue
                break

            if not content_type and upstream.status_code in {200, 206}:
                # HTML error pages should never be passed to <audio>.
                preview = upstream.raw.read(256, decode_content=False) if upstream.raw else b""
                upstream.close()
                upstream = None
                if attempt == 0:
                    music.engine.invalidate_stream(video_id)
                    song = music.engine.get_stream_url(video_id, force_refresh=True)
                    if song and song.stream_url:
                        continue
                raise HTTPException(status_code=502, detail="Upstream returned a non-audio response")

            break
        except requests.RequestException as exc:
            log.warning("Audio upstream request failed for %s: %s", video_id, exc)
            if upstream is not None:
                upstream.close()
                upstream = None
            if attempt == 0:
                music.engine.invalidate_stream(video_id)
                song = music.engine.get_stream_url(video_id, force_refresh=True)
                if song and song.stream_url:
                    continue
            raise HTTPException(status_code=502, detail="Upstream audio source unavailable") from exc

    if upstream is None:
        raise HTTPException(status_code=502, detail=f"Upstream audio source unavailable ({last_status or 'no response'})")

    if upstream.status_code not in {200, 206}:
        status = upstream.status_code
        upstream.close()
        if status == 416:
            return Response(status_code=416, headers={"Content-Range": upstream.headers.get("Content-Range", "bytes */*")})
        raise HTTPException(status_code=502, detail=f"Upstream audio source rejected the request ({status})")

    response_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Content-Disposition": f'inline; filename="{video_id}.audio"',
        "X-Content-Type-Options": "nosniff",
    }

    content_type = _audio_content_type(upstream.headers) or "audio/mp4"
    response_headers["Content-Type"] = content_type

    upstream_content_range = upstream.headers.get("Content-Range")
    upstream_length = upstream.headers.get("Content-Length")
    upstream_total = None
    if upstream_content_range:
        m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", upstream_content_range)
        if m and m.group(3) != "*":
            upstream_total = int(m.group(3))

    # Most YouTube media responses honor Range and return 206 + Content-Range.
    # If the upstream ignores the synthetic initial range and sends 200, only
    # do that for offset 0 and synthesize a correct partial response.
    if upstream.status_code == 206:
        status_code = 206
        if upstream_content_range:
            response_headers["Content-Range"] = upstream_content_range
        else:
            # Some compatible origins return 206 without Content-Range.
            # Synthesize the range from the request offset and length; `*`
            # is valid when the complete object size is unknown.
            length = int(upstream_length or 0)
            last = range_start + max(0, length - 1)
            response_headers["Content-Range"] = f"bytes {range_start}-{last}/*"
        if upstream_length:
            response_headers["Content-Length"] = str(min(int(upstream_length), AUDIO_PROXY_CHUNK_BYTES))
    elif upstream.status_code == 200:
        if range_start != 0:
            upstream.close()
            # A non-zero seek without upstream range support is not safe to
            # fake. Ask the browser to retry with a fresh resolution.
            raise HTTPException(status_code=502, detail="Audio source does not support seeking")
        total = upstream_total
        try:
            if total is None and upstream_length:
                total = int(upstream_length)
        except ValueError:
            total = None
        # We deliberately requested a bounded range. If the origin ignored it,
        # stream only one chunk and tell the browser the full object size when
        # known. Chrome will request subsequent ranges.
        if total is not None:
            last_byte = min(total - 1, AUDIO_PROXY_CHUNK_BYTES - 1)
            status_code = 206
            response_headers["Content-Range"] = f"bytes 0-{last_byte}/{total}"
            response_headers["Content-Length"] = str(last_byte + 1)
        else:
            status_code = 200
            if upstream_length:
                response_headers["Content-Length"] = str(min(int(upstream_length), AUDIO_PROXY_CHUNK_BYTES))
    else:
        upstream.close()
        raise HTTPException(status_code=502, detail="Unexpected upstream audio response")

    limit_bytes = AUDIO_PROXY_CHUNK_BYTES
    sent = 0

    def iter_bytes():
        nonlocal sent
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                remaining = limit_bytes - sent
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                sent += len(chunk)
                yield chunk
                if sent >= limit_bytes:
                    break
        finally:
            upstream.close()

    return StreamingResponse(iter_bytes(), status_code=status_code, headers=response_headers)


# --------------------------------------------------------------------------
# Trending / discovery feed
# --------------------------------------------------------------------------
@app.get("/api/trending")
def trending(genre: Optional[str] = None, limit: int = Query(24, ge=1, le=50)):
    results = music.engine.trending(genre=genre, limit=limit)
    return {"genre": genre, "results": [s.to_public_dict() for s in results]}


# --------------------------------------------------------------------------
# ALBUM-BASED discovery -- what the homepage rows actually render.
# --------------------------------------------------------------------------
# /api/trending and /api/feed (below) return individual SONGS, which is
# correct for things like search results or "up next" queues, but is wrong
# for homepage rows that are supposed to look and behave like Spotify's
# "Popular albums" / "Made for you" shelves: each tile there needs to be a
# real album (its own cover, its own title, its own artist line, and a full
# tracklist waiting behind it) -- not a single song mislabeled as an album.
#
# These two endpoints return real, multi-track Album objects (lightweight
# summary form -- see Album.to_summary_dict) built via music.engine's album
# discovery methods, so the frontend can render genuine album tiles that
# open straight into /api/album/{id} with zero extra resolution needed.
# --------------------------------------------------------------------------
@app.get("/api/albums/trending")
def trending_albums(genre: Optional[str] = None, limit: int = Query(12, ge=1, le=30)):
    albums = music.engine.trending_albums(genre=genre, limit=limit)
    return {"genre": genre, "results": [a.to_summary_dict() for a in albums]}


@app.get("/api/albums/feed")
def personalized_album_feed(request: Request, response: Response, limit_per_genre: int = 6):
    session_id, user = get_or_create_session(request, response)
    genres = (user.get("preferences") or {}).get("genres") or []

    if not genres:
        albums = music.engine.trending_albums(limit=12)
        return {"personalized": False, "results": [a.to_summary_dict() for a in albums]}

    albums = music.engine.albums_for_genres(genres, limit_per_genre=limit_per_genre)
    return {"personalized": True, "genres": genres, "results": [a.to_summary_dict() for a in albums]}


# --------------------------------------------------------------------------
# Related songs -> autoplay "Up Next" queue
# --------------------------------------------------------------------------
@app.get("/api/related/{video_id}")
def related(video_id: str, limit: int = 15):
    results = music.engine.related_songs(video_id, limit=limit)
    return {"seed": video_id, "results": [s.to_public_dict() for s in results]}


# --------------------------------------------------------------------------
# ALBUMS -- Spotify-style expanded album view
# --------------------------------------------------------------------------
# The frontend (album.html) calls one of these two endpoints when the user
# clicks a song/card:
#
#   GET /api/album/by-song/{video_id}   <- normal entry point. Given the
#       clicked song's id, resolves (and, on first hit, *builds*) the real
#       album it belongs to, loads every track on the backend via
#       music.engine, and returns the full ordered tracklist ready to play.
#
#   GET /api/album/{album_id}           <- direct lookup once the frontend
#       already knows the album id (e.g. navigating between albums, or a
#       cached URL), avoiding the by-song resolution work entirely.
#
# Both are backed by the same MusicEngine album cache, so repeat opens of
# the same album are instant and never re-hit YouTube unless the cached
# tracklist has expired (see ALBUM_TTL_SECONDS in music.py).
# --------------------------------------------------------------------------
@app.get("/api/album/by-song/{video_id}")
def album_by_song(video_id: str, limit: int = 50):
    album = music.engine.get_album_for_song(video_id, limit=limit)
    if not album:
        raise HTTPException(status_code=404, detail="Could not resolve an album for this song")
    return album.to_public_dict(music.engine)


@app.get("/api/album/{album_id}")
def album_by_id(album_id: str, limit: int = 50):
    album = music.engine.get_album_by_id(album_id, limit=limit)
    if not album or not album.track_ids:
        raise HTTPException(status_code=404, detail="Album not found")
    return album.to_public_dict(music.engine)


@app.get("/api/album/{album_id}/preload")
def album_preload(album_id: str, limit: int = 5):
    """
    Warms the stream-URL cache for the first `limit` tracks of an album in
    the background threadpool as soon as the album view opens, so the first
    few "play" clicks feel instant instead of waiting on a fresh yt-dlp
    resolve. Fire-and-forget from the frontend right after loading the
    tracklist; the response just reports what got warmed.
    """
    album = music.engine.get_album_by_id(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    warmed = []
    for vid in album.track_ids[:limit]:
        song = music.engine.get_song_metadata(vid)
        if song and song.stream_url and (time.time() - song.stream_fetched_at) < music.STREAM_URL_TTL_SECONDS:
            warmed.append(vid)
            continue
        # resolve synchronously here -- endpoint already runs in FastAPI's
        # threadpool (sync def would be needed for true blocking-safety, but
        # get_stream_url is fast enough per-call and the whole point is to
        # do this eagerly right when the album opens, off the play-button's
        # critical path).
        resolved = music.engine.get_stream_url(vid)
        if resolved and resolved.stream_url:
            warmed.append(vid)

    return {"album_id": album_id, "warmed": warmed, "requested": album.track_ids[:limit]}


# --------------------------------------------------------------------------
# Personalized home feed (built from onboarding preferences)
# --------------------------------------------------------------------------
@app.get("/api/feed")
def personalized_feed(request: Request, response: Response):
    session_id, user = get_or_create_session(request, response)
    genres = (user.get("preferences") or {}).get("genres") or []

    if not genres:
        results = music.engine.trending(limit=24)
        return {"personalized": False, "results": [s.to_public_dict() for s in results]}

    results = music.engine.recommendations_for_genres(genres, limit_per_genre=8)
    return {"personalized": True, "genres": genres, "results": [s.to_public_dict() for s in results]}


# --------------------------------------------------------------------------
# User handling
# --------------------------------------------------------------------------
@app.post("/api/users/register")
def register(body: RegisterRequest, request: Request, response: Response):
    """
    Registers/renames the current session's user. Since this project has
    no password requirement, "registering" simply claims a display name on
    top of the already-existing anonymous session -- so history/likes/
    preferences accumulated as a guest carry over seamlessly.
    """
    session_id, user = get_or_create_session(request, response)

    existing = users.get_by_username(body.username)
    if existing and existing[0] != session_id:
        raise HTTPException(status_code=409, detail="Username already taken")

    users.update(session_id, username=body.username)
    user = users.get(session_id)
    return {"session_created": True, "user": _public_user(user)}


@app.get("/api/users/me")
def me(request: Request, response: Response):
    session_id, user = get_or_create_session(request, response)
    return _public_user(user)


@app.post("/api/users/preferences")
def set_preferences(body: PreferencesRequest, request: Request, response: Response):
    session_id, _user = get_or_create_session(request, response)
    valid_genres = [g for g in body.genres if g.lower() in AVAILABLE_GENRES] or body.genres
    users.update(
        session_id,
        onboarded=True,
        preferences={"genres": valid_genres, "language": body.language},
    )
    user = users.get(session_id)
    return {"saved": True, "user": _public_user(user)}


@app.get("/api/users/history")
def history(request: Request, response: Response, limit: int = 50):
    session_id, user = get_or_create_session(request, response)
    hist = (user.get("history") or [])[:limit]
    ids = [h["id"] for h in hist]
    songs = {s.id: s for s in music.engine.bulk_get(ids)}
    enriched = []
    for h in hist:
        song = songs.get(h["id"])
        if song:
            entry = song.to_public_dict()
            entry["played_at"] = h["played_at"]
            enriched.append(entry)
    return {"history": enriched}


@app.post("/api/users/like")
def like_song(body: LikeRequest, request: Request, response: Response):
    session_id, _user = get_or_create_session(request, response)
    try:
        is_liked = users.toggle_like(session_id, body.song_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"song_id": body.song_id, "liked": is_liked}


@app.get("/api/users/liked")
def liked_songs(request: Request, response: Response):
    session_id, user = get_or_create_session(request, response)
    ids = user.get("liked") or []
    songs = music.engine.bulk_get(ids)
    # resolve any liked songs we don't have cached metadata for yet
    missing = [i for i in ids if i not in {s.id for s in songs}]
    for mid in missing:
        s = music.engine.get_stream_url(mid)
        if s:
            songs.append(s)
    return {"results": [s.to_public_dict() for s in songs]}


def _public_user(user: dict) -> dict:
    return {
        "username": user["username"],
        "onboarded": user.get("onboarded", False),
        "preferences": user.get("preferences", {}),
        "liked_count": len(user.get("liked", [])),
        "history_count": len(user.get("history", [])),
    }


# --------------------------------------------------------------------------
# Global error handler -> always return clean JSON, never a raw 500 trace
# --------------------------------------------------------------------------
@app.exception_handler(Exception)
def all_exceptions_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error. Please try again."})


# --------------------------------------------------------------------------
# Static mount for any extra assets (icons, generated files, etc.)
# --------------------------------------------------------------------------
STATIC_DIR = BASE_DIR / "static"
# Vercel deployment files are read-only at runtime. Do not attempt to create
# directories beside the source bundle during function import. Mount the
# directory only when it is actually packaged with the deployment.
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
