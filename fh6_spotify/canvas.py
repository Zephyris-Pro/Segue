"""Auto-resolve the current track's Spotify Canvas video for the overlay.

Talks to the local canvas_service (scripts/canvas_service.py, runs in its own
librespot venv - see that file for why it's a separate process). On each track
change we ask it for the Canvas .cnvs.mp4 URL and stash it in ui["np_video"];
the overlay's video layer plays it (when shown + no manual URL set), hidden when
a track has no Canvas. Fully best-effort: the service being down, disabled, or
slow just leaves np_video empty -> overlay falls back to the cover.
"""
from __future__ import annotations
import json
import threading
import urllib.parse
import urllib.request
_last_key: str | None = None
_lock = threading.Lock()


def update_np_video(title: str, artist: str, ui: dict, enabled: bool, port: int) -> None:
    """Call each now-playing tick. Fires a background resolve only on a track
    change; sets ui['np_video'] when it returns. Non-blocking."""
    global _last_key
    if not enabled:
        if ui.get('np_video'):
            ui['np_video'] = ''
        _last_key = None
    else:
        key = f"{title or ''}|{artist or ''}"
        with _lock:
            if key == _last_key:
                return
            _last_key = key
        if not title:
            ui['np_video'] = ''
        else:
            threading.Thread(target=_resolve, args=(title, artist, key, ui, port), daemon=True).start()


def _resolve(title: str, artist: str, key: str, ui: dict, port: int) -> None:
    url = ''
    try:
        q = urllib.parse.urlencode({'track': title, 'artist': artist})
        req = urllib.request.Request(f'http://127.0.0.1:{port}/canvas?{q}', headers={'User-Agent': 'Segue'})
        with urllib.request.urlopen(req, timeout=9) as r:
            url = json.loads(r.read()).get('url') or ''
    except Exception:
        url = ''
    with _lock:
        if key != _last_key:
            return
        ui['np_video'] = url
