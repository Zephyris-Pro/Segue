"""Forzify â€” the single unified application.\n\nOne process, one QApplication. It owns:\n  * the runtime (telemetry / state / volume / controller / speech worker /\n    overlay / radio-loop), started via `runner.start_runtime` sharing the same\n    Config `c` and state dict `ui`;\n  * the SettingsWindow, editing the SAME `c` in place (runtime reads live) and\n    persisting to disk;\n  * a system-tray icon: click -> show settings; menu Show / Quit;\n  * lifecycle: settings minimize -> hide() to tray (runtime keeps running);\n    settings close (âœ•) or tray Quit -> quit the whole app, which on\n    `aboutToQuit` stops the runtime and restores the Spotify volume.\n\nCOM/threading: per-process audio capture (proc_capture) forces comtypes onto\nMTA at import and the SpeechWorker does its CoInitializeEx(MTA) on its OWN\nthread. The Qt main thread manages its own COM apartment independently, so the\ntwo coexist. GUI objects (overlay, settings, tray) live only on the main\nthread; the radio loop and speech worker live on their own threads.\n"""
import signal
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
import os
from fh6_spotify.config import Config, default_config_path
from fh6_spotify.runner import start_runtime
from fh6_spotify.settings import SettingsWindow, _APP_ICON
_SINGLETON_KEY = 'Segue-single-instance'
def _acquire_single_instance():
    """Return a listening QLocalServer if we\'re the primary instance, or None if\n    another instance is already running (after telling it to show its window).\n\n    `--replace` (used by restart_app) skips the check and just retries listen,\n    waiting out the outgoing instance\'s shutdown."""
    import sys
    import time
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    if '--replace' not in sys.argv:
        probe = QLocalSocket()
        probe.connectToServer(_SINGLETON_KEY)
        if probe.waitForConnected(250):
            probe.write(b'show')
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            return
    QLocalServer.removeServer(_SINGLETON_KEY)
    server = QLocalServer()
    for _ in range(16):
        if server.listen(_SINGLETON_KEY):
            return server
        else:
            QLocalServer.removeServer(_SINGLETON_KEY)
            time.sleep(0.25)
    return server
def _focus_window(app, server):
    """Another instance pinged us (shortcut / Raycast re-launch): surface the\n    settings window. FORCED - the pinging process (or a game / Explorer) owns the\n    foreground, so a plain raise lands behind it and \'nothing happens\', the same\n    reason the launch show is forced."""
    conn = server.nextPendingConnection()
    if conn is not None:
        conn.close()
    seg = getattr(app, '_segue', None)
    if not seg:
        return
    show = seg.get('show_window')
    if show is not None:
        show(force=True)
    w = seg.get('window')
    if w is not None:
        w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()
def _app_icon() -> QIcon:
    """Segue app icon (window / taskbar / tray). Falls back to the drawn glyph."""
    if os.path.exists(_APP_ICON):
        return QIcon(_APP_ICON)
    else:
        return _tray_icon()
def _tray_icon() -> QIcon:
    """A simple white-rounded-square glyph so the tray has an icon without\n    shipping an asset file."""
    pix = QPixmap(32, 32)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor(0, 0, 0, 0))
    p.setBrush(QColor(255, 255, 255))
    p.drawRoundedRect(5, 5, 22, 22, 6, 6)
    p.setBrush(QColor(31, 31, 30))
    p.drawRoundedRect(11, 11, 10, 10, 3, 3)
    p.end()
    return QIcon(pix)
