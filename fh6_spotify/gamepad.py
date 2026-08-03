"""DualSense controller input -> Spotify.\n\nTouchpad is the primary remote (FH6 doesn\'t use the touchpad surface, so it\nnever collides with in-game menus):\n  - vertical swipe   -> volume up/down\n  - horizontal swipe -> next / previous track\n  - light tap        -> pause / play\nD-pad skip is available but OFF by default (it collides with FH6 menu nav).\n\nReads the DualSense directly over **raw, read-only hidapi** (the same approach\n`dualshock.py` uses for the DS4) instead of pydualsense. Why read-only matters:\npydualsense opens the pad read+WRITE and runs an output loop, which fights\nadaptive-trigger mods (HamzaYslmn/Forza-Horizon-DualSense-Python etc.) that also\nhold a write handle -> their trigger writes don\'t land. Segue only ever READS\n(touchpad + buttons), so a read-only handle lets a trigger mod coexist; no Segue\nfeature is lost.\n\nAlso handles USB *and* Bluetooth report layouts and auto-reconnects if the device\ndrops mid-session (Steam Input briefly grabbing the pad, a BT blip, sleep/wake).\n\nDualSense input report (USB id 0x01, 64 bytes; report id at byte 0):\n    [1-4]   LX, LY, RX, RY        (analog sticks - ignored)\n    [5-6]   L2, R2 analog         (ignored)\n    [7]     sequence\n    [8]     low nibble = D-pad hat (0=N,2=E,4=S,6=W,8=neutral);\n            high nibble = square/cross/circle/triangle\n    [9]     L1/R1/L2d/R2d + create(share)/options/L3/R3\n    [10]    bit0 = PS, bit1 = touchpad click, bit2 = mute (mic)\n    [33-36] touch finger 0: byte33 bit7 = !active; bytes 34-36 pack X (12b)+Y(12b)\n    [37-40] touch finger 1\nBluetooth report id 0x31 (78 bytes) is the same payload shifted by 1 byte, so we\nslice off one leading byte and parse with the USB offsets.\n"""
import threading
import time
from fh6_spotify.config import Config
from fh6_spotify.skip_rule import SkipRule
from fh6_spotify import mediakeys
from fh6_spotify.input_backend import named_active
_DS_VENDOR = 1356
_DS_PRODUCTS = (3302, 3570)
_DS_TP_W = 1919
_DS_TP_H = 1079
_RECONNECT_DELAY_S = 1.5
def touch_volume_delta(cur_y: int, prev_y: int, active: bool, was_active: bool, sensitivity: float) -> float:
    """Volume change from a vertical swipe (up = louder; touchpad Y grows downward)."""
    if active and was_active:
        return (prev_y - cur_y) * sensitivity
    else:
        return 0.0
def is_tap(duration_ms: float, movement: int, max_ms: int, max_move: int, min_ms: float=0.0) -> bool:
    """A tap = a short touch that barely moved (distinct from a swipe). min_ms\n    floors out ultra-brief grazes (a real tap lasts longer than a flick of skin)."""
    return min_ms <= duration_ms <= max_ms and movement <= max_move
def tap_thresholds(sensitivity: int) -> tuple[int, int]:
    """Map the 0..100 tap_sensitivity knob to (max_ms, max_move). Higher =\n    looser/easier tap; lower = quicker + more stationary required. (0 is handled\n    by the caller as \'tap off\'.)"""
    f = max(0, min(100, sensitivity)) / 100.0
    return (int(160 + f * 180), int(15 + f * 85))
def classify_swipe(dx: int, dy: int, swipe_threshold: int, vol_deadzone: int=25) -> str | None:
    """Classify a touch gesture once it has moved enough.\n    Returns \'skip-next\' / \'skip-prev\' (dominant horizontal past threshold),\n    \'vol\' (dominant vertical past deadzone), or None (not yet decided).\n\n    Volume requires vertical movement to be ~1.6x the horizontal - thumbs\n    naturally drift upward during a horizontal swipe and the old \"any vertical\n    wins\" rule kept flipping skips into volume changes. Biases toward\n    horizontal classification when both axes are close."""
    if abs(dx) >= swipe_threshold and abs(dx) > abs(dy):
        return 'skip-next' if dx > 0 else 'skip-prev'
    else:
        if abs(dy) >= vol_deadzone and abs(dy) > abs(dx) * 1.6:
            return 'vol'
        else:
            return None
