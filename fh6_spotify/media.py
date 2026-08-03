"""Now-playing info from Windows System Media Transport Controls (SMTC).

Reads title / artist / play-state / cover art for whatever app is playing
(Spotify), with no Spotify login. Runs an asyncio loop in its own thread and
exposes a thread-safe snapshot via get().
"""
import asyncio
import threading
from dataclasses import dataclass


@dataclass
class NowPlaying:
    title: str
    artist: str
    is_playing: bool
    thumb: bytes | None
    shuffle: bool = False
    repeat: str = 'none'
    app: str = ''
    position: float = 0.0
    duration: float = 0.0


_TRACK_HOLD_S = 2.0


class MediaWatcher:

    def __init__(self, poll_s: float=0.2, match=None, debug: bool=False, log_path: str | None=None):
        self._match = match
        self._poll_s = poll_s
        self._debug = debug
        self._log_path = log_path
        self._logged_ids = None
        self._snapshot = None
        self._last_track = None
        self._last_good = None
        self._hold_until = 0.0
        self._thumb = None
        self._thumb_not_before = 0.0
        self._thumb_settle = 0.0
        self._thumb_read_next = 0.0
        self._stop = False
        self._boost_until = 0.0
        self._aloop = None
        self._sess = None
        self._any_session = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _cmd(self, make_coro):
        """Schedule an SMTC control coroutine on the watcher's loop (thread-safe)."""
        import asyncio
        if self._aloop is None or self._sess is None:
            return None
        try:
            asyncio.run_coroutine_threadsafe(make_coro(self._sess), self._aloop)
        except Exception:
            return None

    def toggle_shuffle(self) -> None:
        cur = bool(self._snapshot.shuffle) if self._snapshot else False
        self._cmd(lambda s: s.try_change_shuffle_active_async(not cur))

    def _boost(self) -> None:
        """Poll fast for a moment after OUR OWN transport command, so the new
        title/cover lands with minimal added latency on top of the player's
        SMTC publish delay (~1s for Spotify, source-side)."""
        import time as _t
        self._boost_until = _t.monotonic() + 2.0

    def playpause(self) -> bool:
        """Toggle play/pause on the filtered SMTC session. Returns True if
        routed; False if no session is available (caller should fall back to
        global media keys, which target whatever SMTC considers 'current')."""
        if self._sess is None:
            return False
        self._boost()
        self._cmd(lambda s: s.try_toggle_play_pause_async())
        return True

    def next(self) -> bool:
        if self._sess is None:
            return False
        self._boost()
        self._cmd(lambda s: s.try_skip_next_async())
        return True

    def prev(self) -> bool:
        if self._sess is None:
            return False
        self._boost()
        self._cmd(lambda s: s.try_skip_previous_async())
        return True

    def cycle_repeat(self) -> None:
        from winsdk.windows.media import MediaPlaybackAutoRepeatMode as M
        order = {'none': M.LIST, 'list': M.TRACK, 'track': M.NONE}
        cur = self._snapshot.repeat if self._snapshot else 'none'
        self._cmd(lambda s: s.try_change_auto_repeat_mode_async(order.get(cur, M.LIST)))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def get(self) -> NowPlaying | None:
        return self._snapshot

    def has_any_session(self) -> bool:
        """True if ANY app currently holds an SMTC session (not just the selected
        source). Lets callers tell 'SMTC empty system-wide' (safe to use a global
        media key) from 'other apps are playing but not the selected source'
        (must NOT fire a global key - it would leak to those other apps)."""
        return self._any_session

    def _diag(self, msg: str) -> None:
        """Append a line to smtc.log. The media loop dying SILENTLY made a
        missing/empty log ambiguous - now the file always exists and fatal
        errors leave a traceback."""
        try:
            if not self._log_path:
                return
            import time as _t
            with open(self._log_path, 'a', encoding='utf-8') as f:
                f.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            return None

    def _run(self) -> None:
        self._diag('media loop starting')
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        try:
            asyncio.run(self._loop())
            self._diag('media loop exited cleanly')
        except Exception:
            try:
                import traceback
                self._diag('FATAL media loop died:\n' + traceback.format_exc())
            except Exception:
                return None

    async def _loop(self) -> None:
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Mgr, GlobalSystemMediaTransportControlsSessionPlaybackStatus as PStatus
        from winsdk.windows.media import MediaPlaybackAutoRepeatMode as M
        _REPEAT = {M.NONE: 'none', M.TRACK: 'track', M.LIST: 'list'}
        self._aloop = asyncio.get_running_loop()
        mgr = await Mgr.request_async()
        while not self._stop:
            try:
                sess = self._pick_session(mgr)
                self._sess = sess
                import time as _t2
                _now2 = _t2.monotonic()
                cand = None
                if sess is not None:
                    props = await sess.try_get_media_properties_async()
                    info = sess.get_playback_info()
                    title = props.title or ''
                    artist = props.artist or ''
                    playing = info.playback_status == PStatus.PLAYING
                    shuffle = bool(info.is_shuffle_active) if info.is_shuffle_active is not None else False
                    repeat = _REPEAT.get(info.auto_repeat_mode, 'none') if info.auto_repeat_mode is not None else 'none'
                    pos = dur = 0.0
                    try:
                        tl = sess.get_timeline_properties()
                        if tl is not None:
                            st = tl.start_time.total_seconds()
                            dur = max(0.0, tl.end_time.total_seconds() - st)
                            pos = max(0.0, tl.position.total_seconds() - st)
                    except Exception:
                        pass
                    sid = sess.source_app_user_model_id or ''
                    track = f'{sid}|{artist}—{title}'
                    if track != self._last_track:
                        self._last_track = track
                        self._thumb_not_before = _now2 + 0.3
                        self._thumb_settle = _now2 + 8.0
                        self._boost_until = _now2 + 5.0
                    if _now2 >= getattr(self, '_thumb_not_before', 0.0):
                        if _now2 >= getattr(self, '_thumb_read_next', 0.0):
                            self._thumb_read_next = _now2 + 0.12
                            nt = await self._read_thumb(props.thumbnail)
                            if nt is not None:
                                if nt != self._thumb:
                                    self._thumb = nt
                            elif _now2 >= getattr(self, '_thumb_settle', 0.0):
                                self._thumb = None
                    cand = NowPlaying(title, artist, playing, self._thumb, shuffle, repeat, app=sid, position=pos, duration=dur)
                _empty = cand is None or (not cand.title and not cand.artist)
                if not _empty:
                    self._last_good = cand
                    self._hold_until = _now2 + _TRACK_HOLD_S
                    self._snapshot = cand
                elif _now2 < self._hold_until and self._last_good is not None:
                    self._snapshot = self._last_good
                else:
                    self._last_good = None
                    self._snapshot = cand
            except Exception:
                pass
            import time as _t
            await asyncio.sleep(0.05 if _t.monotonic() < self._boost_until else self._poll_s)

    def _pick_session(self, mgr):
        """Session for the chosen source. With no matcher, the current session.
        With one, the first session whose app id matches (or None -> shows the
        '<source> not playing' state instead of leaking the other source)."""
        matched = None
        preferred = None
        all_ids = []
        try:
            sessions = mgr.get_sessions()
            self._any_session = sessions.size > 0
            for i in range(sessions.size):
                s = sessions.get_at(i)
                sid = s.source_app_user_model_id or ''
                all_ids.append(sid)
                if self._match is None or self._match(sid):
                    if matched is None:
                        matched = s
                    if preferred is None and 'spotify' in sid.lower():
                        preferred = s
        except Exception:
            pass
        chosen = preferred or matched
        if self._debug:
            self._log_sessions(all_ids, chosen)
        if self._match is None and chosen is None:
            return mgr.get_current_session()
        return chosen

    def _log_sessions(self, ids, chosen) -> None:
        """Append the current SMTC session app-ids (and which one we picked) to the
        debug log, but only when the set changes. Lets us see exactly what a player
        like Firefox reports so the matcher can be fixed."""
        try:
            key = tuple(ids)
            if key == self._logged_ids or not self._log_path:
                return None
            self._logged_ids = key
            chosen_id = ''
            try:
                chosen_id = (chosen.source_app_user_model_id or '') if chosen else ''
            except Exception:
                pass
            import time as _t
            line = f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] sessions={ids!r} picked={chosen_id!r}\n"
            with open(self._log_path, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            return None

    async def _read_thumb(self, ref) -> bytes | None:
        if ref is None:
            return
        try:
            from winsdk.windows.storage.streams import DataReader
            stream = await ref.open_read_async()
            size = stream.size
            if not size:
                return
            reader = DataReader(stream)
            await reader.load_async(size)
            data = bytearray(size)
            reader.read_bytes(data)
            return bytes(data)
        except Exception:
            return
