"""DualShock 4 (PS4) controller input -> Spotify.\n\nSame gestures and UX as DualSenseInput - touchpad swipe = volume, tap = pause,\nD-pad = skip - but reads the DS4 directly via hidapi instead of pydualsense\n(which is PS5-only). Exposes a pydualsense-compatible `state` object so the\nrebind UI, skip rule, and gesture parser in `gamepad.py` work unchanged.\n\nDS4 USB HID report 0x01 (64 bytes) layout used here:\n    [1-2]   LX, LY               (analog stick - ignored)\n    [3-4]   RX, RY               (analog stick - ignored)\n    [5]     low nibble  = D-pad (0=N, 2=E, 4=S, 6=W, 8=neutral)\n            high nibble = square/cross/circle/triangle\n    [6]     L1/R1/L2d/R2d + share/options/L3/R3\n    [7]     bit 0 = PS, bit 1 = touchpad click\n    [8-9]   L2/R2 analog        (ignored)\n    [35-38] touch 1: byte35 high bit = !active, ID in low bits;\n            bytes 36-38 pack X (12 bits) and Y (12 bits)\n\nRun-time deps: hidapi-usb (already pulled in by pydualsense). Touchpad coords\nare 0..1919 (X) and 0..941 (Y); we re-scale to roughly match DualSense\'s\ntrackpad range so existing sensitivity/threshold defaults still feel right.\n"""

import threading
import time
from fh6_spotify.config import Config
from fh6_spotify.skip_rule import SkipRule
from fh6_spotify import mediakeys
from fh6_spotify.gamepad import touch_volume_delta, is_tap, classify_swipe
from fh6_spotify.input_backend import named_active

_DS4_VENDOR = 1356
_DS4_PRODUCTS = (1476, 2508, 2976)
_DS_TP_W = 1919
_DS_TP_H = 1079
_DS4_TP_W = 1919
_DS4_TP_H = 941
_RECONNECT_DELAY_S = 1.5


class _DSButtonState:
    """Pydualsense-state-compatible flat object. Attribute names match the\n    DualSense fields we read elsewhere (cross/circle/square/triangle, L1/R1,\n    L3/R3, share/options/micBtn, DpadUp/Down/Left/Right, trackPadTouch0).\n    `micBtn` doesn\'t exist on DS4 - we expose it as always False so any code\n    that checks for it just sees no press."""

    def __init__(self):
        self.cross = False
        self.circle = False
        self.square = False
        self.triangle = False
        self.L1 = False
        self.R1 = False
        self.L2 = False
        self.R2 = False
        self.L3 = False
        self.R3 = False
        self.share = False
        self.options = False
        self.micBtn = False
        self.touchBtn = False
        self.ps = False
        self.DpadUp = False
        self.DpadDown = False
        self.DpadLeft = False
        self.DpadRight = False
        self.trackPadTouch0 = _TouchPoint()
        self.trackPadTouch1 = _TouchPoint()

    def clear(self):
        """Reset every input to neutral (used on disconnect so nothing sticks)."""
        for n in [
            "cross",
            "circle",
            "square",
            "triangle",
            "L1",
            "R1",
            "L2",
            "R2",
            "L3",
            "R3",
            "share",
            "options",
            "micBtn",
            "touchBtn",
            "ps",
            "DpadUp",
            "DpadDown",
            "DpadLeft",
            "DpadRight",
        ]:
            setattr(self, n, False)
        for tp in [self.trackPadTouch0, self.trackPadTouch1]:
            tp.isActive = False


class _TouchPoint:
    def __init__(self):
        self.isActive = False
        self.ID = 0
        self.X = 0
        self.Y = 0


def _open_ds4():
    """Open the first connected DS4. Returns a `hidapi.Device`, or raises so\n    the caller can fall back to another backend. Uses the same hidapi-usb\n    package that pydualsense already pulls in - no new dependency."""
    import hidapi

    for product in _DS4_PRODUCTS:
        try:
            return hidapi.Device(
                vendor_id=_DS4_VENDOR, product_id=product, blocking=False
            )
        except (OSError, IOError):
            pass
        else:
            pass
    raise RuntimeError("DualShock 4 not found")


