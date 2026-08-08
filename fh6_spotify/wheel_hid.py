"""Read-only sim-wheel input via raw HID + Windows HidP report parsing.

WHY THIS EXISTS (LoafOf_Toast, Reddit): the old wheel path was pygame -> SDL2
-> DirectInput, and SDL's dinput backend acquires any device that reports
force-feedback capability with DISCL_EXCLUSIVE | DISCL_BACKGROUND (so it
*could* drive rumble - which Segue never does). Exclusive access is
single-owner, so the game silently loses the FFB pipe: the wheel goes limp,
exactly like having the wheel vendor's settings page open while driving.

This backend never touches DirectInput at all. Each HID game device is opened
with CreateFile(GENERIC_READ, share READ|WRITE) - a plain shared read handle,
no write access, no acquisition - and input reports are parsed with the
HidP_* API (hid.dll does the report-descriptor math, so any wheel layout
works without per-vendor tables). The game's DirectInput exclusive acquire
and its FFB writes are completely unaffected. Same philosophy as the
DualSense read-only rewrite in gamepad.py.

Buttons land on HID usage page 0x09, hats on page 0x01 usage 0x39; both are
flattened across every connected device (wheel base + pedals + shifter
enumerate separately) into the same "btn:N" / "hat:N:dir" code space the
pygame backend used, so the Controls rebind UI works unchanged.
"""

from __future__ import annotations
import ctypes
import threading
import time
from ctypes import wintypes
from fh6_spotify import mediakeys
from fh6_spotify.input_backend import (
    effective_bindings,
    evaluate_binds,
    open_gesture,
    vol_delta,
)

_k32 = ctypes.windll.kernel32
_hid = ctypes.windll.hid
_GENERIC_READ = 2147483648
_FILE_SHARE_READ = 1
_FILE_SHARE_WRITE = 2
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 1073741824
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0
_HIDP_INPUT = 0
_HIDP_STATUS_SUCCESS = 1114112
_USAGE_PAGE_GENERIC = 1
_USAGE_PAGE_BUTTON = 9
_USAGE_HATSWITCH = 57
_GAME_USAGES = (4, 5, 8)


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


class _CAPS_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", ctypes.c_ushort),
        ("UsageMax", ctypes.c_ushort),
        ("StringMin", ctypes.c_ushort),
        ("StringMax", ctypes.c_ushort),
        ("DesignatorMin", ctypes.c_ushort),
        ("DesignatorMax", ctypes.c_ushort),
        ("DataIndexMin", ctypes.c_ushort),
        ("DataIndexMax", ctypes.c_ushort),
    ]


class _HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", ctypes.c_ushort),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", ctypes.c_ushort),
        ("LinkCollection", ctypes.c_ushort),
        ("LinkUsage", ctypes.c_ushort),
        ("LinkUsagePage", ctypes.c_ushort),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", ctypes.c_ushort),
        ("ReportCount", ctypes.c_ushort),
        ("Reserved2", ctypes.c_ushort * 5),
        ("UnitsExp", ctypes.c_ulong),
        ("Units", ctypes.c_ulong),
        ("LogicalMin", ctypes.c_long),
        ("LogicalMax", ctypes.c_long),
        ("PhysicalMin", ctypes.c_long),
        ("PhysicalMax", ctypes.c_long),
        ("Range", _CAPS_RANGE),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", ctypes.c_void_p),
    ]


_HAT_XY = {
    0: (0, 1),
    1: (1, 1),
    2: (1, 0),
    3: (1, -1),
    4: (0, -1),
    5: (-1, -1),
    6: (-1, 0),
    7: (-1, 1),
}


