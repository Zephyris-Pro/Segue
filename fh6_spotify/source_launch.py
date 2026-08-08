"""Open the active music source app on demand.

First step toward a one-click "start session": Segue opens the configured
source if it isn't already running. The launch is resolved the most reliable
way available, in order:

  1. already running         -> nothing to do (RUNNING)
  2. Start Menu shortcut      -> launch the .lnk (covers desktop installs)
  3. Store-app AUMID          -> shell:AppsFolder\\<id> (Apple Music etc. have no
                                .lnk and no protocol; resolved via Get-StartApps)
  4. registered URI protocol  -> spotify:, tidal://
  5. give up                  -> caller asks the user to open it (NOT_FOUND)

Browser / local-files sources are UNSUPPORTED: "open the browser" or "open a
media player" has no single right target, so the caller just tells the user.

The public entry point `launch_source` takes injectable seams (running / finder
/ opener) so the resolution logic is unit-tested without touching the OS.
"""

import os
from collections import namedtuple

LaunchResult = namedtuple("LaunchResult", "status display detail")
_RECIPES = {
    "spotify": ("Spotify", ["Spotify"], "spotify:"),
    "applemusic": ("Apple Music", ["Apple Music"], None),
    "tidal": ("TIDAL", ["TIDAL"], "tidal://"),
    "amazonmusic": ("Amazon Music", ["Amazon Music"], None),
    "ytmusic": ("YouTube Music", ["YouTube Music"], None),
}
_UNSUPPORTED = {"browser": "your browser", "localmedia": "your media player"}


def _norm(s: str) -> str:
    """Lowercase, alphanumeric-only, for fuzzy shortcut/name matching."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def recipe_for(config):
    """(display, [start-menu name candidates], protocol) for the config's source.
    Custom resolves from the saved label + the exe base of its process names.
    Returns (None, [], None) for unsupported / unknown sources."""
    src = getattr(config, "source", "spotify")
    if src in _RECIPES:
        return _RECIPES[src]
    if src == "custom":
        label = getattr(config, "custom_label", "") or "your source"
        names = [label]
        for exe in getattr(config, "custom_process_names", ()):
            base = exe[:-4] if exe.lower().endswith(".exe") else exe
            if base and base not in names:
                names.append(base)
        return (label, names, None)
    return (None, [], None)


_LAUNCH_IGNORE_PROCS = {"amplibraryagent.exe"}


def _source_candidates(config):
    """Process names that mean 'the source UI is already running' - the list
    Segue ducks, minus pure background agents (see _LAUNCH_IGNORE_PROCS) so a
    lingering helper doesn't make quick-launch think the app is already up."""
    from fh6_spotify.spotify_volume import SpotifyVolume

    names = SpotifyVolume(config)._candidates()
    ui = [n for n in names if n and n.lower() not in _LAUNCH_IGNORE_PROCS]
    return ui or names


def source_running(config, names=None, proc_names=None) -> bool:
    """True if any of the source's processes is alive. `proc_names` (an iterable
    of running process names) is injectable for tests; otherwise psutil is used."""
    names = names if names is not None else _source_candidates(config)
    wanted = {n.lower() for n in names if n}
    if not wanted:
        return False
    if proc_names is None:
        import psutil

        proc_names = []
        for p in psutil.process_iter(["name"]):
            try:
                proc_names.append(p.info.get("name") or "")
            except Exception:
                pass
    return any(nm and nm.lower() in wanted for nm in proc_names)


def _start_menu_roots():
    roots = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            roots.append(
                os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")
            )
    return roots


def _walk_lnks():
    for root in _start_menu_roots():
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk"):
                    yield os.path.join(dirpath, f)


