"""Universal controller input -> Spotify actions, via pygame (Xbox / PlayStation
/ sim wheels). DualSense touchpad gestures stay in gamepad.py; this maps plain
buttons + hat (POV) to bindable ACTIONS so any device works.

A binding code is a short string:
  "btn:5"        -> joystick button index 5
  "hat:0:up"     -> hat 0 pushed up   (dir = up/down/left/right)
Keyboard codes ("key:...") are handled by a separate backend (later stage).
"""

import os
from fh6_spotify import mediakeys

os.environ.setdefault("SDL_VIDEO_ALLOW_SCREENSAVER", "1")
os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "0")
ACTIONS = [
    "prev",
    "next",
    "vol_down",
    "vol_up",
    "pause",
    "safe_mode",
    "open",
    "menu_latch",
]
ACTION_LABELS = {
    "prev": "Previous track",
    "next": "Next track",
    "vol_down": "Volume down",
    "vol_up": "Volume up",
    "pause": "Pause / play",
    "safe_mode": "Lock Skip",
    "open": "Open Segue",
    "menu_latch": "Interact button",
}
_MOD_VKS = (16, 17, 18)


def parse_key_code(code: str):
    """Parse a keyboard bind code into (required_modifier_vks, main_vk).

    "key:65"        -> (frozenset(), 65)            bare key
    "key:17+65"     -> (frozenset({17}), 65)        Ctrl+A
    "key:17+16+80"  -> (frozenset({17,16}), 80)     Ctrl+Shift+P
    Returns (frozenset(), None) for anything unparseable."""
    if not code or not code.startswith("key:"):
        return (frozenset(), None)
    parts = code[4:].split("+")
    try:
        nums = [int(p) for p in parts if p != ""]
    except ValueError:
        return (frozenset(), None)
    if not nums:
        return (frozenset(), None)
    return (frozenset(nums[:-1]), nums[-1])


DEVICES = ["playstation", "xbox", "wheel", "keyboard"]
DEFAULTS = {
    "playstation": {},
    "xbox": {"prev": "hat:0:left", "next": "hat:0:right", "menu_latch": "btn:2"},
    "wheel": {"menu_latch": "btn:0"},
    "keyboard": {
        "prev": "key:219",
        "next": "key:221",
        "pause": "key:220",
        "vol_down": "key:189",
        "vol_up": "key:187",
        "safe_mode": "key:186",
        "menu_latch": "key:13",
    },
}
_HAT_DIRS = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}
KEYBOARD_GENERAL = {
    "prev": "key:36",
    "next": "key:35",
    "vol_up": "key:33",
    "vol_down": "key:34",
    "pause": "key:45",
    "safe_mode": "key:46",
}


def effective_bindings(device: str, overrides: dict, mode: str | None = None) -> dict:
    """Device defaults with user overrides layered on top. `mode` ("general" /
    "forza") only matters for keyboard: general-mode games get the nav-key
    cluster, Forza keeps the Forza-safe symbol cluster. Modifier combos
    (e.g. Ctrl+Shift+P) are supported for user rebinds but not the defaults."""
    if device == "keyboard" and mode == "general":
        out = dict(KEYBOARD_GENERAL)
    else:
        out = dict(DEFAULTS.get(device, {}))
    out.update(overrides or {})
    return out


def code_active(code: str, buttons, hats) -> bool:
    """Is this binding code currently pressed? `buttons` = list of bools,
    `hats` = list of (x, y) tuples. Pure (unit-tested).

    A "+" joins a COMBO (e.g. "btn:4+btn:2" = LB + Square): every part must
    be held. Order-independent."""
    if not code:
        return False
    if "+" in code:
        return all(code_active(part, buttons, hats) for part in code.split("+"))
    kind, _, rest = code.partition(":")
    if kind == "btn":
        try:
            return bool(buttons[int(rest)])
        except (ValueError, IndexError):
            return False
    if kind == "hat":
        hat_s, _, direction = rest.partition(":")
        try:
            hx, hy = hats[int(hat_s)]
        except (ValueError, IndexError):
            return False
        want = _HAT_DIRS.get(direction)
        if want is None:
            return False
        return (
            (hx, hy) == want
            or (want[0] and hx == want[0])
            or (want[1] and hy == want[1])
        )
    return False