class _HidDevice:
    """One opened HID game device: shared read-only handle + preparsed data +
    a reader thread that keeps per-report-ID button/hat state fresh."""

    def __init__(self, path: bytes, name: str):
        self.name = name or "wheel"
        self.alive = False
        self.pressed = {}
        self.hats = {}
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._h = _k32.CreateFileW(
            path.decode("utf-8", "ignore"),
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED,
            None,
        )
        if self._h == _INVALID_HANDLE or self._h is None:
            raise OSError("CreateFile failed")
        self._pp = ctypes.c_void_p()
        if not _hid.HidD_GetPreparsedData(self._h, ctypes.byref(self._pp)):
            _k32.CloseHandle(self._h)
            raise OSError("HidD_GetPreparsedData failed")
        caps = _HIDP_CAPS()
        if _hid.HidP_GetCaps(self._pp, ctypes.byref(caps)) != _HIDP_STATUS_SUCCESS:
            self._free()
            raise OSError("HidP_GetCaps failed")
        self._report_len = int(caps.InputReportByteLength)
        if self._report_len <= 0:
            self._free()
            raise OSError("no input reports")
        self.button_space = (
            int(_hid.HidP_MaxUsageListLength(_HIDP_INPUT, _USAGE_PAGE_BUTTON, self._pp))
            or 0
        )
        self._hat_caps = []
        self._logged_events = set()
        n = ctypes.c_ushort(caps.NumberInputValueCaps)
        if n.value:
            arr = (_HIDP_VALUE_CAPS * n.value)()
            if (
                _hid.HidP_GetValueCaps(_HIDP_INPUT, arr, ctypes.byref(n), self._pp)
                == _HIDP_STATUS_SUCCESS
            ):
                for i in range(n.value):
                    vc = arr[i]
                    usage = vc.Range.UsageMin if vc.IsRange else vc.Range.UsageMin
                    if (
                        vc.UsagePage == _USAGE_PAGE_GENERIC
                        and usage == _USAGE_HATSWITCH
                    ):
                        self._hat_caps.append((int(vc.ReportID), int(vc.LogicalMin)))
        self.hat_count = len(self._hat_caps)
        self.alive = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"wheel-hid:{self.name}"
        )
        self._thread.start()

    def _free(self):
        if getattr(self, "_pp", None):
            try:
                _hid.HidD_FreePreparsedData(self._pp)
            except Exception:
                pass
            self._pp = None
        if getattr(self, "_h", None) not in (None, _INVALID_HANDLE):
            try:
                _k32.CloseHandle(self._h)
            except Exception:
                pass
            self._h = None

    def _run(self):
        buf = (ctypes.c_ubyte * self._report_len)()
        ev = _k32.CreateEventW(None, True, False, None)
        ov = _OVERLAPPED()
        try:
            while not self._stop:
                _k32.ResetEvent(ev)
                ctypes.memset(ctypes.byref(ov), 0, ctypes.sizeof(ov))
                ov.hEvent = ev
                ok = _k32.ReadFile(
                    self._h, buf, self._report_len, None, ctypes.byref(ov)
                )
                if not ok and _k32.GetLastError() != _ERROR_IO_PENDING:
                    break
                while not self._stop:
                    if _k32.WaitForSingleObject(ev, 250) == _WAIT_OBJECT_0:
                        break
                if self._stop:
                    _k32.CancelIoEx(self._h, None)
                    break
                got = wintypes.DWORD(0)
                if not _k32.GetOverlappedResult(
                    self._h, ctypes.byref(ov), ctypes.byref(got), False
                ):
                    break
                if got.value:
                    self._parse(buf)
        finally:
            try:
                _k32.CloseHandle(ev)
            except Exception:
                pass
            self.alive = False

    def _parse(self, buf):
        report_id = int(buf[0])
        max_u = self.button_space
        pressed = set()
        if max_u:
            usages = (ctypes.c_ushort * max_u)()
            ulen = ctypes.c_ulong(max_u)
            st = _hid.HidP_GetUsages(
                _HIDP_INPUT,
                _USAGE_PAGE_BUTTON,
                0,
                usages,
                ctypes.byref(ulen),
                self._pp,
                buf,
                self._report_len,
            )
            if st == _HIDP_STATUS_SUCCESS:
                pressed = {int(usages[i]) for i in range(ulen.value)}
        hats = {}
        for slot, (rid, lmin) in enumerate(self._hat_caps):
            if rid != report_id:
                continue
            val = ctypes.c_ulong(0)
            st = _hid.HidP_GetUsageValue(
                _HIDP_INPUT,
                _USAGE_PAGE_GENERIC,
                0,
                _USAGE_HATSWITCH,
                ctypes.byref(val),
                self._pp,
                buf,
                self._report_len,
            )
            if st == _HIDP_STATUS_SUCCESS:
                hats[slot] = _HAT_XY.get(int(val.value) - lmin, (0, 0))
        with self._lock:
            self.pressed[report_id] = pressed
            for slot, xy in hats.items():
                self.hats[slot] = xy
        if pressed or any(xy != (0, 0) for xy in hats.values()):
            key = (report_id, tuple(sorted(pressed)), tuple(sorted(hats.items())))
            if key not in self._logged_events:
                self._logged_events.add(key)
                if len(self._logged_events) <= 200:
                    try:
                        import os as _os

                        base = _os.path.join(_os.environ.get("APPDATA", "."), "Segue")
                        _os.makedirs(base, exist_ok=True)
                        with open(_os.path.join(base, "wheel_reports.log"), "a") as f:
                            f.write(
                                "rid=%d pressed=%s hats=%s\n"
                                % (report_id, sorted(pressed), dict(hats))
                            )
                    except Exception:
                        pass

    def snapshot(self):
        """(buttons_bool_list, hats_xy_list) sized to this device's spaces."""
        with self._lock:
            down = set()
            for s in self.pressed.values():
                down |= s
            hats = [self.hats.get(i, (0, 0)) for i in range(self.hat_count)]
        buttons = [u + 1 in down for u in range(self.button_space)]
        return (buttons, hats)

    def close(self):
        self._stop = True
        try:
            if self._h not in (None, _INVALID_HANDLE):
                _k32.CancelIoEx(self._h, None)
        except Exception:
            pass
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._free()


