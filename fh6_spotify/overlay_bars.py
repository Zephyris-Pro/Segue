"""Music-reactive EQ band levels for the stream overlay.

Captures the music-source app's audio via per-process WASAPI loopback - the SAME
capture the voice-ducking uses (:mod:`fh6_spotify.proc_capture`), so it ships in
every build and does NOT depend on the optional (publicly-stripped) visualizer.
The signal is reduced to 5 smoothed log-frequency band levels (0..1) that the
overlay polls through /np and draws as the EQ bars.

Fully best-effort: if numpy / the capture / the music app is unavailable, it just
yields zeros and the overlay falls back to its canned animation. Never raises.

COM rule (see proc_capture): the capture object is constructed AND iterated on
this module's own worker thread - never hand it to another thread.
"""
from __future__ import annotations
import threading
import time
_RATE = 16000
_CH = 1
_FFT_N = 1024
_N_BANDS = 12
_F_LO, _F_HI = (40.0, 7000.0)
_KA = (1.53512485958697, -2.69169618940638, 1.19839281085285, -1.69065929318241, 0.73248077421585)
_KB = (1.0, -2.0, 1.0, -1.99004745483398, 0.99007225036621)
_LUFS_ST_S = 3.0
_LUFS_MOM_S = 0.4
_LUFS_CALIB_LU = 0.0


def _music_proc(cfg):
    """Image name of the running music-source process, or None. (Standalone copy
    of the visualizer's helper so this module is independent of it.)"""
    src = getattr(cfg, 'source', 'spotify')
    if src == 'browser':
        cands = getattr(cfg, 'browser_process_names', ())
    elif src == 'applemusic':
        cands = getattr(cfg, 'applemusic_process_names', ())
    elif src == 'localmedia':
        cands = getattr(cfg, 'localmedia_process_names', ())
    elif src == 'tidal':
        cands = getattr(cfg, 'tidal_process_names', ('TIDAL.exe',))
    elif src == 'amazonmusic':
        cands = getattr(cfg, 'amazonmusic_process_names', ('Amazon Music.exe',))
    elif src == 'ytmusic':
        cands = getattr(cfg, 'ytmusic_process_names', ('YouTube Music.exe',))
    else:
        cands = (getattr(cfg, 'spotify_process_name', 'Spotify.exe'),)
    try:
        import psutil
        running = {(p.info.get('name') or '').lower() for p in psutil.process_iter(['name'])}
    except Exception:
        return None
    for c in cands:
        if c and c.lower() in running:
            return c
    return None


