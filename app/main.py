import asyncio
import glob
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time

import httpx
import yt_dlp
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic

DEFAULT_USER = os.getenv("SONORA_USER", "sonora").strip()
DEFAULT_PASSWORD = os.getenv("SONORA_PASSWORD", "sonora")
REGISTRATION_ENABLED = os.getenv("REGISTRATION_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)
COOKIES_FILE = os.getenv("COOKIES_FILE", "/cookies/cookies.txt").strip() or None
POT_PROVIDER_URL = os.getenv("POT_PROVIDER_URL", "").strip() or None
CACHE_DIR = os.getenv("TRANSCODE_CACHE_DIR", "/cache")
CACHE_MAX_BYTES = int(os.getenv("TRANSCODE_CACHE_MAX_BYTES", str(5 * 1024 * 1024 * 1024)))
URL_CACHE_TTL = int(os.getenv("URL_CACHE_TTL", "1800"))
DB_PATH = os.getenv("DB_PATH", "/data/sonora.db")
MUSIC_DIR = os.getenv("MUSIC_DIR", "/music")
LIDARR_URL = os.getenv("LIDARR_URL", "").strip().rstrip("/")
LIDARR_API_KEY = os.getenv("LIDARR_API_KEY", "").strip()
LIDARR_QUALITY_PROFILE = int(os.getenv("LIDARR_QUALITY_PROFILE", "2"))
LIDARR_ROOT = os.getenv("LIDARR_ROOT", "/music")
SERVER_SETUP_ENABLED = os.getenv("SERVER_SETUP_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)

QUALITY_PROFILES = {
    "normal": ("128k", "bestaudio[ext=m4a]/bestaudio"),
    "high": ("192k", "bestaudio"),
}
DEFAULT_QUALITY = "normal"

app = FastAPI(title="Sonora", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ytmusic = YTMusic()
_url_cache: dict[tuple[str, str], dict] = {}
_transcode_locks: dict[str, asyncio.Lock] = {}

os.makedirs(CACHE_DIR, exist_ok=True)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            data TEXT NOT NULL,
            added_at REAL NOT NULL,
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(playlists)")}
    if "user_id" not in columns:
        conn.execute("ALTER TABLE playlists ADD COLUMN user_id INTEGER")
    conn.commit()
    conn.close()


_init_db()


def _extract_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.query_params.get("token") or "").strip()


def _session_user_id(request: Request) -> int | None:
    token = _extract_token(request)
    if not token:
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    finally:
        conn.close()
    return row["user_id"] if row else None


def require_token(request: Request):
    if _session_user_id(request) is None:
        raise HTTPException(status_code=401, detail="authentication required")


def _upscale_thumb(url: str | None, size: int = 544) -> str | None:
    if not url:
        return url
    return re.sub(r"=w\d+-h\d+", f"=w{size}-h{size}", url)


def _pick_thumb(thumbs):
    return _upscale_thumb(thumbs[-1]["url"]) if thumbs else None


def _song_result(r: dict) -> dict | None:
    video_id = r.get("videoId")
    if not video_id:
        return None
    album = r.get("album")
    return {
        "videoId": video_id,
        "title": r.get("title"),
        "artists": [
            {"name": a.get("name"), "id": a.get("id")}
            for a in r.get("artists", [])
            if a.get("name")
        ],
        "album": album.get("name") if isinstance(album, dict) else album,
        "duration": r.get("duration"),
        "durationSeconds": r.get("duration_seconds"),
        "thumbnail": _pick_thumb(r.get("thumbnails", [])),
    }


def _ydl_opts(audio_format: str) -> dict:
    opts = {
        "format": audio_format,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "extractor_retries": 3,
        "fragment_retries": 3,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if POT_PROVIDER_URL:
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]},
        }
    return opts


def _resolve(video_id: str, audio_format: str, quality: str) -> dict:
    key = (video_id, quality)
    cached = _url_cache.get(key)
    if cached and (time.time() - cached["ts"]) < URL_CACHE_TTL:
        return cached
    opts = _ydl_opts(audio_format)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"resolve failed: {e}")
    url = info.get("url")
    if not url and info.get("requested_formats"):
        info = info["requested_formats"][0]
        url = info.get("url")
    if not url:
        raise HTTPException(status_code=502, detail="could not resolve audio url")
    resolved = {
        "url": url,
        "acodec": (info.get("acodec") or "").lower(),
        "ext": (info.get("ext") or "").lower(),
        "headers": info.get("http_headers") or {},
        "ts": time.time(),
    }
    _url_cache[key] = resolved
    return resolved


def _is_aac(resolved: dict) -> bool:
    return resolved["acodec"].startswith("mp4a") or resolved["ext"] in ("m4a", "mp4")


async def _open_upstream(url: str, headers: dict, range_header: str | None):
    forward = dict(headers)
    if range_header:
        forward["Range"] = range_header
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None), follow_redirects=True
    )
    upstream = await client.send(
        client.build_request("GET", url, headers=forward), stream=True
    )
    return client, upstream


def _proxy_response(client, upstream) -> StreamingResponse:
    async def body():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Accept-Ranges": "bytes"}
    for name in ("content-length", "content-range", "content-type"):
        if name in upstream.headers:
            headers[name.title()] = upstream.headers[name]
    headers.setdefault("Content-Type", "audio/mp4")
    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


def _download_source(video_id: str, audio_format: str, quality: str) -> str:
    opts = _ydl_opts(audio_format)
    opts["overwrites"] = True
    opts["outtmpl"] = os.path.join(CACHE_DIR, f".src-{video_id}.{quality}.%(ext)s")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=True
            )
            return ydl.prepare_filename(info)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"download failed: {e}")


