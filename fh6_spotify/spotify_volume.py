import json
import os
import time
from fh6_spotify.config import Config


def _session_tokens(candidates):
    """App-name tokens from process candidates ("Spotify.exe" -> "spotify"),
    space-insensitive so multi-word apps ("Amazon Music.exe") still match a
    packaged identifier ("AmazonMusic..."). Used for the MS Store / packaged-app
    fallback in _session_matches."""
    out = set()
    for c in candidates:
        t = c.lower()
        if t.endswith('.exe'):
            t = t[:-4]
        t = t.strip()
        if t:
            out.add(t)
            out.add(t.replace(' ', ''))
    return out


def _session_matches(session, wanted, tokens):
    """True if this audio session is one of our target apps.

    Primary: the session's process name matches a candidate. Fallback (MS Store
    / packaged apps, e.g. Store Spotify): those run with a process Segue can't
    read (`session.Process` is None / access-denied), so the name match misses
    even though the session is right there in the mixer. The session INSTANCE
    IDENTIFIER still carries the app path or package family name, so match a
    token against that. Process is tried first, so a readable app never
    false-matches via the looser identifier path."""
    try:
        p = session.Process
        if p and p.name().lower() in wanted:
            return True
    except Exception:
        pass
    if tokens:
        try:
            ident = (session._ctl.GetSessionInstanceIdentifier() or '').lower()
            if any(t in ident or t in ident.replace(' ', '') for t in tokens):
                return True
        except Exception:
            pass
    return False


def _all_render_sessions():
    """Audio sessions across ALL active render endpoints, not just the Windows
    default. Virtual mixers (SteelSeries Sonar, Voicemeeter) and Windows 11
    per-app output overrides put an app's session on a NON-default device, where
    the default-only AudioUtilities.GetAllSessions() never sees it. Mirrors
    pycaw's GetAllSessions but walks every active render device. Falls back to
    the default-endpoint enumeration if the multi-device walk fails."""
    try:
        import comtypes
        from pycaw.utils import AudioSession
        from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator, EDataFlow, DEVICE_STATE
        enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        coll = enum.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        sessions = []
        for i in range(coll.GetCount()):
            try:
                dev = coll.Item(i)
                o = dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
                mgr = o.QueryInterface(IAudioSessionManager2)
                se = mgr.GetSessionEnumerator()
                for j in range(se.GetCount()):
                    ctl = se.GetSession(j)
                    if ctl is None:
                        continue
                    ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                    if ctl2 is not None:
                        sessions.append(AudioSession(ctl2))
            except Exception:
                pass
        if sessions:
            return sessions
    except Exception:
        pass
    try:
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetAllSessions()
    except Exception:
        return []


def _find_session(candidates):
    """SimpleAudioVolume for the first audio session matching one of
    `candidates` - by process name, or by session identifier for packaged /
    MS Store apps - across all render devices, or None."""
    wanted = {c.lower() for c in candidates}
    tokens = _session_tokens(candidates)
    for session in _all_render_sessions():
        try:
            if _session_matches(session, wanted, tokens):
                return session.SimpleAudioVolume
        except Exception:
            pass


def _find_sessions(candidates):
    """ALL matching audio sessions' SimpleAudioVolume interfaces, across all
    render devices. Some apps (Apple Music's AMPLibraryAgent) expose multiple
    sessions and the audible one isn't always first, so volume must be set on
    every match. Matches by process name or, for packaged / MS Store apps, by
    session identifier."""
    wanted = {c.lower() for c in candidates}
    tokens = _session_tokens(candidates)
    out = []
    for session in _all_render_sessions():
        try:
            if _session_matches(session, wanted, tokens):
                out.append(session.SimpleAudioVolume)
        except Exception:
            pass
    return out


def _dirty_path() -> str:
    """Pre-Segue volume snapshot. Lives at %APPDATA%/Segue/dirty_volumes.json
    so a hard crash can be undone on next launch (the file is only deleted
    when Segue restores cleanly on exit). Shape:
        {"spotify.exe": 0.74, "chrome.exe": 0.62}
    """
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'Segue', 'dirty_volumes.json')