def _enumerate_game_devices():
    """All HID top-level collections that look like game devices, as
    (path, name) sorted by path for a stable btn:N index space."""
    try:
        import pydualsense
    except Exception:
        pass
    import hidapi

    out = []
    for d in hidapi.enumerate():
        if d.usage_page == _USAGE_PAGE_GENERIC and d.usage in _GAME_USAGES:
            name = d.product_string or ""
            if isinstance(name, bytes):
                name = name.decode("utf-8", "ignore")
            out.append((d.path, name))
    out.sort(key=lambda t: t[0])
    return out


class WheelHidInput:
    """Drop-in replacement for ActionGamepad for input_device == "wheel".
    Same poll/read_pressed/safe_mode/close surface; bindings stay in the
    btn:N / hat:N:dir code space (flattened across devices)."""

    def __init__(
        self,
        config,
        on_next=mediakeys.media_next,
        on_prev=mediakeys.media_prev,
        on_pause=mediakeys.media_playpause,
        on_open=None,
    ):
        self.c = config
        self._on = {"next": on_next, "prev": on_prev, "pause": on_pause}
        self._on_open = on_open or (lambda: None)
        self._open_down_t = None
        self._open_fired = False
        self._safe_mode = config.safe_mode_default
        self._prev_active = set()
        self._code_down_since = {}
        self._code_consumed = set()
        self.comms_latch_edge = False
        self._suppressed = False
        self._rebind_baseline = None
        self._devices = []
        for path, name in _enumerate_game_devices():
            try:
                self._devices.append(_HidDevice(path, name))
            except OSError:
                pass
        if not self._devices:
            raise RuntimeError("no controller detected")

    @property
    def available(self) -> bool:
        return any(d.alive for d in self._devices)

    @property
    def device_name(self) -> str:
        try:
            d = max(self._devices, key=lambda d: d.button_space)
            return d.name or "Wheel"
        except Exception:
            return "Wheel"

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    def set_safe_mode(self, value: bool) -> None:
        self._safe_mode = bool(value)

    def _agg(self):
        """Flatten buttons + hats across all devices (stable order)."""
        buttons, hats = ([], [])
        for d in self._devices:
            b, h = d.snapshot()
            buttons.extend(b)
            hats.extend(h)
        return (buttons, hats)

    def poll(self, now: float, can_skip: bool) -> float:
        if not self.available:
            raise RuntimeError("wheel disconnected")
        buttons, hats = self._agg()
        binds = effective_bindings(
            self.c.input_device, self.c.bindings, getattr(self.c, "mode", None)
        )
        holds = set(getattr(self.c, "hold_actions", ()) or ())
        hold_ms = getattr(self.c, "bind_hold_ms", 300) / 1000.0
        active, pressed = evaluate_binds(
            binds,
            holds,
            buttons,
            hats,
            now,
            self._code_down_since,
            self._prev_active,
            hold_ms,
            self._code_consumed,
        )
        self._prev_active = active
        if self._suppressed:
            if self._rebind_baseline is None:
                self._rebind_baseline = self._down_codes()
            self._open_down_t = None
            self._open_fired = False
            return 0.0
        self._rebind_baseline = None
        delta = 0.0
        open_gesture(self, active, pressed, now)
        if "safe_mode" in pressed:
            self._safe_mode = not self._safe_mode
        if "menu_latch" in pressed:
            self.comms_latch_edge = True
        if "pause" in pressed:
            self._on["pause"]()
        vdelta = vol_delta(
            active,
            pressed,
            now,
            self.__dict__.setdefault("_vol_state", {}),
            self.c.vol_step,
            getattr(self.c, "vol_hold_sensitivity", 1.0),
        )
        if can_skip:
            delta += vdelta
        if can_skip and not self._safe_mode:
            if "next" in pressed:
                self._on["next"]()
            if "prev" in pressed:
                self._on["prev"]()
        return delta

    def _down_codes(self) -> set:
        """All binding codes currently held down, buttons and hats."""
        buttons, hats = self._agg()
        down = {f"btn:{i}" for i, d in enumerate(buttons) if d}
        for h, (hx, hy) in enumerate(hats):
            if hy == 1:
                down.add(f"hat:{h}:up")
            if hy == -1:
                down.add(f"hat:{h}:down")
            if hx == -1:
                down.add(f"hat:{h}:left")
            if hx == 1:
                down.add(f"hat:{h}:right")
        return down

    def read_pressed(self):
        """Binding code of a currently-pressed button/hat (rebind UI), or None.
        Inputs already held when the popup opened (the rebind baseline) are
        ignored until released once, so a stuck/always-on input can't hijack
        the capture - deliberately re-pressing it still binds."""
        if self._suppressed and self._rebind_baseline is None:
            self._rebind_baseline = self._down_codes()
            return
        down = self._down_codes()
        if self._rebind_baseline:
            self._rebind_baseline &= down
            down = down - self._rebind_baseline
        if not down:
            return
        btns = sorted(
            (c for c in down if c.startswith("btn:")),
            key=lambda c: int(c.split(":")[1]),
        )
        if btns:
            return btns[0]
        return sorted(down)[0]

    def close(self) -> None:
        for d in self._devices:
            try:
                d.close()
            except Exception:
                pass
        self._devices = []
