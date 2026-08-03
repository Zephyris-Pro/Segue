"""
Per-process WASAPI loopback capture for FH6-Spotify voice-detection.

WHY THIS EXISTS
===============
``capture.py`` (pyaudiowpatch / PortAudio) can only do PER-DEVICE loopback, so
it hears the game AND Spotify mixed together and the VAD ducks on Spotify
vocals. This module captures the audio of ONE specific process (e.g.
``forzahorizon6.exe``) with ZERO bleed from other apps playing on the same
output device. It yields frames of the SAME shape as ``GameAudioCapture``:
640-byte, 16 kHz, mono, 16-bit little-endian PCM (FRAME_BYTES, 20 ms).

HOW IT WORKS
============
Uses process-loopback activation (Windows 10 2004 / build 19041):
``ActivateAudioInterfaceAsync`` against ``"VAD\\Process_Loopback"`` with an
``AUDIOCLIENT_ACTIVATION_PARAMS`` PROPVARIANT (VT_BLOB) carrying
ActivationType = PROCESS_LOOPBACK and a {TargetProcessId, ProcessLoopbackMode}
payload. A hand-rolled ``IActivateAudioInterfaceCompletionHandler`` signals an
event when activation completes; we read the result, cast it to ``IAudioClient``
(reusing pycaw's comtypes definition) and Initialize it with
LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY.

We define our own corrected ``WAVEFORMATEX``: pycaw's has nSamplesPerSec /
nAvgBytesPerSec declared as 16-bit WORD instead of 32-bit DWORD.

PLATFORM
========
Windows 10 version 2004 (build 19041) or newer ONLY.

COM THREADING - IMPORTANT
=========================
All WASAPI/COM objects are apartment-bound to the thread that created them.
``__init__`` calls ``CoInitializeEx(MULTITHREADED)`` and does the full
activation on the CALLING thread; ``frames()`` and ``close()`` MUST run on that
same (dedicated worker) thread.
"""
import ctypes
import ctypes.wintypes
import sys
import time
from ctypes import POINTER
from typing import Generator, Optional
if not hasattr(sys, 'coinit_flags'):
    sys.coinit_flags = 0