def named_active(code: str, state) -> bool:
    """PlayStation combo matcher: True if EVERY '+'-joined named button in
    `code` is currently down on the DualSense/DS4 state object. Handles a single
    name ("share") and a combo ("L1+square", "DpadUp+triangle"). Pure given the
    state object; used by gamepad.py / dualshock.py for every user-bindable
    button so a captured combo actually fires in-game."""
    if not code:
        return False
    return all(bool(getattr(state, part, False)) for part in code.split("+"))


def suppress_subsets(active, binds):
    """Combo precedence: drop any active action whose bind code is a STRICT
    subset of another active action's code, so holding a combo (e.g.
    "vol_up"="btn:4+hat:0:right") wins over its component single bind (e.g.
    "next"="hat:0:right") instead of firing both. `active` = set of action
    names currently held; `binds` = {action: code}. Returns the filtered set.
    Pure (unit-tested)."""
    parts = {
        a: frozenset(p for p in (binds.get(a, "") or "").split("+")) for a in active
    }
    keep = set(active)
    for a in active:
        pa = parts[a]
        if not pa:
            continue
        if any(a != b and pa < parts[b] for b in active):
            keep.discard(a)
    return keep


def resolve_actions(binds, holds, held_codes, passed_codes, hold_codes):
    """Which actions count as ACTIVE this tick under tap/hold rules:
      - a HOLD-mode action is active only once its code is held past the hold
        threshold (`passed_codes`);
      - a TAP action whose code also has a HOLD twin (`hold_codes`) is NEVER
        sustained-active - it fires on release instead (handled by the caller);
      - a plain TAP action (no hold twin) is active while its code is held.
    Pure (unit-tested)."""
    out = set()
    for a, c in binds.items():
        if not c:
            continue
        if a in holds:
            if c in passed_codes:
                out.add(a)
        elif c in hold_codes:
            continue
        elif c in held_codes:
            out.add(a)
    return out


def evaluate_binds(
    binds, holds, buttons, hats, now, down_since, prev_active, hold_ms, consumed=None
):
    """Resolve a controller poll into (active, pressed) under tap/hold + combo
    rules. `down_since` is a {code: monotonic-time-first-held} dict and
    `consumed` is a set of codes swallowed by a combo - both kept by the caller
    across polls (mutated here). `prev_active` is last tick's active set.

      active  - actions sustained this tick (drives volume ramp / open gesture)
      pressed - actions newly triggered this tick (drives skip/pause/lock).

    Combo precedence (suppress_subsets) still applies, so L1+DpadRight wins over
    a lone DpadRight bind. A single code that is part of a CURRENTLY-ACTIVE combo
    is marked consumed, so releasing it does NOT also fire its own tap action."""
    if consumed is None:
        consumed = set()
    held_codes = {c for c in binds.values() if c and code_active(c, buttons, hats)}
    hold_codes = {binds[a] for a in holds if binds.get(a)}
    for c in held_codes:
        down_since.setdefault(c, now)
    tap_fired = set()
    for c in [c for c in list(down_since) if c not in held_codes]:
        dur = now - down_since.pop(c)
        was_consumed = c in consumed
        consumed.discard(c)
        if was_consumed or dur >= hold_ms or c not in hold_codes:
            continue
        for a, ac in binds.items():
            if ac == c and a not in holds:
                tap_fired.add(a)
    passed = {c for c in held_codes if now - down_since.get(c, now) >= hold_ms}
    active = suppress_subsets(
        resolve_actions(binds, holds, held_codes, passed, hold_codes), binds
    )
    active_parts = [
        frozenset(p for p in binds[a].split("+")) for a in active if binds.get(a)
    ]
    for c in held_codes:
        cp = frozenset(p for p in c.split("+") if p)
        if any(cp < kp for kp in active_parts):
            consumed.add(c)
    pressed = active - prev_active | tap_fired
    return (active, pressed)


_VOL_REPEAT_DELAY = 0.28
_VOL_REPEAT_RATE = 0.05


def open_trigger_for(cfg) -> str:
    """Resolved Open Segue trigger: the user's explicit choice, else the
    device default - press on keyboard (a bound combo can't misfire), hold
    on controllers (a stray button press shouldn't yank a window up)."""
    t = getattr(cfg, "open_trigger", "")
    if t in ["hold", "press"]:
        return t
    return "press" if getattr(cfg, "input_device", "") == "keyboard" else "hold"