def _evict_cache_if_needed() -> None:
    entries = []
    total = 0
    for name in os.listdir(CACHE_DIR):
        if not name.endswith(".m4a"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            continue
        entries.append((path, stat.st_atime, stat.st_size))
        total += stat.st_size
    if total <= CACHE_MAX_BYTES:
        return
    entries.sort(key=lambda e: e[1])
    for path, _, size in entries:
        if total <= CACHE_MAX_BYTES:
            break
        try:
            os.remove(path)
            total -= size
        except FileNotFoundError:
            continue


async def _audio_codec(path: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace").strip()


async def _transcode(source_path: str, out_path: str, bitrate: str, allow_copy: bool) -> None:
    codec = await _audio_codec(source_path)
    audio_args = (
        ["-c:a", "copy"]
        if allow_copy and codec == "aac"
        else ["-c:a", "aac", "-b:a", bitrate]
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_path,
        "-vn",
        *audio_args,
        "-movflags",
        "+faststart",
        "-f",
        "ipod",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        detail = stderr.decode(errors="replace")[-300:] if stderr else "unknown"
        raise HTTPException(status_code=502, detail=f"transcode failed: {detail}")


async def _ensure_transcoded(video_id: str, quality: str) -> str:
    bitrate, audio_format = QUALITY_PROFILES[quality]
    final_path = os.path.join(CACHE_DIR, f"{video_id}.{quality}.m4a")
    if os.path.exists(final_path):
        os.utime(final_path, None)
        return final_path
    lock = _transcode_locks.setdefault(f"{video_id}:{quality}", asyncio.Lock())
    async with lock:
        if os.path.exists(final_path):
            return final_path
        source_path = await asyncio.to_thread(
            _download_source, video_id, audio_format, quality
        )
        tmp_path = final_path + ".part"
        try:
            await _transcode(
                source_path, tmp_path, bitrate, allow_copy=quality == "normal"
            )
            os.replace(tmp_path, final_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            for leftover in glob.glob(
                os.path.join(CACHE_DIR, f".src-{video_id}.{quality}.*")
            ):
                try:
                    os.remove(leftover)
                except FileNotFoundError:
                    pass
        _evict_cache_if_needed()
        return final_path


@app.get("/health")
def health():
    files = glob.glob(os.path.join(CACHE_DIR, "*.m4a"))
    cookies_active = bool(COOKIES_FILE and os.path.exists(COOKIES_FILE))
    return {"status": "ok", "cached_tracks": len(files), "cookies": cookies_active}


@app.get("/api/config")
def config():
    return {"serverSetup": SERVER_SETUP_ENABLED}


@app.get("/api/search")
def search(
    q: str,
    type: str = "songs",
    limit: int = 20,
    artists: int = 0,
    _=Depends(require_token),
):
    if not q.strip():
        return {"results": [], "artists": []}
    try:
        raw = ytmusic.search(q, filter=type, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"search failed: {e}")

    results = [s for s in (_song_result(r) for r in raw) if s]

    artist_hits = []
    if artists and type == "songs":
        try:
            araw = ytmusic.search(q, filter="artists", limit=3)
        except Exception:
            araw = []
        for a in araw:
            if len(artist_hits) >= 3:
                break
            browse_id = a.get("browseId")
            name = a.get("artist") or a.get("title")
            if not browse_id or not name:
                continue
            artist_hits.append(
                {
                    "name": name,
                    "id": browse_id,
                    "thumbnail": _pick_thumb(a.get("thumbnails", [])),
                }
            )

    return {"results": results, "artists": artist_hits}


def _albums_from_search(name: str) -> list[dict]:
    try:
        raw = ytmusic.search(name, filter="albums", limit=10)
    except Exception:
        return []
    albums = []
    for a in raw:
        browse_id = a.get("browseId")
        if not browse_id:
            continue
        albums.append(
            {
                "browseId": browse_id,
                "title": a.get("title"),
                "year": a.get("year"),
                "thumbnail": _pick_thumb(a.get("thumbnails", [])),
            }
        )
    return albums


@app.get("/api/artist/{channel_id}")
def artist(channel_id: str, name: str = "", _=Depends(require_token)):
    cached = _ARTIST_CACHE.get(channel_id)
    if cached is not None:
        return cached
    result = _compute_artist(channel_id, name)
    _ARTIST_CACHE[channel_id] = result
    return result


def _compute_artist(channel_id: str, name: str = ""):
    try:
        data = ytmusic.get_artist(channel_id)
        songs_section = data.get("songs") or {}
        songs = [
            s for s in (_song_result(r) for r in songs_section.get("results", [])) if s
        ]
        albums = []
        for a in (data.get("albums") or {}).get("results", []):
            browse_id = a.get("browseId")
            if not browse_id:
                continue
            albums.append(
                {
                    "browseId": browse_id,
                    "title": a.get("title"),
                    "year": a.get("year"),
                    "thumbnail": _pick_thumb(a.get("thumbnails", [])),
                }
            )
        return {
            "name": data.get("name") or name,
            "description": data.get("description"),
            "thumbnail": _pick_thumb(data.get("thumbnails", [])),
            "songs": songs,
            "albums": albums,
        }
    except Exception as e:
        if not name.strip():
            raise HTTPException(status_code=502, detail=f"artist failed: {e}")
        try:
            raw = ytmusic.search(name, filter="songs", limit=15)
        except Exception:
            raw = []
        songs = [s for s in (_song_result(r) for r in raw) if s]
        return {
            "name": name,
            "description": None,
            "thumbnail": songs[0]["thumbnail"] if songs else None,
            "songs": songs,
            "albums": _albums_from_search(name),
        }


@app.get("/api/album/{browse_id}")
def album(browse_id: str, _=Depends(require_token)):
    try:
        data = ytmusic.get_album(browse_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"album failed: {e}")

    thumb = _pick_thumb(data.get("thumbnails", []))
    tracks = []
    for r in data.get("tracks", []):
        song = _song_result(r)
        if not song:
            continue
        if not song["thumbnail"]:
            song["thumbnail"] = thumb
        tracks.append(song)

    return {
        "title": data.get("title"),
        "year": data.get("year"),
        "thumbnail": thumb,
        "artists": [
            {"name": a.get("name"), "id": a.get("id")}
            for a in data.get("artists", [])
            if a.get("name")
        ],
        "tracks": tracks,
    }


@app.get("/api/stream/{video_id}")
async def stream(
    video_id: str,
    request: Request,
    q: str = "normal",
    _=Depends(require_token),
):
    quality = q if q in QUALITY_PROFILES else DEFAULT_QUALITY
    try:
        path = await _ensure_transcoded(video_id, quality)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"stream failed: {e}")
    return FileResponse(path, media_type="audio/mp4", filename=f"{video_id}.m4a")


@app.get("/api/lyrics/{video_id}")
def lyrics(video_id: str, _=Depends(require_token)):
    cached = _LYRICS_CACHE.get(video_id)
    if cached is not None:
        return cached
    result = _compute_lyrics(video_id)
    _LYRICS_CACHE[video_id] = result
    return result


def _compute_lyrics(video_id: str):
    empty = {"synced": False, "lines": [], "plain": None, "source": None}
    try:
        watch = ytmusic.get_watch_playlist(videoId=video_id)
        browse_id = watch.get("lyrics")
    except Exception:
        return empty
    if not browse_id:
        return empty
    try:
        data = ytmusic.get_lyrics(browse_id, timestamps=True)
    except TypeError:
        try:
            data = ytmusic.get_lyrics(browse_id)
        except Exception:
            return empty
    except Exception:
        return empty

    payload = data.get("lyrics") if isinstance(data, dict) else None
    source = data.get("source") if isinstance(data, dict) else None
    if isinstance(payload, list):
        lines = []
        for line in payload:
            if isinstance(line, dict):
                text = line.get("text")
                start = line.get("start_time")
            else:
                text = getattr(line, "text", None)
                start = getattr(line, "start_time", None)
            lines.append({"text": text or "", "start": start})
        return {"synced": True, "lines": lines, "plain": None, "source": source}
    return {"synced": False, "lines": [], "plain": payload, "source": source}


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 100_000
    ).hex()
    return f"{salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    expected = _hash_password(password, salt).split("$", 1)[1]
    return secrets.compare_digest(expected, digest)


def _create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_hex(24)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, time.time()),
    )
    return token


def _current_user_id(request: Request) -> int | None:
    return _session_user_id(request)


def _seed_default_user() -> None:
    conn = _db()
    try:
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if row is not None:
            user_id = row["id"]
        elif DEFAULT_USER and DEFAULT_PASSWORD:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (DEFAULT_USER.lower(), _hash_password(DEFAULT_PASSWORD), time.time()),
            )
            user_id = cur.lastrowid
        else:
            return
        conn.execute(
            "UPDATE playlists SET user_id = ? WHERE user_id IS NULL", (user_id,)
        )
        conn.commit()
    finally:
        conn.close()