def _load_dirty() -> dict:
    try:
        with open(_dirty_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_dirty(d: dict) -> None:
    path = _dirty_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        return None


def _clear_dirty() -> None:
    try:
        os.remove(_dirty_path())
    except OSError:
        return None


def restore_dirty_volumes() -> None:
    """If Segue's previous session crashed, the dirty_volumes.json file
    survives. Walk the entries and set each process's master volume back
    to whatever it was before Segue first touched it, then drop the file.
    Safe to call on every launch - no-ops cleanly when the file is absent."""
    d = _load_dirty()
    if not d:
        return
    from pycaw.pycaw import AudioUtilities
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return None
    for session in sessions:
        try:
            if not session.Process:
                continue
            name = session.Process.name().lower()
            if name not in d:
                continue
            level = float(d[name])
            level = max(0.0, min(1.0, level))
            session.SimpleAudioVolume.SetMasterVolume(level, None)
        except Exception:
            pass
    _clear_dirty()


class SpotifyVolume:
    """Controls the music source's per-app volume. The source (Spotify or a
    browser playing YouTube etc.) is read live from config, so switching it in the
    UI takes effect on the next tick - and the app you switched away from is handed
    back to full volume so it isn't left ducked."""

    def __init__(self, config: Config, session_lookup=None):
        self.c = config
        self._lookup = session_lookup
        self._last_applied = None
        self._last_source = None
        self.has_audio = False
        self._audio_check_t = 0.0
        self._pending_source = None
        self._pending_since = 0.0

    def _candidates(self, source: str | None=None):
        source = source if source is not None else self.c.source
        if source == 'browser':
            return list(self.c.browser_process_names)
        if source == 'applemusic':
            return list(getattr(self.c, 'applemusic_process_names', ('AppleMusic.exe',)))
        if source == 'localmedia':
            return list(getattr(self.c, 'localmedia_process_names', ('vlc.exe', 'wmplayer.exe')))
        if source == 'tidal':
            return list(getattr(self.c, 'tidal_process_names', ('TIDAL.exe',)))
        if source == 'amazonmusic':
            return list(getattr(self.c, 'amazonmusic_process_names', ('Amazon Music.exe',)))
        if source == 'ytmusic':
            return list(getattr(self.c, 'ytmusic_process_names', ('YouTube Music.exe',)))
        if source == 'custom':
            return list(getattr(self.c, 'custom_process_names', ()))
        return [self.c.spotify_process_name]

    def _resolve(self, source: str | None=None):
        src = source if source is not None else self.c.source
        cands = self._candidates(src)
        if self._lookup is not None:
            return self._lookup(cands)
        return _find_session(cands)

    def _resolve_all(self, source: str | None=None):
        """All matching session interfaces (for SETTING volume). Single-session
        sources (Spotify/browser) return one; Apple Music may return several."""
        src = source if source is not None else self.c.source
        cands = self._candidates(src)
        if self._lookup is not None:
            iface = self._lookup(cands)
            return [iface] if iface is not None else []
        return _find_sessions(cands)

    def _candidate_key(self, source: str) -> str:
        """Stable key for the dirty-volumes snapshot. We don't know which exact
        browser the user has running; pick the first candidate. Worst case we
        restore the wrong browser's volume, which is harmless because we only
        ever set it to its OWN pre-Segue value."""
        cands = self._candidates(source)
        if cands:
            return cands[0].lower()
        return source

    def _snapshot_pre_segue(self, source: str) -> None:
        """Before the first Segue-driven volume change of a session, write the
        source app's current level to dirty_volumes.json so we can roll back
        on graceful exit OR on a crash (next launch restores from the file)."""
        try:
            iface = self._resolve(source)
            if iface is None:
                return
            current = iface.GetMasterVolume()
            d = _load_dirty()
            key = self._candidate_key(source)
            if key in d:
                return
            d[key] = float(current)
            _save_dirty(d)
        except Exception:
            return None

    def _release(self, source: str, ramp_ms: int=0) -> None:
        """Hand a source's app back to its pre-Segue volume. Falls back to
        1.0 (full) if we never recorded a snapshot, which matches the old
        behaviour of just "un-ducking" the app.

        ramp_ms > 0 eases from the current (possibly ducked) level up to the
        target instead of snapping."""
        try:
            ifaces = self._resolve_all(source)
            if not ifaces:
                return
            d = _load_dirty()
            key = self._candidate_key(source)
            target = float(d.get(key, 1.0))
            target = max(0.0, min(1.0, target))
            cur = None
            if ramp_ms > 0:
                try:
                    cur = float(ifaces[0].GetMasterVolume())
                except Exception:
                    cur = None
            if cur is not None and abs(target - cur) > 0.02:
                steps = 18
                for i in range(1, steps + 1):
                    v = max(0.0, min(1.0, cur + (target - cur) * (i / steps)))
                    try:
                        for iface in ifaces:
                            iface.SetMasterVolume(v, None)
                    except Exception:
                        break
                    time.sleep(ramp_ms / 1000.0 / steps)
            else:
                for iface in ifaces:
                    iface.SetMasterVolume(target, None)
            if key in d:
                d.pop(key, None)
                if d:
                    _save_dirty(d)
                else:
                    _clear_dirty()
        except Exception:
            return None

    _SOURCE_SETTLE_S = 0.3

    def apply(self, level: float) -> None:
        src = self.c.source
        if src != self._last_source:
            now = time.monotonic()
            if src != self._pending_source:
                self._pending_source = src
                self._pending_since = now
                return
            if now - self._pending_since < self._SOURCE_SETTLE_S:
                return
            if self._last_source is not None:
                self._release(self._last_source)
            self._last_source = src
            self._last_applied = None
        else:
            self._pending_source = src
        if level == self._last_applied:
            now = time.monotonic()
            if now - getattr(self, '_audio_check_t', 0.0) >= 0.5:
                self._audio_check_t = now
                self.has_audio = bool(self._resolve_all())
            return None
        if self._last_applied is None:
            self._snapshot_pre_segue(self.c.source)
        ifaces = self._resolve_all()
        self.has_audio = bool(ifaces)
        self._audio_check_t = time.monotonic()
        if not ifaces:
            return
        try:
            for iface in ifaces:
                iface.SetMasterVolume(level, None)
        except Exception:
            return None
        self._last_applied = level

    def current_level(self) -> float | None:
        """The source app's current volume (0.0-1.0), or None if no session."""
        iface = self._resolve()
        return iface.GetMasterVolume() if iface is not None else None