def open_gesture(backend, active, pressed, now):
    """Shared "Open Segue" firing for the generic backends. `backend` carries
    `_on_open`, `_open_down_t`, `_open_fired` and config `c`. Trigger comes
    from open_trigger_for: "press" fires on the press edge, "hold" fires once
    after cfg.open_hold_ms (summon-toggle, mirrors the DualSense gesture)."""
    if "open" not in active:
        backend._open_down_t = None
        backend._open_fired = False
        return
    if open_trigger_for(backend.c) == "press":
        if "open" in pressed:
            try:
                backend._on_open()
            except Exception:
                return
            return
    else:
        if backend._open_down_t is None:
            backend._open_down_t = now
            backend._open_fired = False
        elif not backend._open_fired:
            if (now - backend._open_down_t) * 1000.0 >= getattr(
                backend.c, "open_hold_ms", 1200
            ):
                backend._open_fired = True
                try:
                    backend._on_open()
                except Exception:
                    return None


def vol_delta(active, pressed, now, state, step, hold_sens=1.0):
    """Return the volume delta for this poll. `active` = actions held now,
    `pressed` = actions newly pressed this poll, `state` = a per-backend dict
    (carried across polls). One step on the initial press, then steady repeats
    after _VOL_REPEAT_DELAY while held.

    `hold_sens` scales ONLY the sustained auto-repeat step (the held sweep
    speed), not the initial press."""
    d = 0.0
    for act, sign in [("vol_up", 1.0), ("vol_down", -1.0)]:
        if act in pressed:
            d += sign * step
            state[act] = now + _VOL_REPEAT_DELAY
        elif act in active:
            if now >= state.get(act, float("inf")):
                d += sign * step * hold_sens
                state[act] = now + _VOL_REPEAT_RATE
        else:
            state.pop(act, None)
    return d


class ActionGamepad:
    """Reads ALL pygame joysticks and fires bound actions. Mimics the
    DualSenseInput interface (poll/safe_mode/close) so the runner can swap it in.

    Sim wheels (e.g. Fanatec DD1 + Formula rim) enumerate as SEVERAL devices -
    wheel base, pedals, shifter - and the base often isn't joystick 0. So we
    flatten buttons + hats across every connected joystick into one index space:
    btn:0.. spans all devices in order, giving each physical button a stable
    unique index for both binding and runtime."""

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
        self._suppressed = False
        self._last_dev_chk = 0.0
        self.comms_latch_edge = False
        self.latch_uses_hold = True
        import pygame

        self._pg = pygame
        if not pygame.get_init():
            pygame.init()
        try:
            pygame.display.set_allow_screensaver(True)
        except Exception:
            pass
        pygame.joystick.init()
        n = pygame.joystick.get_count()
        if n == 0:
            raise RuntimeError("no controller detected")
        self._joys = []
        for i in range(n):
            try:
                j = pygame.joystick.Joystick(i)
                j.init()
                self._joys.append(j)
            except Exception:
                pass
        if not self._joys:
            raise RuntimeError("no controller detected")

    def _agg(self):
        """Flatten buttons + hats across all devices into single index spaces."""
        buttons, hats = ([], [])
        for j in self._joys:
            try:
                for i in range(j.get_numbuttons()):
                    buttons.append(bool(j.get_button(i)))
                for h in range(j.get_numhats()):
                    hats.append(j.get_hat(h))
            except Exception:
                pass
        return (buttons, hats)

    def _refresh_devices(self, now: float) -> None:
        """Hotplug: re-enumerate joysticks when the device count changes, so a
        controller plugged in (or reconnected) AFTER startup is picked up.
        Throttled to ~1 Hz."""
        if now - self._last_dev_chk < 1.0:
            return
        self._last_dev_chk = now
        try:
            cnt = self._pg.joystick.get_count()
        except Exception:
            return
        if cnt == len(self._joys):
            return
        joys = []
        for i in range(cnt):
            try:
                j = self._pg.joystick.Joystick(i)
                j.init()
                joys.append(j)
            except Exception:
                pass
        if joys:
            self._joys = joys
            self._prev_active = set()

    @property
    def available(self) -> bool:
        return bool(self._joys)

    @property
    def device_name(self) -> str:
        try:
            j = max(self._joys, key=lambda d: d.get_numbuttons())
            return j.get_name()
        except Exception:
            return "controller"

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    def set_safe_mode(self, value: bool) -> None:
        self._safe_mode = bool(value)

    def _bindings(self) -> dict:
        return effective_bindings(
            self.c.input_device, self.c.bindings, getattr(self.c, "mode", None)
        )

    def poll(self, now: float, can_skip: bool) -> float:
        self._pg.event.pump()
        self._refresh_devices(now)
        buttons, hats = self._agg()
        binds = self._bindings()
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
            self._open_down_t = None
            self._open_fired = False
            return 0.0
        delta = 0.0
        open_gesture(self, active, pressed, now)
        if "safe_mode" in pressed:
            self._safe_mode = not self._safe_mode
        if "pause" in pressed:
            if can_skip or getattr(self.c, "input_device", "") != "xbox":
                self._on["pause"]()
        if "menu_latch" in pressed:
            self.comms_latch_edge = True
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

    def read_pressed(self):
        """Binding code of the currently-held button(s)/hat(s) for rebinding, or
        None. ALL held inputs are joined with "+" so holding e.g. LB + Square
        captures a combo ("btn:4+btn:2"); a single press is just "btn:4"."""
        buttons, hats = self._agg()
        parts = []
        for i, pressed in enumerate(buttons):
            if pressed:
                parts.append(f"btn:{i}")
        for h, (hx, hy) in enumerate(hats):
            if hy == 1:
                parts.append(f"hat:{h}:up")
            elif hy == -1:
                parts.append(f"hat:{h}:down")
            elif hx == -1:
                parts.append(f"hat:{h}:left")
            elif hx == 1:
                parts.append(f"hat:{h}:right")
        return "+".join(parts) if parts else None

    def close(self) -> None:
        for j in getattr(self, "_joys", []):
            try:
                j.quit()
            except Exception:
                pass
        self._joys = []
        try:
            self._pg.joystick.quit()
        except Exception:
            return None