class OverlayBars:
    """Background music -> 5-band tap. Read :attr:`levels` (list of 5 floats in
    0..1, newest). Best-effort; `levels` decays to zeros when nothing is playing
    or capture is unavailable. Call :meth:`stop` to end the worker."""

    def __init__(self, cfg, ui=None):
        self._cfg = cfg
        self._ui = ui
        self._stop = False
        self.alive = False
        self.levels = [0.0] * _N_BANDS
        self.lufs_s = None
        self.lufs_m = None
        self.excite = 0.0
        self._thread = threading.Thread(target=self._run, name='segue-overlay-bars', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def _decay(self, f=0.5):
        self.levels = [round(v * f, 3) for v in self.levels]
        self.lufs_s = None
        self.lufs_m = None
        self.excite = float(self.excite) * f

    def _run(self):
        try:
            import numpy as np
            from fh6_spotify.proc_capture import ProcessLoopbackCapture
        except Exception:
            return
        win = np.hanning(_FFT_N).astype(np.float32)
        freqs = np.fft.rfftfreq(_FFT_N, 1.0 / _RATE)
        edges = np.geomspace(_F_LO, _F_HI, _N_BANDS + 1)
        idx = []
        for i in range(_N_BANDS):
            b = np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0]
            if not len(b):
                ctr = (edges[i] + edges[i + 1]) / 2.0
                b = np.array([int(np.argmin(np.abs(freqs - ctr)))])
            idx.append(b)
        gref = 0.001
        bref = np.full(_N_BANDS, 0.001, dtype=np.float32)
        smooth = np.zeros(_N_BANDS, dtype=np.float32)
        buf = np.zeros(0, dtype=np.float32)
        ka_s = np.zeros(4, dtype=np.float64)
        kb_s = np.zeros(4, dtype=np.float64)
        _CHUNK = int(_RATE * 0.05)
        _MWIN = max(1, int(_LUFS_MOM_S / 0.05))
        _SWIN = max(1, int(_LUFS_ST_S / 0.05))
        mBuf = np.zeros(_MWIN, dtype=np.float64)
        mIdx = 0
        mFill = 0
        sBuf = np.zeros(_SWIN, dtype=np.float64)
        sIdx = 0
        sFill = 0
        acc = 0.0
        accN = 0
        env_hi = None
        env_lo = None
        adapt = 0.18
        contrast = 1.3
        while not self._stop:
            try:
                proc = _music_proc(self._cfg)
                if not proc:
                    self._decay()
                    time.sleep(1.0)
                    continue
                cap = ProcessLoopbackCapture(proc, sample_rate=_RATE, channels=_CH, frame_ms=20)
                self.alive = True
                last = time.monotonic()
                try:
                    for frame in cap.frames(heartbeat=True):
                        if self._stop:
                            break
                        if not frame:
                            if time.monotonic() - last > 3.0 or _music_proc(self._cfg) != proc:
                                break
                            self._decay(0.85)
                            continue
                        last = time.monotonic()
                        pcm = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                        if _CH > 1:
                            pcm = pcm.reshape(-1, _CH).mean(axis=1)
                        _pcm_lufs = pcm
                        try:
                            _applied = float(self._ui.get('applied', 1.0)) if self._ui else 1.0
                        except Exception:
                            _applied = 1.0
                        if _applied > 0.001 and _applied < 0.999:
                            _pcm_lufs = pcm / _applied
                        kA0, kA1, kA2, kA3, kA4 = _KA
                        kB0, kB1, kB2, kB3, kB4 = _KB
                        _acc = float(acc)
                        _accN = accN
                        ax1, ax2, ay1, ay2 = (float(ka_s[0]), float(ka_s[1]), float(ka_s[2]), float(ka_s[3]))
                        bx1, bx2, by1, by2 = (float(kb_s[0]), float(kb_s[1]), float(kb_s[2]), float(kb_s[3]))
                        for _x in _pcm_lufs:
                            _yA = kA0 * _x + kA1 * ax1 + kA2 * ax2 - kA3 * ay1 - kA4 * ay2
                            ax2 = ax1
                            ax1 = float(_x)
                            ay2 = ay1
                            ay1 = _yA
                            _yB = kB0 * _yA + kB1 * bx1 + kB2 * bx2 - kB3 * by1 - kB4 * by2
                            bx2 = bx1
                            bx1 = _yA
                            by2 = by1
                            by1 = _yB
                            _acc += _yB * _yB
                            _accN += 1
                            if _accN >= _CHUNK:
                                _p = _acc / _accN if _accN else 0.0
                                mBuf[mIdx] = _p
                                mIdx = (mIdx + 1) % _MWIN
                                if mFill < _MWIN:
                                    mFill += 1
                                sBuf[sIdx] = _p
                                sIdx = (sIdx + 1) % _SWIN
                                if sFill < _SWIN:
                                    sFill += 1
                                _acc = 0.0
                                _accN = 0
                                mM = float(mBuf[:mFill].mean()) if mFill else 0.0
                                mS = float(sBuf[:sFill].mean()) if sFill else 0.0
                                lM = -0.691 + 10.0 * float(np.log10(mM)) if mM > 1e-12 else None
                                lS = -0.691 + 10.0 * float(np.log10(mS)) if mS > 1e-12 else None
                                if lM is not None:
                                    lM += _LUFS_CALIB_LU
                                if lS is not None:
                                    lS += _LUFS_CALIB_LU
                                self.lufs_m = lM
                                self.lufs_s = lS
                                if lS is not None:
                                    if env_lo is None:
                                        env_lo = lS
                                        env_hi = lS
                                    decay = adapt * 0.02 + 0.0004
                                    if lS > env_hi:
                                        env_hi = lS
                                    else:
                                        env_hi += (lS - env_hi) * decay * 0.5
                                    if lS < env_lo:
                                        env_lo = lS
                                    else:
                                        env_lo += (lS - env_lo) * decay
                                    _span = env_hi - env_lo
                                    if _span < 1e-09:
                                        n = 0.5
                                    else:
                                        n = (lS - env_lo) / _span
                                        n = 0.0 if n < 0 else n
                                        n = 1.0 if n > 1 else n
                                    exc = (n - 0.5) * contrast + 0.5
                                    exc = 0.0 if exc < 0 else exc
                                    exc = 1.0 if exc > 1 else exc
                                    self.excite = float(exc)
                        acc, accN = (_acc, _accN)
                        ka_s[0], ka_s[1], ka_s[2], ka_s[3] = (ax1, ax2, ay1, ay2)
                        kb_s[0], kb_s[1], kb_s[2], kb_s[3] = (bx1, bx2, by1, by2)
                        buf = np.concatenate((buf, pcm))
                        while len(buf) >= _FFT_N:
                            chunk = buf[:_FFT_N]
                            buf = buf[_FFT_N // 2:]
                            silent = float(np.max(np.abs(chunk))) < 0.0015
                            mag = np.abs(np.fft.rfft(chunk * win))
                            raw = np.array([float(mag[ix].mean()) for ix in idx], dtype=np.float32)
                            gref = max(gref * 0.99, float(raw.max()), 1e-05)
                            floor = gref * 0.05
                            for i in range(_N_BANDS):
                                if silent:
                                    nv = 0.0
                                else:
                                    bref[i] = max(float(bref[i]) * 0.985, float(raw[i]), floor)
                                    nv = min(1.0, float(raw[i]) / float(bref[i]) * (1.0 + 0.05 * i)) ** 0.7
                                a = 0.7 if nv > smooth[i] else 0.22
                                smooth[i] += (nv - smooth[i]) * a
                            self.levels = [round(float(v), 3) for v in smooth]
                except Exception:
                    pass
                try:
                    cap.close()
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self.alive = False
                self._decay()
                time.sleep(1.5)
