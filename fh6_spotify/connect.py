"""Runner-side client for the local canvas_service Connect endpoints.\n\nDependency-free so the URL builder is unit-testable without a live Spotify\nsession. send_skip() is best-effort: any failure returns False and the caller\nfalls back to media keys.\n"""
from __future__ import annotations
import json
import urllib.request
def skip_url(port: int, n: int) -> str:
    """URL to skip the signed net offset (n>0 = next, n<0 = prev)."""
    return 'http://127.0.0.1:{}/skip?n={}'.format(port, int(n))
def send_skip(port: int, n: int, timeout: float=4.0) -> bool:
    """Ask canvas_service to fire the net offset as N Connect skips. True on ok.
    Generous timeout: canvas_service may wait up to ~1.5s for the cluster to
    populate on cold-start, then ~0.5s for the actual POST + a resume call. A
    short 1s timeout aborted the request mid-flight -> runner fell to the
    media-key drain even though the Connect path was about to succeed."""
    try:
        req = urllib.request.Request(skip_url(port, n), headers={'User-Agent': 'Segue'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.loads(r.read()).get('ok'))
    except Exception:
        return False
def _get_json(port: int, path: str, timeout: float=6.0):
    """GET a canvas_service JSON endpoint. None on any failure."""
    try:
        url = 'http://127.0.0.1:{}{}'.format(port, path)
        req = urllib.request.Request(url, headers={'User-Agent': 'Segue'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None
def health(port: int):
    """canvas_service /health dict, or None if unreachable."""
    return _get_json(port, '/health', timeout=3.0)
def login(port: int):
    """Start the browser OAuth in canvas_service. Returns the JSON ack or None."""
    return _get_json(port, '/login', timeout=8.0)
def login_status(port: int):
    return _get_json(port, '/login_status', timeout=3.0)
def logout(port: int):
    return _get_json(port, '/logout', timeout=4.0)