class KeyboardInput:
    """Global keyboard backend via Win32 GetAsyncKeyState (works regardless of
    focus, no extra deps). Same interface as the gamepad backends."""

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
        self.comms_latch_edge = False
        self._suppressed = False
        import ctypes

        self._u = ctypes.windll.user32

    @property
    def available(self) -> bool:
        return True

    @property
    def device_name(self) -> str:
        return "Keyboard"

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    def set_safe_mode(self, value: bool) -> None:
        self._safe_mode = bool(value)

    def _down(self, vk: int) -> bool:
        return bool(self._u.GetAsyncKeyState(vk) & 32768)

    def poll(self, now: float, can_skip: bool) -> float:
        binds = effective_bindings(
            "keyboard", self.c.bindings, getattr(self.c, "mode", None)
        )
        mods_down = frozenset(vk for vk in _MOD_VKS if self._down(vk))
        active = set()
        for action, code in binds.items():
            req_mods, main_vk = parse_key_code(code)
            if main_vk is None or not self._down(main_vk):
                continue
            if req_mods:
                if mods_down == req_mods:
                    active.add(action)
            elif not mods_down:
                active.add(action)
        pressed = active - self._prev_active
        self._prev_active = active
        if self._suppressed:
            self._open_down_t = None
            self._open_fired = False
            return 0.0
        delta = 0.0
        open_gesture(self, active, pressed, now)
        if "safe_mode" in pressed:
            self._safe_mode = not self._safe_mode
        if "menu_latch" in pressed:
            self.comms_latch_edge = True
        if "pause" in pressed:
            self._on["pause"]()
        delta += vol_delta(
            active,
            pressed,
            now,
            self.__dict__.setdefault("_vol_state", {}),
            self.c.vol_step,
            getattr(self.c, "vol_hold_sensitivity", 1.0),
        )
        if not self._safe_mode:
            if "next" in pressed:
                self._on["next"]()
            if "prev" in pressed:
                self._on["prev"]()
        return delta

    def read_pressed(self):
        """Code of any currently-pressed key (for rebinding)."""
        for vk in range(8, 255):
            if vk in [1, 2, 4]:
                continue
            if self._down(vk):
                return f"key:{vk}"
        return

    def close(self) -> None:
        return


def capture_input(timeout_s: float = 6.0) -> str | None:
    """Open the first pygame joystick, return the binding code of the next
    button/hat pressed, or None on timeout."""
    import time
    import pygame

    if not pygame.get_init():
        pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        return
    js = pygame.joystick.Joystick(0)
    js.init()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            pygame.event.pump()
            for i in range(js.get_numbuttons()):
                if js.get_button(i):
                    return f"btn:{i}"
            for h in range(js.get_numhats()):
                hx, hy = js.get_hat(h)
                if hy == 1:
                    return f"hat:{h}:up"
                elif hy == -1:
                    return f"hat:{h}:down"
                elif hx == -1:
                    return f"hat:{h}:left"
                elif hx == 1:
                    return f"hat:{h}:right"
            time.sleep(0.02)
        return
    finally:
        try:
            js.quit()
        except Exception:
            pass