class DualShockInput:
    """Mirrors `DualSenseInput`\'s public surface so runner.py + settings.py\n    can drive a DS4 with zero special-casing."""

    def __init__(
        self,
        config: Config,
        on_next=mediakeys.media_next,
        on_prev=mediakeys.media_prev,
        on_tap=mediakeys.media_playpause,
        on_open=None,
    ):
        self.c = config
        self._on_next = on_next
        self._on_prev = on_prev
        self._on_tap = on_tap
        self._on_open = on_open or (lambda: None)
        self._open_down_t = None
        self._open_fired = False
        self._rule = SkipRule(config)
        self._dev = _open_ds4()
        self._connected = True
        self.state = _DSButtonState()
        self._stop = False
        self._is_bt = False
        self._seen_shapes = set()
        self._last_touch_seg = None
        self._touch_log_n = 0
        self._touch_log_t = 0.0
        self._diag_full = None
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self._prev_dpad = {"up": False, "down": False, "left": False, "right": False}
        self._prev_face = {
            "cross": False,
            "circle": False,
            "square": False,
            "triangle": False,
        }
        self._safe_mode = config.safe_mode_default
        self._suppressed = False
        self._prev_safe_btn = False
        self._prev_pause_btn = False
        self._prev_skip_btn = False
        self._prev_touch_btn = False
        self._prev_can_skip = False
        self._prev_comms_view = False
        self.comms_latch_edge = False
        self._prev_latch_btn = False
        self._tp_was_active = False
        self._tp_start_x = 0
        self._tp_start_y = 0
        self._tp_start_t = 0.0
        self._tp_prev_y = 0
        self._tp_moved = 0
        self._tp_mode = None

    @property
    def available(self) -> bool:
        return True

    @property
    def connected(self) -> bool:
        """Live HID connection state (False during a reconnect blip)."""
        return self._connected

    @property
    def device_name(self) -> str:
        return "DualShock 4"

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    def set_safe_mode(self, value: bool) -> None:
        self._safe_mode = bool(value)

    _CAPTURE_BUTTONS = (
        "cross",
        "circle",
        "square",
        "triangle",
        "L1",
        "R1",
        "L3",
        "R3",
        "share",
        "options",
        "touchBtn",
        "ps",
        "DpadUp",
        "DpadDown",
        "DpadLeft",
        "DpadRight",
    )

    def read_pressed(self):
        """All held capture buttons joined with \"+\" (for combo rebinds), or None.\n        Single press = its name; held together = \"L1+square\". Deterministic order."""
        # ***<module>.DualShockInput.read_pressed: Failure: Different control flow
        s = self.state
        parts = [
            name for name in self._CAPTURE_BUTTONS if bool(getattr(s, name, False))
        ]
        return "+".join(parts) if parts else None

    def _read_loop(self):
        """Drain HID reports continuously. Self-healing: on device loss it clears
        state and retries opening every ~1.5 s (Steam Input grab, BT blip,
        sleep/wake) instead of dying. Reads 78 bytes so the full Bluetooth report
        comes through - reading only 64 truncated it, so the BT branch never fired
        and wireless DS4 input was misparsed."""
        while not self._stop:
            dev = self._dev
            if dev is None:
                try:
                    self._dev = _open_ds4()
                    self._connected = True
                    dev = self._dev
                except Exception:
                    if self._dev is not None:
                        try:
                            self._dev.close()
                        except Exception:
                            pass
                    self._dev = None
                    self._connected = False
                    self.state.clear()
                    time.sleep(_RECONNECT_DELAY_S)
                    continue
            try:
                data = dev.read(78, timeout_ms=100)
                if not data:
                    continue
                if self._diag_full is None:
                    try:
                        import os as _os

                        _p = _os.path.join(
                            _os.environ.get("APPDATA", "."), "Segue", "ds4_reports.log"
                        )
                        self._diag_full = (
                            _os.path.exists(_p) and _os.path.getsize(_p) > 262144
                        )
                    except Exception:
                        self._diag_full = False
                if self._diag_full:
                    self._touch_log_n = 99
                try:
                    shape = (data[0], len(data))
                    if shape not in self._seen_shapes:
                        self._seen_shapes.add(shape)
                        import os as _os

                        base = _os.path.join(_os.environ.get("APPDATA", "."), "Segue")
                        _os.makedirs(base, exist_ok=True)
                        with open(_os.path.join(base, "ds4_reports.log"), "a") as f:
                            f.write(
                                "report id=0x%02X len=%d head=%s\n"
                                % (data[0], len(data), bytes(data[:16]).hex(" "))
                            )
                except Exception:
                    pass
                try:
                    if len(data) >= 46 and self._touch_log_n < 24:
                        _now = time.monotonic()
                        if _now - self._touch_log_t >= 0.5:
                            self._touch_log_t = _now
                            self._touch_log_n += 1
                            seg = bytes(data[28 : min(len(data), 78)])
                            import os as _os

                            base = _os.path.join(
                                _os.environ.get("APPDATA", "."), "Segue"
                            )
                            _os.makedirs(base, exist_ok=True)
                            with open(_os.path.join(base, "ds4_reports.log"), "a") as f:
                                f.write("tail[28:]=%s\n" % seg.hex(" "))
                except Exception:
                    pass
                if len(data) < 35:
                    continue
                if data[0] == 17 and len(data) >= 78:
                    data = data[2:]
                self._parse_usb_report(data)
            except (OSError, IOError):
                try:
                    dev.close()
                except Exception:
                    pass
                self._dev = None
                self._connected = False
                self.state.clear()

    def _parse_usb_report(self, d):
        """Decode the ~64-byte USB report into self.state. Tolerant of short\n        reports - any field we can\'t read is left at its previous value."""
        s = self.state
        if len(d) > 5:
            face = d[5]
            dpad = face & 15
            s.DpadUp = dpad in (0, 1, 7)
            s.DpadRight = dpad in (1, 2, 3)
            s.DpadDown = dpad in (3, 4, 5)
            s.DpadLeft = dpad in (5, 6, 7)
            s.square = bool(face & 16)
            s.cross = bool(face & 32)
            s.circle = bool(face & 64)
            s.triangle = bool(face & 128)
        if len(d) > 6:
            b6 = d[6]
            s.L1 = bool(b6 & 1)
            s.R1 = bool(b6 & 2)
            s.L2 = bool(b6 & 4)
            s.R2 = bool(b6 & 8)
            s.share = bool(b6 & 16)
            s.options = bool(b6 & 32)
            s.L3 = bool(b6 & 64)
            s.R3 = bool(b6 & 128)
        if len(d) > 7:
            b7 = d[7]
            s.ps = bool(b7 & 1)
            s.touchBtn = bool(b7 & 2)
        if len(d) >= 39:
            self._decode_touch(d, 35, s.trackPadTouch0)
        if len(d) >= 43:
            self._decode_touch(d, 39, s.trackPadTouch1)

    def _decode_touch(self, d, off, tp):
        tag = d[off]
        active = tag & 128 == 0
        tp.isActive = active
        if not active:
            return
        else:
            tp.ID = tag & 127
            x_raw = d[off + 1] | (d[off + 2] & 15) << 8
            y_raw = d[off + 2] >> 4 | d[off + 3] << 4
            tp.X = int(x_raw * _DS_TP_W / _DS4_TP_W)
            tp.Y = int(y_raw * _DS_TP_H / _DS4_TP_H)

    def _poll_dpad(self, s, now: float, can_skip: bool) -> None:
        dpad = {
            "up": s.DpadUp,
            "down": s.DpadDown,
            "left": s.DpadLeft,
            "right": s.DpadRight,
        }
        for direction, pressed in dpad.items():
            if pressed and (not self._prev_dpad[direction]):
                action = self._rule.on_dpad(direction, can_skip, now)
                if action == "next":
                    self._on_next()
                else:
                    if action == "prev":
                        self._on_prev()
            self._prev_dpad[direction] = pressed
        face = {
            "cross": s.cross,
            "circle": s.circle,
            "square": s.square,
            "triangle": s.triangle,
        }
        for name, pressed in face.items():
            if pressed and (not self._prev_face[name]):
                self._rule.on_resume()
            self._prev_face[name] = pressed

    def poll(self, now: float, can_skip: bool) -> float:
        # irreducible cflow, using cdg fallback
        # ***<module>.DualShockInput.poll: Failure: Different control flow
        s = self.state
        if self._suppressed:
            sa = self.c.safe_mode_button or "touchBtn"
            if sa == "micBtn":
                sa = "touchBtn"
            self._prev_safe_btn = named_active(sa, s)
            if self.c.pause_button:
                self._prev_pause_btn = named_active(self.c.pause_button, s)
            if self.c.skip_button:
                self._prev_skip_btn = named_active(self.c.skip_button, s)
            self._prev_touch_btn = bool(getattr(s, "touchBtn", False))
            self._prev_comms_view = bool(getattr(s, "share", False))
            self._prev_latch_btn = named_active(
                getattr(self.c, "latch_button", "share") or "share", s
            )
            for d in ["up", "down", "left", "right"]:
                self._prev_dpad[d] = bool(getattr(s, "Dpad" + d.capitalize(), False))
            for f in ["cross", "circle", "square", "triangle"]:
                self._prev_face[f] = bool(getattr(s, f, False))
            self._tp_was_active = bool(s.trackPadTouch0.isActive)
            self._prev_can_skip = can_skip
            self._open_down_t = None
            self._open_fired = False
            return 0.0
        open_name = getattr(self.c, "open_button", "") or ""
        if open_name == "micBtn":
            open_name = "touchBtn"
        open_down = named_active(open_name, s)
        if open_down and self._open_down_t is None:
            self._open_down_t = now
            self._open_fired = False
        else:
            if (
                open_down
                and (not self._open_fired)
                and (self._open_down_t is not None)
                and (
                    (now - self._open_down_t) * 1000
                    >= getattr(self.c, "open_hold_ms", 1200)
                )
            ):
                self._open_fired = True
                try:
                    self._on_open()
                except Exception:
                    pass
            else:
                if not open_down:
                    self._open_down_t = None
        safe_attr = self.c.safe_mode_button or "touchBtn"
        if safe_attr == "micBtn":
            safe_attr = "touchBtn"
        safe_btn = named_active(safe_attr, s)
        if safe_attr == open_name:
            if not safe_btn and self._prev_safe_btn and (not self._open_fired):
                self._safe_mode = not self._safe_mode
        else:
            if safe_btn and (not self._prev_safe_btn):
                self._safe_mode = not self._safe_mode
        self._prev_safe_btn = safe_btn
        if self.c.pause_button:
            pause_btn = named_active(self.c.pause_button, s)
            if pause_btn and (not self._prev_pause_btn):
                self._on_tap()
            self._prev_pause_btn = pause_btn
        if self.c.pause_input == "press":
            touch_btn = bool(getattr(s, "touchBtn", False))
            if touch_btn and (not self._prev_touch_btn):
                self._on_tap()
            self._prev_touch_btn = touch_btn
        if self.c.skip_button:
            skip_btn = named_active(self.c.skip_button, s)
            if (
                skip_btn
                and (not self._prev_skip_btn)
                and can_skip
                and (not self._safe_mode)
            ):
                self._on_next()
            self._prev_skip_btn = skip_btn
        if self.c.mode == "forza":
            view_btn = bool(getattr(s, "share", False))
            if view_btn and (not self._prev_comms_view):
                self._rule.on_comms(now)
            self._prev_comms_view = view_btn
            latch_attr = getattr(self.c, "latch_button", "share") or "share"
            latch_btn = named_active(latch_attr, s)
            if latch_btn and (not self._prev_latch_btn):
                self.comms_latch_edge = True
            self._prev_latch_btn = latch_btn
        if can_skip and (not self._prev_can_skip):
            self._rule.on_resume()
        self._prev_can_skip = can_skip
        if self.c.gamepad_skip_enabled:
            self._poll_dpad(s, now, can_skip and (not self._safe_mode))
        delta = 0.0
        tp = s.trackPadTouch0
        active = bool(tp.isActive)
        if active and not self._tp_was_active:
            self._tp_start_x, self._tp_start_y, self._tp_start_t = (tp.X, tp.Y, now)
            self._tp_prev_y = tp.Y
            self._tp_moved = 0
            self._tp_mode = None
        if active:
            dx, dy = (tp.X - self._tp_start_x, tp.Y - self._tp_start_y)
            self._tp_moved = max(self._tp_moved, abs(dx) + abs(dy))
            if self._tp_mode is None:
                kind = classify_swipe(dx, dy, self.c.swipe_skip_threshold)
                if kind == "skip-next":
                    self._tp_mode = "skip"
                    if self.c.touchpad_skip_enabled:
                        self._on_next()
                elif kind == "skip-prev":
                    self._tp_mode = "skip"
                    if self.c.touchpad_skip_enabled:
                        self._on_prev()
                elif kind == "vol":
                    self._tp_mode = "vol"
            if self._tp_mode == "vol" and self.c.touchpad_volume_enabled:
                delta = touch_volume_delta(
                    tp.Y, self._tp_prev_y, True, True, self.c.touchpad_sensitivity
                )
            self._tp_prev_y = tp.Y
        if self._tp_was_active and not active:
            tp_end = s.trackPadTouch0
            dx_end = tp_end.X - self._tp_start_x
            dy_end = tp_end.Y - self._tp_start_y
            intent = int(self.c.swipe_skip_threshold * 0.6)
            if self._tp_mode is None:
                if (
                    self.c.touchpad_skip_enabled
                    and abs(dx_end) >= intent
                    and abs(dx_end) > abs(dy_end)
                ):
                    if dx_end > 0:
                        self._on_next()
                    else:
                        self._on_prev()
                    self._tp_mode = "skip"
                if (
                    self._tp_mode is None
                    and self.c.touchpad_tap_enabled
                    and self.c.pause_input == "tap"
                    and is_tap(
                        (now - self._tp_start_t) * 1000,
                        self._tp_moved,
                        self.c.tap_max_ms,
                        self.c.tap_move_threshold,
                    )
                ):
                    self._on_tap()
        self._tp_was_active = active
        return delta

    def close(self) -> None:
        self._stop = True
