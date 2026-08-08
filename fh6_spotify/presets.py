"""Named profiles of the user-tunable mixer/overlay settings.\n\nStored as ONE JSON FILE PER PRESET under %APPDATA%/Segue/presets/ :\n    presets/Race day.json\n    presets/Chill.json\nso the user can back up, copy, or share an individual preset by just\ngrabbing its file (and drop a .json in the folder to import one). Each\nfile holds the captured fields + two metadata keys:\n    __name__  display name (lets the file be renamed without losing it)\n    __game__  the game preset it was saved on (drives the menu icon + filter)\n\nLegacy single-file presets.json from older builds is auto-split into\nindividual files on first load, then renamed to presets.json.bak.\n"""

import glob
import json
import os
import re
from fh6_spotify.config import Config, default_config_path

PRESET_FIELDS = [
    "full_level",
    "menu_level",
    "duck_level",
    "unfocused_level",
    "volume_ramp_in",
    "overlay_enabled",
    "overlay_position",
    "overlay_custom_x",
    "overlay_custom_y",
    "overlay_scale",
    "overlay_compact",
    "overlay_screen",
    "ducking_enabled",
    "low_cpu_mode",
]
GAME_TAG_KEY = "__game__"
NAME_KEY = "__name__"


def presets_dir(base: str | None = None) -> str:
    base = base or os.path.dirname(default_config_path())
    return os.path.join(base, "presets")


def presets_path() -> str:
    return os.path.join(os.path.dirname(default_config_path()), "presets.json")


def _safe_filename(name: str) -> str:
    """Filesystem-safe filename from a preset name (strip Windows-illegal\n    chars, clamp length). Display name is preserved separately via NAME_KEY."""
    s = re.sub('[\\\\/:*?"<>|]', "_", name or "").strip()
    return (s or "preset")[:80]


def capture(cfg) -> dict:
    """Snapshot the preset-able fields from a live Config + stamp the game\n    it was saved on so the menu can tag + filter it."""
    d = {field: getattr(cfg, field) for field in PRESET_FIELDS}
    d[GAME_TAG_KEY] = getattr(cfg, "game_preset", "")
    return d


def preset_game(data: dict) -> str:
    """The game a preset was saved on (\'\' = legacy / universal)."""
    return (data or {}).get(GAME_TAG_KEY, "") if isinstance(data, dict) else ""


def _migrate_legacy(directory: str) -> None:
    """Split an old single-file presets.json (sibling of the presets/ dir)\n    into one file per preset, then rename the legacy file to .bak."""
    legacy = os.path.join(os.path.dirname(directory), "presets.json")
    if not os.path.isfile(legacy):
        return
    else:
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                os.makedirs(directory, exist_ok=True)
                for name, data in old.items():
                    if isinstance(data, dict):
                        save_preset(name, data, directory)
            os.replace(legacy, legacy + ".bak")
        except Exception:
            return None


def load_presets(path: str | None = None) -> dict:
    """Return {name: data}. Reads one-file-per-preset from the presets/ dir.\n\n    If `path` ends in .json it\'s treated as a legacy single-file store\n    (used by the unit tests + back-compat)."""
    # ***<module>.load_presets: Failure: Different control flow
    target = path or presets_dir()
    if target.endswith(".json"):
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    else:
        _migrate_legacy(target)
        out = {}
        if os.path.isdir(target):
            for fp in sorted(glob.glob(os.path.join(target, "*.json"))):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        name = (
                            data.get(NAME_KEY)
                            or os.path.splitext(os.path.basename(fp))[0]
                        )
                        out[name] = data
                except (OSError, ValueError):
                    pass
        return out


def save_preset(name: str, data: dict, directory: str | None = None) -> None:
    """Write a single preset to <dir>/<safe name>.json with its display\n    name embedded."""
    d = directory or presets_dir()
    os.makedirs(d, exist_ok=True)
    payload = dict(data)
    payload[NAME_KEY] = name
    fp = os.path.join(d, _safe_filename(name) + ".json")
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, fp)


def delete_preset(name: str, directory: str | None = None) -> None:
    """Remove the preset whose stored/display name matches."""
    d = directory or presets_dir()
    if not os.path.isdir(d):
        return
    for fp in glob.glob(os.path.join(d, "*.json")):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            nm = data.get(NAME_KEY) or os.path.splitext(os.path.basename(fp))[0]
            if nm == name:
                os.remove(fp)
                break
        except (OSError, ValueError):
            pass


def save_presets(presets: dict, path: str | None = None) -> None:
    """Bulk save. Legacy .json path -> single dict file (tests). Otherwise\n    writes one file per preset into the dir AND deletes files whose preset\n    is no longer in the dict (keeps the folder in sync after a delete)."""
    target = path or presets_dir()
    if target.endswith(".json"):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2)
    else:
        os.makedirs(target, exist_ok=True)
        keep = set()
        for name, data in presets.items():
            save_preset(name, data, target)
            keep.add(_safe_filename(name) + ".json")
        for fp in glob.glob(os.path.join(target, "*.json")):
            if os.path.basename(fp) not in keep:
                try:
                    os.remove(fp)
                except OSError:
                    pass


DEFAULT_PRESET = "PlayStation (recommended)"


def ensure_default_preset(path: str | None = None) -> None:
    """Legacy seed helper (no longer called at app boot - game presets\n    cover recommended defaults now). Retained for the unit tests."""
    data = load_presets(path)
    if not data:
        seed = capture(Config())
        seed[GAME_TAG_KEY] = ""
        if path and path.endswith(".json"):
            data[DEFAULT_PRESET] = seed
            save_presets(data, path)
        else:
            save_preset(DEFAULT_PRESET, seed, path)
