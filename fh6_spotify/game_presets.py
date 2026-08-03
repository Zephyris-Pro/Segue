"""Curated per-game presets.\n\nEach preset bundles:\n- label              user-facing name shown in chevron / pickers\n- exe                process name (lowercase) used for auto-detect\n- mode               \"forza\" (telemetry) or \"general\" (process-presence)\n- supported_devices  subset of {playstation, xbox, keyboard, wheel} that\n                     makes sense for this game; first-run device picker\n                     hides the rest\n- show               which Mixer/Extras controls to surface in the UI\n                     (irrelevant ones get hidden so the panel feels\n                     curated for the game)\n- defaults           cfg overrides applied on preset switch (gamepad_skip\n                     on/off, ducking on/off, etc.)\n\nApply via apply_preset(cfg, key) - writes the fields, persists later\nvia the caller\'s _queue_save.\n\n\"other\" preset is the catch-all (any process the user picks themselves).\n"""
from __future__ import annotations
from typing import Iterable
GAME_PRESETS: dict = {'forza': {'label': 'Forza Horizon', 'exe': 'forzahorizon6.exe', 'exe_aliases': ['forzahorizon5.exe', 'forzahorizon4.exe'], 'mode': 'forza', 'supported_devices': ['playstation', 'xbox', 'keyboard', 'wheel'], 'show': {'menu_volume': True, 'unfocused_volume': False, 'ducked_volume': True, 'speech_recognition': True, 'save_cpu': True, 'fade_length': True}, 'defaults': {'mode': 'forza', 'gamepad_skip_enabled': True, 'touchpad_skip_enabled': False, 'ducking_enabled': True, 'pause_input': 'tap'}}, 'rocketleague': {'label': 'Rocket League', 'exe': 'rocketleague.exe', 'mode': 'general', 'supported_devices': ['playstation', 'keyboard'], 'show': {'menu_volume': False, 'unfocused_volume': True, 'ducked_volume': True, 'speech_recognition': True, 'save_cpu': True, 'fade_length': True}, 'defaults': {'mode': 'general', 'general_target_process': 'rocketleague.exe', 'gamepad_skip_enabled': False, 'touchpad_skip_enabled': True, 'ducking_enabled': True, 'duck_scope': 'system', 'pause_input': 'tap'}}, 'other': {'label': 'Other game', 'exe': '', 'mode': 'general', 'supported_devices': ['playstation', 'xbox', 'keyboard', 'wheel'], 'show': {'menu_volume': False, 'unfocused_volume': True, 'ducked_volume': True, 'speech_recognition': True, 'save_cpu': True, 'fade_length': True}, 'defaults': {'mode': 'general', 'gamepad_skip_enabled': False, 'touchpad_skip_enabled': True, 'ducking_enabled': True}}}
AUTO_DETECT_KEYS: tuple = ('forza', 'rocketleague')
def get(key: str) -> dict:
    """Return preset dict for `key`, falling back to \'other\' on unknown."""
    return GAME_PRESETS.get(key) or GAME_PRESETS['other']
def label_for(key: str) -> str:
    return get(key).get('label', 'Other game')
def show_control(preset_key: str, control: str) -> bool:
    """True if the named control row should be visible for this preset.\n    Defaults to True for unknown controls so new code added later doesn\'t\n    silently hide things just because the preset dict wasn\'t updated."""
    return bool(get(preset_key).get('show', {}).get(control, True))
def supported_devices(preset_key: str) -> list:
    return list(get(preset_key).get('supported_devices', ['playstation', 'xbox', 'keyboard', 'wheel']))
def exes_for(preset_key: str) -> set:
    """Every exe name (lowercase) this preset answers to: the canonical\n    `exe` plus any `exe_aliases` (e.g. the Forza preset covers FH6/FH5/FH4).\n    Used by the runner\'s process probe, game-focus check and auto-detect."""
    p = get(preset_key)
    out = {(p.get('exe') or '').lower()}
    out |= {a.lower() for a in p.get('exe_aliases', [])}
    out.discard('')
    return out
def apply_preset(cfg, key: str) -> str:
    """Apply preset `key` to cfg in place. Returns the resolved key (after\n    falling back to \'other\' for unknown). Caller is responsible for\n    persisting via _queue_save / cfg.save."""
    preset = get(key)
    resolved_key = key if key in GAME_PRESETS else 'other'
    cfg.game_preset = resolved_key
    for field, value in preset.get('defaults', {}).items():
        try:
            setattr(cfg, field, value)
        except AttributeError:
            pass
    return resolved_key
def detect_preset_from_running(running_exe_names: Iterable[str]) -> str | None:
    """Given an iterable of lowercase exe names currently running on the\n    system, return the first auto-detect-key whose exe matches. None if\n    no known game preset matches.\n\n    Caller (runner) decides whether to actually switch (respects user\'s\n    cfg.auto_detect_game flag and avoids redundant switches)."""
    names = {n.lower() for n in running_exe_names if n}
    for key in AUTO_DETECT_KEYS:
        if exes_for(key) & names:
            return key