class _TouchPoint:
    def __init__(self):
        self.isActive = False
        self.ID = 0
        self.X = 0
        self.Y = 0
class _DSState:
    """Flat, pydualsense-state-compatible object. Attribute names match the\n    fields the rest of the app reads (cross/circle/square/triangle, L1/R1,\n    L3/R3, share/options/micBtn/touchBtn/ps, DpadUp/Down/Left/Right,\n    trackPadTouch0/1)."""
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
        """Reset every input to neutral (used on disconnect so no button or\n        touch sticks as \'pressed\' while the pad is gone)."""
        for n in ['cross', 'circle', 'square', 'triangle', 'L1', 'R1', 'L2', 'R2', 'L3', 'R3', 'share', 'options', 'micBtn', 'touchBtn', 'ps', 'DpadUp', 'DpadDown', 'DpadLeft', 'DpadRight']:
            setattr(self, n, False)
        for tp in [self.trackPadTouch0, self.trackPadTouch1]:
            tp.isActive = False
def _open_dualsense():
    """Open the first connected DualSense read-only via hidapi. Returns a\n    `hidapi.Device`, or raises so the caller can fall back to another backend.\n    Uses the same hidapi package pydualsense already pulls in - no new dep, and\n    crucially NO write handle (so trigger mods keep theirs)."""
    import hidapi
    last = None
    for product in _DS_PRODUCTS:
        try:
            return hidapi.Device(vendor_id=_DS_VENDOR, product_id=product, blocking=False)
        except (OSError, IOError) as exc:
            last = exc
        else:
            pass
    raise RuntimeError(f'DualSense not found ({last})')
