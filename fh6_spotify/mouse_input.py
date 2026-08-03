"""Mouse music control: hold a side button + scroll = volume (any app / game).

A low-level global mouse hook (WH_MOUSE_LL). While the chosen side button is held:
  - scroll        -> music volume up/down
  - left-click    -> previous track   (spatial: left = back)
  - right-click   -> next track       (spatial: right = forward)
  - middle-click  -> play/pause
The clicks/scroll are swallowed so the app under the cursor doesn't also react.
A plain click on the modifier (press+release, no scroll/action) is preserved: we
swallow it in real time, then re-synthesize a normal back/forward click on release
so browsing still works. Only a hold-and-do triggers music mode.

Pure local input remap (same category as the gamepad/keyboard backends) - never
touches the game. Additive: runs alongside whatever controller is selected.

Design notes:
  - The hook callback runs on a dedicated thread with its own message pump and must
    return FAST (Windows LowLevelHooksTimeout). So it only updates flags/counters;
    the runner's poll() applies the volume, fires the routed actions, and does the
    re-synth (SendInput off the hook thread).
  - Clean teardown: PostThreadMessage(WM_QUIT) + UnhookWindowsHookEx.
"""
from __future__ import annotations
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
_MDBG = bool(os.environ.get('SEGUE_MOUSE_DBG'))


def _mlog(m):
    if not _MDBG:
        return
    try:
        with open(os.path.join(os.path.dirname(__file__), '..', 'scripts', '.mouse_dbg.log'), 'a', encoding='utf-8') as f:
            f.write(m + '\n')
    except Exception:
        return None


_WH_MOUSE_LL = 14
_WM_QUIT = 18
_WM_MOUSEWHEEL = 522
_WM_LBUTTONDOWN, _WM_LBUTTONUP = (513, 514)
_WM_RBUTTONDOWN, _WM_RBUTTONUP = (516, 517)
_WM_MBUTTONDOWN, _WM_MBUTTONUP = (519, 520)
_WM_XBUTTONDOWN, _WM_XBUTTONUP = (523, 524)
_XBUTTON1, _XBUTTON2 = (1, 2)
_LLMHF_INJECTED = 1
_INPUT_MOUSE = 0
_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP = (128, 256)
_WH_KEYBOARD_LL = 13
_WM_KEYDOWN, _WM_KEYUP = (256, 257)
_WM_SYSKEYDOWN, _WM_SYSKEYUP = (260, 261)
_VK_SPACE = 32
_VK_CAPITAL = 20
_LLKHF_INJECTED = 16
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 2
_CAPS_TAP_MAX = 0.25
_ULONG_PTR = ctypes.c_size_t
_LRESULT = ctypes.c_ssize_t
_CLICK_ACTIONS = {_WM_LBUTTONDOWN: ('prev', _WM_LBUTTONUP), _WM_RBUTTONDOWN: ('next', _WM_RBUTTONUP)}
_REPEAT_ACTIONS = set()
_UP_TO_ACTION = {up: act for act, up in _CLICK_ACTIONS.values()}
_REPEAT_DELAY = 0.4
_REPEAT_INTERVAL = 0.5
_ACCEL_FAST_GAP = 0.06
_ACCEL_STREAK_MIN = 3
_ACCEL_STREAK_RAMP = 4
_ACCEL_MAX = 10.0
_INSTALL_DELAY = 5.0


class _POINT(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', _POINT), ('mouseData', wintypes.DWORD), ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', _ULONG_PTR)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', _ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', _ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', _ULONG_PTR)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [('mi', _MOUSEINPUT), ('ki', _KEYBDINPUT)]
    _anonymous_ = ('u',)
    _fields_ = [('type', wintypes.DWORD), ('u', _U)]


_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def _xbtn_for(name: str) -> int:
    return _XBUTTON2 if str(name).lower() == 'forward' else _XBUTTON1


