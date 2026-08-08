"""Silero VAD (ONNX) speech classifier â€” distinguishes real speech from engine/noise.\n\nModel inputs  (v5):\n  input  : float32 [1, chunk_samples]  â€” audio chunk normalised to [-1, 1]\n  state  : float32 [2, 1, 128]         â€” recurrent state (carry between calls)\n  sr     : int64   scalar              â€” sample rate (16000)\n\nModel outputs:\n  output : float32 [1, 1]              â€” speech probability\n  stateN : float32 [2, 1, 128]        â€” updated recurrent state\n"""

import os
import numpy as np

_MODEL = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")
_CHUNK = 512
_CTX = 64


class SileroClassifier:
    """Callable: feed 640-byte (320-sample int16) frames, returns bool \'is speech now\'.\n\n    Buffers pairs of 320-sample frames into 512-sample chunks for the model.\n    Carries recurrent state across calls so context is preserved.\n"""

    def __init__(self, threshold: float = 0.5, model_path: str = _MODEL):
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            so.add_session_config_entry("session.inter_op.allow_spinning", "0")
        except Exception:
            pass
        self._sess = ort.InferenceSession(
            model_path, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._thresh = threshold
        self._in_names = {i.name for i in self._sess.get_inputs()}
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CTX), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)
        self._buf = np.empty(0, dtype=np.float32)
        self._last = False
        self._last_prob = 0.0

    def __call__(self, frame_bytes: bytes) -> bool:
        samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32)
        samples *= 3.0517578125e-05
        if not hasattr(self, "_pending") or self._pending is None:
            self._pending = [self._buf] if self._buf.size else []
            self._pending_len = self._buf.size
        self._pending.append(samples)
        self._pending_len += samples.size
        if self._pending_len < _CHUNK:
            return self._last
        else:
            self._buf = (
                np.concatenate(self._pending)
                if len(self._pending) > 1
                else self._pending[0]
            )
            self._pending = None
            while len(self._buf) >= _CHUNK:
                chunk = self._buf[:_CHUNK]
                self._buf = self._buf[_CHUNK:]
                x = np.concatenate(
                    [self._context, chunk.reshape(1, _CHUNK)], axis=1
                ).astype(np.float32)
                candidate_feeds = {"input": x, "state": self._state, "sr": self._sr}
                feeds = {
                    k: v for k, v in candidate_feeds.items() if k in self._in_names
                }
                out = self._sess.run(None, feeds)
                prob = float(np.array(out[0]).flatten()[0])
                self._state = np.array(out[1], dtype=np.float32)
                self._context = x[:, -_CTX:]
                self._last_prob = prob
                self._last = prob >= self._thresh
            return self._last