class DualSenseInput:
    def __init__(self, config: Config, on_next=mediakeys.media_next, on_prev=mediakeys.media_prev, on_tap=mediakeys.media_playpause, on_open=None):
        self.c = config
        self._on_next = on_next
        self._on_prev = on_prev
        self._on_tap = on_tap
        self._on_open = on_open or (lambda: None)
        self._open_down_t = None
        self._open_fired = False
        self._rule = SkipRule(config)
        self.state = _DSState()
        self._dev = _open_dualsense()
        self._connected = True
        self._stop = False
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self._prev_dpad = {'up': False, 'down': False, 'left': False, 'right': False}
        self._prev_face = {'cross': False, 'circle': False, 'square': False, 'triangle': False}
        self._safe_mode = config.safe_mode_default
        self._suppressed = False
        self._prev_safe_btn = False
        self._prev_pause_btn = False
        self._prev_skip_btn = False
        self._prev_touch_btn = False
        self._prev_comms_view = False
        self.comms_latch_edge = False
        self._prev_latch_btn = False
        self._prev_can_skip = False
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
        return 'DualSense'
    @property
    def safe_mode(self) -> bool:
        return self._safe_mode
    def set_safe_mode(self, value: bool) -> None:
        self._safe_mode = bool(value)
    _CAPTURE_BUTTONS = ('cross', 'circle', 'square', 'triangle', 'L1', 'R1', 'L3', 'R3', 'micBtn', 'share', 'options', 'DpadUp', 'DpadDown', 'DpadLeft', 'DpadRight')
    def read_pressed(self):
        """All currently-held capture buttons joined with \"+\" (for rebinding), or\n        None. Returning the full held set lets the rebind popup capture a combo\n        (e.g. \"L1+square\") when buttons are held together; a single press is just\n        its name. Deterministic order (_CAPTURE_BUTTONS order). Reads the live\n        state the HID thread maintains; no second device handle."""
        # ***<module>.DualSenseInput.read_pressed: Failure: Different control flow
        s = self.state
        parts = [name for name in self._CAPTURE_BUTTONS if bool(getattr(s, name, False))]
        return '+'.join(parts) if parts else None
    def _read_loop(self):
        """Continuously drain HID reports into self.state. Self-healing: if the
        device errors or vanishes (Steam Input grab, BT blip, sleep/wake), close
        it, clear state, and retry opening every ~1.5 s until it's back."""
        while not self._stop:
            dev = self._dev
            if dev is None:
                try:
                    self._dev = _open_dualsense()
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
                if not data or len(data) < 35:
                    continue
                if data[0] == 49 and len(data) >= 78:
                    data = data[1:]
                self._parse_report(data)
            except (OSError, IOError):
                try:
                    dev.close()
                except Exception:
                    pass
                self._dev = None
                self._connected = False
                self.state.clear()
    def _parse_report(self, d):
        """Decode the USB-layout report into self.state. Tolerant of short\n        reports - any field we can\'t read is left at its previous value."""
        s = self.state
        if len(d) > 8:
            b = d[8]
            hat = b & 15
            s.DpadUp = hat in (0, 1, 7)
            s.DpadRight = hat in (1, 2, 3)
            s.DpadDown = hat in (3, 4, 5)
            s.DpadLeft = hat in (5, 6, 7)
            s.square = bool(b & 16)
            s.cross = bool(b & 32)
            s.circle = bool(b & 64)
            s.triangle = bool(b & 128)
        if len(d) > 9:
            b = d[9]
            s.L1 = bool(b & 1)
            s.R1 = bool(b & 2)
            s.L2 = bool(b & 4)
            s.R2 = bool(b & 8)
            s.share = bool(b & 16)
            s.options = bool(b & 32)
            s.L3 = bool(b & 64)
            s.R3 = bool(b & 128)
        if len(d) > 10:
            b = d[10]
            s.ps = bool(b & 1)
            s.touchBtn = bool(b & 2)
            s.micBtn = bool(b & 4)
        if len(d) >= 37:
            self._decode_touch(d, 33, s.trackPadTouch0)
        if len(d) >= 41:
            self._decode_touch(d, 37, s.trackPadTouch1)
    def _decode_touch(self, d, off, tp):
        tag = d[off]
        active = tag & 128 == 0
        tp.isActive = active
        if not active:
            return
        else:
            tp.ID = tag & 127
            tp.X = d[off + 1] | (d[off + 2] & 15) << 8
            tp.Y = (d[off + 2] & 240) >> 4 | d[off + 3] << 4
    def _poll_dpad(self, s, now: float, can_skip: bool) -> None:
        dpad = {'up': s.DpadUp, 'down': s.DpadDown, 'left': s.DpadLeft, 'right': s.DpadRight}
        for direction, pressed in dpad.items():
            if pressed and (not self._prev_dpad[direction]):
                    action = self._rule.on_dpad(direction, can_skip, now)
                    if action == 'next':
                        self._on_next()
                    else:
                        if action == 'prev':
                            self._on_prev()
            self._prev_dpad[direction] = pressed
        face = {'cross': s.cross, 'circle': s.circle, 'square': s.square, 'triangle': s.triangle}
        for name, pressed in face.items():
            if pressed and (not self._prev_face[name]):
                    self._rule.on_resume()
            self._prev_face[name] = pressed
    def poll(self, now: float, can_skip: bool) -> float:
        # irreducible cflow, using cdg fallback
        """Handle skip/pause (side effects) and return a volume delta from touchpad swipe.\n\n        `can_skip` = auto gate (moving). Skip also requires safe mode off.\n        """
        # ***<module>.DualSenseInput.poll: Failure: Different control flow
        s = self.state
        if self._suppressed:
            self._prev_safe_btn = named_active(self.c.safe_mode_button, s)
            if self.c.pause_button:
                self._prev_pause_btn = named_active(self.c.pause_button, s)
            if self.c.skip_button:
                self._prev_skip_btn = named_active(self.c.skip_button, s)
            self._prev_touch_btn = bool(getattr(s, 'touchBtn', False))
            self._prev_comms_view = bool(getattr(s, 'share', False))
            self._prev_latch_btn = named_active(getattr(self.c, 'latch_button', 'share') or 'share', s)
            for d in ['up', 'down', 'left', 'right']:
                self._prev_dpad[d] = bool(getattr(s, 'Dpad' + d.capitalize(), False))
            for f in ['cross', 'circle', 'square', 'triangle']:
                self._prev_face[f] = bool(getattr(s, f, False))
            self._tp_was_active = bool(s.trackPadTouch0.isActive)
            self._prev_can_skip = can_skip
            self._open_down_t = None
            self._open_fired = False
            return 0.0
        open_name = getattr(self.c, 'open_button', '') or ''
        open_down = named_active(open_name, s)
        if open_down and self._open_down_t is None:
            self._open_down_t = now
            self._open_fired = False
        else:
            if open_down and (not self._open_fired) and (self._open_down_t is not None) and ((now - self._open_down_t) * 1000 >= getattr(self.c, 'open_hold_ms', 1200)):
                self._open_fired = True
                try:
                    self._on_open()
                except Exception:
                    pass
            else:
                if not open_down:
                    self._open_down_t = None
        safe_name = self.c.safe_mode_button
        safe_btn = named_active(safe_name, s)
        if safe_name and safe_name == open_name:
            if not safe_btn and self._prev_safe_btn and (not self._open_fired):
                        self._safe_mode = not self._safe_mode
        else:
            if safe_name and safe_btn and (not self._prev_safe_btn):
                        self._safe_mode = not self._safe_mode
        self._prev_safe_btn = safe_btn
        if self.c.pause_button:
            pause_btn = named_active(self.c.pause_button, s)
            if pause_btn and (not self._prev_pause_btn):
                    self._on_tap()
            self._prev_pause_btn = pause_btn
        if self.c.skip_button:
            skip_btn = named_active(self.c.skip_button, s)
            if skip_btn and (not self._prev_skip_btn) and can_skip and (not self._safe_mode):
                            self._on_next()
            self._prev_skip_btn = skip_btn
        if self.c.pause_input == 'press':
            touch_btn = bool(getattr(s, 'touchBtn', False))
            if touch_btn and (not self._prev_touch_btn):
                    self._on_tap()
            self._prev_touch_btn = touch_btn
        if self.c.mode == 'forza':
            view_btn = bool(getattr(s, 'share', False))
            if view_btn and (not self._prev_comms_view):
                    self._rule.on_comms(now)
            self._prev_comms_view = view_btn
            latch_attr = getattr(self.c, 'latch_button', 'share') or 'share'
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
                if kind == 'skip-next':
                    self._tp_mode = 'skip'
                    if self.c.touchpad_skip_enabled:
                        self._on_next()
                elif kind == 'skip-prev':
                    self._tp_mode = 'skip'
                    if self.c.touchpad_skip_enabled:
                        self._on_prev()
                elif kind == 'vol':
                    self._tp_mode = 'vol'
            if self._tp_mode == 'vol' and self.c.touchpad_volume_enabled:
                delta = touch_volume_delta(tp.Y, self._tp_prev_y, True, True, self.c.touchpad_sensitivity)
            self._tp_prev_y = tp.Y
        if self._tp_was_active and not active:
            tp_end = s.trackPadTouch0
            dx_end = tp_end.X - self._tp_start_x
            dy_end = tp_end.Y - self._tp_start_y
            intent = int(self.c.swipe_skip_threshold * 0.6)
            if self._tp_mode is None:
                if self.c.touchpad_skip_enabled and abs(dx_end) >= intent and abs(dx_end) > abs(dy_end):
                    if dx_end > 0:
                        self._on_next()
                    else:
                        self._on_prev()
                    self._tp_mode = 'skip'
                if self._tp_mode is None and self.c.touchpad_tap_enabled and self.c.pause_input == 'tap' and getattr(self.c, 'tap_sensitivity', 70) > 0:
                    _max_ms, _max_move = tap_thresholds(getattr(self.c, 'tap_sensitivity', 70))
                    if is_tap((now - self._tp_start_t) * 1000, self._tp_moved, _max_ms, _max_move, min_ms=40.0):
                        self._on_tap()
        self._tp_was_active = active
        return delta
    def close(self) -> None:
        self._stop = True
