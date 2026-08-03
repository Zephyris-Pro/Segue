import os
import signal
import threading
import time
import psutil
from fh6_spotify.config import Config
from fh6_spotify.telemetry import TelemetryListener
from fh6_spotify.state import StateMachine
from fh6_spotify.spotify_volume import SpotifyVolume
from fh6_spotify import mediakeys as _mk
from fh6_spotify import connect as _connect


def _carousel_dbg(line):
    try:
        import os as _os, time as _time
        with open(_os.path.join(_os.path.dirname(__file__), '..', 'scripts', '.carousel_dbg.log'), 'a', encoding='utf-8') as _f:
            _f.write('{:.3f} {}\n'.format(_time.time(), line))
    except Exception:
        return None


_TLOG_ON = bool(os.environ.get('SEGUE_TELEMETRY_DEBUG'))
_tlog_fh = None


def _tlog(now: float, telemetry, menu_latch: bool) -> None:
    global _tlog_fh
    if not _TLOG_ON:
        return
    try:
        if _tlog_fh is None:
            base = os.environ.get('APPDATA') or os.path.expanduser('~')
            d = os.path.join(base, 'Segue')
            os.makedirs(d, exist_ok=True)
            _tlog_fh = open(os.path.join(d, 'telemetry_debug.csv'), 'w', encoding='utf-8')
            _tlog_fh.write('wall,t,is_race_on,live,speed,accel,pos_x,pos_y,pos_z,menu_latch\n')
        p = telemetry.position or (None, None, None)
        _tlog_fh.write(f"{time.strftime('%H:%M:%S')},{now:.3f},{telemetry.is_race_on},{telemetry.live(now)},{telemetry.speed},{telemetry.accel},{p[0]},{p[1]},{p[2]},{menu_latch}\n")
        _tlog_fh.flush()
    except Exception:
        return None


_clog_fh = None
_clog_last = [0.0]


