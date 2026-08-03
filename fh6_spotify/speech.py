import numpy as np
from fh6_spotify.config import Config
SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2
ONSET_MS = 120
RUN_GAP_MS = 220
RMS_FLOOR = 0.012
SYSTEM_ONSET_MS = 150
SYSTEM_HANGOVER_MIN_MS = 320
SYSTEM_HANGOVER_MAX_MS = 1100
WEBRTC_MIN_RUN = 5
def _make_webrtc_classifier(aggressiveness: int, min_run: int=WEBRTC_MIN_RUN):
    import _webrtcvad
    vad = _webrtcvad.create()
    _webrtcvad.init(vad)
    _webrtcvad.set_mode(vad, int(aggressiveness))
    run = [0]
    def classify(frame: bytes) -> bool:
        if len(frame)!= FRAME_BYTES:
            run[0] = 0
            return False
        else:
            hit = bool(_webrtcvad.process(vad, SAMPLE_RATE, frame, FRAME_BYTES // 2))
            run[0] = run[0] + 1 if hit else 0
            return run[0] >= min_run
    return classify
class SpeechDetector:
    def __init__(self, config: Config, classifier=None):
        self.c = config
        self._classify = classifier or _make_webrtc_classifier(config.vad_aggressiveness)
        self._last_hit = None
        self._run_start = None
        self._episode_start = None
        self._latched = False
    def feed(self, frame: bytes, now: float) -> bool:
        system = getattr(self.c, 'duck_scope', 'game') == 'system'
        onset_ms = SYSTEM_ONSET_MS if system else ONSET_MS
        cls = bool(self._classify(frame))
        rms = 0.0
        if frame:
            s = np.frombuffer(frame, dtype=np.int16)
            if s.size:
                rms = float(np.sqrt(np.mean(s.astype(np.float32) ** 2))) / 32768.0
        hit = cls and rms >= RMS_FLOOR
        if hit:
            if self._last_hit is None or (now - self._last_hit) * 1000 > RUN_GAP_MS:
                self._run_start = now
            if self._episode_start is None:
                self._episode_start = now
            self._last_hit = now
            if not self._latched and (now - self._run_start) * 1000 >= onset_ms:
                    self._latched = True
        if system and self._episode_start is not None and (self._last_hit is not None):
            episode_ms = (self._last_hit - self._episode_start) * 1000
            hangover_ms = min(max(episode_ms, SYSTEM_HANGOVER_MIN_MS), SYSTEM_HANGOVER_MAX_MS)
        else:
            hangover_ms = self.c.hangover_ms
        if self._last_hit is None or (now - self._last_hit) * 1000 > hangover_ms:
            self._latched = False
            self._run_start = None
            self._episode_start = None
        return self._latched