class Credentials(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register")
def register(creds: Credentials):
    if not REGISTRATION_ENABLED:
        raise HTTPException(status_code=403, detail="registration disabled")
    username = creds.username.strip().lower()
    if not username or len(creds.password) < 4:
        raise HTTPException(
            status_code=400, detail="username and password (min 4 chars) required"
        )
    conn = _db()
    try:
        if conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone():
            raise HTTPException(status_code=409, detail="username already taken")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, _hash_password(creds.password), time.time()),
        )
        token = _create_session(conn, cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {"token": token, "username": username}


@app.post("/api/auth/login")
def login(creds: Credentials):
    username = creds.username.strip().lower()
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not row or not _verify_password(creds.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="invalid credentials")
        token = _create_session(conn, row["id"])
        conn.commit()
    finally:
        conn.close()
    return {"token": token, "username": username}


@app.post("/api/auth/logout")
def logout(request: Request):
    token = _extract_token(request)
    if token:
        conn = _db()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    return {"status": "ok"}


class PlaylistCreate(BaseModel):
    name: str


def _playlist_or_404(
    conn: sqlite3.Connection, playlist_id: int, user_id: int | None
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name FROM playlists WHERE id = ? AND user_id IS ?",
        (playlist_id, user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="playlist not found")
    return row


@app.get("/api/playlists")
def list_playlists(request: Request, _=Depends(require_token)):
    user_id = _current_user_id(request)
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.name,
                   COUNT(t.id) AS track_count,
                   (SELECT data FROM playlist_tracks
                    WHERE playlist_id = p.id ORDER BY position LIMIT 1) AS first_track
            FROM playlists p
            LEFT JOIN playlist_tracks t ON t.playlist_id = p.id
            WHERE p.user_id IS ?
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    playlists = []
    for row in rows:
        cover = None
        if row["first_track"]:
            try:
                cover = json.loads(row["first_track"]).get("thumbnail")
            except json.JSONDecodeError:
                cover = None
        playlists.append(
            {
                "id": row["id"],
                "name": row["name"],
                "trackCount": row["track_count"],
                "cover": cover,
            }
        )
    return {"playlists": playlists}


@app.post("/api/playlists")
def create_playlist(
    request: Request, payload: PlaylistCreate, _=Depends(require_token)
):
    user_id = _current_user_id(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO playlists (name, created_at, user_id) VALUES (?, ?, ?)",
            (name, time.time(), user_id),
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "trackCount": 0, "cover": None}
    finally:
        conn.close()


@app.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: int, request: Request, _=Depends(require_token)):
    user_id = _current_user_id(request)
    conn = _db()
    try:
        playlist = _playlist_or_404(conn, playlist_id, user_id)
        rows = conn.execute(
            "SELECT data FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()
    finally:
        conn.close()
    tracks = []
    for row in rows:
        try:
            tracks.append(json.loads(row["data"]))
        except json.JSONDecodeError:
            continue
    return {"id": playlist["id"], "name": playlist["name"], "tracks": tracks}


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int, request: Request, _=Depends(require_token)):
    user_id = _current_user_id(request)
    conn = _db()
    try:
        _playlist_or_404(conn, playlist_id, user_id)
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted"}


@app.post("/api/playlists/{playlist_id}/tracks")
def add_track(
    playlist_id: int,
    request: Request,
    track: dict = Body(...),
    _=Depends(require_token),
):
    user_id = _current_user_id(request)
    video_id = track.get("videoId")
    if not video_id:
        raise HTTPException(status_code=400, detail="track.videoId is required")
    conn = _db()
    try:
        _playlist_or_404(conn, playlist_id, user_id)
        exists = conn.execute(
            "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        ).fetchone()
        if exists:
            return {"status": "exists"}
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS pos FROM playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, video_id, position, data, added_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (playlist_id, video_id, row["pos"], json.dumps(track), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "added"}


@app.delete("/api/playlists/{playlist_id}/tracks/{video_id}")
def remove_track(
    playlist_id: int, video_id: str, request: Request, _=Depends(require_token)
):
    user_id = _current_user_id(request)
    conn = _db()
    try:
        _playlist_or_404(conn, playlist_id, user_id)
        conn.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "removed"}


def _sanitize(name: str | None) -> str:
    cleaned = (name or "").strip()
    cleaned = re.sub(r'[/\\:*?"<>|]', "_", cleaned)
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or "Unknown"


def _artist_names(track: dict) -> list[str]:
    names = []
    for a in track.get("artists", []):
        if isinstance(a, dict) and a.get("name"):
            names.append(a["name"])
        elif isinstance(a, str) and a:
            names.append(a)
    return names


async def _download_cover(url: str | None, dest: str) -> str | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and resp.content:
            with open(dest, "wb") as fh:
                fh.write(resp.content)
            return dest
    except Exception:
        return None
    return None


async def _save_track(track: dict):
    video_id = track.get("videoId")
    if not video_id:
        raise HTTPException(status_code=400, detail="track.videoId is required")

    names = _artist_names(track)
    artist_dir = _sanitize(names[0] if names else "Unknown Artist")
    album_dir = _sanitize(track.get("album") or "Singles")
    title = _sanitize(track.get("title"))
    target_dir = os.path.join(MUSIC_DIR, artist_dir, album_dir)
    os.makedirs(target_dir, exist_ok=True)
    final_path = os.path.join(target_dir, f"{title}.m4a")
    if os.path.exists(final_path):
        return ("exists", os.path.relpath(final_path, MUSIC_DIR))

    source_path = await asyncio.to_thread(
        _download_source, video_id, "bestaudio[ext=m4a]/bestaudio", "save"
    )
    cover_path = await _download_cover(
        track.get("thumbnail"), os.path.join(target_dir, f".cover-{video_id}.jpg")
    )
    tmp_path = final_path + ".part"
    try:
        codec = await _audio_codec(source_path)
        audio_args = (
            ["-c:a", "copy"] if codec == "aac" else ["-c:a", "aac", "-b:a", "256k"]
        )
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", source_path]
        if cover_path:
            cmd += ["-i", cover_path]
        cmd += ["-map", "0:a"]
        if cover_path:
            cmd += ["-map", "1:v", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        cmd += audio_args
        cmd += ["-metadata", f"title={track.get('title') or ''}"]
        if names:
            cmd += ["-metadata", f"artist={', '.join(names)}"]
            cmd += ["-metadata", f"album_artist={names[0]}"]
        if track.get("album"):
            cmd += ["-metadata", f"album={track['album']}"]
        cmd += ["-movflags", "+faststart", "-f", "ipod", tmp_path]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            detail = stderr.decode(errors="replace")[-300:] if stderr else "unknown"
            raise HTTPException(status_code=502, detail=f"save failed: {detail}")
        os.replace(tmp_path, final_path)
        return ("saved", os.path.relpath(final_path, MUSIC_DIR))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if cover_path and os.path.exists(cover_path):
            os.remove(cover_path)
        for leftover in glob.glob(os.path.join(CACHE_DIR, f".src-{video_id}.save.*")):
            try:
                os.remove(leftover)
            except FileNotFoundError:
                pass


@app.post("/api/save")
async def save(request: Request, track: dict = Body(...), _=Depends(require_token)):
    status, rel_path = await _save_track(track)
    _record_library_item(_current_user_id(request), track, rel_path)
    return {"status": status}


@app.post("/api/flac")
async def flac(payload: dict = Body(...), _=Depends(require_token)):
    if not (LIDARR_URL and LIDARR_API_KEY):
        raise HTTPException(status_code=503, detail="lidarr not configured")
    term = (payload.get("artist") or "").strip()
    if not term:
        raise HTTPException(status_code=400, detail="artist is required")

    headers = {"X-Api-Key": LIDARR_API_KEY}
    async with httpx.AsyncClient(
        base_url=LIDARR_URL, headers=headers, timeout=30.0
    ) as client:
        lookup = await client.get("/api/v1/artist/lookup", params={"term": term})
        if lookup.status_code != 200 or not lookup.json():
            raise HTTPException(status_code=404, detail="artist not found in lidarr")
        artist = lookup.json()[0]

        if artist.get("id"):
            await client.post(
                "/api/v1/command",
                json={"name": "ArtistSearch", "artistId": artist["id"]},
            )
            return {"status": "searching", "artist": artist.get("artistName")}

        meta = await client.get("/api/v1/metadataprofile")
        meta_id = 1
        if meta.status_code == 200 and meta.json():
            meta_id = meta.json()[0]["id"]
        artist.update(
            {
                "qualityProfileId": LIDARR_QUALITY_PROFILE,
                "metadataProfileId": meta_id,
                "rootFolderPath": LIDARR_ROOT,
                "monitored": True,
                "addOptions": {"monitor": "all", "searchForMissingAlbums": True},
            }
        )
        add = await client.post("/api/v1/artist", json=artist)
        if add.status_code >= 300:
            raise HTTPException(
                status_code=502, detail=f"lidarr add failed: {add.text[:200]}"
            )
    return {"status": "added", "artist": artist.get("artistName")}


@app.get("/", response_class=HTMLResponse)
def home():
    return LANDING_PAGE


LANDING_PAGE = """<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Sonora</title>
<style>
 :root{--bg:#121212;--surface:#1e1e1e;--line:#2a2a2a;--text:#e8e8e8;--muted:#9a9a9a;--accent:#d97757}
 *{box-sizing:border-box}
 body{font-family:system-ui,sans-serif;max-width:680px;margin:0 auto;padding:16px;background:var(--bg);color:var(--text)}
 h1{font-size:22px;margin:8px 0 4px}
 .sub{color:var(--muted);font-size:13px;margin-bottom:20px}
 .row{display:flex;gap:8px;margin-bottom:12px}
 input{flex:1;padding:12px;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--text);font-size:16px}
 button{padding:12px 18px;border-radius:10px;border:0;background:var(--accent);color:#1a0e08;font-weight:700;font-size:15px;cursor:pointer}
 .track{display:flex;gap:12px;align-items:center;padding:8px;border-radius:10px;cursor:pointer}
 .track:hover{background:var(--surface)}
 .track img{width:48px;height:48px;border-radius:6px;object-fit:cover;background:var(--surface)}
 .track .meta{flex:1;min-width:0}
 .track .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .track .a{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 audio{width:100%;margin-top:16px;position:sticky;bottom:8px}
</style></head><body>
<h1>Sonora</h1>
<div class=sub>Self-hosted music streaming. Test interface.</div>
<div class=row id=loginrow><input id=user placeholder="Usuario"><input id=pass placeholder="Contrasena" type=password><button onclick=login()>Entrar</button></div>
<div id=loginhint style="display:none;margin-bottom:12px;font-size:12px;color:var(--muted)">Sesion iniciada. <a href="#" onclick="logoutui();return false" style="color:var(--accent)">Salir</a></div>
<div class=row><input id=q placeholder="Search a song..." autofocus><button onclick=go()>Search</button></div>
<div id=results></div>
<audio id=player controls></audio>
<script>
let sess=localStorage.getItem('sonora_session')||'';
const loginrow=document.getElementById('loginrow');
const loginhint=document.getElementById('loginhint');
function showLogged(){loginrow.style.display='none';loginhint.style.display='block'}
function showLogin(){loginrow.style.display='flex';loginhint.style.display='none'}
if(sess){showLogged()}else{showLogin()}
async function login(){
 const username=document.getElementById('user').value.trim();
 const password=document.getElementById('pass').value;
 const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
 if(!r.ok){alert('Login fallido');return}
 const d=await r.json();sess=d.token;localStorage.setItem('sonora_session',sess);showLogged()
}
function logoutui(){localStorage.removeItem('sonora_session');sess='';showLogin()}
function auth(u){return sess?u+(u.includes('?')?'&':'?')+'token='+encodeURIComponent(sess):u}
async function go(){
 const q=document.getElementById('q').value;
 const r=await fetch(auth('/api/search?q='+encodeURIComponent(q)));
 if(!r.ok){document.getElementById('results').innerHTML='<div class=sub>Error '+r.status+'</div>';return}
 const d=await r.json();const res=document.getElementById('results');res.innerHTML='';
 for(const t of d.results){
  const el=document.createElement('div');el.className='track';
  el.innerHTML='<img src="'+(t.thumbnail||'')+'"><div class=meta><div class=t>'+(t.title||'')+'</div><div class=a>'+((t.artists||[]).join(', '))+(t.duration?' - '+t.duration:'')+'</div></div>';
  el.onclick=()=>{const p=document.getElementById('player');p.src=auth('/api/stream/'+t.videoId);p.play()};
  res.appendChild(el);
 }
}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script>
</body></html>"""


_seed_default_user()


DEMO_USER = os.getenv("SONORA_DEMO_USER", "demo").strip().lower()
DEMO_PASSWORD = os.getenv("SONORA_DEMO_PASSWORD", "demo")


def _seed_demo_user() -> None:
    if not DEMO_USER or not DEMO_PASSWORD:
        return
    conn = _db()
    try:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (DEMO_USER,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (DEMO_USER, _hash_password(DEMO_PASSWORD), time.time()),
            )
            conn.commit()
    finally:
        conn.close()


@app.delete("/api/account")
def delete_account(request: Request, _=Depends(require_token)):
    user_id = _current_user_id(request)
    conn = _db()
    try:
        row = conn.execute(
            "SELECT username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        username = row["username"] if row else ""
        conn.execute("DELETE FROM playlists WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        if username != DEMO_USER:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


_seed_demo_user()


def _init_ratings() -> None:
    conn = _db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ratings ("
            "user_id INTEGER NOT NULL, video_id TEXT NOT NULL, "
            "rating INTEGER NOT NULL, updated_at REAL NOT NULL, "
            "PRIMARY KEY (user_id, video_id))"
        )
        conn.commit()
    finally:
        conn.close()


_init_ratings()


@app.get("/api/rate/{video_id}")
def get_rating(video_id: str, request: Request, _=Depends(require_token)):
    user_id = _current_user_id(request)
    conn = _db()
    try:
        row = conn.execute(
            "SELECT rating FROM ratings WHERE user_id = ? AND video_id = ?",
            (user_id, video_id),
        ).fetchone()
    finally:
        conn.close()
    return {"rating": row["rating"] if row else 0}


@app.post("/api/rate")
def set_rating(request: Request, payload: dict = Body(...), _=Depends(require_token)):
    user_id = _current_user_id(request)
    video_id = str(payload.get("videoId") or "").strip()
    rating = int(payload.get("rating") or 0)
    if not video_id or rating < 0 or rating > 5:
        raise HTTPException(status_code=400, detail="videoId and rating (0-5) required")
    conn = _db()
    try:
        if rating == 0:
            conn.execute(
                "DELETE FROM ratings WHERE user_id = ? AND video_id = ?",
                (user_id, video_id),
            )
        else:
            conn.execute(
                "INSERT INTO ratings (user_id, video_id, rating, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, video_id) DO UPDATE SET "
                "rating = excluded.rating, updated_at = excluded.updated_at",
                (user_id, video_id, rating, time.time()),
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "rating": rating}


_LYRICS_CACHE: dict = {}


_LIB_AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".opus", ".ogg", ".aac", ".wav"}
_LIB_IMG_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png")
_LIB_CACHE = {"ts": 0.0, "songs": None}
_LIB_TTL = 1800.0


def _dir_cover(root, files):
    lower = {f.lower(): f for f in files}
    for name in _LIB_IMG_NAMES:
        if name in lower:
            return os.path.join(root, lower[name])
    for f in files:
        if f.lower().endswith((".jpg", ".jpeg", ".png")) and not f.startswith("."):
            return os.path.join(root, f)
    return None


def _read_tags(full):
    try:
        from mutagen import File as _MF
        m = _MF(full, easy=True)
        if not m or not m.tags:
            return {}
        t = m.tags
        def g(*keys):
            for k in keys:
                v = t.get(k)
                if v:
                    val = v[0] if isinstance(v, list) else v
                    if val:
                        return str(val).strip()
            return None
        return {"title": g("title"), "artist": g("albumartist", "artist"), "album": g("album"), "genre": g("genre")}
    except Exception:
        return {}


def _scan_library():
    now = time.time()
    if _LIB_CACHE["songs"] is not None and now - _LIB_CACHE["ts"] < _LIB_TTL:
        return _LIB_CACHE["songs"]
    base = os.path.realpath(MUSIC_DIR)
    songs = []
    if os.path.isdir(base):
        for root, dirs, files in os.walk(base):
            audio = [f for f in files if os.path.splitext(f)[1].lower() in _LIB_AUDIO_EXTS]
            if not audio:
                continue
            cover_full = _dir_cover(root, files)
            cover_rel = os.path.relpath(cover_full, base) if cover_full else None
            for f in audio:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, base)
                parts = rel.split(os.sep)
                ftitle = os.path.splitext(parts[-1])[0]
                if len(parts) >= 3:
                    fartist, falbum = parts[0], parts[1]
                elif len(parts) == 2:
                    fartist, falbum = parts[0], "Singles"
                else:
                    fartist, falbum = "Unknown Artist", "Singles"
                tags = _read_tags(full)
                title = tags.get("title") or ftitle
                artist = tags.get("artist") or fartist
                album = tags.get("album") or falbum
                genre = tags.get("genre") or "Unknown"
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    mtime = 0.0
                songs.append({
                    "title": title, "artist": artist, "album": album,
                    "path": rel, "cover": cover_rel, "mtime": mtime, "genre": genre,
                })
    _LIB_CACHE["songs"] = songs
    _LIB_CACHE["ts"] = now
    return songs


@app.get("/api/library/songs")
def library_songs(request: Request, artist: str = "", album: str = "", genre: str = "", sort: str = "", limit: int = 0, _=Depends(require_token)):
    songs = [dict(r) for r in _library_rows(_current_user_id(request))]
    if artist:
        songs = [s for s in songs if (s.get("artist") or "") == artist]
    if album:
        songs = [s for s in songs if (s.get("album") or "") == album]
    if genre:
        songs = [s for s in songs if genre in _split_genres(s.get("genre"))]
    if sort == "recent":
        songs.sort(key=lambda s: s.get("added_at") or 0, reverse=True)
    else:
        songs.sort(key=lambda s: ((s.get("artist") or "").lower(), (s.get("album") or "").lower(), (s.get("title") or "").lower()))
    if limit and limit > 0:
        songs = songs[:limit]
    return {"songs": songs}


@app.get("/api/library/artists")
def library_artists(request: Request, _=Depends(require_token)):
    by = {}
    for s in _library_rows(_current_user_id(request)):
        name = s["artist"] or "Unknown Artist"
        a = by.setdefault(name, {"name": name, "albums": set(), "songs": 0, "cover": None})
        a["albums"].add(s["album"] or "Singles")
        a["songs"] += 1
        if a["cover"] is None and s["cover"]:
            a["cover"] = s["cover"]
    out = [{"name": a["name"], "albums": len(a["albums"]), "songs": a["songs"], "cover": a["cover"]} for a in by.values()]
    out.sort(key=lambda a: a["name"].lower())
    return {"artists": out}


@app.get("/api/library/albums")
def library_albums(request: Request, artist: str = "", _=Depends(require_token)):
    by = {}
    for s in _library_rows(_current_user_id(request)):
        art = s["artist"] or "Unknown Artist"
        if artist and art != artist:
            continue
        key = (art, s["album"] or "Singles")
        a = by.setdefault(key, {"artist": key[0], "album": key[1], "songs": 0, "cover": None})
        a["songs"] += 1
        if a["cover"] is None and s["cover"]:
            a["cover"] = s["cover"]
    out = list(by.values())
    out.sort(key=lambda a: (a["artist"].lower(), a["album"].lower()))
    return {"albums": out}


def _split_genres(raw):
    if not raw:
        return ["Unknown"]
    parts = re.split(r"[;/,]", raw)
    out = [x.strip() for x in parts if x.strip()]
    return out or ["Unknown"]


@app.get("/api/library/genres")
def library_genres(request: Request, _=Depends(require_token)):
    by = {}
    for s in _library_rows(_current_user_id(request)):
        for g in _split_genres(s["genre"]):
            a = by.setdefault(g, {"name": g, "songs": 0, "cover": None})
            a["songs"] += 1
            if a["cover"] is None and s["cover"]:
                a["cover"] = s["cover"]
    out = list(by.values())
    out.sort(key=lambda a: a["name"].lower())
    return {"genres": out}


@app.get("/api/library/file")
def library_file(path: str, _=Depends(require_token)):
    base = os.path.realpath(MUSIC_DIR)
    full = os.path.realpath(os.path.join(base, path))
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(status_code=403, detail="forbidden")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(full)



def _init_library():
    conn = _db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS library_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
            "video_id TEXT NOT NULL, title TEXT, artist TEXT, album TEXT, genre TEXT, "
            "path TEXT NOT NULL, cover TEXT, added_at REAL NOT NULL, "
            "UNIQUE(user_id, video_id))"
        )
        conn.commit()
    finally:
        conn.close()


_init_library()


def _library_rows(user_id):
    if not user_id:
        return []
    conn = _db()
    try:
        return conn.execute(
            "SELECT video_id, title, artist, album, genre, path, cover, added_at "
            "FROM library_items WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def _record_library_item(user_id, track, rel_path):
    if not user_id or not rel_path:
        return
    names = _artist_names(track)
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO library_items (user_id, video_id, title, artist, album, genre, path, cover, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, video_id) DO UPDATE SET path=excluded.path, cover=excluded.cover",
            (
                user_id, track.get("videoId"), track.get("title"),
                names[0] if names else "Unknown Artist",
                track.get("album") or "Singles",
                track.get("genre") or "Unknown",
                rel_path, track.get("thumbnail"), time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


_ARTIST_CACHE = {}


# ---------------------------------------------------------------------------
# Per-user favorites, saved albums, and recently played
# ---------------------------------------------------------------------------

_RECENT_LIMIT = 60


def _init_userdata():
    conn = _db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fav_songs ("
            "user_id INTEGER NOT NULL, video_id TEXT NOT NULL, data TEXT NOT NULL, "
            "added_at REAL NOT NULL, PRIMARY KEY(user_id, video_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fav_artists ("
            "user_id INTEGER NOT NULL, name TEXT NOT NULL, "
            "added_at REAL NOT NULL, PRIMARY KEY(user_id, name))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS saved_albums ("
            "user_id INTEGER NOT NULL, browse_id TEXT NOT NULL, data TEXT NOT NULL, "
            "added_at REAL NOT NULL, PRIMARY KEY(user_id, browse_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS recent_tracks ("
            "user_id INTEGER NOT NULL, video_id TEXT NOT NULL, data TEXT NOT NULL, "
            "played_at REAL NOT NULL, PRIMARY KEY(user_id, video_id))"
        )
        conn.commit()
    finally:
        conn.close()


_init_userdata()


def _uid_or_401(request):
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="authentication required")
    return uid


@app.get("/api/fav/songs")
def fav_songs_list(request: Request, _=Depends(require_token)):
    import json as _json
    uid = _uid_or_401(request)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT data FROM fav_songs WHERE user_id = ? ORDER BY added_at DESC", (uid,)
        ).fetchall()
    finally:
        conn.close()
    return {"songs": [_json.loads(r["data"]) for r in rows]}


@app.post("/api/fav/songs")
async def fav_songs_add(request: Request, track: dict = Body(...), _=Depends(require_token)):
    import json as _json
    uid = _uid_or_401(request)
    vid = track.get("videoId")
    if not vid:
        raise HTTPException(status_code=400, detail="videoId required")
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO fav_songs (user_id, video_id, data, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, video_id) DO UPDATE SET data=excluded.data",
            (uid, vid, _json.dumps(track), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/fav/songs")
def fav_songs_del(request: Request, videoId: str, _=Depends(require_token)):
    uid = _uid_or_401(request)
    conn = _db()
    try:
        conn.execute("DELETE FROM fav_songs WHERE user_id = ? AND video_id = ?", (uid, videoId))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/api/fav/artists")
def fav_artists_list(request: Request, _=Depends(require_token)):
    uid = _uid_or_401(request)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT name FROM fav_artists WHERE user_id = ? ORDER BY added_at DESC", (uid,)
        ).fetchall()
    finally:
        conn.close()
    return {"artists": [r["name"] for r in rows]}


@app.post("/api/fav/artists")
async def fav_artists_add(request: Request, body: dict = Body(...), _=Depends(require_token)):
    uid = _uid_or_401(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO fav_artists (user_id, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, name) DO NOTHING",
            (uid, name, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/fav/artists")
def fav_artists_del(request: Request, name: str, _=Depends(require_token)):
    uid = _uid_or_401(request)
    conn = _db()
    try:
        conn.execute("DELETE FROM fav_artists WHERE user_id = ? AND name = ?", (uid, name))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/api/saved-albums")
def saved_albums_list(request: Request, _=Depends(require_token)):
    import json as _json
    uid = _uid_or_401(request)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT data FROM saved_albums WHERE user_id = ? ORDER BY added_at DESC", (uid,)
        ).fetchall()
    finally:
        conn.close()
    return {"albums": [_json.loads(r["data"]) for r in rows]}


@app.post("/api/saved-albums")
async def saved_albums_add(request: Request, album: dict = Body(...), _=Depends(require_token)):
    import json as _json
    uid = _uid_or_401(request)
    bid = album.get("browseId")
    if not bid:
        raise HTTPException(status_code=400, detail="browseId required")
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO saved_albums (user_id, browse_id, data, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, browse_id) DO UPDATE SET data=excluded.data",
            (uid, bid, _json.dumps(album), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/saved-albums")
def saved_albums_del(request: Request, browseId: str, _=Depends(require_token)):
    uid = _uid_or_401(request)
    conn = _db()
    try:
        conn.execute("DELETE FROM saved_albums WHERE user_id = ? AND browse_id = ?", (uid, browseId))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/api/recent")
def recent_list(request: Request, _=Depends(require_token)):
    import json as _json
    uid = _uid_or_401(request)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT data FROM recent_tracks WHERE user_id = ? ORDER BY played_at DESC LIMIT ?",
            (uid, _RECENT_LIMIT),
        ).fetchall()
    finally:
        conn.close()
    return {"tracks": [_json.loads(r["data"]) for r in rows]}


@app.post("/api/recent")
async def recent_add(request: Request, track: dict = Body(...), _=Depends(require_token)):
    import json as _json
    uid = _uid_or_401(request)
    vid = track.get("videoId")
    if not vid:
        raise HTTPException(status_code=400, detail="videoId required")
    now = time.time()
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO recent_tracks (user_id, video_id, data, played_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, video_id) DO UPDATE SET data=excluded.data, played_at=excluded.played_at",
            (uid, vid, _json.dumps(track), now),
        )
        conn.execute(
            "DELETE FROM recent_tracks WHERE user_id = ? AND video_id NOT IN "
            "(SELECT video_id FROM recent_tracks WHERE user_id = ? ORDER BY played_at DESC LIMIT ?)",
            (uid, uid, _RECENT_LIMIT),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/api/recent")
def recent_clear(request: Request, _=Depends(require_token)):
    uid = _uid_or_401(request)
    conn = _db()
    try:
        conn.execute("DELETE FROM recent_tracks WHERE user_id = ?", (uid,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.on_event("startup")
async def _warmup_youtube():
    async def _run():
        try:
            await asyncio.to_thread(ytmusic.search, "top hits", "songs", None, 3)
        except Exception:
            pass
    try:
        asyncio.create_task(_run())
    except Exception:
        pass




# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Alexa custom skill (AudioPlayer) + Account Linking (OAuth)
# ---------------------------------------------------------------------------

ALEXA_SKILL_ID = os.getenv("ALEXA_SKILL_ID", "").strip()
ALEXA_TOKEN = os.getenv("ALEXA_TOKEN", "change-me").strip()
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL", "https://your-server.example.com"
).rstrip("/")
ALEXA_CLIENT_ID = os.getenv("ALEXA_CLIENT_ID", "sonora-alexa").strip()
ALEXA_CLIENT_SECRET = os.getenv(
    "ALEXA_CLIENT_SECRET", "sonora-alexa-secret-change-me"
).strip()

_ALEXA_STATE = {}
_OAUTH_CODES = {}
_ALEXA_REDIRECT_HOSTS = (
    "layla.amazon.com",
    "pitangui.amazon.com",
    "alexa.amazon.co.jp",
)


def _seed_alexa_token():
    conn = _db()
    try:
        uname = os.getenv("ALEXA_USER", "").strip().lower()
        if uname:
            urow = conn.execute(
                "SELECT id FROM users WHERE username = ?", (uname,)
            ).fetchone()
        else:
            urow = conn.execute(
                "SELECT id FROM users ORDER BY id LIMIT 1"
            ).fetchone()
        if not urow:
            return
        uid = urow["id"]
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE token = ?", (ALEXA_TOKEN,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                (ALEXA_TOKEN, uid, time.time()),
            )
            conn.commit()
        elif uname and row["user_id"] != uid:
            conn.execute(
                "UPDATE sessions SET user_id = ? WHERE token = ?", (uid, ALEXA_TOKEN)
            )
            conn.commit()
    finally:
        conn.close()


def _init_oauth():
    conn = _db()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS oauth_refresh ("
            "token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at REAL NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


_seed_alexa_token()
_init_oauth()


def _oauth_valid_redirect(redirect_uri):
    try:
        from urllib.parse import urlparse

        host = urlparse(redirect_uri).netloc.lower()
        return any(
            host == h or host.endswith("." + h) for h in _ALEXA_REDIRECT_HOSTS
        )
    except Exception:
        return False


@app.get("/oauth/authorize", response_class=HTMLResponse)
def oauth_authorize(
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    response_type: str = "code",
    scope: str = "",
):
    if client_id != ALEXA_CLIENT_ID or not _oauth_valid_redirect(redirect_uri):
        return HTMLResponse("<h3>Solicitud invalida</h3>", status_code=400)
    import html as _html

    ru = _html.escape(redirect_uri)
    st = _html.escape(state)
    cid = _html.escape(client_id)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Vincular Sonora con Alexa</title><style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#000;color:#fff;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
        ".card{background:#1c1c1e;padding:28px;border-radius:16px;width:320px}"
        "h2{margin:0 0 4px}p{color:#98989f;margin:0 0 18px;font-size:14px}"
        "input{width:100%;box-sizing:border-box;padding:12px;margin:6px 0;border-radius:10px;"
        "border:none;background:#2c2c2e;color:#fff;font-size:16px}"
        "button{width:100%;padding:13px;margin-top:12px;border:none;border-radius:10px;"
        "background:#d97757;color:#000;font-weight:700;font-size:16px}</style></head>"
        "<body><form class=\"card\" method=\"post\" action=\"/oauth/authorize\">"
        "<h2>Sonora</h2><p>Vincula tu cuenta con Alexa</p>"
        "<input name=\"username\" placeholder=\"Usuario\" autocapitalize=\"none\" autocorrect=\"off\" required>"
        "<input name=\"password\" type=\"password\" placeholder=\"Contrasena\" required>"
        f"<input type=\"hidden\" name=\"client_id\" value=\"{cid}\">"
        f"<input type=\"hidden\" name=\"redirect_uri\" value=\"{ru}\">"
        f"<input type=\"hidden\" name=\"state\" value=\"{st}\">"
        "<button type=\"submit\">Vincular</button></form></body></html>"
    )


@app.post("/oauth/authorize")
def oauth_authorize_post(
    username: str = Form(...),
    password: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
):
    if client_id != ALEXA_CLIENT_ID or not _oauth_valid_redirect(redirect_uri):
        return HTMLResponse("<h3>Solicitud invalida</h3>", status_code=400)
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ?",
            (username.strip().lower(),),
        ).fetchone()
    finally:
        conn.close()
    if not row or not _verify_password(password, row["password_hash"]):
        return HTMLResponse(
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<style>body{font-family:sans-serif;background:#000;color:#fff;text-align:center;"
            "padding-top:60px}a{color:#d97757}</style></head><body>"
            "<h3>Usuario o contrasena incorrectos</h3>"
            "<a href=\"javascript:history.back()\">Volver</a></body></html>",
            status_code=401,
        )
    code = secrets.token_hex(16)
    _OAUTH_CODES[code] = (row["id"], time.time() + 300)
    from urllib.parse import urlencode

    sep = "&" if "?" in redirect_uri else "?"
    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(
        url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302
    )


@app.post("/oauth/token")
async def oauth_token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")
    import base64

    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            dec = base64.b64decode(auth[6:]).decode()
            bid, bsecret = dec.split(":", 1)
            client_id = client_id or bid
            client_secret = client_secret or bsecret
        except Exception:
            pass
    if client_id != ALEXA_CLIENT_ID or client_secret != ALEXA_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "authorization_code":
        entry = _OAUTH_CODES.pop(form.get("code"), None)
        if not entry or entry[1] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        user_id = entry[0]
    elif grant_type == "refresh_token":
        conn = _db()
        try:
            r = conn.execute(
                "SELECT user_id FROM oauth_refresh WHERE token = ?",
                (form.get("refresh_token"),),
            ).fetchone()
        finally:
            conn.close()
        if not r:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        user_id = r["user_id"]
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    conn = _db()
    try:
        access_token = _create_session(conn, user_id)
        refresh_token = secrets.token_hex(24)
        conn.execute(
            "INSERT INTO oauth_refresh (token, user_id, created_at) VALUES (?, ?, ?)",
            (refresh_token, user_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "bearer",
            "refresh_token": refresh_token,
            "expires_in": 3600 * 24 * 365,
        }
    )


def _user_id_from_token(token):
    if not token:
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        return row["user_id"] if row else None
    finally:
        conn.close()


def _alexa_stream_url(video_id, token=None):
    return f"{PUBLIC_BASE_URL}/api/stream/{video_id}?q=high&token={token or ALEXA_TOKEN}"


def _alexa_search(query):
    try:
        raw = ytmusic.search(query, filter="songs", limit=10)
    except Exception:
        raw = []
    songs = [x for x in (_song_result(r) for r in raw) if x]
    if not songs:
        return []
    top = songs[0]
    out = [{
        "videoId": top["videoId"],
        "title": top.get("title") or "",
        "artist": (top.get("artists") or [{}])[0].get("name") or "",
    }]
    seen = {top["videoId"]}
    try:
        watch = ytmusic.get_watch_playlist(videoId=top["videoId"], radio=True)
        tracks = watch.get("tracks") or []
    except Exception:
        tracks = []
    for t in tracks:
        vid = t.get("videoId")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        arts = t.get("artists") or []
        artist = ", ".join(a.get("name") for a in arts if isinstance(a, dict) and a.get("name"))
        out.append({"videoId": vid, "title": t.get("title") or "", "artist": artist})
    if len(out) < 5:
        for x in songs:
            if x["videoId"] in seen:
                continue
            seen.add(x["videoId"])
            out.append({"videoId": x["videoId"], "title": x.get("title") or "", "artist": (x.get("artists") or [{}])[0].get("name") or ""})
    return out


def _alexa_album(query):
    if not query.strip():
        return []
    try:
        res = ytmusic.search(query, filter="albums", limit=1)
        if not res:
            return []
        album = ytmusic.get_album(res[0]["browseId"])
    except Exception:
        return []
    out = []
    for t in album.get("tracks", []) or []:
        vid = t.get("videoId")
        if not vid:
            continue
        artists = ", ".join(
            a.get("name") for a in (t.get("artists") or []) if a.get("name")
        )
        out.append({
            "videoId": vid,
            "title": t.get("title") or "",
            "artist": artists or (album.get("title") or ""),
        })
    return out


def _alexa_playlist(query, token):
    import json as _json

    if not query.strip():
        return [], ""
    uid = _user_id_from_token(token)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, name FROM playlists WHERE user_id IS ?", (uid,)
        ).fetchall()
    finally:
        conn.close()
    ql = query.strip().lower()
    target = None
    for r in rows:
        if (r["name"] or "").lower() == ql:
            target = r
            break
    if target is None:
        for r in rows:
            n = (r["name"] or "").lower()
            if ql in n or n in ql:
                target = r
                break
    if target is None:
        return [], ""
    conn = _db()
    try:
        trows = conn.execute(
            "SELECT data FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
            (target["id"],),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for tr in trows:
        try:
            d = _json.loads(tr["data"])
        except Exception:
            continue
        vid = d.get("videoId")
        if not vid:
            continue
        artists = d.get("artists") or []
        artist = artists[0].get("name") if artists and isinstance(artists[0], dict) else ""
        out.append({"videoId": vid, "title": d.get("title") or "", "artist": artist or ""})
    return out, target["name"]


def _alexa_speak(text, end=True):
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end,
        },
    }


