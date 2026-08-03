"""Colour themes for Segue's UI.

One source of truth for every colour the UI paints, keyed by a small set of
semantic ROLES instead of raw hex scattered through the code. Swapping the active
theme re-points every role, so the whole UI restyles from one place.

Three themes are planned:
  - "dark"     : the shipped look (these values are the exact current hex).
  - "light"    : white surfaces, dark text/icons, same orange accent.
  - "contrast" : high-contrast (blacker bg, brighter borders) - STUB for now,
                 currently a copy of dark; filled in a later phase.

Roles invert SEMANTICALLY, not just by lightness. e.g. `emph_fill` is the
stand-out button fill (play / save / active toggle): white on dark, but a dark
fill on light so it still stands out against a white window, with `emph_text`
(the glyph/text on it) flipping the opposite way.

Accent (orange) is intentionally theme-independent; it reads on both.
"""
ACCENT = '#FF7A1A'
_ACCENT = ACCENT
_ACCENT_HI = '#ff9a32'
_DARK = {'bg': '#1e1e1e', 'titlebar': '#1F1F1E', 'panel': '#2a2a28', 'sunk': '#232321', 'surface': '#2b2b29', 'surface_hi': '#353532', 'surface_dull': '#262624', 'deep': '#1b1b19', 'border': '#3a3a38', 'border_hi': '#4a4a48', 'text': '#f0f0f0', 'text_dim': '#c2c2c0', 'text_hint': '#9a9a98', 'text_disabled': '#6a6a68', 'emph_fill': '#f0f0f0', 'emph_fill_hi': '#ffffff', 'emph_dull': '#d6d6d3', 'emph_dull_hi': '#e6e6e3', 'emph_text': '#1f1f1e', 'btn_fill': '#f0f0f0', 'btn_fill_hi': '#ffffff', 'btn_dull': '#d6d6d3', 'btn_dull_hi': '#e6e6e3', 'btn_text': '#1f1f1e', 'icon': '#e6e6e4', 'icon_dim': '#c2c2c0', 'accent': '#FF7A1A', 'accent_hi': '#ff9a32', 'accent_tint': '#2a1a12', 'danger': '#c42b1c', 'danger_tint': '#4a2422', 'success': '#3FB950', 'scrollbar': '#4e4d4a', 'scrollbar_hi': '#6f6e68', 'verbar_text': '#454543', 'verbar_text_hi': '#c9c9c7'}
_LIGHT = {'bg': '#f6f6f4', 'titlebar': '#f0f0ee', 'panel': '#e9e9e6', 'sunk': '#f1f1ef', 'surface': '#e0e0dd', 'surface_hi': '#d6d6d3', 'surface_dull': '#d8d8d5', 'deep': '#dbdbd8', 'border': '#d4d4d1', 'border_hi': '#bebebb', 'text': '#424240', 'text_dim': '#716f6b', 'text_hint': '#716f6b', 'text_disabled': '#aeaeac', 'emph_fill': '#3c3c38', 'emph_fill_hi': '#313130', 'emph_dull': '#3a3a38', 'emph_dull_hi': '#2b2b29', 'emph_text': '#f4f4f3', 'btn_fill': '#ffffff', 'btn_fill_hi': '#efefee', 'btn_dull': '#f1f1ef', 'btn_dull_hi': '#e8e8e6', 'btn_text': '#1f1f1e', 'icon': '#424240', 'icon_dim': '#6a6a68', 'accent': '#FF7A1A', 'accent_hi': '#ff9a32', 'accent_tint': '#fdeede', 'danger': '#c42b1c', 'danger_tint': '#f6d9d5', 'success': '#2e9e46', 'scrollbar': '#c6c6c3', 'scrollbar_hi': '#a8a8a5', 'verbar_text': '#a8a8a5', 'verbar_text_hi': '#3a3a38'}
_CONTRAST = {'bg': '#000000', 'titlebar': '#000000', 'panel': '#121212', 'sunk': '#0a0a0a', 'surface': '#1c1c1c', 'surface_hi': '#2e2e2e', 'surface_dull': '#141414', 'deep': '#000000', 'border': '#7a7a7a', 'border_hi': '#ffffff', 'text': '#ffffff', 'text_dim': '#dcdcdc', 'text_hint': '#b8b8b8', 'text_disabled': '#777777', 'emph_fill': '#ffffff', 'emph_fill_hi': '#ffffff', 'emph_dull': '#e6e6e6', 'emph_dull_hi': '#ffffff', 'emph_text': '#000000', 'btn_fill': '#ffffff', 'btn_fill_hi': '#ffffff', 'btn_dull': '#e6e6e6', 'btn_dull_hi': '#ffffff', 'btn_text': '#000000', 'icon': '#ffffff', 'icon_dim': '#d0d0d0', 'accent': '#FF7A1A', 'accent_hi': '#ff9a32', 'accent_tint': '#3a1c08', 'danger': '#ff6a5a', 'danger_tint': '#4a1612', 'success': '#4ad65f', 'scrollbar': '#8a8a8a', 'scrollbar_hi': '#ffffff', 'verbar_text': '#9a9a9a', 'verbar_text_hi': '#ffffff'}
THEMES = {'dark': _DARK, 'light': _LIGHT, 'contrast': _CONTRAST}
THEME_ORDER = ('dark', 'light', 'contrast')
THEME_LABELS = {'dark': 'Dark', 'light': 'Light', 'contrast': 'High contrast'}
_active = 'dark'


def set_theme(name: str) -> None:
    """Make `name` the active theme (falls back to dark for an unknown name)."""
    global _active
    if name in THEMES:
        _active = name
    else:
        _active = 'dark'


def active_theme() -> str:
    return _active


def c(role: str) -> str:
    """Hex for a role in the active theme; falls back to dark, then magenta so a
    missing role is loud in testing rather than silently wrong."""
    pal = THEMES.get(_active, _DARK)
    return pal.get(role) or _DARK.get(role) or '#ff00ff'


def _theme_file() -> str:
    import os
    try:
        from fh6_spotify.config import default_config_path
        return os.path.join(os.path.dirname(default_config_path()), 'theme')
    except Exception:
        return os.path.join(os.path.expanduser('~'), '.segue_theme')


def load_theme() -> str:
    """The saved theme name (defaults to dark if unset / unknown)."""
    try:
        with open(_theme_file(), encoding='utf-8') as f:
            name = f.read().strip()
        return name if name in THEMES else 'dark'
    except Exception:
        return 'dark'


def save_theme(name: str) -> None:
    """Persist + activate a theme."""
    import os
    set_theme(name)
    path = _theme_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(name if name in THEMES else 'dark')
    except Exception:
        pass