def build(app: QApplication):
    """Construct the unified app on an existing QApplication. Returns a\n    `quit_fn` that quits the app cleanly (used for tests / smoke runs and the\n    SIGINT handler). Wires the runtime, settings window, and tray.\n\n    Must run on the main/GUI thread.\n    """
    app.setQuitOnLastWindowClosed(False)
    cfg_path = default_config_path()
    c = Config.load(cfg_path)
    try:
        from PySide6.QtGui import QFontDatabase
        _inter = os.path.join(os.path.dirname(__file__), 'assets', 'Inter.ttf')
        if os.path.exists(_inter):
            QFontDatabase.addApplicationFont(_inter)
    except Exception:
        pass
    from fh6_spotify.settings import _SCALE_STEPS, _ACCENT
    _scale = c.ui_scale if c.ui_scale in _SCALE_STEPS else _SCALE_STEPS[0]
    _af = QFont('Segoe UI')
    _af.setPixelSize(int(round(15 * _scale)))
    _af.setStyleStrategy(QFont.PreferAntialias | QFont.NoSubpixelAntialias)
    _af.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(_af)
    from PySide6.QtGui import QPalette, QColor
    _pal = app.palette()
    _pal.setColor(QPalette.Highlight, QColor(_ACCENT))
    _pal.setColor(QPalette.HighlightedText, QColor('#1f1f1e'))
    try:
        _pal.setColor(QPalette.Accent, QColor(_ACCENT))
    except AttributeError:
        pass
    app.setPalette(_pal)
    if not c.intro_reset_migration_v2_done:
        c.tour_done = False
        c.device_chosen = False
        c.tour_reset_migration_done = True
        c.intro_reset_migration_v2_done = True
        c.save(cfg_path)
    if c.game_preset_chosen and c.device_chosen and (c.game_preset == 'forza' and c.forza_gate_seen or c.game_preset == 'rocketleague' and c.rl_gate_seen or (c.game_preset == 'other' and c.other_gate_seen)):
        try:
            import pyi_splash
            pyi_splash.close()
        except Exception:
            pass
    if not c.game_preset_chosen:
        from fh6_spotify.settings import _GamePresetPickerDialog
        from fh6_spotify import game_presets as _gp
        picked_preset = _GamePresetPickerDialog.choose() or 'forza'
        _gp.apply_preset(c, picked_preset)
        c.game_preset_chosen = True
        c.save(cfg_path)
    if not c.device_chosen:
        from fh6_spotify.settings import _DevicePickerDialog
        c.input_device = _DevicePickerDialog.choose(c)
        c.device_chosen = True
        c.save(cfg_path)
    if c.game_preset == 'forza' and (not c.forza_gate_seen):
            try:
                from fh6_spotify.settings import _ForzaSetupGate
                _ForzaSetupGate(c.input_device).exec()
            except Exception as exc:
                print(f'  forza gate failed: {exc}')
            c.forza_gate_seen = True
            c.save(cfg_path)
    if c.game_preset == 'rocketleague' and (not c.rl_gate_seen):
        try:
            from fh6_spotify.settings import _GeneralIntroGate
            _GeneralIntroGate(c.input_device, game_label='Rocket League').exec()
        except Exception as exc:
            print(f'  RL intro failed: {exc}')
        c.rl_gate_seen = True
        c.save(cfg_path)
    else:
        if c.game_preset == 'other' and (not c.other_gate_seen):
                try:
                    from fh6_spotify.settings import _GeneralIntroGate
                    _GeneralIntroGate(c.input_device, game_label='').exec()
                except Exception as exc:
                    print(f'  Other-game intro failed: {exc}')
                c.other_gate_seen = True
                c.save(cfg_path)
    ui = {}
    stop = start_runtime(c, ui)
    overlay_srv = None
    try:
        from fh6_spotify.overlay_server import StreamOverlayServer
        def _save_overlay_preset(p):
            c.overlay_preset = dict(p)
            try:
                c.save(cfg_path)
            except Exception:
                return None
        def _save_overlay_presets(presets, name):
            c.overlay_presets = dict(presets)
            c.overlay_preset_name = name or ''
            try:
                c.save(cfg_path)
            except Exception:
                return None
        overlay_srv = StreamOverlayServer(ui, getattr(c, 'stream_overlay_port', 7345), on_save=_save_overlay_preset, cfg=getattr(c, 'overlay_preset', None) or None, presets=getattr(c, 'overlay_presets', None) or {}, preset_name=getattr(c, 'overlay_preset_name', '') or '', on_persist=_save_overlay_presets)
        if getattr(c, 'stream_overlay', False):
            overlay_srv.start()
    except Exception as exc:
        print(f'  stream overlay init failed: {exc}')
    canvas_proc = None
    try:
        from fh6_spotify.version import NO_CAROUSEL as _NO_CAROUSEL
        if not _NO_CAROUSEL and (getattr(c, 'overlay_video', False) or getattr(c, 'connect_skip', False)):
                import subprocess
                import sys as _sys
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                frozen_dir = os.path.dirname(_sys.executable)
                canvas_exe = os.path.join(frozen_dir, 'canvas', 'segue-canvas.exe')
                svc_py = os.path.join(root, 'scripts', '.lsvenv', 'Scripts', 'python.exe')
                svc = os.path.join(root, 'scripts', 'canvas_service.py')
                port = str(getattr(c, 'canvas_service_port', 7355))
                cmd = None
                if os.path.exists(canvas_exe):
                    cmd = [canvas_exe, port]
                else:
                    if os.path.exists(svc_py) and os.path.exists(svc):
                            cmd = [svc_py, svc, port]
                if cmd:
                    _log_dir = os.path.join(os.environ.get('APPDATA') or root, 'Segue')
                    os.makedirs(_log_dir, exist_ok=True)
                    _svc_log = open(os.path.join(_log_dir, 'canvas_svc.log'), 'w', encoding='utf-8', errors='replace')
                    canvas_proc = subprocess.Popen(cmd, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), stdin=subprocess.DEVNULL, stdout=_svc_log, stderr=subprocess.STDOUT)
                    print('  canvas service spawned:', os.path.basename(cmd[0]))
    except Exception as exc:
        print(f'  canvas resolver spawn failed: {exc}')
    _runtime_stop = stop
    def stop():
        try:
            if canvas_proc:
                canvas_proc.terminate()
        except Exception:
            pass
        try:
            if overlay_srv:
                overlay_srv.stop()
        except Exception:
            pass
        _runtime_stop()
    quitting = {'done': False}
    state = {'window': None}
    def quit_fn():
        if quitting['done']:
            return
        else:
            quitting['done'] = True
            w = state['window']
            if w is not None:
                w._quitting = True
            try:
                c.save(cfg_path)
            except Exception:
                pass
            app.quit()
    def restart_app():
        """Relaunch the app (used after an input-device change), then quit this one.\n        --replace tells the new instance to skip the single-instance check and wait\n        out this one\'s shutdown (which frees the telemetry port + local server)."""
        import subprocess
        import sys
        try:
            stop(restore=False)
        except Exception:
            pass
        try:
            subprocess.Popen([sys.executable, '-m', 'fh6_spotify', '--replace'])
        except Exception as exc:
            print(f'  restart failed: {exc}')
        quit_fn()
    def make_window():
        return SettingsWindow(cfg=c, path=cfg_path, on_close=quit_fn, on_minimize=lambda: state['window'].hide(), ui=ui, on_scale=rebuild_window, on_restart=restart_app)
    def rebuild_window():
        """Recreate the settings window at the new UI scale, keeping position."""
        import fh6_spotify.settings as _st
        _st._SCALE = c.ui_scale if c.ui_scale in _st._SCALE_STEPS else _st._SCALE_STEPS[0]
        new_af = QFont('Segoe UI')
        new_af.setPixelSize(int(round(15 * _st._SCALE)))
        new_af.setStyleStrategy(QFont.PreferAntialias | QFont.NoSubpixelAntialias)
        new_af.setHintingPreference(QFont.PreferFullHinting)
        app.setFont(new_af)
        old = state['window']
        pos = old.pos() if old is not None else None
        is_max = bool(old is not None and old.isMaximized())
        w = make_window()
        if pos is not None:
            w.move(pos)
        if is_max:
            w.showMaximized()
        else:
            w.show()
            w.adjustSize()
        w.raise_()
        w.activateWindow()
        state['window'] = w
        app._segue['window'] = w
        if old is not None:
            old._quitting = True
            old.close()
            old.deleteLater()
    state['window'] = make_window()
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:
        pass
    tray = QSystemTrayIcon(_app_icon(), app)
    tray.setToolTip('Segue')
    def _force_foreground(w):
        """Bring the window in front even while a game owns the foreground.\n        Windows blocks SetForegroundWindow from background processes (the\n        plain raise_/activateWindow path lands BEHIND the game); attaching to\n        the foreground thread\'s input queue is the documented unlock, plus a\n        TOPMOST flip to fix z-order for windows that refuse activation."""
        import ctypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        hwnd = ctypes.c_void_p(int(w.winId()))
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)
        fg = u.GetForegroundWindow()
        tgt = u.GetWindowThreadProcessId(fg, None) if fg else 0
        our = k.GetCurrentThreadId()
        attached = bool(tgt) and tgt != our and bool(u.AttachThreadInput(our, tgt, True))
        try:
            SWP = 67
            u.SetWindowPos(hwnd, ctypes.c_void_p((-1)), 0, 0, 0, 0, SWP)
            u.SetWindowPos(hwnd, ctypes.c_void_p((-2)), 0, 0, 0, 0, SWP)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(our, tgt, False)
    def show_window(force: bool=False):
        w = state['window']
        w.show()
        w.raise_()
        w.activateWindow()
        if force:
            try:
                _force_foreground(w)
            except Exception:
                return None
    menu = QMenu()
    act_show = menu.addAction('Show')
    act_show.triggered.connect(show_window)
    act_quit = menu.addAction('Quit')
    act_quit.triggered.connect(lambda: state['window'].request_quit())
    tray.setContextMenu(menu)
    def on_activated(reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            show_window(force=True)
    tray.activated.connect(on_activated)
    tray.show()
    _summon_seen = [ui.get('summon', 0)]
    def _poll_summon():
        cur = ui.get('summon', 0)
        if cur!= _summon_seen[0]:
            _summon_seen[0] = cur
            w = state['window']
            if w.isVisible() and w.isActiveWindow():
                w.hide()
            else:
                show_window(force=True)
    _summon_timer = QTimer()
    _summon_timer.timeout.connect(_poll_summon)
    _summon_timer.start(150)
    app.aboutToQuit.connect(stop)
    app._segue = {'window': state['window'], 'tray': tray, 'menu': menu, 'stop': stop, 'config': c, 'ui': ui, 'summon_timer': _summon_timer, 'show_window': show_window, 'overlay_srv': overlay_srv}
    from fh6_spotify.settings import bundled_whatsnew_version as _wn_version
    from fh6_spotify.updater import is_newer as _is_newer
    _wn_ver = _wn_version()
    _seen = (getattr(c, 'last_seen_version', '') or '').strip()
    if _wn_ver and _is_newer(_wn_ver, _seen or '0.0.0'):
            def _show_post_update():
                try:
                    state['window'].show_post_update_whatsnew()
                except Exception:
                    return
                c.last_seen_version = _wn_ver
                try:
                    c.save(cfg_path)
                except Exception:
                    return None
            QTimer.singleShot(900, _show_post_update)
    return quit_fn
def _install_crash_logger() -> None:
    """Catch every crash path and append to %APPDATA%\\Segue\\errors.log.\n\n    Frozen windowed exe swallows stderr, so without this, crashes ghost Segue\n    with no notice. Covers:\n      * sys.excepthook    - unhandled Python errors on the main thread\n      * threading.excepthook - background thread crashes (PEP 3151)\n      * faulthandler      - C-level segfaults / access violations from native\n                            modules (hidapi, winsdk, onnxruntime, pycaw COM)\n      * qInstallMessageHandler - Qt fatal/critical messages\n      * asyncio loop exception handler - winsdk SMTC runs on asyncio\n      * atexit            - writes a \"clean exit\" marker so a missing marker\n                            in errors.log = hard crash without an exception\n\n    Startup writes a session-start line so you can tell which run crashed.\n    """
    import sys
    import time
    import traceback
    import atexit
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    log_dir = os.path.join(base, 'Segue')
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass
    log_path = os.path.join(log_dir, 'errors.log')
    def _log(text: str) -> None:
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(text)
        except Exception:
            return None
    _log(f"\n=== [{time.strftime('%Y-%m-%d %H:%M:%S')}] session start (pid={os.getpid()}) ===\n")
    def _hook(exc_type, exc, tb):
        _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] unhandled:\n" + ''.join(traceback.format_exception(exc_type, exc, tb)) + '\n')
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            return None
    sys.excepthook = _hook
    try:
        import threading
        def _t_hook(args):
            _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] thread {args.thread.name}:\n" + ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)) + '\n')
        threading.excepthook = _t_hook
    except Exception:
        pass
    try:
        import faulthandler
        fh_file = open(log_path, 'a', encoding='utf-8', buffering=1)
        fh_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] faulthandler armed\n")
        faulthandler.enable(file=fh_file, all_threads=True)
        sys._segue_fh_file = fh_file
    except Exception as e:
        _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] faulthandler setup failed: {e}\n")
    try:
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType
        def _qt_handler(mode, ctx, msg):
            label = {QtMsgType.QtDebugMsg: 'qt-debug', QtMsgType.QtInfoMsg: 'qt-info', QtMsgType.QtWarningMsg: 'qt-warn', QtMsgType.QtCriticalMsg: 'qt-critical', QtMsgType.QtFatalMsg: 'qt-fatal'}.get(mode, 'qt')
            if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
                return
            file = ctx.file or '?'
            line = ctx.line or 0
            _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label} {file}:{line}: {msg}\n")
        qInstallMessageHandler(_qt_handler)
    except Exception as e:
        _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] qt handler setup failed: {e}\n")
    try:
        import asyncio
        def _aio_handler(loop, ctx):
            msg = ctx.get('message', '')
            exc = ctx.get('exception')
            if exc:
                tb_text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            else:
                tb_text = ''
            _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] asyncio: {msg}\n{tb_text}\n")
        _orig_new_loop = asyncio.new_event_loop
        def _new_loop():
            loop = _orig_new_loop()
            loop.set_exception_handler(_aio_handler)
            return loop
        asyncio.new_event_loop = _new_loop
    except Exception as e:
        _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] asyncio handler setup failed: {e}\n")
    def _clean_exit():
        _log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] clean exit\n")
    atexit.register(_clean_exit)