def _alexa_empty():
    return {"version": "1.0", "response": {"shouldEndSession": True}}


def _alexa_stop():
    return {
        "version": "1.0",
        "response": {
            "directives": [{"type": "AudioPlayer.Stop"}],
            "shouldEndSession": True,
        },
    }


def _alexa_link_account():
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": "Para usar Sonora, abre la app de Alexa, entra a la configuracion de esta skill y toca Vincular cuenta para iniciar sesion."},
            "card": {"type": "LinkAccount"},
            "shouldEndSession": True,
        },
    }


def _alexa_play(item, index, behavior="REPLACE_ALL", offset=0, prev_token=None, speak=None, token=None):
    stream = {
        "token": f"{index}|{item['videoId']}",
        "url": _alexa_stream_url(item["videoId"], token),
        "offsetInMilliseconds": offset,
    }
    if behavior == "ENQUEUE" and prev_token:
        stream["expectedPreviousToken"] = prev_token
    response = {
        "directives": [{
            "type": "AudioPlayer.Play",
            "playBehavior": behavior,
            "audioItem": {
                "stream": stream,
                "metadata": {
                    "title": item.get("title") or "",
                    "subtitle": item.get("artist") or "",
                },
            },
        }],
        "shouldEndSession": True,
    }
    if speak:
        response["outputSpeech"] = {"type": "PlainText", "text": speak}
    return {"version": "1.0", "response": response}


