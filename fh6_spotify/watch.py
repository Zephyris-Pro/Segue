"""Watcher: launch Forzify when Forza starts, stop it when Forza closes.

Run continuously (e.g. at Windows login). Polls for the game process and
starts/stops the Forzify app as a child process so each game session gets a
fresh, clean instance.
"""

import subprocess
import sys
import time
import psutil
from fh6_spotify.config import Config


def _game_running(name: str) -> bool:
    name = name.lower()
    try:
        for proc in psutil.process_iter(["name"]):
            if (proc.info["name"] or "").lower() == name:
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return False


def _acquire_singleton():
    """Named mutex so only one watcher runs (login-launched + manually-spawned
    won't stack). Returns the handle to hold open, or None if one already runs.

    Uses WinDLL(use_last_error=True) + get_last_error(): a plain windll call can
    clobber LastError between CreateMutexW and the check, so the dedup silently
    failed."""
    import ctypes
    from ctypes import wintypes

    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = k.CreateMutexW(None, False, "Segue-watcher-singleton")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:
        return True


def _watch_targets(c: Config) -> set:
    """ALL game exes the watcher should wake on - so a multi-game user gets
    Segue auto-started no matter which of their games they launch, and
    auto-detect then picks the right preset once it's running. Union of:
      - every curated preset's exe (Forza, Rocket League, ...)
      - the user's custom general-mode target
      - the legacy game_process_name
    Lowercased for comparison."""
    names = set()
    try:
        from fh6_spotify import game_presets as _gp

        for p in _gp.GAME_PRESETS.values():
            exe = (p.get("exe") or "").lower()
            if exe:
                names.add(exe)
    except Exception:
        pass
    if c.general_target_process:
        names.add(c.general_target_process.lower())
    if c.game_process_name:
        names.add(c.game_process_name.lower())
    return names


def _any_game_running(names: set) -> bool:
    if not names:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            nm = (proc.info["name"] or "").lower()
            if nm in names:
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return False


def watch(config: Config | None = None) -> None:
    from fh6_spotify.config import default_config_path

    _lock = _acquire_singleton()
    if _lock is None:
        print("Segue watcher already running; exiting.")
        return
    c = config or Config()
    cfg_path = default_config_path()
    cfg_mtime = None
    try:
        import os

        cfg_mtime = os.path.getmtime(cfg_path)
    except OSError:
        pass
    names = _watch_targets(c)
    print(f"Segue watcher: waiting for any of {sorted(names)} ...")
    child = None
    try:
        while True:
            try:
                import os

                m = os.path.getmtime(cfg_path)
                if cfg_mtime is None or m != cfg_mtime:
                    cfg_mtime = m
                    fresh = Config.load(cfg_path)
                    c.apply_from(fresh)
                    new_names = _watch_targets(c)
                    if new_names != names:
                        names = new_names
                        print(f"Segue watcher: targets updated -> {sorted(names)}")
            except OSError:
                pass
            running = _any_game_running(names)
            if running and child is None:
                print("Game detected -> starting Segue")
                app_cmd = (
                    [sys.executable]
                    if getattr(sys, "frozen", False)
                    else [sys.executable, "-m", "fh6_spotify"]
                )
                child = subprocess.Popen(app_cmd)
            elif not running and child is not None:
                print("Game closed -> stopping Segue")
                child.terminate()
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    child.kill()
                child = None
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    if child is not None:
        child.terminate()
