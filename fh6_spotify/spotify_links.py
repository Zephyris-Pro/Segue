"""Resolve a now-playing track / artist NAME to exact Spotify page URIs via the\ngetsegue.app Worker (/api/resolve), which holds the Spotify client-credentials\nsecret server-side. Windows media controls expose names, not Spotify IDs, so a\nserver lookup is the only way to deep-link straight to the artist / release page.\nSpotify source only.\n\nResults are cached per (track, artist). A hard \"no match\" is cached permanently;\nnetwork/transport failures are negative-cached briefly so a flaky click doesn\'t\nhammer the Worker. resolve() BLOCKS on the network - call it off the GUI thread.\n"""
from __future__ import annotations
import json
import time
import urllib.parse
import urllib.request
from typing import Optional
_ENDPOINT = 'https://getsegue.app/api/resolve'
_TIMEOUT_S = 6.0
_cache: dict[tuple[str, str], dict] = {}
_neg_until: dict[tuple[str, str], float] = {}
_NEG_TTL_S = 60.0
def resolve(track: str, artist: str) -> Optional[dict]:
    """Look up the Spotify track/album/artist for these names.\n\n    Returns {\"track\":{id,uri,url}, \"album\":{...}, \"artist\":{...}} or None.\n    Best-effort + blocking; never raises."""
    track = (track or '').strip()
    artist = (artist or '').strip()
    if not track and (not artist):
        return
    else:
        key = (track.lower(), artist.lower())
        hit = _cache.get(key)
        if hit is not None:
            return hit or None
        else:
            if time.monotonic() < _neg_until.get(key, 0.0):
                return
            else:
                qs = urllib.parse.urlencode({'track': track, 'artist': artist})
                req = urllib.request.Request(f'{_ENDPOINT}?{qs}', headers={'User-Agent': 'Segue'})
                try:
                    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                        data = json.loads(r.read().decode('utf-8'))
                except Exception:
                    _neg_until[key] = time.monotonic() + _NEG_TTL_S
                    return None
                if not data.get('ok'):
                    _cache[key] = {}
                    return
                else:
                    _cache[key] = data
                    return data
def search_uri(query: str) -> str:
    """Spotify search deep-link for a name - the fallback when resolve() fails."""
    return 'spotify:search:' + urllib.parse.quote((query or '').strip())