def main(auto_quit_ms: int | None=None) -> None:
    """Launch the unified app. If `auto_quit_ms` is given, schedule a quit after\n    that many ms (used by the headless smoke test)."""
    _install_crash_logger()
    import gc as _gc
    try:
        _gc.disable()
    except Exception:
        pass
    try:
        from fh6_spotify.spotify_volume import restore_dirty_volumes
        restore_dirty_volumes()
    except Exception:
        pass
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
        else:
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Segue.App')
    except Exception:
        pass
    from PySide6.QtCore import Qt
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings, True)
    try:
        from PySide6.QtGui import QSurfaceFormat
        _fmt = QSurfaceFormat.defaultFormat()
        _fmt.setSamples(4)
        _fmt.setSwapInterval(0)
        QSurfaceFormat.setDefaultFormat(_fmt)
    except Exception:
        pass
    app = QApplication.instance() or QApplication([])
    QApplication.setEffectEnabled(Qt.UIEffect.UI_AnimateTooltip, False)
    QApplication.setEffectEnabled(Qt.UIEffect.UI_FadeTooltip, False)
    server = _acquire_single_instance()
    if server is None:
        return
    else:
        app.setWindowIcon(_app_icon())
        quit_fn = build(app)
        server.newConnection.connect(lambda: _focus_window(app, server))
        signal.signal(signal.SIGINT, lambda *_: quit_fn())
        tick = QTimer()
        tick.start(200)
        tick.timeout.connect(lambda: None)
        window = app._segue['window']
        app._segue['show_window'](force=True)
        QTimer.singleShot(250, lambda: app._segue['show_window'](force=True))
        if auto_quit_ms is not None:
            QTimer.singleShot(auto_quit_ms, quit_fn)
        app.exec()
if __name__ == '__main__':
    main()