def _clog(now: float, idle_s: float, wanted: bool, has_ctrl: bool) -> None:
    """Controller back-off trace (screensaver diagnostics): once a second, log
    the idle seconds, whether controller polling is wanted, and whether the
    controller is currently open. Lets us confirm the idle release actually
    fires. Same SEGUE_TELEMETRY_DEBUG gate; off by default."""
    global _clog_fh
    if not _TLOG_ON or now - _clog_last[0] < 1.0:
        return None
    _clog_last[0] = now
    try:
        if _clog_fh is None:
            base = os.environ.get('APPDATA') or os.path.expanduser('~')
            d = os.path.join(base, 'Segue')
            os.makedirs(d, exist_ok=True)
            _clog_fh = open(os.path.join(d, 'ctrl_debug.csv'), 'w', encoding='utf-8')
            _clog_fh.write('wall,idle_s,ctrl_wanted,has_controller\n')
        _clog_fh.write(f"{time.strftime('%H:%M:%S')},{idle_s:.1f},{wanted},{has_ctrl}\n")
        _clog_fh.flush()
    except Exception:
        return None


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _system_idle_seconds() -> float:
    """Seconds since the last REAL user input (keyboard / mouse), via
    GetLastInputInfo. Controller (XInput) polling does NOT update this, so it's
    a clean 'is the user actually away' signal - used to pause controller
    polling when idle so XInput stops resetting the screensaver/sleep timer.
    0.0 on any failure (treat as active = keep polling)."""
    import ctypes

    class _LII(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]
    try:
        lii = _LII()
        lii.cbSize = ctypes.sizeof(_LII)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (tick - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0


def _foreground_exe_name() -> str:
    """Return the lowercase exe name of whichever window owns the foreground
    on Windows. Empty string on any failure. Uses pure Win32 (user32 +
    psutil) - no COM, safe to call from the radio loop at 10-30 Hz."""
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ''
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ''
        try:
            return (psutil.Process(pid.value).name() or '').lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return ''
    except Exception:
        return ''


def _warm_native_imports() -> None:
    """Import the heavy native extension modules ONCE on the main thread before
    any worker thread starts.

    In a PyInstaller-frozen build the import machinery is not reliably
    thread-safe: when several worker threads each FIRST-import a big native
    module at the same moment (winsdk on the media thread, onnxruntime on the
    speech thread, hidapi via pydualsense on the controller thread, comtypes/
    pycaw on the radio-loop thread), the concurrent imports occasionally corrupt
    the heap and the process dies with an access violation (0xC0000409) right
    after the window appears. It reproduced ~1 in 4 launches of the frozen exe;
    running from source never hit it (different import loader).

    Fix: do all the dangerous first-imports here, serially, on one thread. Once
    each module sits in sys.modules the worker threads just reuse it - no second
    import, no race. Every import is best-effort: a missing/broken optional dep
    must not stop startup (the real call sites already degrade gracefully)."""
    for _imp in (lambda: __import__('comtypes'), lambda: __import__('pycaw.pycaw', fromlist=['AudioUtilities']), lambda: __import__('winsdk.windows.media.control', fromlist=['GlobalSystemMediaTransportControlsSessionManager']), lambda: __import__('winsdk.windows.media', fromlist=['MediaPlaybackAutoRepeatMode']), lambda: __import__('onnxruntime'), lambda: __import__('pydualsense'), lambda: __import__('pygame')):
        try:
            _imp()
        except Exception:
            pass


def start_runtime(c: Config, ui: dict):
    """Build + start the runtime sharing config `c` and state dict `ui`.

    Creates the overlay widget (a QApplication must already exist on the main
    thread), starts the radio-loop thread and speech worker. Returns a `stop()`
    callable that tears everything down and restores the Spotify volume. Does
    NOT create a QApplication or call exec.

    Must be called on the GUI/main thread (it creates the overlay widget +
    MediaWatcher). The radio loop and speech worker run on their own threads.
    """
    from fh6_spotify.config import default_config_path
    cfg_path = default_config_path()
    _warm_native_imports()
    volume = SpotifyVolume(c)
    user_volume = c.full_level
    telemetry = TelemetryListener(host=getattr(c, 'telemetry_host', '0.0.0.0'), port=c.port, forward=getattr(c, 'telemetry_forward', ''))
    state = StateMachine(c)
    speech_state = {'worker': None, 'mode': None}

    def _start_speech_worker():
        if speech_state['worker'] is not None:
            return
        try:
            from fh6_spotify.speech_worker import SpeechWorker
            w = SpeechWorker(c)
            w.start()
            speech_state['worker'] = w
            speech_state['mode'] = (bool(c.low_cpu_mode), getattr(c, 'duck_scope', 'game'))
        except Exception as exc:
            print(f'  ducking disabled: {exc}')

    def _stop_speech_worker():
        w = speech_state['worker']
        if w is None:
            return
        try:
            w.stop()
        except Exception:
            pass
        speech_state['worker'] = None
        speech_state['mode'] = None
    if c.ducking_enabled:
        _start_speech_worker()

    def _routed_playpause():
        m = ui.get('media')
        if m is None:
            _mk.media_playpause()
        elif not m.playpause() and not m.has_any_session():
            _mk.media_playpause()

    def _routed_next():
        m = ui.get('media')
        if m is None:
            _mk.media_next()
        elif not m.next() and not m.has_any_session():
            _mk.media_next()

    def _routed_prev():
        m = ui.get('media')
        if m is None:
            _mk.media_prev()
        elif not m.prev() and not m.has_any_session():
            _mk.media_prev()

    def _on_open_window():
        ui['summon'] = ui.get('summon', 0) + 1
    _hotkey_prev = [False]
    controller = None
    last_ctrl_try = 0.0
    CTRL_RETRY_S = 2.0

    def _try_init_controller():
        dev = c.input_device
        try:
            if dev in ['playstation', 'dualsense']:
                from fh6_spotify.gamepad import DualSenseInput
                return DualSenseInput(c, on_next=_routed_next, on_prev=_routed_prev, on_tap=_routed_playpause, on_open=_on_open_window)
        except Exception:
            if dev == 'dualsense':
                return None
        try:
            if dev in ['playstation', 'dualshock']:
                from fh6_spotify.dualshock import DualShockInput
                return DualShockInput(c, on_next=_routed_next, on_prev=_routed_prev, on_tap=_routed_playpause, on_open=_on_open_window)
            if dev == 'wheel' and getattr(c, 'wheel_backend', 'hid') != 'pygame':
                from fh6_spotify.wheel_hid import WheelHidInput
                return WheelHidInput(c, on_next=_routed_next, on_prev=_routed_prev, on_pause=_routed_playpause, on_open=_on_open_window)
            if dev in ('xbox', 'wheel'):
                from fh6_spotify.input_backend import ActionGamepad
                return ActionGamepad(c, on_next=_routed_next, on_prev=_routed_prev, on_pause=_routed_playpause, on_open=_on_open_window)
            if dev == 'keyboard':
                from fh6_spotify.input_backend import KeyboardInput
                return KeyboardInput(c, on_next=_routed_next, on_prev=_routed_prev, on_pause=_routed_playpause, on_open=_on_open_window)
            return None
        except Exception:
            return None
    controller = _try_init_controller()
    ui['routed_playpause'] = _routed_playpause
    ui['routed_next'] = _routed_next
    ui['routed_prev'] = _routed_prev
    ui['controller'] = controller

    def _mouse_browse(step: int, kind: str='click'):
        """click -> media key (instant, no roundtrip). browse (scroll-or-hold
        auto-repeat) -> accumulate into ui['ovl_skip'] which the runner drains
        through Connect after a short settle (same debounced single-jump path
        hover-scroll uses), so a 5-event burst becomes ONE Connect call instead
        of 5 stacked ones racing the cluster."""
        s = int(step)
        if kind != 'browse':
            if getattr(c, 'source', 'spotify') == 'spotify':
                if getattr(c, 'overlay_video', False) or getattr(c, 'connect_skip', False):
                    return None
            (_routed_next if s > 0 else _routed_prev)()
            return
        if s < 0:
            nq = ui.get('np_queue') or {}
            if not nq.get('prev') and not []:
                if os.environ.get('SEGUE_CAROUSEL_DBG'):
                    _carousel_dbg('MOUSE-BLOCK prev-empty (autoqueue)')
                return None
        ui['ovl_skip'] = int(ui.get('ovl_skip', 0) or 0) + s
        ui['_ovl_change_t'] = time.monotonic()
        if os.environ.get('SEGUE_CAROUSEL_DBG'):
            _carousel_dbg('MOUSE-BROWSE accumulate step={} total={}'.format(s, ui['ovl_skip']))

    def _make_mouse():
        if not getattr(c, 'mouse_control_enabled', False):
            if not getattr(c, 'keyboard_summon', False):
                return
        try:
            from fh6_spotify.mouse_input import WindowsMouseInput
            return WindowsMouseInput(c, on_next=lambda kind='click': _mouse_browse(1, kind), on_prev=lambda kind='click': _mouse_browse(-1, kind), on_pause=_routed_playpause)
        except Exception as exc:
            print(f'  mouse control init failed: {exc}')
            return None
    mouse_ctrl = _make_mouse()
    ui['mouse_ctrl'] = mouse_ctrl
    ui['mouse_held'] = False
    if getattr(c, 'overlay_video', False) or getattr(c, 'connect_skip', False):
        try:
            from fh6_spotify import queue as _queue
            _queue.start_queue_poller(ui, lambda: getattr(c, 'canvas_service_port', 7355))
        except Exception as exc:
            print(f'  queue poller init failed: {exc}')
    ui['volume'] = user_volume
    ui['muted'] = False
    ui['safe'] = False
    ui['disabled'] = False
    ui['overlay_enabled'] = c.overlay_enabled
    ui['app_cpu'] = 0.0
    ui['speech_cpu'] = 0.0
    eased_factor = 0.0
    _menu_latch = False
    _share_hold_until = 0.0
    _prev_speed = 0.0
    _speed_ok_since = None
    _cfg_mtime = None
    last_print = None
    last_dbg = 0.0
    _proc = psutil.Process()
    _proc.cpu_percent(None)
    _ncpu = psutil.cpu_count() or 1
    last_cpu = 0.0
    proc_state = {'last_check': 0.0, 'running': False}
    GAME_PROBE_S = 1.0
    _exe_set_cache = {}

    def _target_exe_set(general: bool) -> set:
        """Lowercase exe names the active preset answers to. General mode =
        the user-picked process; Forza mode = the configured exe PLUS the
        preset's aliases (FH6/FH5/FH4 share one preset - pick Forza once and
        whichever Horizon you launch matches)."""
        if general:
            t = (c.general_target_process or '').lower()
            return {t} if t else set()
        key = c.game_preset
        cached = _exe_set_cache.get(key)
        if cached is None:
            try:
                from fh6_spotify import game_presets as _gp
                cached = _gp.exes_for(key)
            except Exception:
                cached = set()
            _exe_set_cache[key] = cached
        t = (c.game_process_name or '').lower()
        return (cached | {t}) - {''}

    def _forza_running() -> bool:
        return proc_state['running']

    def step():
        nonlocal _prev_speed
        nonlocal mouse_ctrl
        nonlocal eased_factor
        nonlocal last_dbg
        nonlocal controller
        nonlocal user_volume
        nonlocal last_ctrl_try
        nonlocal _menu_latch
        nonlocal _cfg_mtime
        nonlocal _speed_ok_since
        nonlocal last_cpu
        nonlocal last_print
        nonlocal _share_hold_until
        now = time.monotonic()
        if 'volume_set' in ui:
            user_volume = _clamp(ui.pop('volume_set'))
        try:
            m = os.path.getmtime(cfg_path)
        except OSError:
            m = None
        if m is not None and m != _cfg_mtime:
            _cfg_mtime = m
            try:
                c.apply_from(Config.load(cfg_path))
            except Exception:
                pass
        general_mode = c.mode == 'general'
        if general_mode:
            is_race_on = True
        else:
            telemetry.poll()
            is_race_on = telemetry.is_race_on
            _saw_off = telemetry.saw_race_off
            telemetry.saw_race_off = False
            if telemetry.last_packet_time is not None:
                if now - telemetry.last_packet_time > c.telemetry_timeout_s:
                    is_race_on = None
            _pos = telemetry.position
            _pos_real = _pos is not None and not (_pos[0] == 0.0 and _pos[1] == 0.0 and _pos[2] == 0.0)
            _menu_ish = is_race_on is False or _saw_off
            if _menu_ish:
                _menu_latch = True
                _speed_ok_since = None
            elif is_race_on is True and telemetry.live(now):
                _spd = telemetry.speed or 0.0
                if _pos_real and _spd >= c.latch_release_speed:
                    if _speed_ok_since is None:
                        _speed_ok_since = now
                else:
                    _speed_ok_since = None
                if _menu_latch and now >= _share_hold_until and _pos_real:
                    if _speed_ok_since is not None:
                        if now - _speed_ok_since >= c.latch_sustain_s:
                            _menu_latch = False
                            _speed_ok_since = None
            else:
                _speed_ok_since = None
            _cur_speed = telemetry.speed or 0.0
            if not _menu_ish:
                if _prev_speed >= c.latch_snap_from_speed:
                    if _cur_speed < c.latch_release_speed:
                        _menu_latch = True
                        _speed_ok_since = None
            _prev_speed = _cur_speed
            _tlog(now, telemetry, _menu_latch)
        if now - proc_state['last_check'] >= GAME_PROBE_S:
            proc_state['last_check'] = now
            try:
                targets = _target_exe_set(general_mode)
                hit_exe = ''
                running = False
                running_names = []
                for p in psutil.process_iter(['name']):
                    nm = p.info.get('name')
                    if not nm:
                        continue
                    nm_l = nm.lower()
                    running_names.append(nm_l)
                    if nm_l in targets and not running:
                        running = True
                        proc_state['match'] = nm_l
                        try:
                            hit_exe = p.exe() or ''
                        except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
                            hit_exe = ''
                if not running:
                    proc_state['match'] = ''
                proc_state['running'] = running
                ui['game_exe_path'] = hit_exe
                w = speech_state.get('worker')
                if w is not None:
                    w.game_running = running
                _telemetry_veto = False
                if c.auto_detect_game and c.game_preset == 'forza':
                    try:
                        _telemetry_veto = telemetry.live(now)
                    except Exception:
                        _telemetry_veto = False
                if c.auto_detect_game and not _telemetry_veto:
                    try:
                        from fh6_spotify import game_presets as _gp
                        detected = _gp.detect_preset_from_running(running_names)
                        if not detected:
                            tgt = (c.general_target_process or '').lower()
                            if tgt and tgt in running_names and c.game_preset != 'other':
                                detected = 'other'
                        if detected and detected != c.game_preset:
                            _gp.apply_preset(c, detected)
                            try:
                                c.save(cfg_path)
                            except Exception:
                                pass
                            general_mode = c.mode == 'general'
                            ui['preset_auto_switched_to'] = detected
                    except Exception:
                        pass
            except Exception:
                pass
        if ui.pop('reinit_controller', False):
            if controller is not None:
                try:
                    controller.close()
                except Exception:
                    pass
            controller = None
            ui['controller'] = None
            last_ctrl_try = 0.0
        if ui.pop('reinit_mouse', False):
            if mouse_ctrl is not None:
                try:
                    mouse_ctrl.close()
                except Exception:
                    pass
            mouse_ctrl = _make_mouse()
            ui['mouse_ctrl'] = mouse_ctrl
        _idle_off = getattr(c, 'controller_idle_poll_off_s', 30.0)
        _idle_now = _system_idle_seconds()
        _ctrl_wanted = not ui.get('disabled') and (proc_state['running'] or _idle_off <= 0 or _idle_now < _idle_off)
        if not _ctrl_wanted and controller is not None:
            try:
                controller.close()
            except Exception:
                pass
            controller = None
            ui['controller'] = None
        _clog(now, _idle_now, _ctrl_wanted, controller is not None)
        if controller is None and _ctrl_wanted:
            if now - last_ctrl_try >= CTRL_RETRY_S:
                last_ctrl_try = now
                controller = _try_init_controller()
                if controller is not None:
                    ui['controller'] = controller
        can_skip = True
        if controller:
            if general_mode:
                can_skip = is_race_on is not False
            elif not proc_state['running']:
                can_skip = True
            elif telemetry.last_packet_time is None:
                can_skip = True
            else:
                can_skip = not _menu_latch
            try:
                if _ctrl_wanted and controller is not None:
                    user_volume = _clamp(user_volume + controller.poll(now, can_skip))
                if getattr(controller, 'comms_latch_edge', False):
                    controller.comms_latch_edge = False
                    if not general_mode:
                        _menu_latch = True
                        _share_hold_until = now + c.share_latch_hold_s if getattr(controller, 'latch_uses_hold', True) else now
            except Exception:
                try:
                    controller.close()
                except Exception:
                    pass
                controller = None
                ui['controller'] = None
        if mouse_ctrl is not None:
            try:
                user_volume = _clamp(user_volume + mouse_ctrl.poll(now, can_skip))
                ui['mouse_held'] = mouse_ctrl.held
                ui['mskip_seq'] = mouse_ctrl._skip_seq
                ui['mskip_dir'] = mouse_ctrl._skip_dir
                ui['mskip_net'] = mouse_ctrl._skip_net
                ui['mbrowse_seq'] = mouse_ctrl._browse_seq
            except Exception:
                pass
        ovl = ui.get('ovl_skip', 0)
        if ovl:
            now_t = time.monotonic()
            if now_t - ui.get('_ovl_change_t', 0.0) >= 0.08:
                if now_t - ui.get('_ovl_connect_t', 0.0) >= 0.45:
                    routed = False
                    if getattr(c, 'source', 'spotify') == 'spotify':
                        if getattr(c, 'overlay_video', False) or getattr(c, 'connect_skip', False):
                            port = getattr(c, 'canvas_service_port', 7355)
                            if ovl < 0:
                                ui['_connect_skip_t'] = time.monotonic()
                            if _connect.send_skip(port, ovl):
                                ui['ovl_skip'] = 0
                                ui['_ovl_connect_t'] = time.monotonic()
                                routed = True
                                if os.environ.get('SEGUE_CAROUSEL_DBG'):
                                    _carousel_dbg('CONNECT skip n={}'.format(ovl))
                    if not routed and now_t - ui.get('_ovl_route_t', 0.0) >= 0.2:
                        ui['_ovl_route_t'] = now_t
                        ui['_ovl_hold_release'] = ui.get('_ovl_hold_release', 0) + 1
                        (_routed_next if ovl > 0 else _routed_prev)()
                        ui['ovl_skip'] = ovl - 1 if ovl > 0 else ovl + 1
                        if os.environ.get('SEGUE_CAROUSEL_DBG'):
                            _carousel_dbg('MEDIAKEY drain dir={} left={}'.format(1 if ovl > 0 else -1, ui['ovl_skip']))
        hk = getattr(c, 'open_hotkey', '')
        if hk:
            from fh6_spotify.input_backend import _MOD_VKS, parse_key_code
            req_mods, main_vk = parse_key_code(hk)
            if main_vk is not None:
                import ctypes
                _u = ctypes.windll.user32
                down = bool(_u.GetAsyncKeyState(main_vk) & 32768)
                mods = frozenset(m for m in _MOD_VKS if _u.GetAsyncKeyState(m) & 32768)
                hit = down and mods == req_mods
                if hit and not _hotkey_prev[0]:
                    if not getattr(controller, '_suppressed', False):
                        _on_open_window()
                _hotkey_prev[0] = hit
        else:
            _hotkey_prev[0] = False
        if c.ducking_enabled and speech_state['worker'] is None:
            _start_speech_worker()
        elif not c.ducking_enabled and speech_state['worker'] is not None:
            _stop_speech_worker()
        elif speech_state['worker'] is not None:
            if speech_state['mode'] != (bool(c.low_cpu_mode), getattr(c, 'duck_scope', 'game')):
                _stop_speech_worker()
                _start_speech_worker()
        sw = speech_state['worker']
        if sw is not None:
            sw.set_paused(is_race_on is not True)
        speech = sw.speech_active if sw else False
        _need_fg = c.mode == 'general' or getattr(c, 'overlay_in_game_only', False)
        _fg = _foreground_exe_name() if _need_fg else None
        is_focused = None
        if c.mode == 'general' and proc_state['running']:
            target = (c.general_target_process or '').lower()
            if target:
                is_focused = _fg == target
        ui['is_focused'] = is_focused
        _gset = _target_exe_set(c.mode == 'general')
        ui['game_focused'] = bool(_gset) and _fg in _gset
        vol_race = is_race_on if general_mode else not _menu_latch
        factor = state.update(vol_race, speech, now, speed=telemetry.speed, is_focused=is_focused, is_running=bool(proc_state['running']))
        ramp = c.volume_ramp_in if factor > eased_factor else c.volume_ramp_out
        eased_factor += (factor - eased_factor) * ramp
        applied = user_volume * eased_factor
        if ui.get('disabled'):
            applied = user_volume
        volume.apply(applied)
        ui['source_active'] = getattr(volume, 'has_audio', False)
        ui['volume'] = user_volume
        ui['muted'] = user_volume <= 0.01 or eased_factor <= 0.01
        ui['safe'] = controller.safe_mode if controller else False
        ui['overlay_enabled'] = c.overlay_enabled
        ui['overlay_position'] = c.overlay_position
        ui['overlay_custom_x'] = c.overlay_custom_x
        ui['overlay_custom_y'] = c.overlay_custom_y
        ui['overlay_scale'] = c.overlay_scale
        ui['overlay_screen'] = getattr(c, 'overlay_screen', '')
        ui['overlay_compact'] = c.overlay_compact
        ui['overlay_always_on'] = c.overlay_always_on
        ui['overlay_in_game_only'] = getattr(c, 'overlay_in_game_only', False)
        ui['overlay_drive_only'] = getattr(c, 'overlay_drive_only', False)
        ui['can_skip'] = can_skip
        ui['game'] = is_race_on
        ui['game_running'] = bool(proc_state['running'])
        ui['speech'] = speech
        ui['applied'] = applied
        if now - last_cpu >= 1.0:
            last_cpu = now
            try:
                ui['app_cpu'] = _proc.cpu_percent(None) / _ncpu
            except Exception:
                pass
            ui['speech_cpu'] = sw.cpu_pct if sw else 0.0
        np = media.get() if media else None
        ui['np_title'] = np.title if np else ''
        ui['np_artist'] = np.artist if np else ''
        ui['np_playing'] = bool(np.is_playing) if np else False
        ui['np_thumb'] = np.thumb if np else None
        ui['np_app'] = np.app if np else ''
        ui['shuffle'] = bool(np.shuffle) if np else False
        ui['repeat'] = np.repeat if np else 'none'
        ui['np_pos'] = np.position if np else 0.0
        ui['np_dur'] = np.duration if np else 0.0
        try:
            from fh6_spotify import canvas
            canvas.update_np_video(ui.get('np_title', ''), ui.get('np_artist', ''), ui, getattr(c, 'overlay_video', False), getattr(c, 'canvas_service_port', 7355))
        except Exception:
            pass
        if c.debug and (round(applied, 3) != last_print or now - last_dbg > 3):
            age = now - telemetry.last_packet_time if telemetry.last_packet_time else None
            agestr = f'{age:.1f}s' if age is not None else 'none'
            spd = f'{telemetry.speed:.1f}' if telemetry.speed is not None else '?'
            print(f'[dbg] raceOn={telemetry.is_race_on} pktage={agestr} speed={spd}m/s speech={speech} -> {int(applied * 100)}%')
            last_print = round(applied, 3)
            last_dbg = now
    ctrl_note = 'controls: D-pad L/R=skip, touchpad swipe=volume, tap=pause, Mute=safe mode' if controller else 'controls: off (no DualSense)'
    print(f'Segue on 127.0.0.1:{c.port} ({ctrl_note}).')
    stop_event = threading.Event()

    def radio_loop():
        try:
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        import traceback
        try:
            while not stop_event.is_set():
                try:
                    step()
                except Exception:
                    try:
                        base = os.environ.get('APPDATA') or os.path.expanduser('~')
                        log_dir = os.path.join(base, 'Segue')
                        os.makedirs(log_dir, exist_ok=True)
                        with open(os.path.join(log_dir, 'errors.log'), 'a') as f:
                            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] radio step:\n")
                            f.write(traceback.format_exc())
                            f.write('\n')
                    except Exception:
                        pass
                time.sleep(0.033)
        finally:
            try:
                import comtypes
                comtypes.CoUninitialize()
            except Exception:
                pass
    overlay = None
    media = None
    try:
        from fh6_spotify.overlay import SpotifyOverlay
        from fh6_spotify.media import MediaWatcher

        def _src_match(appid: str) -> bool:
            a = (appid or '').lower()
            if c.source == 'browser':
                if any(b.replace('.exe', '').lower() in a for b in c.browser_process_names):
                    return True
                return 'firefox' in a or 'mozilla' in a or 'chromium' in a or '308046b0af4a39cb' in a
            if c.source == 'applemusic':
                return 'applemusic' in a or 'apple music' in a
            if c.source == 'tidal':
                return 'tidal' in a
            if c.source == 'amazonmusic':
                return 'amazon' in a
            if c.source == 'ytmusic':
                return 'youtube music' in a or 'youtube-music' in a or 'th-ch' in a or 'ytmd' in a
            if c.source == 'custom':
                m = (getattr(c, 'custom_smtc_match', '') or '').lower()
                return bool(m) and m in a
            if c.source == 'localmedia':
                if any(b.replace('.exe', '').lower() in a for b in getattr(c, 'localmedia_process_names', ())):
                    return True
                return 'vlc' in a or 'media player' in a or 'media.player' in a or 'zune' in a or 'foobar' in a or 'musicbee' in a or 'aimp' in a or 'winamp' in a or 'mpc-hc' in a or 'mpc-be' in a or 'mpc_hc' in a
            base = c.spotify_process_name.replace('.exe', '').lower()
            return base in a or 'chromium' in a
        _smtc_log = os.path.join(os.path.dirname(cfg_path), 'smtc.log')
        media = MediaWatcher(match=_src_match, debug=getattr(c, 'debug', False), log_path=_smtc_log)
        media.start()
        ui['media'] = media

        def _overlay_moved(cx, cy, screen_name=''):
            c.overlay_custom_x = cx
            c.overlay_custom_y = cy
            c.overlay_screen = screen_name or ''
            try:
                c.save(cfg_path)
            except Exception:
                return None

        def _overlay_set_move(on):
            ui['overlay_move_mode'] = bool(on)
            ui['overlay_ping'] = time.monotonic()

        def _overlay_snapped(pos_name, screen_name=''):
            c.overlay_position = pos_name
            c.overlay_custom_x = -1.0
            c.overlay_custom_y = -1.0
            c.overlay_screen = screen_name or ''
            try:
                c.save(cfg_path)
            except Exception:
                return None

        def _overlay_resized(scale):
            c.overlay_scale = float(scale)
            try:
                c.save(cfg_path)
            except Exception:
                return None

        def _overlay_skip(net):
            ui['ovl_skip'] = int(net)
            ui['_ovl_change_t'] = time.monotonic()

        def _overlay_hover(on):
            ui['ovl_hover'] = bool(on)
        overlay = SpotifyOverlay(lambda: dict(ui), media, on_move=_overlay_moved, on_move_mode=_overlay_set_move, on_resize=_overlay_resized, on_snap=_overlay_snapped, on_skip=_overlay_skip, on_hover=_overlay_hover)
        overlay.show()
        try:
            if getattr(c, 'mouse_control_enabled', False):
                from fh6_spotify.overlay import _VolumeCursorHud
                _vol_hud = _VolumeCursorHud(lambda: dict(ui))
                ui['_vol_hud'] = _vol_hud
        except Exception as exc:
            print(f'  vol HUD disabled: {exc}')
    except Exception as exc:
        print(f'  overlay disabled: {exc}')
    threading.Thread(target=radio_loop, daemon=True).start()
    _stopped = threading.Event()

    def stop(restore: bool=True):
        if _stopped.is_set():
            return
        _stopped.set()
        stop_event.set()
        _stop_speech_worker()
        if controller:
            controller.close()
        if mouse_ctrl is not None:
            try:
                mouse_ctrl.close()
            except Exception:
                pass
        if overlay is not None:
            try:
                overlay.close()
            except Exception:
                pass
        if media is not None:
            try:
                media.stop()
            except Exception:
                pass
        try:
            if restore:
                volume._release(c.source, ramp_ms=400)
        except Exception:
            pass
        print('Stopped, Spotify volume restored.')
    return stop


def run(config: Config | None=None) -> None:
    """Backward-compat single-process entry: create a QApplication, start the
    runtime, run exec, then tear down. The unified app (`fh6_spotify.app`) is
    the preferred entry point; this remains for ad-hoc / scripted launches."""
    from fh6_spotify.config import default_config_path
    cfg_path = default_config_path()
    c = config if config is not None else Config.load(cfg_path)
    ui = {}
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    app = QApplication.instance() or QApplication([])
    stop = start_runtime(c, ui)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()
    tick.start(200)
    tick.timeout.connect(lambda: None)
    app.aboutToQuit.connect(stop)
    app.exec()