def find_start_menu_shortcut(name_candidates, lister=None):
    """Path to a Start Menu .lnk whose file stem matches one of `name_candidates`
    (alphanumeric, case-insensitive), or None. Tries exact match first, then a
    prefix match (so "Spotify" finds "Spotify - Music and Podcasts.lnk").
    `lister` is a callable -> iterable of .lnk paths, injectable for tests."""
    targets = [_norm(n) for n in name_candidates if n]
    targets = [t for t in targets if t]
    if not targets:
        return
    paths = list((lister or _walk_lnks)())
    stems = [(p, _norm(os.path.splitext(os.path.basename(p))[0])) for p in paths]
    for p, stem in stems:
        if stem in targets:
            return p
    for p, stem in stems:
        for t in targets:
            if len(t) >= 4 and stem.startswith(t):
                return p
    return None


def _resolve_aumid(name_candidates):
    """AppUserModelID of a Store app matching one of `name_candidates`, via
    `Get-StartApps`, or None. Store apps (Apple Music, ...) have no Start-Menu
    .lnk and no URI protocol, so this is the only way to launch them: through
    the shell's AppsFolder by their AUMID (e.g.
    'AppleInc.AppleMusicWin_nzyj5cx40ttqa!App')."""
    targets = [_norm(n) for n in name_candidates if n]
    targets = [t for t in targets if t]
    if not targets:
        return
    import json
    import subprocess

    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=134217728,
        )
        data = json.loads(out.stdout or "null")
    except Exception:
        return
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return
    entries = [
        (_norm(e.get("Name", "")), e.get("AppID", ""))
        for e in data
        if isinstance(e, dict) and e.get("AppID")
    ]
    for nm, aid in entries:
        if nm in targets:
            return aid
    for nm, aid in entries:
        for t in targets:
            if len(t) >= 4 and nm.startswith(t):
                return aid
    return None


def _steam_root():
    """Steam install dir from the registry (HKCU\\Software\\Valve\\Steam SteamPath),
    falling back to the default Program Files locations. None if Steam isn't found."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Valve\\Steam") as k:
            p = winreg.QueryValueEx(k, "SteamPath")[0]
            if p and os.path.isdir(p):
                return p
    except Exception:
        pass
    for d in ("C:\\Program Files (x86)\\Steam", "C:\\Program Files\\Steam"):
        if os.path.isdir(d):
            return d
    return None


def _resolve_steam_appid(name_candidates):
    """Steam appid for a game whose manifest "name" matches one of `name_candidates`
    (normalized, exact then prefix), by scanning every Steam library's
    appmanifest_*.acf. None if not found. Steam games have no Start-Menu .lnk and no
    AUMID, so `steam://rungameid/<appid>` is the only reliable launch path (Forza
    Horizon 6, for one, is Steam-only)."""
    targets = [_norm(n) for n in name_candidates if n]
    targets = [t for t in targets if t]
    if not targets:
        return
    root = _steam_root()
    if not root:
        return
    import re
    import glob

    libs = [os.path.join(root, "steamapps")]
    try:
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        txt = open(vdf, encoding="utf-8", errors="ignore").read()
        for m in re.finditer('"path"\\s*"([^"]+)"', txt):
            p = os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps")
            if p not in libs:
                libs.append(p)
    except Exception:
        pass
    for lib in libs:
        for acf in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
            try:
                txt = open(acf, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            nm = re.search('"name"\\s*"([^"]+)"', txt)
            ap = re.search('"appid"\\s*"(\\d+)"', txt)
            if not nm or not ap:
                continue
            n = _norm(nm.group(1))
            if any(n == t or (len(t) >= 4 and n.startswith(t)) for t in targets):
                return ap.group(1)
    return None


def _open_appsfolder(aumid):
    """Activate a Store/UWP app by AUMID via IApplicationActivationManager - the
    Windows-sanctioned activator (what a Start-menu click uses). It brings the app
    to the foreground, unlike explorer/os.startfile on the 'shell:AppsFolder\\<id>'
    moniker, which from Segue's launcher thread silently no-op or open behind. We
    COM-init this thread and grant foreground rights first so the app can come up.
    Falls back to os.startfile if the COM activator is unavailable."""
    import ctypes

    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    try:
        import comtypes
        from comtypes import GUID, IUnknown, COMMETHOD, HRESULT
        from ctypes import POINTER, c_wchar_p, c_int, c_uint
        import comtypes.client

        try:
            comtypes.CoInitialize()
        except Exception:
            pass

        class _IAppActivationMgr(IUnknown):
            _iid_ = GUID("{2e941141-7f97-4756-ba1d-9decde894a3d}")
            _methods_ = [
                COMMETHOD(
                    [],
                    HRESULT,
                    "ActivateApplication",
                    (["in"], c_wchar_p, "appUserModelId"),
                    (["in"], c_wchar_p, "arguments"),
                    (["in"], c_int, "options"),
                    (["out"], POINTER(c_uint), "processId"),
                )
            ]

        mgr = comtypes.client.CreateObject(
            GUID("{45BA127D-10A8-46EA-8AB7-56EA9078943C}"), interface=_IAppActivationMgr
        )
        mgr.ActivateApplication(aumid, None, 0)
    except Exception:
        os.startfile("shell:AppsFolder\\" + aumid)


def _open_minimized(target):
    """Open a Start-Menu shortcut / URI minimized and WITHOUT stealing the
    foreground (SW_SHOWMINNOACTIVE = 7), so the launched app doesn't pop over
    Segue. Falls back to os.startfile if ShellExecute isn't available."""
    try:
        import ctypes

        rv = ctypes.windll.shell32.ShellExecuteW(None, "open", target, None, None, 7)
        if int(rv) <= 32:
            os.startfile(target)
    except Exception:
        os.startfile(target)


