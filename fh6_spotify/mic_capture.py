"""Default-microphone capture for the optional "duck on my own voice" feature.

Mirrors capture.GameAudioCapture's output contract - frames() yields exactly
FRAME_BYTES (640) byte, 16 kHz, mono, 16-bit little-endian PCM frames, plus a
close() - but captures the DEFAULT INPUT device (your mic) instead of an output
loopback. Used only by SpeechWorker's own-voice path, which opens it solely
during an active voice conversation (a friend has spoken recently).

Local-only: frames go straight into the VAD and are never stored, written, or
sent anywhere. The mic is opened only while a conversation is active and closed
the moment it goes silent.
"""
import array
import audioop
from typing import Generator, Optional
import pyaudiowpatch as pyaudio
from fh6_spotify.speech import SAMPLE_RATE, FRAME_BYTES


class MicCapture:
    """Capture the default input device and yield SpeechDetector-shaped frames.

    Parameters
    ----------
    device_name : str, optional
        Substring of the input device to use (case-insensitive), e.g.
        "Focusrite". Empty -> the WASAPI default input.
    device_index : int, optional
        Explicit PortAudio input device index. Overrides device_name.
    """

    def __init__(self, device_name: str='', device_index: Optional[int]=None) -> None:
        self._pa = pyaudio.PyAudio()
        self._resample_state = None
        if device_index is None:
            device_index = self._find_input_by_name(device_name) if device_name else None
        if device_index is None:
            device_index = self._default_input_index()
        info = self._pa.get_device_info_by_index(device_index)
        self._src_rate = int(info['defaultSampleRate'])
        self._channels = max(int(info['maxInputChannels']), 1)
        self._chunk_frames = int(self._src_rate * 0.02)
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=self._channels, rate=self._src_rate, input=True, input_device_index=device_index, frames_per_buffer=self._chunk_frames)
        self._device_name = info.get('name', '')
        self._device_index = device_index
        self._leftover = b''

    def _find_input_by_name(self, name: str) -> Optional[int]:
        """Index of the first input-capable device whose name contains *name*
        (case-insensitive). Exact match wins over substring. None if no match."""
        want = name.strip().lower()
        if not want:
            return
        exact = None
        substr = None
        for i in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(i)
            except Exception:
                continue
            if int(info.get('maxInputChannels', 0)) <= 0:
                continue
            nm = str(info.get('name', '')).lower()
            if nm == want:
                exact = i
                break
            if substr is None and want in nm:
                substr = i
        if exact is not None:
            return exact
        return substr

    @staticmethod
    def list_input_devices() -> list:
        """List distinct input-device names (for a mic picker UI). Best-effort;
        returns [] on any failure so the caller can fall back to 'default only'."""
        names = []
        pa = None
        try:
            pa = pyaudio.PyAudio()
            seen = set()
            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                except Exception:
                    continue
                if int(info.get('maxInputChannels', 0)) <= 0:
                    continue
                nm = str(info.get('name', '')).strip()
                if nm and nm.lower() not in seen:
                    seen.add(nm.lower())
                    names.append(nm)
        except Exception:
            return []
        finally:
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
        return names

    def _default_input_index(self) -> int:
        """Index of the default capture device. Prefer the WASAPI default input,
        and fall back to PortAudio's generic default input if WASAPI can't be
        queried."""
        try:
            api = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            idx = api.get('defaultInputDevice', -1)
            if idx is not None and idx >= 0:
                return int(idx)
        except Exception:
            pass
        info = self._pa.get_default_input_device_info()
        return int(info['index'])

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def device_index(self) -> int:
        return self._device_index

    def frames(self) -> Generator[bytes, None, None]:
        """Yield 640-byte, 16 kHz, mono, 16-bit LE PCM frames. Blocks on the mic
        read (run on a dedicated worker thread). Runs until close() makes the next
        read raise, or the caller stops iterating."""
        while True:
            raw = self._stream.read(self._chunk_frames, exception_on_overflow=False)
            if self._channels == 2:
                raw = audioop.tomono(raw, 2, 0.5, 0.5)
            elif self._channels > 2:
                samples = array.array('h')
                samples.frombytes(raw)
                n = self._channels
                mono = array.array('h', (sum(samples[i:i + n]) // n for i in range(0, len(samples), n)))
                raw = mono.tobytes()
            if self._src_rate != SAMPLE_RATE:
                raw, self._resample_state = audioop.ratecv(raw, 2, 1, self._src_rate, SAMPLE_RATE, self._resample_state)
            raw = self._leftover + raw
            offset = 0
            while offset + FRAME_BYTES <= len(raw):
                yield raw[offset:offset + FRAME_BYTES]
                offset += FRAME_BYTES
            self._leftover = raw[offset:]

    def close(self) -> None:
        """Stop the stream and release PortAudio. Run on the capture thread."""
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()


if __name__ == '__main__':
    import math
    import struct

    def _rms(data: bytes) -> float:
        n = len(data) // 2
        if not n:
            return 0.0
        s = struct.unpack(f'<{n}h', data)
        return math.sqrt(sum(x * x for x in s) / n)

    print('MicCapture self-test - capturing 50 frames from the default mic...')
    cap = MicCapture()
    print(f'  device: [{cap.device_index}] {cap.device_name}')
    nonzero = 0
    try:
        for i, frame in enumerate(cap.frames()):
            assert len(frame) == FRAME_BYTES, f'Bad frame size: {len(frame)}'
            r = _rms(frame)
            if r > 1.0:
                nonzero += 1
            if i < 5 or (i + 1) % 10 == 0:
                print(f'  frame {i + 1:>3}: {len(frame)} bytes  RMS={r:8.1f}')
            if i >= 49:
                break
    finally:
        cap.close()
    print(f'Done. nonzero-RMS frames: {nonzero}/50 (talk into the mic to see RMS rise).')