import psutil
from comtypes import GUID as _GUID, HRESULT as _COMHRESULT, COMMETHOD as _COMMETHOD, IUnknown as _IUnknown
from pycaw.api.audioclient import IAudioClient as _IAudioClient
from pycaw.api.audioclient.depend import WAVEFORMATEX as _PycawWFX
from fh6_spotify.speech import SAMPLE_RATE, FRAME_BYTES
LPVOID = ctypes.c_void_p
DWORD = ctypes.wintypes.DWORD
HRESULT = ctypes.HRESULT
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = 'VAD\\Process_Loopback'
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 131072
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 262144
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 2147483648
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 134217728
AUDCLNT_BUFFERFLAGS_SILENT = 2
IID_IAudioClient = '{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}'
IID_IAudioCaptureClient = '{C8ADBD64-E71E-48a0-A4DE-185C395CD317}'
WAVE_FORMAT_PCM = 1
VT_BLOB = 65
COINIT_MULTITHREADED = 0
_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = 2147549446
_BUFFER_DURATION_REFTIME = 2000000
_WAIT_TIMEOUT_MS = 200
_ACTIVATE_TIMEOUT_MS = 5000


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [('wFormatTag', ctypes.wintypes.WORD), ('nChannels', ctypes.wintypes.WORD), ('nSamplesPerSec', DWORD), ('nAvgBytesPerSec', DWORD), ('nBlockAlign', ctypes.wintypes.WORD), ('wBitsPerSample', ctypes.wintypes.WORD), ('cbSize', ctypes.wintypes.WORD)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [('vt', ctypes.c_ushort), ('r1', ctypes.c_ushort), ('r2', ctypes.c_ushort), ('r3', ctypes.c_ushort), ('blob_cb', DWORD), ('blob_ptr', LPVOID)]


class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    """ActivationType + AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS{TargetPid, Mode}."""
    _fields_ = [('ActivationType', DWORD), ('TargetProcessId', DWORD), ('ProcessLoopbackMode', DWORD)]


_QI_T = ctypes.WINFUNCTYPE(HRESULT, LPVOID, LPVOID, POINTER(LPVOID))
_AR_T = ctypes.WINFUNCTYPE(ctypes.c_ulong, LPVOID)
_RL_T = ctypes.WINFUNCTYPE(ctypes.c_ulong, LPVOID)
_AC_T = ctypes.WINFUNCTYPE(HRESULT, LPVOID, LPVOID)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [('QI', _QI_T), ('AR', _AR_T), ('RL', _RL_T), ('AC', _AC_T)]


class _HandlerObj(ctypes.Structure):
    _fields_ = [('v', POINTER(_HandlerVtbl))]


def _make_completion_handler(event_handle):
    """Build a minimal COM completion handler. Returns (handler, vtbl, cbs);
    the caller MUST keep all three alive until activation completes."""
    _rc = ctypes.c_long(1)

    def qi(this, riid, ppv):
        ppv[0] = this
        _rc.value += 1
        return 0

    def addref(this):
        _rc.value += 1
        return _rc.value

    def release(this):
        _rc.value -= 1
        return _rc.value

    def activate_completed(this, op):
        ctypes.windll.kernel32.SetEvent(event_handle)
        return 0

    cbs = (_QI_T(qi), _AR_T(addref), _RL_T(release), _AC_T(activate_completed))
    vtbl = _HandlerVtbl(*cbs)
    handler = _HandlerObj(ctypes.pointer(vtbl))
    return (handler, vtbl, cbs)


def _raw_vtable_call(ptr, idx, restype, *typed_args):
    """Call a COM method by vtable index. Used only for the async-op interface
    (IActivateAudioInterfaceAsyncOperation), for which we have no comtypes def."""
    vpp = ctypes.cast(ptr, POINTER(LPVOID))
    vp = ctypes.cast(vpp[0], POINTER(LPVOID))
    types = [LPVOID] + [t for t, _ in typed_args]
    values = [ptr] + [v for _, v in typed_args]
    return ctypes.WINFUNCTYPE(restype, *types)(vp[idx])(*values)


class _IAudioCaptureClient(_IUnknown):
    _iid_ = _GUID(IID_IAudioCaptureClient)
    _methods_ = [
        _COMMETHOD([], _COMHRESULT, 'GetBuffer',
            (['out'], POINTER(LPVOID), 'ppData'),
            (['out'], POINTER(ctypes.c_uint), 'pNumFramesToRead'),
            (['out'], POINTER(DWORD), 'pdwFlags'),
            (['out'], POINTER(ctypes.c_ulonglong), 'pu64DevicePosition'),
            (['out'], POINTER(ctypes.c_ulonglong), 'pu64QPCPosition')),
        _COMMETHOD([], _COMHRESULT, 'ReleaseBuffer',
            (['in'], ctypes.c_uint, 'NumFramesRead')),
        _COMMETHOD([], _COMHRESULT, 'GetNextPacketSize',
            (['out'], POINTER(ctypes.c_uint), 'pNumFramesInNextPacket')),
    ]


def _find_pid(process_name: str) -> int:
    """Return the TREE-ROOT pid for *process_name* (case-insensitive): the
    matching process whose parent is NOT also that process. Apps like Spotify and
    browsers spawn several same-named child processes; the audio renderer lives in
    the tree, and process-loopback works on the target pid + its CHILD tree.
    Raises ProcessNotFoundError if none."""
    target = process_name.lower()
    matches = {}
    for proc in psutil.process_iter(['name', 'pid', 'ppid']):
        if (proc.info.get('name') or '').lower() == target:
            matches[proc.info['pid']] = proc.info.get('ppid')
    if not matches:
        raise ProcessNotFoundError('No running process named %r. Is the target application started?' % (process_name,))
    for pid, ppid in matches.items():
        if ppid not in matches:
            return pid
    return next(iter(matches))


class ProcessNotFoundError(RuntimeError):
    """Raised when the requested process image name is not currently running."""


class ProcessLoopbackCapture:
    """Captures a single process's audio via per-process WASAPI loopback and
    yields 640-byte, 16 kHz, mono, 16-bit PCM frames suitable for SpeechDetector.
    Shape-compatible with ``capture.GameAudioCapture``. All COM/WASAPI objects
    are bound to the thread that runs ``__init__``; ``frames()`` and ``close()``
    MUST run on that same thread."""

    def __init__(self, process_name: str, exclude: bool=False, sample_rate: int=None, channels: int=1, frame_ms: int=20) -> None:
        self.process_name = process_name
        self._exclude = exclude
        self._rate = int(sample_rate or SAMPLE_RATE)
        self._channels = max(1, int(channels))
        self._frame_bytes = int(self._rate * frame_ms / 1000) * 2 * self._channels
        self.pid = None
        self._client = None
        self._capture = None
        self._event = None
        self._handler_keepalive = None
        self._leftover = bytearray()
        self._available = False
        self._started = False
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        self._we_init_com = hr == _S_OK
        if hr not in (_S_OK, _S_FALSE, _RPC_E_CHANGED_MODE):
            raise RuntimeError(f'CoInitializeEx failed: 0x{hr & 4294967295:08X}')
        self.pid = _find_pid(process_name)
        try:
            self._open(self.pid)
        except Exception:
            self._uninit_com()
            raise
        self._available = True

    def _open(self, pid: int) -> None:
        fmt = _WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.nChannels = self._channels
        fmt.nSamplesPerSec = self._rate
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample // 8
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
        fmt.cbSize = 0
        self._block_align = fmt.nBlockAlign
        mode = PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE if self._exclude else PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        ap = _AUDIOCLIENT_ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, pid, mode)
        pv = _PROPVARIANT()
        pv.vt = VT_BLOB
        pv.blob_cb = ctypes.sizeof(ap)
        pv.blob_ptr = ctypes.cast(ctypes.pointer(ap), LPVOID)
        activate_evt = ctypes.windll.kernel32.CreateEventW(None, True, False, None)
        handler, vtbl, cbs = _make_completion_handler(activate_evt)
        self._handler_keepalive = (handler, vtbl, cbs)
        fn = ctypes.WinDLL('Mmdevapi.dll').ActivateAudioInterfaceAsync
        fn.restype = HRESULT
        aop = LPVOID()
        iid_ac = _GUID(IID_IAudioClient)
        hr = fn(ctypes.c_wchar_p(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK), ctypes.byref(iid_ac), ctypes.byref(pv), ctypes.byref(handler), ctypes.byref(aop))
        if hr != 0:
            ctypes.windll.kernel32.CloseHandle(activate_evt)
            raise RuntimeError(f'ActivateAudioInterfaceAsync failed: 0x{hr & 4294967295:08X}')
        ctypes.windll.kernel32.WaitForSingleObject(activate_evt, _ACTIVATE_TIMEOUT_MS)
        ctypes.windll.kernel32.CloseHandle(activate_evt)
        ahr = HRESULT()
        aunk = LPVOID()
        _raw_vtable_call(aop, 3, HRESULT, (POINTER(HRESULT), ctypes.pointer(ahr)), (POINTER(LPVOID), ctypes.pointer(aunk)))
        _raw_vtable_call(aop, 2, ctypes.c_ulong)
        if ahr.value != 0 or not aunk.value:
            raise RuntimeError(f'GetActivateResult failed: 0x{ahr.value & 4294967295:08X} (activation refused; common cause: target pid not rendering, or Windows build < 19041)')
        client = ctypes.cast(aunk.value, POINTER(_IAudioClient))
        stream_flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        fmt_ptr = ctypes.cast(ctypes.pointer(fmt), POINTER(_PycawWFX))
        try:
            client.Initialize(AUDCLNT_SHAREMODE_SHARED, stream_flags, _BUFFER_DURATION_REFTIME, 0, fmt_ptr, None)
        except Exception as e:
            raise RuntimeError(f'IAudioClient.Initialize failed: {e!r}')
        data_evt = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
        client.SetEventHandle(data_evt)
        cc_iid = _GUID(IID_IAudioCaptureClient)
        cc_ptr = client.GetService(ctypes.pointer(cc_iid))
        capture = ctypes.cast(cc_ptr, POINTER(_IAudioCaptureClient))
        self._client = client
        self._capture = capture
        self._event = data_evt

    @property
    def available(self) -> bool:
        """True once a process-loopback stream was successfully opened."""
        return self._available

    def frames(self, heartbeat: bool=False) -> Generator[bytes, None, None]:
        """Yields exactly FRAME_BYTES (640) bytes per iteration: 16-bit signed
        PCM, mono, 16000 Hz, little-endian (20 ms). Blocks on the engine's
        data-ready event. Packets arrive in ~10 ms / 320-byte chunks; they are
        concatenated and re-sliced into 640-byte frames, carrying the partial
        remainder between reads. heartbeat=True also yields b"" ~every half
        second while no packets arrive so the consumer can notice a dead stream."""
        if not self._available or self._client is None:
            raise RuntimeError('Capture is not available / already closed.')
        if not self._started:
            self._client.Start()
            self._started = True
        kernel32 = ctypes.windll.kernel32
        capture = self._capture
        block_align = self._block_align
        last_emit = time.monotonic()
        while True:
            kernel32.WaitForSingleObject(self._event, _WAIT_TIMEOUT_MS)
            if heartbeat:
                now = time.monotonic()
                if now - last_emit > 0.5:
                    last_emit = now
                    yield b''
            while True:
                try:
                    nframes = capture.GetNextPacketSize()
                except Exception:
                    nframes = 0
                if not nframes:
                    break
                data, frames, flags, _pos, _qpc = capture.GetBuffer()
                try:
                    if data and frames > 0:
                        nbytes = frames * block_align
                        buf = self._leftover
                        if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                            buf.extend(b'\x00' * nbytes)
                        else:
                            buf.extend(ctypes.string_at(data, nbytes))
                finally:
                    capture.ReleaseBuffer(frames)
                buf = self._leftover
                offset = 0
                end = len(buf)
                fb = self._frame_bytes
                while offset + fb <= end:
                    last_emit = time.monotonic()
                    yield bytes(buf[offset:offset + fb])
                    offset += fb
                if offset:
                    del buf[:offset]

    def close(self) -> None:
        """Stop the stream and release COM/WASAPI resources. Must run on the
        same thread that constructed this object."""
        if self._client is not None and self._started:
            try:
                self._client.Stop()
            except Exception:
                pass
        self._started = False
        self._capture = None
        self._client = None
        if self._event:
            ctypes.windll.kernel32.CloseHandle(self._event)
            self._event = None
        self._handler_keepalive = None
        self._available = False
        self._uninit_com()

    def _uninit_com(self) -> None:
        if getattr(self, '_we_init_com', False):
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass
            self._we_init_com = False


