"""Manage \'launch with Forza\' â€” a Startup shortcut to the Segue watcher.\n\nThe watcher (`-m fh6_spotify --watch`) waits for the game, starts Segue when it\nlaunches, and stops it when the game closes. Toggled from the settings window.\n"""

import os
import sys
import subprocess

_CREATE_NO_WINDOW = 134217728


def _startup_lnk() -> str:
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(
        base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Segue.lnk"
    )


def is_installed() -> bool:
    return os.path.exists(_startup_lnk())


def installed_mode():
    """\'watch\' | \'direct\' | None - which mode the Startup shortcut is in, read from\n    its Arguments. Lets the two startup toggles (game-watch vs Start-with-Windows)\n    reflect the single shared Segue.lnk correctly."""
    lnk = _startup_lnk()
    if not os.path.exists(lnk):
        return
    else:
        try:
            ps = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{}');Write-Output $s.Arguments".format(
                lnk
            )
            out = (
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True,
                    text=True,
                    creationflags=_CREATE_NO_WINDOW,
                    timeout=10,
                ).stdout
                or ""
            )
            return "watch" if "--watch" in out else "direct"
        except Exception:
            return "watch"


def _target_args_workdir(direct=False):
    """Shortcut target. direct=False -> the --watch watcher (game-gated Segue, the\n    legacy \'Auto-start\'); direct=True -> launch Segue itself at login (runs always,\n    no game needed = \'Start with Windows\')."""
    if getattr(sys, "frozen", False):
        return (
            sys.executable,
            "" if direct else "--watch",
            os.path.dirname(sys.executable),
        )
    else:
        py = sys.executable.replace("python.exe", "pythonw.exe")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return (py, "-m fh6_spotify" if direct else "-m fh6_spotify --watch", root)


def _watcher_cmd():
    """(argv list, workdir) to launch the watcher process directly."""
    if getattr(sys, "frozen", False):
        return ([sys.executable, "--watch"], os.path.dirname(sys.executable))
    else:
        py = sys.executable.replace("python.exe", "pythonw.exe")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return ([py, "-m", "fh6_spotify", "--watch"], root)


def _spawn_watcher() -> None:
    """Start the watcher now (so \'Open with Forza\' takes effect without a reboot).\n    A second watcher exits itself via the single-instance mutex in watch.py."""
    cmd, cwd = _watcher_cmd()
    try:
        subprocess.Popen(cmd, cwd=cwd, creationflags=_CREATE_NO_WINDOW)
    except Exception as exc:
        print(f"watcher spawn failed: {exc}")


def _kill_watchers() -> None:
    """Stop any running watcher process (used when turning the toggle off)."""
    try:
        import psutil
    except Exception:
        return None
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = proc.info["cmdline"] or []
            if (
                proc.pid != me
                and "--watch" in cl
                and any(("fh6_spotify" in c or "Segue" in c for c in cl))
            ):
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def install(direct: bool = False) -> None:
    """Write the Startup shortcut. direct=False = the game watcher (default, legacy\n    \'Auto-start\'); direct=True = launch Segue itself at login (\'Start with Windows\').\n    One shared Segue.lnk, so the two modes are mutually exclusive (last write wins)."""
    lnk = _startup_lnk()
    target, args, workdir = _target_args_workdir(direct)
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    ps = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{t}';$s.Arguments='{a}';$s.WorkingDirectory='{w}';$s.Save()".format(
        lnk=lnk, t=target, a=args, w=workdir
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=_CREATE_NO_WINDOW,
            timeout=10,
        )
    except Exception as exc:
        print(f"autostart install failed: {exc}")
    if not direct:
        _spawn_watcher()


def uninstall() -> None:
    try:
        os.remove(_startup_lnk())
    except OSError:
        pass
    _kill_watchers()