def _foreground_by_title(needles) -> bool:
    """Bring a visible top-level window whose TITLE contains one of `needles` to the
    foreground (restoring it if minimized). Returns True if one was found.

    Title - not PID - because UWP apps (Apple Music) render their window inside
    ApplicationFrameHost.exe, so a PID match against the app's own process never
    finds it. Call from the GUI thread (which holds the foreground after the click)
    so SetForegroundWindow is allowed."""
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    needles = [n.lower() for n in needles if n]
    if not needles:
        return False
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd):
            return True
        if u.GetWindow(hwnd, 4):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        t = buf.value.lower()
        if any(nd in t for nd in needles):
            found.append(hwnd)
        return True

    u.EnumWindows(_cb, 0)
    if not found:
        return False
    hwnd = found[0]
    try:
        u.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)
    u.SetForegroundWindow(hwnd)
    u.BringWindowToTop(hwnd)
    return True


def launch_source(
    config,
    *,
    to_front=None,
    running=None,
    finder=None,
    opener=None,
    aumid_finder=None,
    app_opener=None,
):
    """Open the config's source app. Returns a LaunchResult. Seams:
      running(config) -> bool, finder(names) -> path|None, opener(target) -> None,
      aumid_finder(names) -> aumid|None, app_opener(aumid) -> None.

    to_front=True: don't short-circuit when it's already running, and open
    NORMALLY (foreground) instead of minimized. Re-opening a single-instance
    app's shortcut / URI focuses the existing window, so clicking the source icon
    brings a running app to the front (or launches it up front when it's off).
    """
    display, names, protocol = recipe_for(config)
    src = getattr(config, "source", "spotify")
    if src in _UNSUPPORTED or display is None:
        return LaunchResult(
            "unsupported", _UNSUPPORTED.get(src, display or "your source"), ""
        )
    if not to_front and (running or source_running)(config):
        return LaunchResult("running", display, "")
    if to_front:
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
    _open = opener or (os.startfile if to_front else _open_minimized)
    lnk = (finder or find_start_menu_shortcut)(names)
    if lnk:
        try:
            _open(lnk)
            return LaunchResult("launched", display, lnk)
        except Exception:
            pass
    aumid = (aumid_finder or _resolve_aumid)(names)
    if aumid:
        try:
            (app_opener or _open_appsfolder)(aumid)
            return LaunchResult("launched", display, aumid)
        except Exception:
            pass
    if protocol:
        try:
            _open(protocol)
            return LaunchResult("launched", display, protocol)
        except Exception:
            pass
    return LaunchResult("not_found", display, "")
