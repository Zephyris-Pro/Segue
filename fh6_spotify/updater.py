"""Update notifier.

Polls a public GitHub raw URL once per launch (and at most once per 24h)
for a `latest.json` shaped like:

    {"version": "1.0.1",
     "ko_fi_url": "https://ko-fi.com/segueapp",
     "notes": "fix touchpad ...",
     "installer_url": "https://ko-fi.com/segueapp/shop/..."  # optional}

If the remote version is newer than `fh6_spotify.version.VERSION`, the
caller (settings window) is told to show a dismissable banner. State
(last check timestamp + dismissed-for-this-version) lives in
`%APPDATA%/Segue/update_state.json` so dismissing a banner stays sticky
across launches but only for that one version.

Network is best-effort: any error (no internet, GitHub blip, malformed
JSON, etc.) silently drops the check. We never block the UI thread.
"""

from __future__ import annotations
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from fh6_spotify.version import VERSION

MANIFEST_URL = os.environ.get(
    "SEGUE_MANIFEST_URL",
    "https://raw.githubusercontent.com/Segueapp/segue-releases/main/latest.json",
)
POLL_INTERVAL_S = 3600
HTTP_TIMEOUT_S = 4.0
DOWNLOAD_TIMEOUT_S = 60.0


@dataclass
class UpdateInfo:
    """Result handed to the settings window when an update is available."""

    version: str
    ko_fi_url: str
    notes: str
    installer_url: str = ""
    headline: str = ""
    cta_label: str = ""
    changelog_url: str = ""
    color: str = ""
    image_url: str = ""
    sha256: str = ""


def _state_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Segue", "update_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        return None


def _parse_version(v: str) -> tuple:
    """Parse '1.2.3' into (1, 2, 3) for tuple comparison. Non-numeric chunks
    collapse to 0 so '1.0.0-beta' < '1.0.0' and broken values lose."""
    parts = []
    for chunk in (v or "").split("."):
        try:
            parts.append(int(chunk.split("-")[0].split("+")[0]))
        except (TypeError, ValueError):
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(remote: str, local: str = VERSION) -> bool:
    return _parse_version(remote) > _parse_version(local)


def _is_https_exe(url: str) -> bool:
    """True only for a direct, downloadable HTTPS .exe (rejects http + Ko-fi pages)."""
    u = (url or "").strip().lower()
    return u.startswith("https://") and u.endswith(".exe")


def installer_is_directly_updatable(info) -> bool:
    """True when the manifest supports the in-app download path: a verified hash
    AND a direct HTTPS .exe url. Otherwise the banner uses the browser fallback."""
    return bool(getattr(info, "sha256", "")) and _is_https_exe(
        getattr(info, "installer_url", "")
    )


def verify_sha256(path: str, expected_hex: str) -> bool:
    """Stream-hash `path` and compare to `expected_hex` (case-insensitive).
    False on missing file, read error, or empty/blank expected hash."""
    expected = (expected_hex or "").strip().lower()
    if not expected:
        return False
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest() == expected


def _update_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "segue-update")
    os.makedirs(d, exist_ok=True)
    return d


def download_installer(info, on_progress=None) -> str:
    """Download info.installer_url to %TEMP%/segue-update/, verify its SHA-256,
    and return the local path. Reuses an already-verified cached file. Reports
    progress via on_progress(downloaded_bytes, total_bytes) (total 0 if unknown).
    Raises ValueError/urllib errors on failure - caller falls back to the browser."""
    url = (getattr(info, "installer_url", "") or "").strip()
    if not _is_https_exe(url):
        raise ValueError("installer_url is not a direct https .exe")
    dest = os.path.join(_update_dir(), "Segue_Setup_{}.exe".format(info.version))
    if os.path.exists(dest) and verify_sha256(dest, info.sha256):
        if on_progress:
            sz = os.path.getsize(dest)
            on_progress(sz, sz)
        return dest
    tmp = dest + ".part"
    req = urllib.request.Request(
        url, headers={"User-Agent": f"Segue/{VERSION} update-dl"}
    )
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    if not verify_sha256(tmp, info.sha256):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError("installer sha256 mismatch")
    os.replace(tmp, dest)
    return dest


def apply_update(installer_path: str, quit_fn) -> None:
    """Launch the (already-verified) installer silently + detached so it survives
    this process exiting, then quit. The Inno installer kills any remaining Segue,
    installs over %LocalAppData%\\Programs\\Segue (per-user, no UAC), and relaunches
    Segue (see installer/Segue.iss WizardSilent [Run]). Config in %APPDATA% is kept.
    Caller only invokes this with a path download_installer() already verified."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(
        [installer_path, "/VERYSILENT", "/UPDATED"], creationflags=flags, close_fds=True
    )
    quit_fn()


def dismiss_version(version: str) -> None:
    """Record that the user clicked × on the banner for this version. The
    banner won't reappear for the same version, but a later release will
    still trigger it."""
    state = _load_state()
    state["dismissed_version"] = version
    _save_state(state)


def _fetch_manifest() -> Optional[dict]:
    """Best-effort GET of the latest.json manifest. Returns None on any
    failure - caller should never see exceptions from this module."""
    try:
        if not MANIFEST_URL.lower().startswith(("http://", "https://")):
            with open(MANIFEST_URL, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None
    try:
        req = urllib.request.Request(
            MANIFEST_URL, headers={"User-Agent": f"Segue/{VERSION} update-check"}
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def check_async(
    on_available: Callable[[UpdateInfo], None],
    force: bool = False,
    ignore_cooldown: bool = False,
) -> None:
    """Spawn a daemon thread that hits the manifest URL. If a newer
    version is found AND the user hasn't already dismissed it,
    `on_available(UpdateInfo)` is posted back. Caller is responsible for
    marshalling the callback onto the GUI thread.

    `force=True` ignores BOTH the cooldown and the per-version dismissal -
    use it from the "Check for updates" menu item.
    `ignore_cooldown=True` ignores the cooldown but still respects dismissal -
    use it for the launch check + the periodic re-check.
    """

    def _worker():
        state = _load_state()
        now = time.time()
        last = float(state.get("last_check_ts", 0) or 0)
        if not force and not ignore_cooldown and now - last < POLL_INTERVAL_S:
            return
        manifest = _fetch_manifest()
        state["last_check_ts"] = now
        _save_state(state)
        if not manifest:
            return
        remote_v = str(manifest.get("version", "")).strip()
        if not remote_v or not is_newer(remote_v):
            return
        if state.get("dismissed_version") == remote_v and not force:
            return
        info = UpdateInfo(
            version=remote_v,
            ko_fi_url=str(manifest.get("ko_fi_url", "")).strip(),
            notes=str(manifest.get("notes", "")).strip(),
            installer_url=str(manifest.get("installer_url", "")).strip(),
            headline=str(manifest.get("headline", "")).strip(),
            cta_label=str(manifest.get("cta_label", "")).strip(),
            changelog_url=str(manifest.get("changelog_url", "")).strip(),
            color=str(manifest.get("color", "")).strip(),
            image_url=str(manifest.get("image_url", "")).strip(),
            sha256=str(manifest.get("sha256", "")).strip().lower(),
        )
        try:
            on_available(info)
        except Exception:
            return None

    t = threading.Thread(target=_worker, name="segue-update-check", daemon=True)
    t.start()