def _init_alexa_state():
    conn = _db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS alexa_state (user_key TEXT PRIMARY KEY, queue TEXT, idx INTEGER, offset REAL, updated_at REAL)")
        conn.commit()
    finally:
        conn.close()


_init_alexa_state()


def _alexa_load_state(key):
    import json as _json
    conn = _db()
    try:
        row = conn.execute("SELECT queue, idx, offset FROM alexa_state WHERE user_key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"queue": [], "index": 0, "offset": 0}
    try:
        q = _json.loads(row["queue"])
    except Exception:
        q = []
    return {"queue": q, "index": row["idx"] or 0, "offset": row["offset"] or 0}


def _alexa_save_state(key, state):
    import json as _json
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO alexa_state (user_key, queue, idx, offset, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_key) DO UPDATE SET queue=excluded.queue, idx=excluded.idx, offset=excluded.offset, updated_at=excluded.updated_at",
            (key, _json.dumps(state["queue"]), state["index"], state["offset"], time.time()),
        )
        conn.commit()
    finally:
        conn.close()


@app.post("/alexa")
async def alexa(request: Request):
    body = await request.json()
    app_id = (
        ((body.get("context") or {}).get("System") or {}).get("application", {}).get("applicationId")
        or (body.get("session") or {}).get("application", {}).get("applicationId")
    )
    if ALEXA_SKILL_ID and app_id != ALEXA_SKILL_ID:
        raise HTTPException(status_code=403, detail="invalid skill id")

    req = body.get("request") or {}
    rtype = req.get("type")
    sys_user = ((body.get("context") or {}).get("System") or {}).get("user") or {}
    user_key = sys_user.get("userId") or "default"
    access_token = sys_user.get("accessToken")
    user_token = access_token or ALEXA_TOKEN
    state = _alexa_load_state(user_key)

    if access_token is None and rtype in ("LaunchRequest", "IntentRequest"):
        return _alexa_link_account()

    if rtype == "LaunchRequest":
        return _alexa_speak("Bienvenido a Sonora. Di, por ejemplo, pon reggaeton.", end=False)

    if rtype == "SessionEndedRequest":
        return _alexa_empty()

    if rtype and rtype.startswith("AudioPlayer."):
        sub = rtype.split(".", 1)[1]
        token = req.get("token") or ""
        offset = req.get("offsetInMilliseconds") or 0
        if sub == "PlaybackStarted":
            try:
                state["index"] = int(token.split("|", 1)[0])
                _alexa_save_state(user_key, state)
            except Exception:
                pass
        elif sub in ("PlaybackStopped", "PlaybackNearlyFinished"):
            state["offset"] = offset
            _alexa_save_state(user_key, state)
        if sub == "PlaybackNearlyFinished":
            nxt = state["index"] + 1
            if 0 <= nxt < len(state["queue"]):
                return _alexa_play(state["queue"][nxt], nxt, behavior="ENQUEUE", prev_token=token, token=user_token)
        return _alexa_empty()

    if rtype == "IntentRequest":
        intent = req.get("intent") or {}
        name = intent.get("name")
        if name == "PlayQueryIntent":
            query = ((intent.get("slots") or {}).get("query") or {}).get("value") or ""
            if not query.strip():
                return _alexa_speak("No entendi que quieres escuchar.", end=False)
            results = _alexa_search(query)
            if not results:
                return _alexa_speak(f"No encontre nada para {query}.")
            state = {"queue": results, "index": 0, "offset": 0}
            _alexa_save_state(user_key, state)
            item = results[0]
            return _alexa_play(item, 0, speak=f"Reproduciendo {item['title']} de {item['artist']}.", token=user_token)
        if name == "PlayAlbumIntent":
            query = ((intent.get("slots") or {}).get("query") or {}).get("value") or ""
            results = _alexa_album(query)
            if not results:
                return _alexa_speak(f"No encontre el album {query}.")
            state = {"queue": results, "index": 0, "offset": 0}
            _alexa_save_state(user_key, state)
            return _alexa_play(results[0], 0, speak=f"Reproduciendo el album {query}.", token=user_token)
        if name == "PlayPlaylistIntent":
            query = ((intent.get("slots") or {}).get("query") or {}).get("value") or ""
            results, pname = _alexa_playlist(query, user_token)
            if not results:
                return _alexa_speak(f"No encontre la playlist {query}.")
            state = {"queue": results, "index": 0, "offset": 0}
            _alexa_save_state(user_key, state)
            return _alexa_play(results[0], 0, speak=f"Reproduciendo la playlist {pname}.", token=user_token)
        if name == "AMAZON.NextIntent":
            nxt = state["index"] + 1
            if 0 <= nxt < len(state["queue"]):
                state["index"] = nxt
                state["offset"] = 0
                _alexa_save_state(user_key, state)
                return _alexa_play(state["queue"][nxt], nxt, token=user_token)
            return _alexa_speak("No hay mas canciones.")
        if name == "AMAZON.PreviousIntent":
            prv = state["index"] - 1
            if 0 <= prv < len(state["queue"]):
                state["index"] = prv
                state["offset"] = 0
                _alexa_save_state(user_key, state)
                return _alexa_play(state["queue"][prv], prv, token=user_token)
            return _alexa_speak("No hay cancion anterior.")
        if name in ("AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return _alexa_stop()
        if name == "AMAZON.ResumeIntent":
            if state["queue"] and 0 <= state["index"] < len(state["queue"]):
                return _alexa_play(state["queue"][state["index"]], state["index"], offset=state.get("offset", 0), token=user_token)
            return _alexa_speak("No hay nada para reanudar.")
        if name == "AMAZON.HelpIntent":
            return _alexa_speak("Di, pon, seguido del nombre de una cancion, artista, album o playlist.", end=False)
        return _alexa_speak("No entendi. Intenta decir, pon una cancion.", end=False)

    return _alexa_empty()
