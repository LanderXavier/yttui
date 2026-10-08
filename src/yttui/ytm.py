"""Rutas de config y wrappers de YouTube Music + yt-dlp."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "yttui"
COOKIES_TXT = CONFIG_DIR / "cookies.txt"
BROWSER_JSON = CONFIG_DIR / "browser.json"


def ensure_config_dir() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def get_ytmusic():
    """Devuelve YTMusic autenticado si hay browser.json, si no anónimo."""
    from ytmusicapi import YTMusic

    ensure_config_dir()
    if BROWSER_JSON.exists():
        try:
            return YTMusic(auth=str(BROWSER_JSON)), True
        except Exception:
            return YTMusic(), False
    return YTMusic(), False


def norm_song(item: dict) -> dict:
    artists = item.get("artists") or []
    return {
        "videoId": item.get("videoId", ""),
        "title": item.get("title", "Sin título"),
        "artists": ", ".join(a.get("name", "") for a in artists) or "Desconocido",
        "album": (item.get("album") or {}).get("name", "") if isinstance(item.get("album"), dict) else "",
        "duration": item.get("duration", "") or "",
    }


def norm_playlist(item: dict) -> dict:
    return {
        "playlistId": item.get("playlistId", item.get("browseId", "")),
        "title": item.get("title", "Sin título"),
        "author": item.get("author", "") or "",
        "count": item.get("itemCount", "") or "",
    }


def search_songs(query: str, limit: int = 20) -> list[dict]:
    yt, _ = get_ytmusic()
    results = yt.search(query, filter="songs", limit=limit)
    out = []
    for r in results:
        if r.get("videoId"):
            out.append(norm_song(r))
    return out[:limit]


def search_playlists(query: str, limit: int = 10) -> list[dict]:
    """Busca playlists públicas (no necesita login)."""
    yt, _ = get_ytmusic()
    try:
        results = yt.search(query, filter="playlists", limit=limit)
    except Exception:
        return []
    out = []
    for r in results:
        pid = r.get("playlistId", r.get("browseId", ""))
        if pid:
            out.append(norm_playlist(r))
    return out[:limit]


def search_all(query: str, song_limit: int = 15, pl_limit: int = 8) -> tuple[list[dict], list[dict]]:
    """Devuelve (canciones, playlists)."""
    return search_songs(query, limit=song_limit), search_playlists(query, limit=pl_limit)


def get_liked_songs(limit: int = 100) -> tuple[list[dict], bool]:
    """Devuelve (canciones, necesita_auth)."""
    yt, authed = get_ytmusic()
    if not authed:
        return [], True
    try:
        data = yt.get_liked_songs(limit=limit)
        tracks = data.get("tracks", []) if isinstance(data, dict) else []
        return [norm_song(t) for t in tracks if t.get("videoId")], False
    except Exception:
        return [], True


def get_library_playlists(limit: int = 50) -> tuple[list[dict], bool]:
    yt, authed = get_ytmusic()
    if not authed:
        return [], True
    try:
        pls = yt.get_library_playlists(limit=limit)
        return [{"playlistId": p.get("playlistId", ""), "title": p.get("title", ""), "count": p.get("count", "")} for p in pls], False
    except Exception:
        return [], True


def get_playlist_tracks(playlist_id: str, limit: int = 100) -> list[dict]:
    yt, _ = get_ytmusic()
    pl = yt.get_playlist(playlist_id, limit=limit)
    out = []
    for t in pl.get("tracks", []):
        if t.get("videoId"):
            out.append(norm_song(t))
    return out


_url_cache: dict[str, tuple[str, float]] = {}  # videoId -> (url, cuando)
_URL_TTL = 4 * 3600  # las urls de googlevideo caducan en horas; vale para la sesión


def get_stream_url(video_id: str, timeout: int = 25) -> str:
    """Resuelve URL directa de audio con yt-dlp (sin ads). Usa caché de sesión."""
    hit = _url_cache.get(video_id)
    if hit and time.time() - hit[1] < _URL_TTL:
        return hit[0]
    url = f"https://music.youtube.com/watch?v={video_id}"
    cmd = ["yt-dlp", "-g", "-f", "bestaudio/best", "--no-playlist", "--no-warnings"]
    if COOKIES_TXT.exists():
        cmd += ["--cookies", str(COOKIES_TXT)]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "yt-dlp falló").strip()[-500:])
    first = (proc.stdout or "").strip().splitlines()
    if not first:
        raise RuntimeError("yt-dlp no devolvió URL")
    direct = first[0].strip()
    _url_cache[video_id] = (direct, time.time())
    return direct


def preresolve_url(video_id: str) -> None:
    """Resuelve en segundo plano para que el siguiente cambio sea instantáneo."""
    if not video_id or (video_id in _url_cache and time.time() - _url_cache[video_id][1] < _URL_TTL):
        return

    def job() -> None:
        try:
            get_stream_url(video_id)
        except Exception:
            pass

    threading.Thread(target=job, daemon=True).start()


def browser_json_status() -> dict:
    return {
        "config_dir": str(CONFIG_DIR),
        "cookies_exists": COOKIES_TXT.exists(),
        "browser_json_exists": BROWSER_JSON.exists(),
    }


def get_radio_tracks(video_id: str, limit: int = 15) -> list[dict]:
    """Temas parecidos para modo radio (como el autoplay de YouTube)."""
    yt, _ = get_ytmusic()
    try:
        data = yt.get_watch_playlist(videoId=video_id, limit=limit, radio=True)
    except Exception:
        return []
    out = []
    for t in data.get("tracks", []):
        if t.get("videoId") and t["videoId"] != video_id:
            out.append(norm_song(t))
    return out[:limit]