class SystemAudioCapture:
    """WASAPI loopback of the DEFAULT output device - i.e. EVERYTHING the PC is
    playing (DAW, browser, games, all mixed). Mirrors ProcessLoopbackCapture's
    ``frames(heartbeat=)`` contract: yields interleaved 16-bit STEREO PCM
    resampled to ``sample_rate``, and yields b"" as a heartbeat when no audio is
    flowing. Backed by pyaudiowpatch."""

    def __init__(self, sample_rate: int=48000, channels: int=2, frame_ms: int=10):
        import pyaudiowpatch as pyaudio
        self._sr = int(sample_rate)
        self._pa = pyaudio.PyAudio()
        info = self._pa.get_default_wasapi_loopback()
        self._src_rate = int(info['defaultSampleRate'])
        self._src_ch = max(int(info['maxInputChannels']), 2)
        self._chunk = max(1, int(self._src_rate * frame_ms / 1000))
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=self._src_ch, rate=self._src_rate, input=True, input_device_index=int(info['index']), frames_per_buffer=self._chunk)

    def frames(self, heartbeat: bool=False):
        import numpy as np
        while True:
            try:
                raw = self._stream.read(self._chunk, exception_on_overflow=False)
                if not raw:
                    if heartbeat:
                        yield b''
                    continue
                a = np.frombuffer(raw, dtype=np.int16)
                if self._src_ch != 2:
                    a = a.reshape(-1, self._src_ch)[:, :2].reshape(-1)
                if self._src_rate != self._sr:
                    st = a.reshape(-1, 2).astype(np.float32)
                    m = st.shape[0]
                    n_out = int(m * self._sr / self._src_rate)
                    if n_out < 1:
                        if heartbeat:
                            yield b''
                        continue
                    xs = np.linspace(0.0, m - 1, n_out)
                    x0 = np.arange(m)
                    out = np.empty((n_out, 2), dtype=np.int16)
                    out[:, 0] = np.interp(xs, x0, st[:, 0]).astype(np.int16)
                    out[:, 1] = np.interp(xs, x0, st[:, 1]).astype(np.int16)
                    yield out.tobytes()
                else:
                    yield a.tobytes()
            except Exception:
                if heartbeat:
                    yield b''
                return

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        try:
            self._pa.terminate()
        except Exception:
            return None


if __name__ == '__main__':
    import math
    import struct

    def _rms(data: bytes) -> float:
        n = len(data) // 2
        if not n:
            return 0.0
        samples = struct.unpack(f'<{n}h', data)
        return math.sqrt(sum(s * s for s in samples) / n)

    name = sys.argv[1] if len(sys.argv) > 1 else 'Spotify.exe'
    print(f'[*] Windows build: {sys.getwindowsversion().build} (needs >= 19041)')
    print(f'[*] ProcessLoopbackCapture self-test - target {name!r}, 50 frames...')
    cap = ProcessLoopbackCapture(name)
    print(f'    pid={cap.pid} available={cap.available}')
    nonzero = 0
    try:
        for i, frame in enumerate(cap.frames()):
            assert len(frame) == FRAME_BYTES, f'Bad frame size: {len(frame)}'
            r = _rms(frame)
            if r > 1.0:
                nonzero += 1
            if i < 5 or (i + 1) % 10 == 0:
                print(f'    frame {i + 1:>3}: {len(frame)} bytes  RMS={r:8.1f}')
            if i >= 49:
                break
    finally:
        cap.close()
    print(f'[*] Done. nonzero-RMS frames: {nonzero}/50')