class WindowsMouseInput:
    """Side-button + scroll music control. Same callback shape as the other input
    backends: poll(now, can_skip) -> volume delta; fires on_next/on_prev/on_pause."""

    def __init__(self, config, on_next, on_prev, on_pause, on_open=None):
        self._cfg = config
        self.on_next, self.on_prev, self.on_pause = (on_next, on_prev, on_pause)
        self.on_open = on_open
        self._mod = _xbtn_for(getattr(config, 'mouse_modifier', 'back'))
        self._actions = bool(getattr(config, 'mouse_music_actions', True))
        self._held = False
        self._used = False
        self._swallow_ups = set()
        self._held_btns = {}
        self._mid_down = False
        self._mid_used = False
        self._skip_seq = 0
        self._skip_dir = 0
        self._browse_seq = 0
        self._skip_net = 0
        self._space_down = False
        self._kb_summon = bool(getattr(config, 'keyboard_summon', False))
        self._caps_held = False
        self._synth_caps = False
        self._caps_down_t = 0.0
        self._last_wheel_t = 0.0
        self._fast_streak = 0
        self._vol = 0.0
        self._pending = []
        self._synth = 0
        self._lock = threading.Lock()
        self._hook = None
        self._kbd_hook = None
        self._tid = None
        self._stop = False
        self._u32 = ctypes.windll.user32
        self._u32.CallNextHookEx.restype = _LRESULT
        self._u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self._u32.SetWindowsHookExW.restype = wintypes.HHOOK
        self._u32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        self._u32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self._k32 = ctypes.windll.kernel32
        self._k32.GetModuleHandleW.restype = wintypes.HMODULE
        self._k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._k32.GetCurrentThreadId.restype = wintypes.DWORD
        self._u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._proc = _HOOKPROC(self._hook_proc)
        self._kbd = _HOOKPROC(self._kbd_proc)
        self._thread = threading.Thread(target=self._run, name='segue-mouse-hook', daemon=True)
        self._thread.start()

    def _run(self):
        if sys.platform != 'win32':
            return
        end = time.monotonic() + _INSTALL_DELAY
        while not self._stop and time.monotonic() < end:
            time.sleep(0.1)
        if self._stop:
            return
        try:
            self._tid = self._k32.GetCurrentThreadId()
            hmod = self._k32.GetModuleHandleW(None)
            self._hook = self._u32.SetWindowsHookExW(_WH_MOUSE_LL, self._proc, hmod, 0)
            self._kbd_hook = self._u32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._kbd, hmod, 0)
            _mlog('hook install: mouse={} kbd={} mod={:#x}'.format(bool(self._hook), bool(self._kbd_hook), self._mod))
            if not self._hook:
                print(f'  mouse hook: SetWindowsHookEx failed (err {self._k32.GetLastError()})')
                return
            msg = wintypes.MSG()
            while not self._stop and self._u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self._u32.TranslateMessage(ctypes.byref(msg))
                self._u32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for h in [self._hook, self._kbd_hook]:
                try:
                    if h:
                        self._u32.UnhookWindowsHookEx(h)
                except Exception:
                    pass
            self._hook = None
            self._kbd_hook = None

    def _hook_proc(self, nCode, wParam, lParam):
        if nCode == 0:
            try:
                if self._decide(wParam, ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT))[0]):
                    return 1
            except Exception as _e:
                _mlog('decide EXC {}'.format(_e))
        return self._u32.CallNextHookEx(None, nCode, wParam, lParam)

    def _kbd_proc(self, nCode, wParam, lParam):
        if nCode == 0:
            try:
                ks = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT))[0]
                inj = bool(ks.flags & _LLKHF_INJECTED)
                if self._kb_summon and ks.vkCode == _VK_CAPITAL and not inj:
                    if wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                        if not self._caps_held:
                            self._caps_held = True
                            self._caps_down_t = time.monotonic()
                            self._held, self._used = (True, False)
                            self._fast_streak = 0
                            self._mid_down = False
                            self._space_down = False
                            self._swallow_ups.clear()
                            with self._lock:
                                self._held_btns.clear()
                        return 1
                    if wParam in (_WM_KEYUP, _WM_SYSKEYUP):
                        if self._caps_held:
                            self._caps_held = False
                            self._held = False
                            with self._lock:
                                self._held_btns.clear()
                                if time.monotonic() - self._caps_down_t < _CAPS_TAP_MAX:
                                    if not self._used:
                                        self._synth_caps = True
                        return 1
                if self._held and ks.vkCode == _VK_SPACE and not inj:
                    if wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                        if not self._space_down:
                            self._space_down = True
                            with self._lock:
                                self._pending.append(('pause', 'click'))
                        return 1
                    if wParam in (_WM_KEYUP, _WM_SYSKEYUP):
                        self._space_down = False
                    return 1
            except Exception:
                pass
        return self._u32.CallNextHookEx(None, nCode, wParam, lParam)

    def _decide(self, msg, ms) -> bool:
        if msg in (_WM_XBUTTONDOWN, _WM_XBUTTONUP, _WM_MOUSEWHEEL, _WM_MBUTTONDOWN):
            _mlog('decide msg={:#x} held={} mid={}'.format(msg, self._held, self._mid_down))
        if ms.flags & _LLMHF_INJECTED:
            return False
        mod = self._mod
        if msg == _WM_XBUTTONDOWN:
            xb = ms.mouseData >> 16 & 65535
            if xb == mod:
                self._held, self._used = (True, False)
                self._fast_streak = 0
                self._mid_down = False
                self._space_down = False
                self._swallow_ups.clear()
                with self._lock:
                    self._held_btns.clear()
                return True
            return False
        if msg == _WM_XBUTTONUP:
            xb = ms.mouseData >> 16 & 65535
            if xb == mod and self._held:
                self._held = False
                self._mid_down = False
                with self._lock:
                    self._held_btns.clear()
                    if not self._used:
                        self._synth = mod
                return True
            return False
        if msg in self._swallow_ups:
            self._swallow_ups.discard(msg)
            if msg == _WM_MBUTTONUP:
                if not self._mid_used:
                    with self._lock:
                        self._pending.append(('pause', 'click'))
                self._mid_down = False
                return True
            act = _UP_TO_ACTION.get(msg)
            if act:
                with self._lock:
                    self._held_btns.pop(act, None)
            return True
        if not self._held:
            return False
        if msg == _WM_MBUTTONDOWN:
            self._mid_down = True
            self._mid_used = False
            self._swallow_ups.add(_WM_MBUTTONUP)
            return True
        if msg == _WM_MOUSEWHEEL:
            delta = ctypes.c_short(ms.mouseData >> 16 & 65535).value
            base = float(getattr(self._cfg, 'vol_step', 0.05) or 0.05)
            t = time.monotonic()
            dt = t - self._last_wheel_t
            self._last_wheel_t = t
            if delta > 0:
                self._fast_streak = 0
                step = base
            else:
                self._fast_streak = self._fast_streak + 1 if dt <= _ACCEL_FAST_GAP else 0
                over = max(0, self._fast_streak - _ACCEL_STREAK_MIN)
                mult = 1.0 + (_ACCEL_MAX - 1.0) * min(1.0, over / _ACCEL_STREAK_RAMP)
                step = base * mult
            with self._lock:
                self._vol += step if delta > 0 else -step
            self._used = True
            return True
        if self._actions and msg in _CLICK_ACTIONS:
            action, up = _CLICK_ACTIONS[msg]
            with self._lock:
                self._pending.append((action, 'click'))
                if action in _REPEAT_ACTIONS:
                    self._held_btns[action] = time.monotonic() + _REPEAT_DELAY
            self._used = True
            self._swallow_ups.add(up)
            return True
        return False

    @property
    def held(self) -> bool:
        """Is the music modifier currently held? (drives the overlay queue strip)"""
        return self._held

    def poll(self, now, can_skip):
        nowm = time.monotonic()
        repeats = []
        with self._lock:
            d, self._vol = (self._vol, 0.0)
            acts, self._pending = (self._pending, [])
            synth, self._synth = (self._synth, 0)
            synth_caps, self._synth_caps = (self._synth_caps, False)
            for a in list(self._held_btns):
                if nowm >= self._held_btns[a]:
                    repeats.append((a, 'browse'))
                    self._held_btns[a] = nowm + _REPEAT_INTERVAL
        if d or acts or repeats:
            _mlog('poll DRAIN d={:.3f} acts={} rep={}'.format(d, acts, repeats))
        for a, kind in acts + repeats:
            if a in ['next', 'prev']:
                step = 1 if a == 'next' else -1
                self._skip_dir = step
                self._skip_seq += 1
                self._skip_net += step
                if kind == 'browse':
                    self._browse_seq += 1
            try:
                cb = {'next': self.on_next, 'prev': self.on_prev, 'pause': self.on_pause}[a]
                if a in ['next', 'prev']:
                    try:
                        cb(kind)
                    except TypeError:
                        cb()
                else:
                    cb()
            except Exception:
                pass
        if synth:
            self._synth_xclick(synth)
        if synth_caps:
            self._synth_caps_key()
        return d

    def _synth_xclick(self, xb):
        try:
            n = 2
            arr = (_INPUT * n)()
            for i, fl in enumerate((_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP)):
                arr[i].type = _INPUT_MOUSE
                arr[i].mi = _MOUSEINPUT(0, 0, xb, fl, 0, 0)
            self._u32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))
        except Exception:
            return None

    def _synth_caps_key(self):
        try:
            arr = (_INPUT * 2)()
            for i, fl in enumerate((0, _KEYEVENTF_KEYUP)):
                arr[i].type = _INPUT_KEYBOARD
                arr[i].ki = _KEYBDINPUT(_VK_CAPITAL, 0, fl, 0, 0)
            self._u32.SendInput(2, ctypes.byref(arr), ctypes.sizeof(_INPUT))
        except Exception:
            return None

    def close(self):
        self._stop = True
        try:
            if self._tid:
                self._u32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
        except Exception:
            pass
        try:
            if self._hook:
                self._u32.UnhookWindowsHookEx(self._hook)
        except Exception:
            return None
